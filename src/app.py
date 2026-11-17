"""壁纸解包 —— RePKG 的图形界面外壳（pywebview + WebView2）"""

import base64
import io
import json
import os
import queue
from pathlib import Path
import shutil
import sys
import tempfile
import threading

import webview
from PIL import Image, ImageFilter

# ---------------------------------------------------------------- 常量

TITLE = "壁纸解包"
IMG_EXTS = (".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp")
VIDEO_EXTS = (".mp4", ".webm", ".avi", ".mov")
PKG_EXTS = (".pkg", ".tex")

PREVIEW_SIZE = (1400, 1000)   # 预览图最大边
THUMB_SIZE = (176, 116)       # 胶片条缩略图最大边
PREVIEW_CACHE_MAX = 8         # 预览缓存条数（前后翻页命中即秒开）

CACHE_DIR = os.path.join(tempfile.gettempdir(), "repkg_ui_cache")

TYPE_NAMES = {
    "scene": "Scene 场景",
    "video": "Video 视频",
    "web": "Web 网页",
    "application": "Application 应用",
    "tex": "TEX 纹理",
    "unknown": "未识别",
}

THEME_BG = {"dark": "#171326", "light": "#F2EEFB", "blush": "#FDF1F4"}
# DWM 标题栏 (背景, 文字)，Windows 里是 BGR 字节序
THEME_CAPTION = {
    "dark": (0x00261317, 0x00FFEEF2),
    "light": (0x00FBEEF2, 0x0040232A),
    "blush": (0x00F4F1FD, 0x003A2E4A),
}

_BASE_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
             else os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_BASE_DIR, "repkg_ui_config.json")

_THEME = {"name": "dark", "dark": True}
_ICONS = None


def _resource(name):
    """打包后资源在 _MEIPASS 下，开发时在脚本同级"""
    return os.path.join(getattr(sys, "_MEIPASS", _BASE_DIR), name)


# ---------------------------------------------------------------- 壁纸识别

def detect_kind(path):
    """根据 project.json / 文件扩展判断壁纸类型"""
    if not path or not os.path.exists(path):
        return "unknown"

    if os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pkg":
            return "scene"
        if ext == ".tex":
            return "tex"
        if ext in VIDEO_EXTS:
            return "video"
        return "unknown"

    data = {}
    pj = os.path.join(path, "project.json")
    if os.path.isfile(pj):
        try:
            with open(pj, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}

    wtype = (data.get("type") or "").lower()
    if wtype == "scene" or os.path.isfile(os.path.join(path, "scene.pkg")):
        return "scene"
    if wtype in ("video", "web", "application"):
        return wtype
    if os.path.isfile(os.path.join(path, "index.html")):
        return "web"

    try:
        names = os.listdir(path)
    except OSError:
        names = []
    if any(n.lower().endswith(VIDEO_EXTS) for n in names):
        return "video"
    if any(n.lower().endswith(PKG_EXTS) for n in names):
        return "scene"
    return "unknown"


def type_name(path):
    return TYPE_NAMES.get(detect_kind(path), TYPE_NAMES["unknown"])


def resolve_targets(inp):
    """目录时找出所有 pkg/tex，scene.pkg 排最前"""
    if os.path.isfile(inp):
        return [inp]
    try:
        names = os.listdir(inp)
    except OSError:
        return []
    targets = [os.path.join(inp, n) for n in names if n.lower().endswith(PKG_EXTS)]
    targets.sort(key=lambda p: (os.path.basename(p).lower() != "scene.pkg", p))
    return targets


def collect_images(folder):
    """收集解包结果里的图片（按路径排序，保证顺序稳定）"""
    res = []
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(IMG_EXTS):
                res.append(os.path.join(root, f))
    return sorted(res)


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def _find_video(folder):
    """在壁纸目录里定位视频文件（优先读 project.json 的 file 字段）"""
    pj = os.path.join(folder, "project.json")
    if os.path.isfile(pj):
        try:
            with open(pj, "r", encoding="utf-8") as f:
                fname = json.load(f).get("file")
            if fname and os.path.isfile(os.path.join(folder, fname)):
                return os.path.join(folder, fname)
        except (OSError, json.JSONDecodeError):
            pass
    try:
        for n in os.listdir(folder):
            if n.lower().endswith(VIDEO_EXTS):
                return os.path.join(folder, n)
    except OSError:
        pass
    return None


