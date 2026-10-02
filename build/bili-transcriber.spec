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

# ---------- nvidia CUDA DLL 不再打包(模型/GPU 运行时外置) ----------
# v0.1.15 起:核心包为 CPU-only,GPU 加速改为独立「GPU 加速包 zip」
# (bili-transcriber-gpu-<ver>.zip,由 release.py 生成),用户按需下载解压到
# %LOCALAPPDATA%\Bili Note\gpu\ 目录;app/gpu_runtime.py 检测到才注册 DLL 启用 CUDA。
# 这使核心包体积从 ~2.3GB 降到 ~300MB 级(本体 266MB)。

# ---------- 收集 NiceGUI 静态资源 ----------
# nagisa 是 qwen-asr 强制对齐器的依赖,其 __init__ 导入时即实例化 Tagger()(需 data/ 模型文件),
# 故必须收集其子模块与 data 目录,否则打包后 import qwen_asr 会因缺失 nagisa 资源而失败。
datas = (
    collect_data_files("nicegui")
    + collect_data_files("faster_whisper")
    # sherpa-onnx 的 onnxruntime.dll / sherpa-onnx-c-api.dll 位于 lib/ 子目录,
    # 必须作为数据收集,否则打包后 import sherpa_onnx 报 DLL load failed。
    + collect_data_files("sherpa_onnx")
    # nagisa 用绝对导入(import prepro/model/mecab_system_eval)+ sys.path.append(自身目录),
    # 必须把 .py 也作为松散文件收集,否则打包后 import nagisa 报 No module named 'prepro'。
    + collect_data_files("nagisa", include_py_files=True)
    + collect_data_files("qwen_asr")  # 含 inference/assets/korean_dict_jieba.dict(强制对齐器运行时读取)
    # imageio-ffmpeg 随包静态 ffmpeg(讲义流合并/关键帧检测/转码回退)
    + collect_data_files("imageio_ffmpeg")
    # 应用图标(app_256.png 供窗口 favicon 使用;main.py 冻结运行时从 _internal/frozen 加载)
    + [(str(PROJECT / "app" / "assets"), "app/assets")]
)

# ---------- 隐藏导入(动态加载的子模块) ----------
hiddenimports = (
    collect_submodules("nicegui")
    + collect_submodules("faster_whisper")
    + collect_submodules("ctranslate2")
    + collect_submodules("sherpa_onnx")
    + collect_submodules("nagisa")
    + collect_submodules("dynet")
    + [
        # qwen_asr 的 inference/core 子包是 PEP420 命名空间包(无 __init__.py),
        # collect_submodules 走 pkgutil.walk_packages 只能找到 __main__,必须逐个显式列出,
        # 否则打包后 from qwen_asr import Qwen3ASRModel 报 ModuleNotFoundError。
        "qwen_asr",
        "qwen_asr.inference",
        "qwen_asr.inference.qwen3_asr",
        "qwen_asr.inference.qwen3_forced_aligner",
        "qwen_asr.inference.utils",
        "qwen_asr.core",
        "qwen_asr.core.transformers_backend",
        "qwen_asr.core.transformers_backend.configuration_qwen3_asr",
        "qwen_asr.core.transformers_backend.modeling_qwen3_asr",
        "qwen_asr.core.transformers_backend.processing_qwen3_asr",
        "pywebview",
        "pywebview.platforms.edgechromium",
        "pywebview.platforms.win32",
        "yt_dlp",
        "imageio_ffmpeg",
        "imageio_ffmpeg.binaries",  # importlib.resources 定位二进制需要它在 PYZ 里可导入
        "huggingface_hub",
        "pystray",
        # Qwen3-ASR 引擎(qwen-asr 延迟导入,需显式收集;transformers 大量使用
        # lazy module,PyInstaller 官方 hook 无法完全收集,这里显式补充)
        "transformers",
        "transformers.activations",
        "transformers.cache_utils",
        "transformers.generation",
        "transformers.generation.beam_constraints",
        "transformers.generation.beam_search",
        "transformers.generation.candidate_generator",
        "transformers.generation.configuration_utils",
        "transformers.generation.logits_process",
        "transformers.generation.stopping_criteria",
        "transformers.generation.streamers",
        "transformers.generation.utils",
        "transformers.generation.watermarking",
        "transformers.integrations",
        "transformers.masking_utils",
        "transformers.modeling_flash_attention_utils",
        "transformers.modeling_layers",
        "transformers.modeling_outputs",
        "transformers.modeling_rope_utils",
        "transformers.modeling_utils",
        "transformers.models.auto",
        "transformers.processing_utils",
        "transformers.utils.deprecation",
        "transformers.utils.generic",
        "accelerate",
        "torch",
        "nagisa",
        "dynet",
        "_dynet",
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
    runtime_hooks=[str(PROJECT / "build" / "preload_transformers.py")],
    excludes=[
        "tkinter", "matplotlib", "sklearn",
        "IPython", "jupyter", "notebook",
        # CUDA 相关重型依赖在核心包中不打包(已外置为 GPU 加速包)
        "nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc",
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
    icon=str(PROJECT / "app" / "assets" / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="bili-transcriber",
)
