"""GPU 加速包外置:检测 %LOCALAPPDATA%/Bili Note/gpu 目录中的 CUDA DLL 并注册。

v0.1.15 起,安装包不再内置 ~2GB 的 nvidia CUDA 运行时。需要 GPU 加速的用户
下载 bili-transcriber-gpu-<ver>.zip(由 release.py 生成),解压到
%LOCALAPPDATA%/Bili Note/gpu/ 目录(与模型目录同级约定),软件启动/加载模型时
检测到 DLL 即通过 os.add_dll_directory 注册,ctranslate2 探测通过 → 启用 CUDA;
未检测到则回退 CPU,不影响使用。

GPU 包 zip 内部结构(与 PyInstaller 原布局一致,解压后即得):
    nvidia/cublas/bin/cublas64_12.dll  cublasLt64_12.dll
    nvidia/cudnn/bin/cudnn*.dll
    nvidia/cuda_runtime/bin/cudart64_12.dll
    nvidia/cuda_nvrtc/bin/nvrtc64_*.dll
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 与模型目录同级的 GPU 运行时目录名
GPU_DIR_NAME = "gpu"

# os.add_dll_directory 返回的句柄必须保持存活,否则被 GC 回收后
# DLL 搜索路径立即失效(表现为模型加载成功、转写时 cublas64_12.dll 加载失败)
_dll_handles: list = []


def default_gpu_dir() -> Path:
    """默认 GPU 运行时目录:%LOCALAPPDATA%/Bili Note/gpu(Windows),否则 ~/Bili Note/gpu。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Bili Note" / GPU_DIR_NAME


def _dll_root(gpu_dir: Path) -> Path | None:
    """在 GPU 目录中定位 cublas 根(nvidia/cublas/bin 或直接放 bin)。"""
    candidates = [
        gpu_dir / "nvidia" / "cublas" / "bin",
        gpu_dir / "cublas" / "bin",
        gpu_dir,
    ]
    for c in candidates:
        if (c / "cublas64_12.dll").is_file():
            return c
    return None


def gpu_available(gpu_dir: Path | str | None = None) -> bool:
    """GPU 加速包是否已放置(cublas64_12.dll 存在即可判为可用)。"""
    root = Path(gpu_dir) if gpu_dir else default_gpu_dir()
    return _dll_root(root) is not None


def register_gpu_runtime(gpu_dir: Path | str | None = None) -> bool:
    """注册 GPU 包中的 CUDA DLL 搜索路径。成功返回 True,否则 False。

    ctranslate2 以 CUDA_DYNAMIC_LOADING=ON 编译,运行时按名 LoadLibrary
    (cublas64_12.dll 等);把 GPU 包各 bin 目录加入 DLL 搜索路径后,
    ctranslate2.get_cuda_device_count() 即可探测到 CUDA 后端。
    """
    root = Path(gpu_dir) if gpu_dir else default_gpu_dir()
    base = _dll_root(root)
    if base is None:
        logger.info("未检测到 GPU 加速包(%s),回退 CPU", root)
        return False
    registered = 0
    for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
        d = base.parent.parent / sub / "bin" if base.parent.name == "cublas" else root / "nvidia" / sub / "bin"
        if not d.is_dir():
            # 兼容扁平布局:直接放根目录
            d = root / sub / "bin"
        if not d.is_dir():
            continue
        # ctranslate2 延迟加载 cuBLAS 不使用 Windows "安全搜索模式",
        # add_dll_directory 注册的目录对其不可见(实测 ctypes 能加载但转写仍失败);
        # 必须同时把目录前置到 PATH,二者并用。
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        try:
            h = os.add_dll_directory(str(d))
            _dll_handles.append(h)  # 保持句柄存活,防止 DLL 搜索路径被 GC 回收
        except (OSError, AttributeError):
            pass
        registered += 1
    if registered:
        logger.info("GPU 加速包已注册(%d 个 DLL 目录): %s", registered, root)
        return True
    logger.warning("GPU 加速包目录存在但未找到可注册的 DLL 目录: %s", root)
    return False


def ensure_gpu_or_cpu() -> str:
    """返回 'cuda' 或 'cpu':检测到 GPU 包则注册并返回 cuda,否则 cpu。"""
    if sys.platform != "win32":
        return "cpu"
    try:
        if not register_gpu_runtime():
            return "cpu"  # 未放置 GPU 包,直接 CPU,不再探测
        import ctypes
        # 提前验证 cublas 可加载(与 ctranslate2 动态加载同机制),避免转写时才崩溃
        ctypes.WinDLL("cublas64_12.dll")
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:  # noqa: BLE001 — 探测失败一律回退 CPU
        logger.exception("GPU 探测失败,回退 CPU")
    return "cpu"