def _video_card(folder):
    """视频壁纸信息：引用源文件，解包阶段不复制"""
    src = _find_video(folder)
    if not src:
        return None
    try:
        uri = Path(src).resolve().as_uri()
    except ValueError:
        uri = "file:///" + src.replace("\\", "/")
    return {
        "name": os.path.basename(src),
        "size": _fmt_size(os.path.getsize(src)),
        "dir": folder,
        "src": src,
        "uri": uri,
        "saved": "",
    }


def _thumb_data_url(path):
    """生成预览用 data URL：有透明走 WebP，无透明走 JPEG（都远快于 PNG）"""
    with Image.open(path) as img:
        if img.format == "JPEG":
            img.draft("RGB", PREVIEW_SIZE)  # JPEG 可跳采样解码，省一半时间
        img.thumbnail(PREVIEW_SIZE, Image.LANCZOS)
        alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info

        buf = io.BytesIO()
        try:
            if alpha:
                img.convert("RGBA").save(buf, format="WEBP", quality=86, method=0)
                mime = "image/webp"
            else:
                img.convert("RGB").save(buf, format="JPEG", quality=88)
                mime = "image/jpeg"
        except (KeyError, OSError):
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            mime = "image/png"

    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"


def _thumb_small(path):
    """胶片条用的小图：尺寸小，用 BILINEAR 优先保证速度"""
    with Image.open(path) as img:
        if img.format == "JPEG":
            img.draft("RGB", THUMB_SIZE)
        img.thumbnail(THUMB_SIZE, Image.BILINEAR)
        alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info

        buf = io.BytesIO()
        try:
            if alpha:
                img.convert("RGBA").save(buf, format="WEBP", quality=70, method=0)
                mime = "image/webp"
            else:
                img.convert("RGB").save(buf, format="JPEG", quality=72)
                mime = "image/jpeg"
        except (KeyError, OSError):
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            mime = "image/png"

    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"


# ---------------------------------------------------------------- 业务接口

