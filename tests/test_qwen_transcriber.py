"""Qwen3-ASR 转写引擎测试(纯逻辑,不加载真实模型)。"""

from pathlib import Path
import threading

import numpy as np
import pytest

from app.qwen_transcriber import (
    QwenASRTranscriber, Segment, _find_aligner, _find_local_model,
    _map_qwen_language, _split_sentences, _MAX_NEW_TOKENS, _WAV_SR,
)
from app.task_control import CancelledError


# ---------- _find_local_model / _find_aligner ----------

def _mk_qwen(root: Path, name: str = "Qwen3-ASR-0.6B") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    (d / "model.safetensors").write_bytes(b"x")
    return d


def _mk_aligner(root: Path) -> Path:
    d = root / "Qwen3-ForcedAligner-0.6B"
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    return d


def test_find_local_model_direct_name(tmp_path: Path):
    _mk_qwen(tmp_path)
    assert _find_local_model(str(tmp_path), "Qwen3-ASR-0.6B") == tmp_path / "Qwen3-ASR-0.6B"


def test_find_local_model_short_name(tmp_path: Path):
    _mk_qwen(tmp_path, "Qwen3-ASR-0.6B")
    assert _find_local_model(str(tmp_path), "0.6B") == tmp_path / "Qwen3-ASR-0.6B"


def test_find_local_model_missing(tmp_path: Path):
    assert _find_local_model(str(tmp_path), "Qwen3-ASR-0.6B") is None
    assert _find_local_model(str(tmp_path / "nope"), "Qwen3-ASR-0.6B") is None


def test_find_aligner(tmp_path: Path):
    _mk_aligner(tmp_path)
    assert _find_aligner(str(tmp_path)) == tmp_path / "Qwen3-ForcedAligner-0.6B"


def test_find_aligner_missing(tmp_path: Path):
    assert _find_aligner(str(tmp_path)) is None


# ---------- 句子切分 ----------

def test_split_sentences_chinese():
    assert _split_sentences("你好。世界！这是测试?") == ["你好。", "世界！", "这是测试?"]


def test_split_sentences_no_boundary():
    assert _split_sentences("没有标点的一句话") == ["没有标点的一句话"]


def test_split_sentences_empty():
    assert _split_sentences("") == []
    assert _split_sentences("   ") == []


# ---------- _estimate_segments(无对齐器降级) ----------

def test_estimate_segments_splits_by_duration():
    t = QwenASRTranscriber()
    segs = t._estimate_segments("你好。世界。", 10.0)
    assert len(segs) == 2
    assert segs[0].text == "你好。"
    assert abs(segs[0].start - 0.0) < 1e-3
    assert abs(segs[1].end - 10.0) < 1e-3
    assert segs[0].end <= segs[1].start


def test_estimate_segments_empty_text():
    t = QwenASRTranscriber()
    assert t._estimate_segments("", 10.0) == []
    assert t._estimate_segments(" ", 10.0) == []


def test_estimate_segments_no_duration():
    t = QwenASRTranscriber()
    segs = t._estimate_segments("只有一句。", 0.0)
    assert segs == [Segment(0.0, 0.0, "只有一句。")]


# ---------- _merge_items_to_segments(字级时间戳合并) ----------

class FakeItem:
    def __init__(self, text: str, start: float, end: float):
        self.text, self.start_time, self.end_time = text, start, end


def test_merge_items_groups_by_punctuation():
    t = QwenASRTranscriber()
    items = [
        FakeItem("你", 0.0, 0.2), FakeItem("好", 0.2, 0.4), FakeItem("。", 0.4, 0.5),
        FakeItem("世", 0.6, 0.8), FakeItem("界", 0.8, 1.0), FakeItem("！", 1.0, 1.1),
    ]
    segs = t._merge_items_to_segments(items, "你好。世界！")
    assert len(segs) == 2
    assert segs[0].text == "你好。"
    assert segs[0].start == 0.0 and segs[0].end == 0.5
    assert segs[1].text == "世界！"
    assert segs[1].start == 0.6 and segs[1].end == 1.1


