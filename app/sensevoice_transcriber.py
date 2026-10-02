"""SenseVoice 转写引擎:sherpa-onnx 离线识别,CPU 上约 30~40 倍实时。

接口与 app.transcriber.Transcriber 对齐(pipeline 通过 engine 选择实现):
  - load(): 从本地模型目录加载(模型外置),未命中报清晰错误
  - transcribe(wav, duration, progress) -> list[Segment]
  - resolved_device: 已解析设备(当前 sherpa-onnx 内置 onnxruntime 仅 CPU)

依赖:sherpa-onnx(仅 onnxruntime,无 torch),体积小、加载快(约 1 秒);
模型目录规范见 app/models.py(sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/)。
模型包需含 model.int8.onnx 与 tokens.txt,支持中/英/日/韩/粤 + 标点 + 数字归一化(ITN)。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]  # 0.0 ~ 1.0


@dataclass
class Segment:
    start: float  # 秒
    end: float
    text: str


# 转写输入统一为 16kHz 单声道(converter.to_wav16k_mono 保证)
_WAV_SR = 16000

# 单块时长(秒)。sherpa-onnx SenseVoice 官方推荐单次输入 <= 30s,长音频按此分块,
# 时间戳按块起始偏移累加,块间无重叠。
_CHUNK_SECONDS = 30

# 句子边界(中文/英文标点),用于把 token 流切成带时间戳的句子片段
_SENTENCE_BOUNDARY = "。！？!?；;"

# 应用语言码 -> SenseVoice 语言参数(auto/zh/en/ja/ko/yue)
_LANG_MAP = {
    "zh": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-hant": "zh",
    "en": "en", "en-us": "en", "en-gb": "en",
    "ja": "ja", "ja-jp": "ja",
    "ko": "ko", "ko-kr": "ko",
    "yue": "yue", "cantonese": "yue",
}


def _map_sensevoice_language(lang: str | None) -> str:
    """应用语言码 -> SenseVoice 语言参数;空值/未识别返回 auto(自动检测)。"""
    if not lang or not str(lang).strip():
        return "auto"
    key = str(lang).strip().lower()
    if key == "auto":
        return "auto"
    return _LANG_MAP.get(key, "auto")


def _read_wav(wav: Path) -> tuple[np.ndarray, int]:
    """读取 wav 为 float32 单声道数组 + 采样率。"""
    import soundfile as sf

    samples, sr = sf.read(str(wav), dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32), int(sr)


def _tokens_to_segments(tokens: list[str], timestamps: list[float],
                        offset: float) -> list[Segment]:
    """把单块 token 流按句子边界切成带时间戳的片段(时间戳为块内相对秒,加 offset)。

    时间戳缺失或与 token 数不一致时,片段时间戳用块内估算(不丢文本)。
    返回空列表表示该块无有效文本。
    """
    segments: list[Segment] = []
    ts_valid = len(timestamps) == len(tokens)
    cur_tokens: list[str] = []
    cur_ts: list[float] = []
    for i, tok in enumerate(tokens):
        cur_tokens.append(tok)
        if ts_valid:
            cur_ts.append(timestamps[i])
        if tok.strip() and tok.strip()[-1] in _SENTENCE_BOUNDARY:
            seg = _make_segment(cur_tokens, cur_ts if ts_valid else [], offset)
            if seg is not None:
                segments.append(seg)
            cur_tokens, cur_ts = [], []
    if cur_tokens:
        seg = _make_segment(cur_tokens, cur_ts if ts_valid else [], offset)
        if seg is not None:
            segments.append(seg)
    return segments


def _make_segment(tokens: list[str], timestamps: list[float], offset: float) -> Segment | None:
    text = "".join(tokens).strip()
    if not text:
        return None
    if timestamps:
        start = float(timestamps[0])
        end = float(timestamps[-1])
        if end < start:
            end = start
    else:
        # 无时间戳:按 token 数在 30s 块内均匀分配
        start, end = 0.0, _CHUNK_SECONDS
    return Segment(offset + start, offset + end, text)


class SenseVoiceTranscriber:
    """SenseVoice 转写引擎:懒加载、CPU 推理、模型外置目录加载。

    on_event 回调把阶段消息(加载中/逐块进度/完成)实时上报给 pipeline,
    直达运行日记与任务日志。
    """

    def __init__(self, model_size: str = "SenseVoiceSmall", device: str = "auto",
                 compute_type: str = "auto", language: str | None = None,
                 vad: bool = True, model_dir: str = "",
                 on_event: Callable[[str], None] | None = None):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language or None
        self.vad = vad
        self.model_dir = model_dir or ""
        self.on_event = on_event
        self._model = None
        self._lock = threading.Lock()

    def _emit(self, message: str) -> None:
        """阶段消息:写日志 + 转发 on_event 回调(UI 运行日记可见)。"""
        logger.info("%s", message)
        if self.on_event is not None:
            try:
                self.on_event(message)
            except Exception:  # 回调异常不影响转写
                logger.exception("on_event 回调失败")

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def resolved_device(self) -> str:
        # sherpa-onnx 内置 onnxruntime 仅 CPU 提供者;CPU 上已 30~40 倍实时,无需 GPU
        return "cpu"

    def _model_path(self) -> Path:
        from app.models import resolve_model_dir, find_sensevoice_model
        root = resolve_model_dir(self.model_dir)
        local = find_sensevoice_model(root, self.model_size)
        if local is not None:
            return local
        raise FileNotFoundError(
            f"未在模型目录找到 SenseVoice 模型 {self.model_size!r}。"
            f"请下载 sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 模型包"
            f"(含 model.int8.onnx 与 tokens.txt)并放入模型目录。"
            f"设置 → 打开模型目录 可查看当前位置。"
        )

    def load(self):
        # double-checked locking:仅最终的模型赋值在锁内,避免两个 worker 并发
        # 首次转写时各加载一次模型(双倍内存峰值 + 浪费算力)。
        with self._lock:
            if self._model is not None:
                return
        import sherpa_onnx  # 延迟导入,加快 UI 启动

        model_dir = self._model_path()
        model_file = model_dir / "model.int8.onnx"
        tokens_file = model_dir / "tokens.txt"
        if not model_file.is_file() or not tokens_file.is_file():
            raise FileNotFoundError(
                f"SenseVoice 模型包不完整:{model_dir}\n需要 model.int8.onnx 与 tokens.txt。"
            )
        self._emit(f"正在加载 SenseVoice 模型:{model_dir.name}")
        num_threads = min(8, max(2, (os.cpu_count() or 4)))
        with self._lock:
            if self._model is not None:  # 二次复检:加载期间另一 worker 可能已加载完
                return
            self._model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model_file),
                tokens=str(tokens_file),
                num_threads=num_threads,
                use_itn=True,
                language=_map_sensevoice_language(self.language),
                debug=False,
            )
        self._emit("SenseVoice 模型加载完成")

    def transcribe(self, wav: Path, duration: float = 0.0,
                   progress: ProgressCallback | None = None) -> list[Segment]:
        """转写 wav,返回按时间排序的句子片段列表。duration>0 时按块结束时间估算进度。"""
        self.load()
        samples, sr = _read_wav(wav)
        if sr != _WAV_SR:
            logger.warning("采样率 %d ≠ %d,按 %d 处理(converter 应已归一化)", sr, _WAV_SR, _WAV_SR)
        total = len(samples) / sr
        if duration <= 0:
            duration = total

        chunk_len = int(_WAV_SR * _CHUNK_SECONDS)
        segments: list[Segment] = []
        with self._lock:
            n_chunks = max(1, (len(samples) + chunk_len - 1) // chunk_len)
            logger.info("开始转写 %s (%.1fs, %d 块, language=%s)",
                        wav, total, n_chunks, _map_sensevoice_language(self.language))
            for i in range(n_chunks):
                chunk = samples[i * chunk_len:(i + 1) * chunk_len]
                if chunk.size == 0:
                    break
                offset = i * chunk_len / _WAV_SR
                stream = self._model.create_stream()
                stream.accept_waveform(_WAV_SR, chunk)
                self._model.decode_stream(stream)
                result = stream.result
                tokens = list(getattr(result, "tokens", None) or [])
                timestamps = list(getattr(result, "timestamps", None) or [])
                if len(timestamps) != len(tokens):
                    timestamps = []
                segments.extend(_tokens_to_segments(tokens, timestamps, offset))
                if progress is not None:
                    progress(min((offset + _CHUNK_SECONDS) / duration, 1.0))
                self._emit(f"SenseVoice 转写进度:{i + 1}/{n_chunks} 块")
            if progress is not None:
                progress(1.0)
        segments.sort(key=lambda s: s.start)
        logger.info("转写完成:%s → %d 段", wav, len(segments))
        return segments