class Api:
    def __init__(self):
        # 私有命名：pywebview 会反射 js_api 的公开属性，公开的 window 会触发递归
        self._window = None
        self._images = []
        self._pv_cache = {}
        self._video = None
        self.cfg = self._load_config()

    # ---------- 配置 ----------
    @staticmethod
    def _load_config():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _cfg_set(self, **items):
        """写配置；值没变就不落盘"""
        if all(self.cfg.get(k) == v for k, v in items.items()):
            return
        self.cfg.update(items)
        self._save_config()

    # ---------- 前端事件 ----------
    def _emit(self, name, payload):
        try:
            data = json.dumps(payload, ensure_ascii=False)
            self._window.evaluate_js(f"window.__ui&&window.__ui.{name}({data})")
        except Exception:  # noqa: BLE001
            pass

    # ---------- 初始化 ----------
    def init(self):
        inp = self.cfg.get("input_path", "")
        return {
            "input": inp,
            "output": self.cfg.get("output_path", ""),
            "only": bool(self.cfg.get("only_images", True)),
            "theme": self.cfg.get("theme", "dark"),
            "type": type_name(inp) if inp else "未选择",
        }

    # ---------- 选择路径 ----------
    def _pick(self, dialog, **kwargs):
        res = self._window.create_file_dialog(dialog, **kwargs)
        return res[0] if res else None

    def _set_input(self, path):
        if not path:
            return {"cancelled": True}
        self._cfg_set(input_path=path)
        return {"path": path, "type": type_name(path)}

    def pick_file(self):
        return self._set_input(self._pick(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Wallpaper 文件 (*.pkg;*.tex)", "所有文件 (*.*)")))

    def pick_folder(self):
        return self._set_input(self._pick(webview.FOLDER_DIALOG))

    def pick_output(self):
        path = self._pick(webview.FOLDER_DIALOG)
        if not path:
            return {"cancelled": True}
        self._cfg_set(output_path=path)
        return {"path": path}

    def check_type(self, path):
        return {"type": type_name((path or "").strip())}

    def set_theme(self, theme):
        theme = theme if theme in THEME_BG else "dark"
        self._cfg_set(theme=theme)
        _THEME["name"] = theme
        _THEME["dark"] = theme == "dark"
        threading.Thread(target=_apply_window_style, daemon=True).start()
        return {"ok": True}

    def save_prefs(self, payload):
        self._cfg_set(
            input_path=payload.get("input", ""),
            output_path=payload.get("output", ""),
            only_images=bool(payload.get("only", True)),
        )
        return {"ok": True}

    # ---------- 解包 ----------
    def extract(self, payload):
        inp = (payload.get("input") or "").strip()
        out_dir = (payload.get("output") or "").strip()
        only = bool(payload.get("only", True))

        if not inp or not os.path.exists(inp):
            return {"ok": False, "msg": "请先选择存在的壁纸文件或文件夹"}
        if not out_dir:
            out_dir = self._pick(webview.FOLDER_DIALOG)
            if not out_dir:
                return {"ok": False, "msg": "已取消：未选择保存位置", "cancelled": True}
            self._cfg_set(output_path=out_dir)

        kind = detect_kind(inp)
        if kind not in ("scene", "tex", "video", "web", "application"):
            return {"ok": False,
                    "msg": "无法识别该壁纸类型：支持 scene.pkg / .tex / 视频 / 网页壁纸"}

        stem = (os.path.splitext(os.path.basename(inp))[0] if os.path.isfile(inp)
                else os.path.basename(inp.rstrip("/\\")))

        self._images, self._pv_cache = [], {}
        self._video = None
        video = None

        try:
            if kind in ("scene", "tex"):
                ok, text, out = self._extract_scene(inp, only)
            elif kind == "video":
                video = self._video = _video_card(inp)
                if not video:
                    return {"ok": False, "msg": "该文件夹里没找到视频文件"}
                # 只准备预览，不复制；保存是显式动作
                ok, text, out = True, "视频已就绪，预览确认后点「保存视频」", None
            else:
                ok, text, out = self._extract_tree(inp, out_dir, stem, kind)

            if out is None and kind != "video":
                return {"ok": False, "msg": text}

            # 视频没有图片可预览；绝不能扫保存目录，否则会把目录里的旧图当成结果
            if kind == "video":
                self._images = []
            elif ok:
                self._images = collect_images(out)

            return {
                "ok": ok, "msg": text, "kind": kind, "count": len(self._images),
                "output": out_dir, "video": video,
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": str(e)}

    def _extract_scene(self, inp, only):
        """scene.pkg / tex：用内置引擎解到临时缓存"""
        import repkg_core                    # 延迟导入，缩短启动时间

        targets = resolve_targets(inp)
        if not targets:
            return False, "选中的目录里没有 scene.pkg 或 .tex 文件", None

        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        os.makedirs(CACHE_DIR, exist_ok=True)

        self._emit("log", {"text": "开始解包（内置引擎）"})
        written, errors = [], []
        for target in targets:
            try:
                if target.lower().endswith(".tex"):
                    written += repkg_core.extract_tex(target, CACHE_DIR)
                else:
                    written += repkg_core.extract_pkg(
                        target, CACHE_DIR, only_images=only, on_progress=self._pkg_progress)
            except (repkg_core.PkgError, OSError) as e:
                errors.append(f"{os.path.basename(target)}：{e}")

        for msg in errors:
            self._emit("log", {"text": msg})
        if not written and errors:
            return False, "\n".join(errors), None

        return True, f"解出 {len(written)} 个文件" + ("，部分失败" if errors else ""), CACHE_DIR

    def _pkg_progress(self, index, total, name):
        self._emit("log", {"text": f"[{index + 1}/{total}] {name}"})

    def _extract_tree(self, inp, out_dir, stem, kind):
        """web / application：整目录复制到保存位置下的同名子文件夹"""
        out = os.path.join(out_dir, stem)
        self._emit("log", {"text": f"复制{TYPE_NAMES[kind]}壁纸 → {out}"})
        ok, text = self._copy_tree(inp, out)
        return ok, text, out

    # ---------- 保存 ----------

    @staticmethod
    def _copy_tree(folder, out):
        try:
            os.makedirs(out, exist_ok=True)
            names = os.listdir(folder)
            for n in names:
                s = os.path.join(folder, n)
                d = os.path.join(out, n)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
            return True, f"已复制 {len(names)} 个文件"
        except OSError as e:
            return False, str(e)

    # ---------- 预览 ----------
    def _clamp(self, index):
        return max(0, min(int(index), len(self._images) - 1))

    def _cache_put(self, path, src):
        if path in self._pv_cache:
            self._pv_cache.pop(path)
        elif len(self._pv_cache) >= PREVIEW_CACHE_MAX:
            self._pv_cache.pop(next(iter(self._pv_cache)))
        self._pv_cache[path] = src

    def preview(self, index):
        if not self._images:
            return {"empty": True}

        index = self._clamp(index)
        path = self._images[index]
        name = os.path.basename(path)
        src = self._pv_cache.get(path)

        if src is None:
            try:
                src = _thumb_data_url(path)
            except Exception as e:  # noqa: BLE001
                return {"error": str(e), "index": index,
                        "total": len(self._images), "name": name}
            self._cache_put(path, src)
        else:
            self._cache_put(path, src)  # 命中后移到队尾，保持 LRU

        return {"index": index, "total": len(self._images), "name": name, "src": src}

    def warm(self, index):
        """只预热缓存，不返回图片数据（翻页时提前准备相邻张）"""
        if not self._images:
            return {"ok": False}
        index = self._clamp(index)
        path = self._images[index]
        if path not in self._pv_cache:
            try:
                self._cache_put(path, _thumb_data_url(path))
            except Exception:  # noqa: BLE001
                return {"ok": False}
        return {"ok": True}

    def thumbs(self):
        """返回全部图片的小缩略图，供底部胶片条使用"""
        out = []
        for i, p in enumerate(self._images):
            try:
                out.append({"i": i, "src": _thumb_small(p)})
            except Exception:  # noqa: BLE001
                out.append({"i": i, "src": ""})
        return out

    # ---------- 保存 ----------
    def save_image(self, index, name, mode):
        if not self._images:
            return {"ok": False, "msg": "当前没有可保存的图片"}
        index = self._clamp(index)
        src = self._images[index]
        dst_dir = (self.cfg.get("output_path") or "").strip()
        if not dst_dir:
            return {"ok": False, "msg": "请先选择保存位置"}

        src_ext = os.path.splitext(src)[1]
        given = (name or "").strip() or os.path.basename(src)
        stem, ext = os.path.splitext(given)
        if not ext or ext.lower() != src_ext.lower():
            given = stem + src_ext

        try:
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, given)
            if os.path.exists(dst):
                if mode == "auto":
                    return {"exists": True, "name": given}
                if mode == "rename":
                    base, e2 = os.path.splitext(dst)
                    i = 1
                    while os.path.exists(dst):
                        dst = f"{base} ({i}){e2}"
                        i += 1
            shutil.copy2(src, dst)
        except OSError as e:
            return {"ok": False, "msg": f"保存失败：{e}"}

        return {"ok": True, "msg": f"已保存：{os.path.basename(dst)}",
                "name": os.path.basename(dst)}

    def save_as(self, index, name):
        if not self._images:
            return {"ok": False, "msg": "当前没有可保存的图片"}
        index = self._clamp(index)
        src = self._images[index]
        ext = os.path.splitext(src)[1]

        initial = (name or "").strip() or os.path.basename(src)
        if not os.path.splitext(initial)[1]:
            initial += ext

        dst = self._pick(webview.SAVE_DIALOG, save_filename=initial,
                         file_types=(f"图片 (*{ext})", "所有文件 (*.*)"))
        if not dst:
            return {"cancelled": True}
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            return {"ok": False, "msg": f"保存失败：{e}"}
        return {"ok": True, "msg": f"已保存：{os.path.basename(dst)}"}

    def save_video(self, payload):
        """把当前视频复制到保存位置（同名自动加序号）"""
        v = self._video
        if not v or not os.path.isfile(v.get("src", "")):
            return {"ok": False, "msg": "当前没有可保存的视频"}
        out = ((payload or {}).get("dir") or self.cfg.get("output_path") or "").strip()
        if not out:
            return {"ok": False, "msg": "请先设置保存位置"}
        try:
            os.makedirs(out, exist_ok=True)
            dst = self._copy_with_suffix(v["src"], out)
        except OSError as e:
            return {"ok": False, "msg": f"保存失败：{e}"}
        v["saved"] = dst
        return {"ok": True, "name": os.path.basename(dst), "path": dst,
                "msg": f"已保存视频：{os.path.basename(dst)}"}

    def save_video_as(self):
        v = self._video
        if not v or not os.path.isfile(v.get("src", "")):
            return {"ok": False, "msg": "当前没有可保存的视频"}
        src = v["src"]
        ext = os.path.splitext(src)[1]
        dst = self._pick(webview.SAVE_DIALOG, save_filename=os.path.basename(src),
                         file_types=(f"视频 (*{ext})", "所有文件 (*.*)"))
        if not dst:
            return {"cancelled": True}
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            return {"ok": False, "msg": f"保存失败：{e}"}
        v["saved"] = dst
        return {"ok": True, "name": os.path.basename(dst), "path": dst,
                "msg": f"已保存视频：{os.path.basename(dst)}"}

    @staticmethod
    def _copy_with_suffix(src, out):
        """复制到目录，重名时追加 (1)(2)…；已在目标位置则跳过"""
        name = os.path.basename(src)
        dst = os.path.join(out, name)
        if os.path.abspath(dst) == os.path.abspath(src):
            return dst
        stem, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(dst):
            dst = os.path.join(out, f"{stem} ({n}){ext}")
            n += 1
        shutil.copy2(src, dst)
        return dst

    # ---------- 其他 ----------
    def open_output(self):
        path = (self.cfg.get("output_path") or "").strip()
        if not path:
            path = self._pick(webview.FOLDER_DIALOG)
            if not path:
                return {"ok": False, "msg": "未选择保存位置"}
            self._cfg_set(output_path=path)
        if not os.path.isdir(path):
            return {"ok": False, "msg": "保存位置不存在"}
        os.startfile(path)
        return {"ok": True}

    def close(self):
        self._save_config()


# ---------------------------------------------------------------- 窗口外观

def _load_icons():
    """加载窗口图标句柄（32/256 两档），只读一次"""
    global _ICONS
    if _ICONS is None:
        import ctypes

        _ICONS = []
        path = _resource(os.path.join("ui", "app.ico"))
        if os.path.isfile(path):
            for size in (32, 256):
                h = ctypes.windll.user32.LoadImageW(None, path, 1, size, size, 0x0010)
                if h:
                    _ICONS.append((size, h))
    return _ICONS


def _apply_window_style():
    """给主窗口套标题栏配色（跟随主题）、圆角、应用图标"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi

    handles = []

    def _collect(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value == TITLE and user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_collect)
    user32.EnumWindows(proc, 0)
    if not handles:
        return False

    dark = ctypes.c_int(1 if _THEME["dark"] else 0)
    corner = ctypes.c_int(2)  # DWMWCP_ROUND
    caption, text = THEME_CAPTION.get(_THEME["name"], THEME_CAPTION["dark"])
    cap = ctypes.c_int(caption)
    txt = ctypes.c_int(text)

    for hwnd in handles:
        try:
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(dark), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(cap), 4)
            for size, hicon in _load_icons():
                user32.SendMessageW(hwnd, 0x0080, 1 if size > 32 else 0, hicon)
        except Exception:  # noqa: BLE001
            pass
    return True


def _style_window():
    """窗口刚创建时标题栏属性会被重置，前 12 秒持续重刷"""
    import time

    for _ in range(24):
        _apply_window_style()
        time.sleep(0.5)


# ---------------------------------------------------------------- 启动画面

def _run_splash(done_q, theme="dark"):
    """启动画面：Tk 原生窗口秒开，盖住 WebView2 的初始化空窗期"""
    import tkinter as tk
    from PIL import ImageDraw, ImageFont, ImageTk

    w, h = 400, 250
    if theme == "blush":
        top, bottom = (255, 246, 243), (250, 232, 240)
        glow_cols = ((244, 143, 177, 95), (255, 196, 150, 75))
        c_title, c_sub, c_load = (74, 42, 56), (140, 100, 112), (166, 126, 140)
    elif theme == "light":
        top, bottom = (246, 241, 255), (234, 246, 249)
        glow_cols = ((150, 112, 255, 80), (80, 200, 210, 70))
        c_title, c_sub, c_load = (42, 35, 64), (110, 102, 140), (122, 114, 152)
    else:
        top, bottom = (34, 26, 60), (20, 28, 42)
        glow_cols = ((124, 108, 248, 90), (72, 176, 200, 70))
        c_title, c_sub, c_load = (240, 238, 252), (176, 168, 205), (201, 194, 230)

    # 与主界面一致的底色渐变
    img = Image.new("RGB", (w, h))
    px = img.load()
    d0, d1, d2 = (bottom[i] - top[i] for i in range(3))
    xstep = 0.35 / w
    for y in range(h):
        ty = (y / h) * 0.65
        for x in range(w):
            t = ty + x * xstep
            px[x, y] = (int(top[0] + d0 * t), int(top[1] + d1 * t), int(top[2] + d2 * t))

    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for cx, cy, r, col in ((70, 60, 110, glow_cols[0]), (330, 200, 120, glow_cols[1])):
        gd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
    base = Image.alpha_composite(img.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(18)))

    try:
        font_big = ImageFont.truetype("msyh.ttc", 30)
        font_small = ImageFont.truetype("msyh.ttc", 12)
    except OSError:
        font_big = font_small = ImageFont.load_default()
    ImageDraw.Draw(base).text((w // 2, 95), TITLE, font=font_big, fill=c_title, anchor="mm")
    ImageDraw.Draw(base).text((w // 2, 130), "RePKG · Wallpaper Engine 素材提取",
                              font=font_small, fill=c_sub, anchor="mm")

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    sx = (root.winfo_screenwidth() - w) // 2
    sy = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{sx}+{sy}")

    canvas = tk.Label(root, bd=0, highlightthickness=0)
    canvas.pack()
    frames = {}

    def tick(n=0):
        if not root.winfo_exists():
            return
        key = n % 4
        if key not in frames:
            frame = base.copy()
            ImageDraw.Draw(frame).text((w // 2, 196), "正在加载" + "." * key,
                                       font=font_small, fill=c_load, anchor="mm")
            frames[key] = ImageTk.PhotoImage(frame)
        canvas.configure(image=frames[key])
        try:
            done_q.get_nowait()
            root.destroy()
            return
        except queue.Empty:
            root.after(250, lambda: tick(n + 1))

    root.after(120, tick)
    root.mainloop()


# ---------------------------------------------------------------- 入口

def main():
    api = Api()
    theme = api.cfg.get("theme", "dark")
    theme = theme if theme in THEME_BG else "dark"
    _THEME["name"] = theme
    _THEME["dark"] = theme == "dark"

    # 必须用 file:// 加载：只有 file:// 页面才能直接播放本地视频
    index = Path(_resource(os.path.join("ui", "index.html"))).resolve().as_uri()

    window = webview.create_window(
        TITLE, url=index, js_api=api,
        width=1080, height=740, min_size=(900, 620),
        background_color=THEME_BG[theme], hidden=True,
    )
    api._window = window

    splash_done = queue.Queue()
    threading.Thread(target=_run_splash, args=(splash_done, theme), daemon=True).start()

    window.events.closed += api.close

    def on_loaded():
        splash_done.put(True)
        window.show()

    window.events.loaded += on_loaded
    threading.Thread(target=_style_window, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
