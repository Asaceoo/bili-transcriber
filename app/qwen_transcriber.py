"""Qwen3-ASR 封装(官方 qwen-asr 包,transformers 后端)。

接口与 app.transcriber.Transcriber 对齐(pipeline 通过 engine 选择实现):
  - load(): 从本地模型目录加载(模型外置),未命中报清晰错误
  - transcribe(wav, duration, progress) -> list[Segment]
  - resolved_device: 已解析设备(cpu/cuda)

依赖:qwen-asr(含 torch/transformers),体积较大,仅在打包时包含;
模型目录规范见 app/models.py(Qwen3-ASR-0.6B/ 或 Qwen3-ASR-1.7B/)。

时间戳策略:模型目录内检测到 Qwen3-ForcedAligner-0.6B 时启用字级时间戳,
否则回退为整段文本 + 按句切分估算(无精确对齐)。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]  # 0.0 ~ 1.0


@dataclass
class Segment:
    start: float  # 秒
    end: float
    text: str


@dataclass
class _TsItem:
    """字级时间戳项(已按块起始时间偏移),供 _merge_items_to_segments 合并。"""

    text: str
    start_time: float
    end_time: float


# 手动分块时的单块时长上限(秒)。
# qwen-asr 底层:无时间戳 ASR 单块上限 MAX_ASR_INPUT_SECONDS=1200s;
# 开启时间戳(强制对齐器)时单块上限 MAX_FORCE_ALIGN_INPUT_SECONDS=180s。
# 无对齐器模式收窄到 120s:①进度粒度细(20 分钟音频 = 10 块,每块推进一次进度条);
# ②单块文本短,生成 token 数远离上限,杜绝截断。
_TS_CHUNK_SECONDS = 180
_ASR_CHUNK_SECONDS = 120

# 生成 token 上限。qwen-asr transformers 后端默认 512:120 秒中文语音 ≈ 500 字
# ≈ 600+ token,600s 长块(旧值)≈ 2400 字必然被截断——转写结果只剩开头一段,
# 表现为"转写一直不行"。2048 对 120s 块有充足余量,generate 遇 EOS 会提前停。
_MAX_NEW_TOKENS = 2048

# 转写输入统一为 16kHz 单声道(converter.to_wav16k_mono 保证;qwen-asr 亦按 16k 处理)
_WAV_SR = 16000


# whisper 惯例语言码 -> qwen-asr 规范全名(qwen-asr 只接受 "Chinese"/"English" 等全名,
# 传入 "zh" 会被 normalize 成 "Zh" 后校验失败抛 ValueError)
_LANG_MAP = {
    "zh": "Chinese", "zh-cn": "Chinese", "zh-hans": "Chinese", "zh-hant": "Chinese",
    "en": "English", "en-us": "English", "en-gb": "English",
    "ja": "Japanese", "ja-jp": "Japanese",
    "ko": "Korean", "ko-kr": "Korean",
    "fr": "French", "de": "German", "es": "Spanish", "pt": "Portuguese",
    "it": "Italian", "ru": "Russian", "th": "Thai", "vi": "Vietnamese",
    "ar": "Arabic", "tr": "Turkish", "nl": "Dutch", "id": "Indonesian",
    "ms": "Malay", "sv": "Swedish", "da": "Danish", "fi": "Finnish",
    "pl": "Polish", "cs": "Czech", "fil": "Filipino", "fa": "Persian",
    "el": "Greek", "ro": "Romanian", "hu": "Hungarian", "mk": "Macedonian",
}


def _map_qwen_language(lang: str | None) -> str | None:
    """把应用设置的语言码映射为 qwen-asr 规范名;空值/未识别返回 None(自动检测)。"""
    if not lang or not str(lang).strip():
        return None
    key = str(lang).strip().lower()
    if key == "auto":
        return None
    try:
        from qwen_asr.inference.utils import SUPPORTED_LANGUAGES
        for name in SUPPORTED_LANGUAGES:
            if name.lower() == key:
                return name
    except Exception:
        pass
    return _LANG_MAP.get(key)


def _find_local_model(model_dir: str, model_name: str) -> Path | None:
    """在模型目录中查找 Qwen3-ASR 模型包目录。"""
    from app.models import resolve_model_dir, find_qwen_asr_model
    root = resolve_model_dir(model_dir)
    return find_qwen_asr_model(root, model_name)


def _find_aligner(model_dir: str) -> Path | None:
    """在模型目录中查找 Qwen3-ForcedAligner-0.6B(可选,提供字级时间戳)。"""
    from app.models import resolve_model_dir
    root = resolve_model_dir(model_dir)
    if not root.is_dir():
        return None
    for name in ("Qwen3-ForcedAligner-0.6B", "Qwen3-ForcedAligner-0.6B-0.6B"):
        p = root / name
        if p.is_dir() and (p / "config.json").is_file():
            return p
    return None


def _split_sentences(text: str) -> list[str]:
    """按中文/英文句子边界切分(。！？.!? 等)。"""
    parts = re.split(r"(?<=[。！？!?;；])", text)
    return [p.strip() for p in parts if p.strip()]


def _split_chunks(wav, sr: int, max_chunk_sec: float) -> list[tuple]:
    """按低能量边界手动分块(复用 qwen-asr 官方同款切分函数,含块起始偏移)。

    延迟导入以保持 qwen_asr 缩时导入:测试可 monkeypatch 本函数注入假分块器。
    返回 [(chunk_wav, offset_sec), ...],各块拼接可无损还原原音频。
    """
    from qwen_asr.inference.utils import split_audio_into_chunks
    return split_audio_into_chunks(wav=wav, sr=int(sr), max_chunk_sec=max_chunk_sec)


class QwenASRTranscriber:
    """Qwen3-ASR 转写引擎:懒加载、GPU 优先、模型外置目录加载。

    on_event 回调把阶段消息(加载中/逐块进度/完成)实时上报给 pipeline,
    直达运行日记与任务日志——模型首次加载需 1~2 分钟,没有阶段消息时
    用户面对 0% 进度条无从判断,只能取消任务。
    """

    def __init__(self, model_size: str = "Qwen3-ASR-0.6B", device: str = "auto",
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
        self._aligner_path: Path | None = None
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
        return self._resolve()[0]

    def _resolve(self) -> tuple[str, str]:
        """解析要使用的设备与计算精度。

        Qwen3-ASR 走 torch/transformers,是否可用 CUDA 完全取决于打包的 torch
        是否带 CUDA 构建(尚未启用时若设了 cuda,加载会抛
        "Torch not compiled with CUDA enabled")。因此即便设置里指定了
        device=cuda,也要先用 torch.cuda.is_available() 复核,不支持就自动
        回退 CPU,避免转写直接崩掉。
        """
        import os
        env_device = os.environ.get("BILI_DEVICE")
        device = env_device or self.device

        torch_cuda = False
        try:
            import torch
            torch_cuda = bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001 — 探测失败一律视为无 CUDA
            torch_cuda = False

        if device == "auto":
            device = "cuda" if torch_cuda else "cpu"
        elif device == "cuda" and not torch_cuda:
            logger.warning(
                "当前构建的 torch 未启用 CUDA(Torch not compiled with CUDA enabled),"
                "自动回退 CPU 转写;如需 GPU 加速请使用 Whisper 引擎。"
            )
            device = "cpu"

        compute = self.compute_type
        if compute == "auto":
            compute = "bfloat16" if device == "cuda" else "float32"
        return device, compute

    def _model_path(self) -> Path:
        local = _find_local_model(self.model_dir, self.model_size)
        if local is not None:
            return local
        raise FileNotFoundError(
            f"未在模型目录找到 Qwen3-ASR 模型 {self.model_size!r}。"
            f"请从 ModelScope 下载 Qwen/Qwen3-ASR-0.6B(或 1.7B)并解压到模型目录。"
            f"设置 → 打开模型目录 可查看当前位置。"
        )

    def load(self):
        # double-checked locking:仅最终的模型赋值在锁内,避免两个 worker 并发
        # 首次转写时各加载一次模型(双倍内存峰值 + 浪费算力)。
        with self._lock:
            if self._model is not None:
                return
        from qwen_asr import Qwen3ASRModel  # 延迟导入,加快 UI 启动

        device, compute = self._resolve()
        import torch

        dtype = torch.bfloat16 if device == "cuda" else torch.float32
        model_path = self._model_path()
        self._aligner_path = _find_aligner(self.model_dir)

        aligner_kwargs: dict | None = None
        if self._aligner_path is not None:
            aligner_kwargs = dict(dtype=dtype, device_map="cuda:0" if device == "cuda" else "cpu")

        logger.info(
            "加载 Qwen3-ASR 模型 %s (device=%s, compute=%s, aligner=%s)",
            model_path, device, compute,
            self._aligner_path.name if self._aligner_path else "无(降级估算时间戳)",
        )
        if device == "cpu" and "1.7B" in self.model_size:
            self._emit(
                "提示:1.7B 模型在 CPU 上转写非常慢(约为 0.6B 的 3 倍耗时),"
                "建议改用 Qwen3-ASR-0.6B,或 Whisper 引擎 + GPU 加速包"
            )
        self._emit(
            f"正在加载 Qwen3-ASR 模型(首次加载约 1~2 分钟,请勿关闭应用):{model_path.name} @ {device}"
        )
        with self._lock:
            if self._model is not None:  # 二次复检:加载期间另一 worker 可能已加载完
                return
            self._model = Qwen3ASRModel.from_pretrained(
                str(model_path),
                dtype=dtype,
                device_map="cuda:0" if device == "cuda" else "cpu",
                max_inference_batch_size=8,
                max_new_tokens=_MAX_NEW_TOKENS,
                forced_aligner=str(self._aligner_path) if self._aligner_path else None,
                forced_aligner_kwargs=aligner_kwargs,
            )
        self._emit("Qwen3-ASR 模型加载完成,开始转写")

    def transcribe(self, wav: Path, duration: float = 0.0,
                   progress: ProgressCallback | None = None) -> list[Segment]:
        """转写 wav,返回按时间排序的片段列表。

        采用官方同款「手动分块 + 逐块转写」的最成熟方案:长音频先按低能量边界
        切成小块(有对齐器时 180s/块、无对齐器 120s/块),逐块调用底层 transcribe,
        从而:
          - 长音频不再一次性阻塞到底,每完成一块就回报一次进度(UI 进度条实时走动);
          - 每块之间可响应任务暂停/取消(progress 回调经 pipeline 抛 CancelledError);
          - 各块文本拼接、字级时间戳按块起始时间偏移后合并,结果等价于整段转写。
        """
        self.load()
        data, sr = self._read_wav_16k(wav)
        if data is None or len(data) == 0:
            if progress is not None:
                progress(1.0)
            return []
        total_sec = len(data) / float(sr) if sr else 0.0
        total_sec = duration or total_sec

        chunk_sec = _TS_CHUNK_SECONDS if self._aligner_path is not None else _ASR_CHUNK_SECONDS
        chunks = _split_chunks(data, sr, chunk_sec)
        n = max(1, len(chunks))

        pieces: list[str] = []
        merged_items: list[_TsItem] = []
        with self._lock:
            for i, (cwav, offset_sec) in enumerate(chunks):
                if progress is not None:
                    progress(i / n)  # 块间检查暂停/取消,取消时由回调抛 CancelledError
                self._emit(f"Qwen3-ASR 转写进度:{i + 1}/{n} 块")
                results = self._model.transcribe(
                    audio=(cwav, _WAV_SR),
                    language=_map_qwen_language(self.language),
                    return_time_stamps=self._aligner_path is not None,
                )
                text = results[0].text.strip() if results and results[0].text else ""
                if text:
                    pieces.append(text)
                    timestamps = getattr(results[0], "time_stamps", None)
                    items = getattr(timestamps, "items", None) if timestamps is not None else None
                    if items:
                        for it in items:
                            t = getattr(it, "text", "") or ""
                            if not t:
                                continue
                            merged_items.append(_TsItem(
                                text=t,
                                start_time=self._ts_value(it, "start_time", 0.0) + offset_sec,
                                end_time=self._ts_value(it, "end_time", 0.0) + offset_sec,
                            ))
        if progress is not None:
            progress(1.0)  # 终值,同时作为最后一次取消检查

        full_text = "".join(pieces).strip()
        if not full_text:
            return []
        if merged_items:
            # 有字级时间戳:按句切分,合并为片段(时间已含块偏移)
            return self._merge_items_to_segments(merged_items, full_text)
        # 无对齐器:按句切分,时间在总时长内线性估算
        return self._estimate_segments(full_text, total_sec)

    @staticmethod
    def _read_wav_16k(wav: Path) -> tuple:
        """读取 wav 为 float32 单声道 16kHz 波形;转换器已保证 16k 单声道,这里仅防御。

        返回 (waveform: np.ndarray | None, sr: int)。读取失败报清晰错误而非静默崩掉。
        """
        import numpy as np
        import soundfile as sf
        try:
            data, sr = sf.read(str(wav), dtype="float32", always_2d=False)
        except Exception as exc:  # noqa: BLE001 — 文件损坏/解码失败统一报错
            raise RuntimeError(f"Qwen3-ASR 读取音频失败:{wav}:{exc}") from exc
        if data.ndim > 1:
            data = np.mean(data, axis=-1).astype(np.float32)
        data = np.asarray(data, dtype=np.float32)
        if sr != _WAV_SR:
            import librosa
            data = librosa.resample(data, orig_sr=sr, target_sr=_WAV_SR).astype(np.float32)
            sr = _WAV_SR
        return data, int(sr)

    @staticmethod
    def _ts_value(it, attr: str, default: float) -> float:
        """取字级时间戳项的时间值;None/非数值(对齐器异常输出)回退默认,避免 float(None) 崩溃。"""
        v = getattr(it, attr, None)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _merge_items_to_segments(items, full_text: str) -> list[Segment]:
        """把字级时间戳项按句合并成片段。"""
        segs: list[Segment] = []
        cur_start: float | None = None
        cur_text: list[str] = []
        for it in items:
            t = getattr(it, "text", "") or ""
            if not t:
                continue
            if cur_start is None:
                cur_start = QwenASRTranscriber._ts_value(it, "start_time", 0.0)
            cur_text.append(t)
            if t in "。！？!?；;":
                segs.append(Segment(
                    start=cur_start,
                    end=QwenASRTranscriber._ts_value(it, "end_time", cur_start),
                    text="".join(cur_text).strip(),
                ))
                cur_start, cur_text = None, []
        if cur_text and cur_start is not None:
            segs.append(Segment(
                start=cur_start,
                end=QwenASRTranscriber._ts_value(items[-1], "end_time", cur_start),
                text="".join(cur_text).strip(),
            ))
        if not segs:
            # 无标点长文本:整体一段
            segs = [Segment(
                start=QwenASRTranscriber._ts_value(items[0], "start_time", 0.0),
                end=QwenASRTranscriber._ts_value(items[-1], "end_time", 0.0),
                text=full_text,
            )]
        return [s for s in segs if s.text]

    @staticmethod
    def _estimate_segments(text: str, duration: float) -> list[Segment]:
        """无对齐器时:按句切分,时间按字符占比线性估算。"""
        text = text.strip()
        sentences = _split_sentences(text)
        if not sentences:
            return [Segment(0.0, duration, text)] if text else []
        total_chars = sum(len(s) for s in sentences)
        if total_chars <= 0 or duration <= 0:
            return [Segment(0.0, duration, text)]
        segs: list[Segment] = []
        cursor = 0.0
        for s in sentences:
            frac = len(s) / total_chars
            end = cursor + duration * frac
            segs.append(Segment(start=round(cursor, 3), end=round(end, 3), text=s))
            cursor = end
        return segs
