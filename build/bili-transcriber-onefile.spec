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

# ---------- nvidia CUDA DLL 不再打包(模型/GPU 运行时外置) ----------
# v0.1.15 起:单文件版同样为 CPU-only 核心;GPU 加速走独立「GPU 加速包 zip」
# (bili-transcriber-gpu-<ver>.zip),用户解压到 %LOCALAPPDATA%\Bili Note\gpu\ 后
# app/gpu_runtime.py 自动注册 DLL 并启用 CUDA。单文件版体积从 ~1.1GB 降到 ~300MB 级。
# 注:ctranslate2 用 CUDA_DYNAMIC_LOADING=ON 编译,探测到 cublas64_12.dll 即通过,
# 未检测到 GPU 包时走 CPU(int8),不影响启动。

# ---------- 收集 NiceGUI / faster-whisper 静态资源 ----------
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
    # 应用图标(app_256.png 供窗口 favicon 使用;main.py 冻结运行时从 sys._MEIPASS 加载)
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
    binaries=runtime_bins,       # 显式收集 Anaconda VC 运行时 DLL(否则 _ctypes 导入期加载失败)
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[
        str(PROJECT / "build" / "onefile_browser.py"),
        str(PROJECT / "build" / "preload_transformers.py"),
    ],
    excludes=[
        "tkinter", "matplotlib", "sklearn",
        "IPython", "jupyter", "notebook",
        # CUDA 相关重型依赖在单文件版中不打包(已外置为 GPU 加速包)
        "nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc",
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
    icon=str(PROJECT / "app" / "assets" / "app.ico"),
)
