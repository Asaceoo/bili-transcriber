# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for bili-transcriber (--onedir 便携版)。

nvidia CUDA DLL 约 2 GB,不适合 --onefile 每次解压;
--onedir 生成一个自包含目录,Inno Setup 再打包成安装程序。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------- 路径 ----------
PROJECT = Path(SPECPATH).parent          # D:\bilibili
VENV_SP = PROJECT / ".venv" / "Lib" / "site-packages"

# ---------- 收集 Anaconda 运行时 DLL ----------
# 基础 Python 来自 Anaconda(sys.base_prefix),CPython 标准库 C 扩展(_ctypes/_sqlite3/
# _lzma/_bz2/_decimal/pyexpat/_zstd)直接 import "ffi.dll"/"sqlite3.dll"/... 等位于
# Anaconda/Library/bin 的卫星 DLL。在 onedir 结构中,_ctypes.pyd 与这些 DLL 同在
# _internal/ 目录下即可被加载器找到,故 dst 用 "."。
ANACONDA = Path(sys.base_prefix)
binaries = []
_anaconda_libbin = ANACONDA / "Library" / "bin"
for _n in ("ffi.dll", "sqlite3.dll", "liblzma.dll", "LIBBZ2.dll",
           "libmpdec-4.dll", "libexpat.dll", "zstd.dll"):
    _f = _anaconda_libbin / _n
    if _f.is_file():
        binaries.append((str(_f), "."))
# VC 运行时全家桶(部分第三方扩展依赖其变体)
for _pat in ("vcruntime140*.dll", "msvcp140*.dll", "concrt140.dll"):
    for _f in _anaconda_libbin.glob(_pat):
        binaries.append((str(_f), "."))
# python314.dll / python3.dll(稳定 ABI 的 python3.dll 是 _ctypes.pyd 直接依赖)
for _cand in (ANACONDA / "python314.dll", ANACONDA / "python3.dll"):
    if _cand.is_file():
        binaries.append((str(_cand), "."))
# API 集转发 DLL
for _f in ANACONDA.glob("api-ms-win-*.dll"):
    if _f.is_file():
        binaries.append((str(_f), "."))

# ---------- 收集 nvidia DLL ----------
for pkg in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
    src = VENV_SP / "nvidia" / pkg / "bin"
    dst = f"nvidia/{pkg}/bin"
    if src.is_dir():
        for f in src.iterdir():
            if f.suffix.lower() == ".dll":
                binaries.append((str(f), dst))

# ---------- 收集 NiceGUI 静态资源 ----------
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
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "scipy", "PIL",
        "IPython", "jupyter", "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # --onedir
    name="bili-transcriber",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,              # GUI 程序,不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="bili-transcriber",
)
