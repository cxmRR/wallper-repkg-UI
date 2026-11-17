"""RePKG 底层格式的纯 Python 实现（PKG 包 / TEX 纹理）。

格式取自 RePKG 源码（RePKG.Application / RePKG.Core），仅保留解包所需的读取逻辑，
写入 / 打包部分不在此实现。

主要入口：
    read_package(path)                       -> Package
    read_tex(data)                           -> Tex
    Tex.to_image(index=0)                    -> PIL.Image
    extract_pkg(path, out_dir, only_images)  -> list[str]
    extract_tex(path, out_dir)               -> list[str]
"""

import io
import math
import os
import struct

import lz4.block
import texture2ddecoder
from PIL import Image

MAX_PKG_BYTES = 1 << 30        # 单个 pkg 最大 1GB，防止误读
MAX_ENTRIES = 100_000
MAX_IMAGES = 100
MAX_MIPMAPS = 32
MAX_MIPMAP_BYTES = 2_000_000_000   # 与改过的 Constants.MaximumMipmapByteCount 一致

# TexFormat
FMT_RGBA8888 = 0
FMT_DXT5 = 4
FMT_DXT3 = 6
FMT_DXT1 = 7
FMT_RG88 = 8
FMT_R8 = 9

# FreeImageFormat 里用得到的两个
FIF_UNKNOWN = -1
FIF_MP4 = 35

# TexFlags
FLAG_GIF = 4
FLAG_VIDEO = 32

# 内嵌图片格式 -> 扩展名（TEXB0003/0004 会直接存 PNG/JPEG 等整张图片）
FIF_EXT = {
    0: "bmp", 1: "ico", 2: "jpg", 3: "jng", 4: "koa", 5: "lbm", 6: "mng",
    7: "pbm", 8: "pbm", 9: "pcd", 10: "pcx", 11: "pgm", 12: "pgm",
    13: "png", 14: "ppm", 15: "ppm", 16: "ras", 17: "tga", 18: "tif",
    19: "wbmp", 20: "psd", 21: "cut", 22: "xbm", 23: "xpm", 24: "dds",
    25: "gif", 26: "hdr", 27: "g3", 28: "sgi", 29: "exr", 30: "j2k",
    31: "jp2", 32: "pfm", 33: "pict", 34: "raw", 35: "mp4",
}


class PkgError(Exception):
    """包 / 纹理格式错误"""


# ---------------------------------------------------------------- 二进制小工具

def _i32(data, pos):
    return struct.unpack_from("<i", data, pos)[0], pos + 4


def _read_nstring(data, pos, max_len=-1):
    """C# 的 ReadNString：读到 \0 为止（最多 max_len 个字符）"""
    end = pos
    limit = len(data) if max_len < 0 else min(len(data), pos + max_len)
    while end < limit and data[end] != 0:
        end += 1
    return data[pos:end].decode("utf-8", "replace"), end + 1


def _read_i32_string(data, pos, max_len=-1):
    """C# 的 ReadStringI32Size：int32 长度 + UTF-8 字节"""
    size, pos = _i32(data, pos)
    if max_len >= 0:
        size = min(size, max_len)
    if size < 0:
        raise PkgError("字符串长度非法")
    return data[pos:pos + size].decode("utf-8", "replace"), pos + size


# ---------------------------------------------------------------- PKG 包

class Entry:
    __slots__ = ("path", "offset", "length")

    def __init__(self, path, offset, length):
        self.path = path
        self.offset = offset
        self.length = length

    @property
    def name(self):
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def extension(self):
        return os.path.splitext(self.path)[1].lower()

    @property
    def directory(self):
        return os.path.dirname(self.path)

    def __repr__(self):
        return f"<Entry {self.path} {self.length}B>"


class Package:
    """pkg 文件：头部 + 条目表 + 数据区（数据偏移以头部长度为准）"""

    def __init__(self, path, magic, header_size, data, entries):
        self.path = path
        self.magic = magic
        self.header_size = header_size
        self._data = data
        self.entries = entries

    def read(self, entry):
        start = entry.offset + self.header_size
        end = start + entry.length
        if start < 0 or end > len(self._data):
            raise PkgError(f"条目越界：{entry.path}")
        return self._data[start:end]