def test_merge_items_no_punctuation_falls_back():
    t = QwenASRTranscriber()
    items = [FakeItem("全", 0.0, 0.3), FakeItem("文", 0.3, 0.6)]
    segs = t._merge_items_to_segments(items, "全文")
    assert len(segs) == 1
    assert segs[0].text == "全文"
    assert segs[0].start == 0.0 and segs[0].end == 0.6


def test_merge_items_none_timestamps_do_not_crash():
    """对齐器异常输出 None 时间戳时不应 float(None) 崩溃,应回退默认值。"""
    t = QwenASRTranscriber()

    class NoneItem:
        def __init__(self, text):
            self.text, self.start_time, self.end_time = text, None, None

    items = [NoneItem("你"), NoneItem("好"), NoneItem("。"), NoneItem("世"), NoneItem("界")]
    segs = t._merge_items_to_segments(items, "你好。世界")
    assert [s.text for s in segs] == ["你好。", "世界"]
    assert all(s.start == 0.0 and s.end == 0.0 for s in segs)


def test_ts_value_guards_bad_values():
    t = QwenASRTranscriber()
    assert t._ts_value(FakeItem("x", 1.5, 2.5), "start_time", 0.0) == 1.5
    assert t._ts_value(FakeItem("x", None, None), "start_time", 9.0) == 9.0
    assert t._ts_value(object(), "missing", 7.0) == 7.0


# ---------- 设备解析 ----------

def _patch_torch_cuda(monkeypatch, available: bool):
    """把 qwen_transcriber 里 `import torch` 替换为带指定 cuda 能力的假 torch。"""
    import builtins
    real_import = builtins.__import__

    class _FakeCuda:
        @staticmethod
        def is_available():
            return available

    class _FakeTorch:
        cuda = _FakeCuda

    def _fake_import(name, *a, **k):
        if name == "torch":
            return _FakeTorch
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


def test_resolve_device_cpu_default(monkeypatch):
    monkeypatch.delenv("BILI_DEVICE", raising=False)
    t = QwenASRTranscriber(device="auto")
    # 无 torch.cuda 时回退 cpu;这里直接指定 cpu 验证
    t.device = "cpu"
    dev, compute = t._resolve()
    assert dev == "cpu" and compute == "float32"


def test_resolve_device_env_override(monkeypatch):
    monkeypatch.setenv("BILI_DEVICE", "cuda")
    monkeypatch.setenv("BILI_COMPUTE_TYPE", "bfloat16")
    _patch_torch_cuda(monkeypatch, available=True)
    t = QwenASRTranscriber()
    dev, compute = t._resolve()
    assert dev == "cuda" and compute == "bfloat16"


def test_resolve_device_cuda_falls_back_cpu_when_torch_cpu_only(monkeypatch):
    """回归:设置 device=cuda 但打包 torch 为 CPU-only 时,自动回退 CPU。

    此前 qwen 引擎拿到 whisper 的 cuda 建议后直接用 cuda 加载 torch,
    抛 "Torch not compiled with CUDA enabled" 崩溃。
    """
    monkeypatch.delenv("BILI_DEVICE", raising=False)
    _patch_torch_cuda(monkeypatch, available=False)
    t = QwenASRTranscriber(device="cuda", compute_type="auto")
    dev, compute = t._resolve()
    assert dev == "cpu"          # 不崩溃,自动回退
    assert compute == "float32"  # cuda 专属 bfloat16 也不应用


def test_resolve_device_auto_falls_back_cpu_when_no_cuda(monkeypatch):
    """device=auto 且 torch CPU-only -> 解析为 cpu + float32。"""
    monkeypatch.delenv("BILI_DEVICE", raising=False)
    _patch_torch_cuda(monkeypatch, available=False)
    t = QwenASRTranscriber(device="auto")
    dev, compute = t._resolve()
    assert dev == "cpu" and compute == "float32"


