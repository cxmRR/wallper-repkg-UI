# 第三方组件与许可声明

本作品「壁纸解包」在实现过程中参考并使用了以下第三方成果。所有第三方代码/素材的版权归原作者所有，本项目按其许可条款使用。

## 1. RePKG（格式实现的参考来源）

本项目的 PKG / TEX 解析内核由作者使用 Python 重写（`src/repkg_core.py`），文件格式的读取逻辑参考了 RePKG 的实现。RePKG 采用 MIT 许可证，完整许可原文见 `LICENSES/RePKG-LICENSE.txt`。

- 项目：RePKG
- 作者：notscuffed
- 地址：https://github.com/NotScuffed/repkg
- 许可：MIT License，Copyright (c) 2019 notscuffed

依据 MIT 许可证，本作品保留上述版权声明与许可声明；本项目未使用 RePKG 的 C# 源码或其可执行文件，界面、交互与打包流程均为独立实现。

## 2. 运行依赖

| 组件 | 版本 | 许可 | 用途 |
| --- | --- | --- | --- |
| pywebview | 6.2.1 | BSD-3-Clause | 桌面窗口与 WebView2 宿主 |
| pythonnet | 3.2.1 | MIT | .NET 互操作（pywebview 依赖） |
| clr-loader | 0.3.1 | MIT | .NET 运行时加载（pywebview 依赖） |
| Pillow | 13.0.0 | MIT-CMU (HPND) | 纹理解码为 PNG/JPEG |
| lz4 | 4.4.5 | BSD-2-Clause | PKG 内 LZ4 数据解压 |
| texture2ddecoder | 1.0.6 | MIT（Copyright (c) 2020 K0lb3） | DXT/BC 系列纹理解码 |
| PyInstaller | 6.22.3 | GPL-2.0-or-later + Bootloader Exception | 打包为 Windows 可执行文件 |

说明：

- PyInstaller 的 Bootloader Exception 明确允许将打包产物以任意许可分发，不影响本作品自身的授权方式。
- Pillow、lz4、pythonnet、pywebview 等组件的完整许可文本随安装包一同位于 `_internal/` 目录下对应包的 `LICENSE` / `METADATA` 中。
- WebView2 运行时由 Microsoft 提供，随 Windows 系统或 Edge 分发，不在本作品内打包。

## 3. 商标与免责

- 「Wallpaper Engine」为其开发者的商标。本作品是**非官方第三方工具**，与 Wallpaper Engine 的开发、发行方无任何隶属或授权关系，仅用于解析用户本机已有的文件。
- 本作品不包含、不分发任何壁纸素材。用户导出的内容版权归原作者所有，请在符合当地法律与平台条款的前提下自行使用。

## 4. 许可文本

- RePKG（MIT）：见 `LICENSES/RePKG-LICENSE.txt`
- pywebview（BSD-3-Clause）：Copyright (c) 2014-2017, Roman Sirokov and contributors。允许在保留版权声明与免责声明的前提下以源码或二进制形式再分发。
- lz4（BSD-2-Clause）：Copyright (c) 2012-2013, Christopher Chen。允许在保留版权声明与免责声明的前提下再分发。
- pythonnet / clr-loader（MIT）、texture2ddecoder（MIT）：允许在保留版权声明与许可声明的前提下自由使用、修改与再分发。
- Pillow（MIT-CMU / HPND）：允许在保留版权声明与许可声明的前提下使用与再分发。
