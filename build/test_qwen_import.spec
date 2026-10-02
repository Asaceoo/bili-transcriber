# -*- mode: python ; coding: utf-8 -*-
"""最小复现用 PyInstaller spec：验证 qwen_asr / transformers.generation 在打包后的导入。"""
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT = Path(SPECPATH).parent
VENV_SP = PROJECT / ".venv" / "Lib" / "site-packages"

ANACONDA = Path(sys.base_prefix)
binaries = []
_anaconda_libbin = ANACONDA / "Library" / "bin"
for _n in ("ffi.dll", "sqlite3.dll", "liblzma.dll", "LIBBZ2.dll",
           "libmpdec-4.dll", "libexpat.dll", "zstd.dll"):
    _f = _anaconda_libbin / _n
    if _f.is_file():
        binaries.append((str(_f), "."))
for _pat in ("vcruntime140*.dll", "msvcp140*.dll", "concrt140.dll"):
    for _f in _anaconda_libbin.glob(_pat):
        binaries.append((str(_f), "."))
for _cand in (ANACONDA / "python314.dll", ANACONDA / "python3.dll"):
    if _cand.is_file():
        binaries.append((str(_cand), "."))
for _f in ANACONDA.glob("api-ms-win-*.dll"):
    if _f.is_file():
        binaries.append((str(_f), "."))

datas = (
    collect_data_files("nicegui")
    + collect_data_files("faster_whisper")
    + collect_data_files("nagisa")
)

hiddenimports = (
    collect_submodules("nicegui")
    + collect_submodules("faster_whisper")
    + collect_submodules("ctranslate2")
    + collect_submodules("qwen_asr")
    + collect_submodules("nagisa")
    + collect_submodules("dynet")
    + [
        "pywebview",
        "pywebview.platforms.edgechromium",
        "pywebview.platforms.win32",
        "yt_dlp",
        "huggingface_hub",
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
    [str(PROJECT / "build" / "test_qwen_import.py")],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(PROJECT / "build" / "preload_transformers.py")],
    excludes=[
        "tkinter", "matplotlib", "scipy", "sklearn", "PIL",
        "IPython", "jupyter", "notebook",
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
    name="test_qwen_import",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