def test_model_missing_raises_clear_error(tmp_path: Path):
    t = QwenASRTranscriber(model_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError, match="未在模型目录找到"):
        t._model_path()


# ---------- 语言码 -> qwen-asr 规范名映射 ----------

def test_map_language_whisper_codes_to_canonical():
    assert _map_qwen_language("zh") == "Chinese"
    assert _map_qwen_language("zh-CN") == "Chinese"
    assert _map_qwen_language("en") == "English"
    assert _map_qwen_language("ja") == "Japanese"
    assert _map_qwen_language("ko") == "Korean"


def test_map_language_canonical_passthrough():
    # qwen-asr 已支持的全名(不区分大小写)原样返回
    assert _map_qwen_language("Chinese") == "Chinese"
    assert _map_qwen_language("english") == "English"
    assert _map_qwen_language("Cantonese") == "Cantonese"


def test_map_language_empty_and_auto_to_none():
    assert _map_qwen_language(None) is None
    assert _map_qwen_language("") is None
    assert _map_qwen_language("  ") is None
    assert _map_qwen_language("auto") is None


def test_map_language_unknown_falls_back_to_auto():
    # 未识别语言码不抛错,回退自动检测
    assert _map_qwen_language("xx-klingon") is None


# ---------- 分块转写(长音频逐块进度 + 时间戳偏移 + 取消) ----------

class _Ts:
    """模拟 results[0].time_stamps。"""

    def __init__(self, items):
        self.items = items


class _Result:
    """模拟 ASRTranscription。"""

    def __init__(self, text, items=None):
        self.text = text
        self.time_stamps = _Ts(items) if items is not None else None


class _FakeModel:
    """按调用顺序返回预先给定结果的假模型,并记录收到的 language。"""

    def __init__(self, per_chunk):
        self.per_chunk = per_chunk
        self.calls = []  # 每次 transcribe 收到的 language

    def transcribe(self, audio, language=None, return_time_stamps=False):
        self.calls.append(language)
        return [self.per_chunk[len(self.calls) - 1]]


def _item(text, start, end):
    return type("I", (), {"text": text, "start_time": start, "end_time": end})()


def _ready_transcriber(t, model, aligner_path=None) -> QwenASRTranscriber:
    """给 t 塞入假模型使 load() 直接返回,并指定对齐器存在与否。"""
    t._model = model
    t._aligner_path = aligner_path
    return t


def test_transcribe_chunks_offsets_and_progress(monkeypatch, tmp_path):
    """有对齐器:两块 180s 音频转写,字级时间戳按块偏移合并,进度逐块回报。"""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")  # 占位,_read_wav_16k 会被替换
    t = QwenASRTranscriber(device="cpu", language="zh")
    model = _FakeModel([
        _Result("你好。", [_item("你", 0.0, 0.2), _item("好", 0.2, 0.4), _item("。", 0.4, 0.5)]),
        _Result("世界！", [_item("世", 0.0, 0.2), _item("界", 0.2, 0.4), _item("！", 0.4, 0.5)]),
    ])
    _ready_transcriber(t, model, aligner_path=Path("/x/Qwen3-ForcedAligner-0.6B"))
    # 分两块,偏移分别为 0s / 180s
    monkeypatch.setattr(
        "app.qwen_transcriber._split_chunks",
        lambda data, sr, sec: [
            (np.zeros(sr * 180, np.float32), 0.0),
            (np.zeros(sr * 180, np.float32), 180.0),
        ],
    )
    monkeypatch.setattr(
        "app.qwen_transcriber.QwenASRTranscriber._read_wav_16k",
        lambda self, w: (np.zeros(_WAV_SR * 360, np.float32), _WAV_SR),
    )
    progress_calls = []
    segs = t.transcribe(wav, duration=360.0, progress=progress_calls.append)

    assert [s.text for s in segs] == ["你好。", "世界！"]
    assert segs[0].start == 0.0 and segs[0].end == 0.5
    # 第二块偏移 180s
    assert abs(segs[1].start - 180.0) < 1e-3 and abs(segs[1].end - 180.5) < 1e-3
    # 语言映射 zh -> Chinese 传入模型
    assert model.calls == ["Chinese", "Chinese"]
    # 进度:起始 0.0 → 双块间 → 终值 1.0,单调不减
    assert progress_calls[0] == 0.0 and progress_calls[-1] == 1.0
    assert all(progress_calls[i] <= progress_calls[i + 1] for i in range(len(progress_calls) - 1))


