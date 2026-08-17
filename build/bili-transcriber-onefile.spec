# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for bili-transcriber (--onefile 单文件便携版, CPU 模式)。

单文件版刻意不包含 ~2GB 的 nvidia CUDA DLL,因此:
  - 体积更小(约 100~200MB)、启动更快、可任意拷贝直接运行;
  - 转写走 CPU(int8),速度慢于 GPU,但通用性最好。
需要 GPU 加速请使用便携包(onedir zip)或安装版。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------- 路径 ----------
PROJECT = Path(SPECPATH).parent          # D:\bilibili
# 基础 Python 实际是 Anaconda(sys.base_prefix),其一堆扩展模块依赖 Library/bin 下的卫星 DLL。
# 典型坑:_ctypes.pyd 直接 import "ffi.dll"(Anaconda 的 libffi 改名),而 import ctypes 走的是
# LOAD_LIBRARY_SEARCH_DEFAULT_DIRS(不查环境 PATH),所以这些 DLL 必须被打进包内、且位于扩展模块
# 同级目录(_MEIxxxx 根)才能被加载器找到。onedir 版因打包 CUDA DLL 时被 PyInstaller 一并收集,
# 故能运行;onefile 版刻意不含 CUDA,这些依赖不会被自动收集,这里显式收集。
#
# 下面分两类:
#  1) CPython 标准库 C 扩展的 Anaconda 卫星 DLL(PyInstaller 构建期报 "Library not found" 的那些):
#     ffi.dll(_ctypes)、sqlite3.dll(_sqlite3,app 启动即用)、liblzma.dll(_lzma)、
#     LIBBZ2.dll(_bz2)、libmpdec-4.dll(_decimal)、libexpat.dll(pyexpat)、zstd.dll(_zstd)
#  2) VC 运行时全家桶:部分第三方扩展模块依赖 vcruntime140*/msvcp140* 的变体
ANACONDA = Path(sys.base_prefix)
runtime_bins = []
_libbin = ANACONDA / "Library" / "bin"
_stdlib_dlls = ("ffi.dll", "sqlite3.dll", "liblzma.dll", "LIBBZ2.dll",
                "libmpdec-4.dll", "libexpat.dll", "zstd.dll")
for _n in _stdlib_dlls:
    _f = _libbin / _n
    if _f.is_file():
        runtime_bins.append((str(_f), "."))
for _pat in ("vcruntime140*.dll", "msvcp140*.dll", "concrt140.dll"):
    for _f in _libbin.glob(_pat):
        runtime_bins.append((str(_f), "."))
# python314.dll / python3.dll(保险:稳定 ABI 的 python3.dll 是 _ctypes.pyd 的直接依赖,
# 在 onefile 最小依赖分析下常被漏收,必须显式带上)
for _cand in (ANACONDA / "python314.dll", ANACONDA / "python3.dll"):
    if _cand.is_file():
        runtime_bins.append((str(_cand), "."))
# api-ms-win-crt-*.dll 等 API 集转发 DLL(运行时依赖,统一收进包内)
for _f in ANACONDA.glob("api-ms-win-*.dll"):
    if _f.is_file():
        runtime_bins.append((str(_f), "."))

# ---------- 收集 NiceGUI / faster-whisper 静态资源 ----------
datas = collect_data_files("nicegui") + collect_data_files("faster_whisper")

# ---------- 隐藏导入(动态加载的子模块) ----------
hiddenimports = (
    collect_submodules("nicegui")
    + collect_submodules("faster_whisper")
    + collect_submodules("ctranslate2")
    + [
        "pywebview",
        "pywebview.platforms.edgechromium",
        "pywebview.platforms.win32",
        "yt_dlp",
        "huggingface_hub",
    ]
)

a = Analysis(
    [str(PROJECT / "app" / "main.py")],
    pathex=[str(PROJECT)],
    binaries=runtime_bins,       # 显式收集 Anaconda VC 运行时 DLL(否则 _ctypes 导入期加载失败)
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "scipy", "PIL",
        "IPython", "jupyter", "notebook",
        # CUDA 相关重型依赖在单文件版中不打包,进一步瘦身
        "nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc",
        "cupy", "torch",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="bili-transcriber-single",   # 单文件 exe 名(与 onedir 目录名区分)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                    # GUI 程序,不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