def read_package(path):
    """只读头部与条目表，条目内容按需切片（整个文件读入内存一次）"""
    size = os.path.getsize(path)
    if size > MAX_PKG_BYTES:
        raise PkgError(f"包过大：{size / 1048576:.0f}MB")

    with open(path, "rb") as f:
        data = f.read()

    magic, pos = _read_i32_string(data, 0, 32)
    count, pos = _i32(data, pos)
    if not 0 <= count <= MAX_ENTRIES:
        raise PkgError(f"条目数量非法：{count}（可能不是 pkg 文件）")

    entries = []
    for _ in range(count):
        full_path, pos = _read_i32_string(data, pos, 255)
        offset, pos = _i32(data, pos)
        length, pos = _i32(data, pos)
        if length < 0:
            raise PkgError(f"条目长度非法：{full_path}")
        entries.append(Entry(full_path, offset, length))

    return Package(path, magic, pos, data, entries)


# ---------------------------------------------------------------- TEX 纹理

class Tex:
    __slots__ = ("format", "flags", "tex_w", "tex_h", "img_w", "img_h",
                 "image_format", "images", "frames")

    def __init__(self):
        self.format = FMT_RGBA8888
        self.flags = 0
        self.tex_w = self.tex_h = 0
        self.img_w = self.img_h = 0
        self.image_format = FIF_UNKNOWN
        self.images = []          # [[mipmap, ...], ...]，每个 mipmap 是 (w, h, bytes)
        self.frames = []          # GIF 帧 (image_id, frametime, x, y, w, w_y, h_x, h)

    @property
    def is_gif(self):
        return bool(self.flags & FLAG_GIF)

    @property
    def is_video(self):
        return bool(self.flags & FLAG_VIDEO) or self.image_format == FIF_MP4

    def _image(self, index=0):
        if index >= len(self.images) or not self.images[index]:
            raise PkgError("纹理里没有图像数据")
        w, h, raw = self.images[index][0]
        return w, h, _mipmap_to_image(raw, w, h, self.image_format, self.format)

    def to_image(self, index=0):
        """转成 PIL.Image；GIF 纹理取第一帧，其余按 ImageWidth/Height 裁剪"""
        if self.is_video:
            raise PkgError("这是视频纹理，不是图片")
        if self.is_gif and self.frames:
            return self._frame_image(self.frames[0])

        w, h, img = self._image(index)
        if w != self.img_w or h != self.img_h:
            img = img.crop((0, 0, min(self.img_w, w), min(self.img_h, h)))
        return img

    def _frame_image(self, frame):
        image_id, _, x, y, fw, fw_y, fh_x, fh = frame
        # 旋转过的帧宽高会对调，取值方式与 RePKG 一致
        width = fw if fw != 0 else fh_x
        height = fh if fh != 0 else fw_y
        x0 = min(x, x + width)
        y0 = min(y, y + height)

        _, _, sheet = self._image(image_id)
        img = sheet.crop((int(x0), int(y0), int(x0 + abs(width)), int(y0 + abs(height))))

        angle = -(math.atan2(1 if height >= 0 else -1, 1 if width >= 0 else -1) - math.pi / 4)
        deg = int(round(math.degrees(angle)))
        if deg % 360:
            img = img.rotate(-deg)      # ImageSharp 正向为顺时针
        return img

    def frame_bytes(self, index=0):
        """视频纹理：取原始 mp4 字节"""
        w, h, raw = self.images[index][0]
        return raw


def read_tex(data):
    """解析 .tex 文件字节"""
    tex = Tex()
    magic1, pos = _read_nstring(data, 0, 16)
    if magic1 != "TEXV0005":
        raise PkgError(f"不是 tex 文件（magic={magic1!r}）")
    magic2, pos = _read_nstring(data, pos, 16)
    if magic2 != "TEXI0001":
        raise PkgError(f"tex 版本不支持（magic={magic2!r}）")

    tex.format, pos = _i32(data, pos)
    tex.flags, pos = _i32(data, pos)
    tex.tex_w, pos = _i32(data, pos)
    tex.tex_h, pos = _i32(data, pos)
    tex.img_w, pos = _i32(data, pos)
    tex.img_h, pos = _i32(data, pos)
    pos += 4                                    # UnkInt0

    if tex.format not in (FMT_RGBA8888, FMT_DXT5, FMT_DXT3, FMT_DXT1, FMT_RG88, FMT_R8):
        raise PkgError(f"未知的 tex 格式：{tex.format}")

    # ---- 图像容器
    container, pos = _read_nstring(data, pos, 16)
    image_count, pos = _i32(data, pos)
    if not 0 <= image_count <= MAX_IMAGES:
        raise PkgError(f"图像数量非法：{image_count}")

    if container in ("TEXB0001", "TEXB0002"):
        version = int(container[4:])
    elif container == "TEXB0003":
        tex.image_format, pos = _i32(data, pos)
        version = 3
    elif container == "TEXB0004":
        fmt, pos = _i32(data, pos)
        is_mp4, pos = _i32(data, pos)
        tex.image_format = FIF_MP4 if (fmt == FIF_UNKNOWN and is_mp4 == 1) else fmt
        version = 4 if tex.image_format == FIF_MP4 else 3
    else:
        raise PkgError(f"未知的图像容器：{container!r}")

    for _ in range(image_count):
        mipmaps, pos = _read_image(data, pos, version)
        tex.images.append(mipmaps)

    if tex.is_gif:
        tex.frames = _read_frames(data, pos)

    return tex


