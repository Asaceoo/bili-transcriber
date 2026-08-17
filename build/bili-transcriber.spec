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

# ---------- 收集 nvidia DLL ----------
binaries = []
for pkg in ("cublas", "cudnn", "cuda_nvrtc"):
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
