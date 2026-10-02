"""faster-whisper 封装:懒加载模型、GPU 优先、按片段流式回报进度。"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from app.task_control import CancelledError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]  # 0.0 ~ 1.0


@dataclass
class Segment:
    start: float  # 秒
    end: float
    text: str


class Transcriber:
    """线程安全:模型只加载一次,转写全程持锁(GPU 串行)。

    model_dir 非空时优先从本地模型目录加载(模型外置,用户自放模型包),
    未命中才走 faster-whisper 在线下载。
    """

    def __init__(self, model_size: str = "large-v3-turbo", device: str = "auto",
                 compute_type: str = "auto", language: str | None = None, vad: bool = True,
                 model_dir: str = ""):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language or None  # None = 自动检测
        self.vad = vad
        self.model_dir = model_dir or ""
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def resolved_device(self) -> str:
        """已解析的实际设备(cpu/cuda),供落盘元数据展示。"""
        return self._resolve()[0]

    def _resolve_model_ref(self) -> str:
        """解析模型引用:本地模型目录优先,否则返回模型名(在线下载)。"""
        if self.model_dir:
            from app.models import resolve_model_dir, find_whisper_model
            root = resolve_model_dir(self.model_dir)
            local = find_whisper_model(root, self.model_size)
            if local is not None:
                logger.info("使用本地模型目录: %s", local)
                return str(local)
            logger.info("模型目录 %s 未找到 %s,回退在线下载", root, self.model_size)
        return self.model_size

    def load(self):
        # 判空 + 加载整体置于锁内(double-checked locking):
        # Pipeline 以 WORKERS=2 共享同一 transcriber 实例,两个 worker 冷启动并发
        # 首次转写会同时进入 load(),若判空在锁外、赋值在锁内,会各自加载一次模型
        # (双倍内存峰值 + 浪费算力)。放到锁内保证只有一个 worker 真正加载。
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel  # 延迟导入,加快 UI 启动

            device, compute = self._resolve()
            model_ref = self._resolve_model_ref()
            kwargs: dict = {}
            if self.model_dir:
                # 在线下载的模型也落到统一模型目录(用户设置的保存路径)
                from app.models import resolve_model_dir
                kwargs["download_root"] = str(resolve_model_dir(self.model_dir))
            logger.info("加载模型 %s (device=%s, compute=%s)", model_ref, device, compute)
            try:
                self._model = WhisperModel(model_ref, device=device, compute_type=compute, **kwargs)
            except Exception:
                logger.exception("模型加载失败: %s (device=%s, compute=%s)", model_ref, device, compute)
                raise
            logger.info("模型加载完成:%s", model_ref)

    def _resolve(self) -> tuple[str, str]:
        """解析设备与计算精度,并确保目标设备所需的运行时已就绪。

        - device=auto  : 注册 GPU 加速包并探测 CUDA,有则用 GPU,否则 CPU
        - device=cuda  : 显式指定 GPU,同样必须先注册 GPU 加速包(及早验证
          cublas 可加载),否则持久化的 device=cuda 会导致 ctranslate2 在转写
          encode 阶段延迟加载 cublas64_12.dll 失败(模型加载成功 ≠ 转写成功)。
        - BILI_DEVICE  : 环境变量强覆盖(单文件版 hook 用 cpu 绕过 CUDA 探测)
        """
        env_device = os.environ.get("BILI_DEVICE")
        env_compute = os.environ.get("BILI_COMPUTE_TYPE")
        requested = env_device or self.device or "cpu"

        cuda_ok = False
        if requested == "auto" or requested == "cuda":
            from app.gpu_runtime import register_gpu_runtime
            if register_gpu_runtime():
                # 注册成功后,提前验证 cublas 可加载 + ctranslate2 可见 CUDA,
                # 与转写时 faster-whisper 的动态加载同机制。任一失败都视为不可用。
                try:
                    import ctypes
                    ctypes.WinDLL("cublas64_12.dll")
                    import ctranslate2
                    cuda_ok = ctranslate2.get_cuda_device_count() > 0
                except Exception:  # noqa: BLE001 — 探测失败视为不可用
                    logger.exception("已注册 GPU 运行时但 CUDA 探测失败")
                    cuda_ok = False

        device = "cuda" if (requested in ("auto", "cuda") and cuda_ok) else "cpu"
        if requested == "cuda" and not cuda_ok:
            logger.warning(
                "请求使用 GPU(cuda)但 CUDA 运行时不可用(cublas 无法加载),"
                "自动回退 CPU 转写;请检查 GPU 加速包是否完整。"
            )
        compute = env_compute if env_compute else self.compute_type
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def transcribe(self, wav: Path, duration: float = 0.0,
                   progress: ProgressCallback | None = None) -> list[Segment]:
        """转写 wav,返回按时间排序的片段列表。duration>0 时按片段结束时间估算进度。

        VAD 开启但零语音时(唱歌/纯音乐场景 Silero 会全部滤除),自动关闭 VAD 重试。
        """
        self.load()
        segments = self._run(wav, duration, progress, vad=self.vad)
        if self.vad and not segments:
            logger.warning("VAD 未检出任何语音(可能是唱歌/纯音乐),关闭 VAD 重试")
            if progress is not None:
                progress(0.0)
            segments = self._run(wav, duration, progress, vad=False)
        return segments

    def _run(self, wav: Path, duration: float, progress: ProgressCallback | None,
             vad: bool) -> list[Segment]:
        segments: list[Segment] = []
        with self._lock:
            logger.info("开始转写 %s (vad=%s, language=%s)", wav, vad, self.language)
            try:
                it, info = self._model.transcribe(
                    str(wav), vad_filter=vad, beam_size=5, language=self.language,
                )
                if progress is not None and duration <= 0:
                    duration = float(getattr(info, "duration", 0.0) or 0.0)
                for seg in it:
                    text = seg.text.strip()
                    if text:
                        segments.append(Segment(float(seg.start), float(seg.end), text))
                    if progress is not None and duration > 0:
                        progress(min(segments[-1].end / duration, 1.0) if segments else 0.0)
            except CancelledError:
                # 用户取消任务:不当作转写失败记录,直接向上传播
                raise
            except Exception:
                logger.exception("转写失败:%s", wav)
                raise
            if progress is not None:
                progress(1.0)
        logger.info("转写完成:%s → %d 段", wav, len(segments))
        return segments


def segments_to_plain_lines(segments: Iterable[Segment]) -> list[str]:
    return [s.text for s in segments]
