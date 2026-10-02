"""硬件检测:探测 CPU/GPU/CUDA 可用性,给出转写设备建议。

检测为耗时操作(nvidia-smi/wmic 子进程 + ctranslate2 探测),调用方应在
后台线程执行,避免阻塞 UI。
"""

from __future__ import annotations

import ctypes
import logging
import platform
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HardwareInfo:
    cpu_name: str = ""
    gpu_name: str = ""
    cuda_available: bool = False
    gpu_pack_present: bool = False
    vram_mb: int = 0
    device: str = "cpu"          # 建议设备:cuda / cpu
    compute_type: str = "int8"   # 建议计算精度
    recommendation: str = ""
    torch_cuda_available: bool = False  # torch(transformers)是否可用 CUDA,与 qwen3-asr 引擎相关
    engine: str = "whisper"            # detect_hardware 针对的引擎


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                v, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                return str(v).strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _gpu_info() -> tuple[str, int]:
    """返回 (GPU 名称, 显存 MB);失败返回 ("", 0)。"""
    if sys.platform != "win32":
        return "", 0
    # 优先 nvidia-smi(显存准确)
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            line = out.stdout.strip().splitlines()[0]
            name, _, mem = line.partition(",")
            return name.strip(), int(float(mem.strip()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    # 回退 wmic(仅取名称,AdapterRAM 对 >4GB 显存不准)
    try:
        out = subprocess.run(
            ["wmic", "path", "win32_VideoController", "get", "name",
             "/format:csv"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 2 and parts[-1].strip():
                    return parts[-1].strip(), 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "", 0


def _probe_cuda() -> bool:
    """注册 GPU 包后探测 ctranslate2 是否可见 CUDA 设备。

    ctranslate2 是 faster-whisper 的推理后端,其 CUDA 通过外置 GPU 包中的
    nvidia DLL 加载。检测到即代表 whisper 引擎可用 GPU 加速。
    """
    from app import gpu_runtime
    try:
        if not gpu_runtime.register_gpu_runtime():
            return False
        ctypes.WinDLL("cublas64_12.dll")
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 — 探测失败一律视为不可用
        logger.exception("CUDA 探测失败")
        return False


def _probe_torch_cuda() -> bool:
    """探测 torch(transformers)是否可用 CUDA,反映 qwen3-asr 引擎的 GPU 能力。

    与 ctranslate2 无关:Qwen3-ASR 走 torch,即使 GPU 加速包已放置、ctranslate2
    可见 CUDA,只要打包的 torch 是 CPU-only(CUDA build 缺失),依然不能用 GPU。
    """
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001 — 探测失败一律视为不可用
        logger.exception("torch CUDA 探测失败")
        return False


def detect_hardware(engine: str = "whisper", settings: object | None = None) -> HardwareInfo:
    """检测硬件并给出针对指定引擎的转写设备建议。

    engine: whisper / qwen3-asr / sensevoice。
      - whisper 的 GPU 靠 ctranslate2 + 外置 GPU 包(cuda_available)
      - qwen3-asr 的 GPU 靠 torch 是否带 CUDA 构建(torch_cuda_available)
    两者探测机制不同,必须分开判定,否则 Qwen 引擎会拿到 whisper 的 cuda 建议
    而在 torch CPU-only 构建上崩溃(Torch not compiled with CUDA enabled)。
    """
    info = HardwareInfo(engine=engine)
    info.cpu_name = _cpu_name()
    info.gpu_name, info.vram_mb = _gpu_info()

    from app import gpu_runtime
    info.gpu_pack_present = gpu_runtime.gpu_available()
    # ctranslate2 探测较慢(nvidia-smi + 加载 DLL),仅 whisper 需要;qwen 只看 torch。
    info.cuda_available = _probe_cuda() if (info.gpu_pack_present and engine == "whisper") else False
    info.torch_cuda_available = _probe_torch_cuda() if engine == "qwen3-asr" else False

    vram = f"{info.vram_mb} MB" if info.vram_mb else "未知"
    gpu_desc = f"{info.gpu_name or 'GPU'}(显存 {vram})" if info.vram_mb else (info.gpu_name or "GPU")

    if engine == "qwen3-asr":
        # Qwen3-ASR 走 torch;只有 torch 带 CUDA 构建才能用 GPU
        if info.torch_cuda_available:
            info.device = "cuda"
            info.compute_type = "bfloat16"
            info.recommendation = (
                f"检测到 torch 支持 CUDA({gpu_desc})。Qwen3-ASR 建议设备:GPU,"
                f"计算精度:bfloat16,转写速度最快。"
            )
        else:
            info.device = "cpu"
            info.compute_type = "float32"
            info.recommendation = (
                "当前构建的 PyTorch 未启用 CUDA(CPU-only),Qwen3-ASR 暂不支持 GPU 加速,"
                "建议设备:CPU,计算精度:float32(转写稳定、无需额外配置)。"
                "若需 GPU 加速,请在设置中切换到 Whisper 引擎(配合 GPU 加速包)。"
            )
        return info

    if engine == "sensevoice":
        # SenseVoice 走 sherpa-onnx(内置 onnxruntime 仅 CPU),CPU 上已 30+ 倍实时
        info.device = "cpu"
        info.compute_type = "int8"
        info.recommendation = (
            "SenseVoice 引擎走 sherpa-onnx CPU 推理(无需 GPU),"
            "CPU 上约 30~40 倍实时,中文转写 13 分钟视频约 30~40 秒出稿。"
            "模型需为 int8 量化版(model.int8.onnx)。"
        )
        return info

    # ---- whisper(默认引擎),沿用 ctranslate2 + 外置 GPU 包 判定 ----
    if info.cuda_available:
        info.device = "cuda"
        info.compute_type = "float16"
        info.recommendation = (
            f"检测到 CUDA 可用({gpu_desc})。"
            f"建议设备:GPU(cuda),计算精度:float16,转写速度最快。"
        )
    elif info.gpu_pack_present:
        info.device = "cpu"
        info.compute_type = "int8"
        info.recommendation = (
            "检测到 GPU 加速包但 CUDA 初始化失败(可能 DLL 缺失或不兼容)。"
            "当前建议回退 CPU(int8)保证可用;如需 GPU,请重新下载匹配的 GPU 加速包。"
        )
    elif info.gpu_name:
        info.device = "cpu"
        info.compute_type = "int8"
        info.recommendation = (
            f"检测到显卡 {info.gpu_name},但未放置 GPU 加速包。"
            "如需 GPU 加速,请下载 bili-transcriber-gpu 包解压到 GPU 目录。"
            "当前建议 CPU(int8)即可正常转写。"
        )
    else:
        info.device = "cpu"
        info.compute_type = "int8"
        info.recommendation = (
            "未检测到独立显卡,建议使用 CPU(int8)转写,速度较慢但稳定可用。"
        )
    return info