def _read_frames(data, pos):
    """GIF 帧信息容器（TEXS0001/0002/0003）"""
    magic, pos = _read_nstring(data, pos, 16)
    count, pos = _i32(data, pos)
    if not 0 <= count <= 100_000:
        raise PkgError(f"帧数量非法：{count}")

    if magic == "TEXS0003":
        pos += 8                               # GifWidth / GifHeight
    elif magic not in ("TEXS0001", "TEXS0002"):
        raise PkgError(f"未知的帧容器：{magic!r}")

    frames = []
    for _ in range(count):
        image_id, pos = _i32(data, pos)
        if magic == "TEXS0001":
            frametime = struct.unpack_from("<f", data, pos)[0]
            rest = struct.unpack_from("<6i", data, pos + 4)
        else:
            values = struct.unpack_from("<7f", data, pos)
            frametime, rest = values[0], values[1:]
        pos += 28
        frames.append((image_id, frametime) + tuple(rest))
    return frames


def _read_image(data, pos, version):
    count, pos = _i32(data, pos)
    if not 0 <= count <= MAX_MIPMAPS:
        raise PkgError(f"mipmap 数量非法：{count}")

    mipmaps = []
    for _ in range(count):
        if version == 1:
            w, pos = _i32(data, pos)
            h, pos = _i32(data, pos)
            raw, pos = _read_bytes(data, pos)
        elif version in (2, 3):
            w, pos = _i32(data, pos)
            h, pos = _i32(data, pos)
            is_lz4, pos = _i32(data, pos)
            size, pos = _i32(data, pos)
            raw, pos = _read_bytes(data, pos)
            if is_lz4 == 1:
                raw = _lz4(raw, size)
        else:                                   # version 4
            _check(data, pos, 1, "param1")
            _check(data, pos + 4, 2, "param2")
            _, pos = _read_nstring(data, pos + 8)          # conditionJson
            _check(data, pos, 1, "param3")
            w, pos = _i32(data, pos + 4)
            h, pos = _i32(data, pos)
            is_lz4, pos = _i32(data, pos)
            size, pos = _i32(data, pos)
            raw, pos = _read_bytes(data, pos)
            if is_lz4 == 1:
                raw = _lz4(raw, size)
        mipmaps.append((w, h, raw))
    return mipmaps, pos


def _check(data, pos, expect, name):
    if pos + 4 > len(data):
        raise PkgError("纹理数据被截断")
    val = struct.unpack_from("<i", data, pos)[0]
    if val != expect:
        raise PkgError(f"纹理头部校验失败：{name}={val}")


def _read_bytes(data, pos):
    size, pos = _i32(data, pos)
    if size < 0 or pos + size > len(data):
        raise PkgError("mipmap 数据长度非法")
    if size > MAX_MIPMAP_BYTES:
        raise PkgError(f"mipmap 数据过大：{size / 1048576:.0f}MB")
    return data[pos:pos + size], pos + size


def _lz4(raw, size):
    if size <= 0:
        raise PkgError("LZ4 解压长度非法")
    try:
        return lz4.block.decompress(raw, uncompressed_size=size)
    except Exception as e:                       # noqa: BLE001
        raise PkgError(f"LZ4 解压失败：{e}") from e


# ---------------------------------------------------------------- 像素解码

_ALPHA_LUT = bytes(v * 17 for v in range(16))
_ALPHA_PAIR = [bytes((_ALPHA_LUT[b & 15], _ALPHA_LUT[b >> 4])) for b in range(256)]


