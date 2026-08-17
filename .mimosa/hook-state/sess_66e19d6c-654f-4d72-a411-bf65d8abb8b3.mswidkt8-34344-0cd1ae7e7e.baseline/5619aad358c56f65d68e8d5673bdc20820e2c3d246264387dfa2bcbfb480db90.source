"""faster-whisper 封装:懒加载模型、GPU 优先、按片段流式回报进度。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]  # 0.0 ~ 1.0


@dataclass
class Segment:
    start: float  # 秒
    end: float
    text: str


class Transcriber:
    """线程安全:模型只加载一次,转写全程持锁(GPU 串行)。"""

    def __init__(self, model_size: str = "large-v3-turbo", device: str = "auto",
                 compute_type: str = "auto", language: str | None = None, vad: bool = True):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language or None  # None = 自动检测
        self.vad = vad
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self):
        if self._model is not None:
            return
        from faster_whisper import WhisperModel  # 延迟导入,加快 UI 启动

        device, compute = self._resolve()
        logger.info("加载模型 %s (device=%s, compute=%s)", self.model_size, device, compute)
        self._model = WhisperModel(self.model_size, device=device, compute_type=compute)

    def _resolve(self) -> tuple[str, str]:
        device = self.device
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
        compute = self.compute_type
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def transcribe(self, wav: Path, duration: float = 0.0,
                   progress: ProgressCallback | None = None) -> list[Segment]:
        """转写 wav,返回按时间排序的片段列表。duration>0 时按片段结束时间估算进度。"""
        self.load()
        segments: list[Segment] = []
        with self._lock:
            kwargs = dict(
                vad_filter=self.vad,
                beam_size=5,
                language=self.language,
            )
            it, info = self._model.transcribe(str(wav), **kwargs)
            if progress is not None and duration <= 0:
                duration = float(getattr(info, "duration", 0.0) or 0.0)
            for seg in it:
                text = seg.text.strip()
                if text:
                    segments.append(Segment(float(seg.start), float(seg.end), text))
                if progress is not None and duration > 0:
                    progress(min(segments[-1].end / duration, 1.0) if segments else 0.0)
            if progress is not None:
                progress(1.0)
        return segments


def segments_to_plain_lines(segments: Iterable[Segment]) -> list[str]:
    return [s.text for s in segments]