def test_transcribe_no_aligner_estimates_segments(monkeypatch, tmp_path):
    """无对齐器:逐块文本拼接后按句估算时间(不携带字级时间戳)。"""
    wav = tmp_path / "b.wav"
    wav.write_bytes(b"")
    t = QwenASRTranscriber(device="cpu")
    model = _FakeModel([_Result("第一句。"), _Result("第二句。")])
    _ready_transcriber(t, model, aligner_path=None)
    monkeypatch.setattr(
        "app.qwen_transcriber._split_chunks",
        lambda data, sr, sec: [
            (np.zeros(sr * 100, np.float32), 0.0),
            (np.zeros(sr * 100, np.float32), 100.0),
        ],
    )
    monkeypatch.setattr(
        "app.qwen_transcriber.QwenASRTranscriber._read_wav_16k",
        lambda self, w: (np.zeros(_WAV_SR * 200, np.float32), _WAV_SR),
    )
    segs = t.transcribe(wav, duration=200.0)
    assert [s.text for s in segs] == ["第一句。", "第二句。"]
    # 无对齐器:模型不要求时间戳;两段合计落在 [0,200] 内
    assert segs[-1].end <= 200.0 + 1e-3
    assert segs[0].start >= 0.0


def test_transcribe_cancel_raises_between_chunks(monkeypatch, tmp_path):
    """取消:进度回调抛 CancelledError 时,在块间中止转写(不再继续下一块)。"""
    wav = tmp_path / "c.wav"
    wav.write_bytes(b"")
    t = QwenASRTranscriber(device="cpu")
    model = _FakeModel([_Result("只有一块")])
    _ready_transcriber(t, model, aligner_path=None)
    monkeypatch.setattr(
        "app.qwen_transcriber._split_chunks",
        lambda data, sr, sec: [(np.zeros(sr * 60, np.float32), 0.0)],
    )
    monkeypatch.setattr(
        "app.qwen_transcriber.QwenASRTranscriber._read_wav_16k",
        lambda self, w: (np.zeros(_WAV_SR * 60, np.float32), _WAV_SR),
    )

    def _cancel_on_start(p):
        raise CancelledError("media_1")

    with pytest.raises(CancelledError):
        t.transcribe(wav, duration=60.0, progress=_cancel_on_start)


def test_transcribe_empty_audio_reports_done(monkeypatch, tmp_path):
    """空音频(0 长度)不转写,直接回报进度 1.0。"""
    wav = tmp_path / "d.wav"
    wav.write_bytes(b"")
    t = QwenASRTranscriber(device="cpu")
    _ready_transcriber(t, _FakeModel([]), aligner_path=None)
    monkeypatch.setattr(
        "app.qwen_transcriber.QwenASRTranscriber._read_wav_16k",
        lambda self, w: (np.zeros(0, np.float32), _WAV_SR),
    )
    progress_calls = []
    assert t.transcribe(wav, duration=0.0, progress=progress_calls.append) == []
    assert progress_calls == [1.0]


def test_read_wav_16k_sterreo_downmixed(monkeypatch, tmp_path):
    """_read_wav_16k:多声道波形降为单声道,并保持在 16k。"""
    import soundfile as sf
    wav = tmp_path / "stereo.wav"
    data = np.zeros((8000, 2), np.float32)
    data[:, 0] = 1.0
    sf.write(str(wav), data, 16000, subtype="PCM_16")
    arr, sr = QwenASRTranscriber._read_wav_16k(wav)
    assert arr.ndim == 1 and sr == 16000


# ---------- 加载参数与阶段事件(on_event) ----------