def _mipmap_to_image(raw, w, h, image_format, tex_format):
    if image_format not in (FIF_UNKNOWN, FIF_MP4):
        return Image.open(io.BytesIO(raw))      # 内嵌整张图片（png/jpg/...）

    if tex_format == FMT_RGBA8888:
        return Image.frombytes("RGBA", (w, h), raw[:w * h * 4])

    if tex_format == FMT_R8:
        return Image.frombytes("L", (w, h), raw[:w * h])

    if tex_format == FMT_RG88:
        return _rg88_to_image(raw, w, h)

    if tex_format == FMT_DXT1:
        return _bgra_image(texture2ddecoder.decode_bc1(raw, w, h), w, h)
    if tex_format == FMT_DXT5:
        return _bgra_image(texture2ddecoder.decode_bc3(raw, w, h), w, h)
    if tex_format == FMT_DXT3:
        return _bc2_to_image(raw, w, h)

    raise PkgError(f"不支持的 tex 格式：{tex_format}")


def _bgra_image(raw, w, h):
    """texture2ddecoder 输出 BGRA，交给 Pillow 按通道重排"""
    return Image.frombytes("RGBA", (w, h), raw, "raw", "BGRA", 0, 1)


def _rg88_to_image(raw, w, h):
    """RG88：ImageSharp 把它当「亮度 + alpha」转 RGBA（R=G=G,B=G,A=R）"""
    n = w * h
    g = raw[1:n * 2:2]
    buf = bytearray(n * 4)
    buf[0::4] = g
    buf[1::4] = g
    buf[2::4] = g
    buf[3::4] = raw[0:n * 2:2]
    return Image.frombytes("RGBA", (w, h), bytes(buf))


def _bc2_to_image(raw, w, h):
    """DXT3/BC2：8 字节 4bit alpha + 8 字节 DXT1 颜色块"""
    blocks = ((w + 3) // 4) * ((h + 3) // 4)
    color = bytearray(blocks * 8)
    alpha = bytearray(blocks * 16)
    for i in range(blocks):
        base = i * 16
        alpha[i * 16:(i + 1) * 16] = b"".join(_ALPHA_PAIR[x] for x in raw[base:base + 8])
        color[i * 8:(i + 1) * 8] = raw[base + 8:base + 16]

    img = _bgra_image(texture2ddecoder.decode_bc1(bytes(color), w, h), w, h)
    img.putalpha(Image.frombytes("L", (w, h), bytes(alpha)))
    return img


# ---------------------------------------------------------------- 导出

def _save_image(img, path):
    """无损写 PNG（与 RePKG 的 raw 格式输出一致），返回实际写出的路径"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    dst = path + ".png"
    img.save(dst, compress_level=3)
    return dst


def extract_pkg(path, out_dir, only_images=True, on_progress=None):
    """解包 pkg，返回写出的文件列表（图片为 png/jpg，其余为原始文件）"""
    pkg = read_package(path)
    entries = [e for e in pkg.entries
               if not only_images or e.extension == ".tex"]
    total = len(entries)
    written = []

    for i, entry in enumerate(entries):
        if on_progress:
            on_progress(i, total, entry.path)

        target = os.path.join(out_dir, entry.path.replace("/", os.sep))
        stem = os.path.splitext(target)[0]

        try:
            raw = pkg.read(entry)
            if entry.extension != ".tex":
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(raw)
                written.append(target)
                continue

            tex = read_tex(raw)
            if tex.is_video:
                os.makedirs(os.path.dirname(stem) or ".", exist_ok=True)
                dst = stem + ".mp4"
                with open(dst, "wb") as f:
                    f.write(tex.frame_bytes())
                written.append(dst)
            elif tex.image_format not in (FIF_UNKNOWN, FIF_MP4):
                # 内嵌整张图片：原样写出，不做二次编码
                os.makedirs(os.path.dirname(stem) or ".", exist_ok=True)
                dst = stem + "." + FIF_EXT.get(tex.image_format, "bin")
                with open(dst, "wb") as f:
                    f.write(tex.images[0][0][2])
                written.append(dst)
            else:
                written.append(_save_image(tex.to_image(), stem))
        except (PkgError, OSError, ValueError, Image.DecompressionBombError) as e:
            if on_progress:
                on_progress(i, total, f"{entry.path} 失败：{e}")

    return written


def extract_tex(path, out_dir):
    """单个 .tex 转图片，返回写出的文件列表"""
    with open(path, "rb") as f:
        raw = f.read()
    tex = read_tex(raw)
    stem = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0])
    return [_save_image(tex.to_image(), stem)]
