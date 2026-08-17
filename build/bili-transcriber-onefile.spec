# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for bili-transcriber (--onefile 单文件便携版, CPU 模式)。

单文件版刻意不包含 ~2GB 的 nvidia CUDA DLL,因此:
  - 体积更小(约 100~200MB)、启动更快、可任意拷贝直接运行;
  - 转写走 CPU(int8),速度慢于 GPU,但通用性最好。
需要 GPU 加速请使用便携包(onedir zip)或安装版。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------- 路径 ----------
PROJECT = Path(SPECPATH).parent          # D:\bilibili
# 不再收集 nvidia CUDA DLL(binaries=[])

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
    binaries=[],                 # 单文件版不含 CUDA DLL(CPU 模式)
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