def test_load_passes_max_new_tokens_and_emits_events(tmp_path, monkeypatch):
    """load() 显式传 max_new_tokens=2048:官方默认 512,120 秒中文语音约 600+ token
    必然截断——「转写只剩开头一段」的根因。同时加载阶段消息经 on_event 上报。"""
    _mk_qwen(tmp_path)
    import qwen_asr
    captured = {}
    monkeypatch.setattr(
        qwen_asr.Qwen3ASRModel, "from_pretrained",
        classmethod(lambda cls, *a, **kw: captured.update(args=a, kwargs=kw) or object()),
    )
    events = []
    t = QwenASRTranscriber(model_dir=str(tmp_path), device="cpu", on_event=events.append)
    t.load()
    assert _MAX_NEW_TOKENS == 2048
    assert captured["kwargs"]["max_new_tokens"] == 2048
    assert any("正在加载" in e for e in events)
    assert any("加载完成" in e for e in events)


def test_load_warns_1_7b_on_cpu(tmp_path, monkeypatch):
    """1.7B 模型 + CPU:加载阶段给出慢速提示,引导用户改回 0.6B 或换 Whisper。"""
    _mk_qwen(tmp_path, name="Qwen3-ASR-1.7B")
    import qwen_asr
    monkeypatch.setattr(
        qwen_asr.Qwen3ASRModel, "from_pretrained",
        classmethod(lambda cls, *a, **kw: object()),
    )
    events = []
    t = QwenASRTranscriber(
        model_size="Qwen3-ASR-1.7B", model_dir=str(tmp_path),
        device="cpu", on_event=events.append,
    )
    t.load()
    assert any("1.7B" in e and "慢" in e for e in events)


def test_transcribe_emits_chunk_progress_events(monkeypatch, tmp_path):
    """逐块转写时 on_event 上报「第 i/n 块」进度消息,UI 运行日记可见。"""
    wav = tmp_path / "e.wav"
    wav.write_bytes(b"")
    events = []
    t = QwenASRTranscriber(device="cpu", on_event=events.append)
    model = _FakeModel([_Result("第一句。"), _Result("第二句。")])
    _ready_transcriber(t, model, aligner_path=None)
    monkeypatch.setattr(
        "app.qwen_transcriber._split_chunks",
        lambda data, sr, sec: [
            (np.zeros(sr * 60, np.float32), 0.0),
            (np.zeros(sr * 60, np.float32), 60.0),
        ],
    )
    monkeypatch.setattr(
        "app.qwen_transcriber.QwenASRTranscriber._read_wav_16k",
        lambda self, w: (np.zeros(_WAV_SR * 120, np.float32), _WAV_SR),
    )
    t.transcribe(wav, duration=120.0)
    assert any("1/2" in e for e in events)
    assert any("2/2" in e for e in events)


def test_load_loads_model_once_under_concurrent_workers(tmp_path, monkeypatch):
    """回归:并发首次转写只加载一次 Qwen 模型(避免双倍内存峰值 + 浪费算力)。

    double-checked locking:仅最终赋值在锁内,两个 worker 并发进入时
    只会有一个真正调用 from_pretrained。
    """
    _mk_qwen(tmp_path)
    import qwen_asr
    counter = {"n": 0}
    counter_lock = threading.Lock()

    def _fake_from_pretrained(cls, *a, **kw):
        with counter_lock:
            counter["n"] += 1
        return object()

    monkeypatch.setattr(
        qwen_asr.Qwen3ASRModel, "from_pretrained", classmethod(_fake_from_pretrained)
    )
    t = QwenASRTranscriber(model_dir=str(tmp_path), device="cpu")

    barrier = threading.Barrier(2)
    errors: list = []

    def worker() -> None:
        try:
            barrier.wait()
            t.load()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=15)

    assert not errors, f"load() 并发执行抛错: {errors}"
    assert counter["n"] == 1, f"并发下模型被加载了 {counter['n']} 次,应为 1 次"
    t.load()
    assert counter["n"] == 1
