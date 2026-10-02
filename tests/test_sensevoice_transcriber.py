"""SenseVoice 转写引擎纯函数测试(不加载模型)。"""

import threading

from app.sensevoice_transcriber import (
    SenseVoiceTranscriber,
    _map_sensevoice_language,
    _tokens_to_segments,
    _make_segment,
    Segment,
)


def test_map_language_auto_for_empty():
    assert _map_sensevoice_language(None) == "auto"
    assert _map_sensevoice_language("") == "auto"
    assert _map_sensevoice_language("auto") == "auto"


def test_map_language_known():
    assert _map_sensevoice_language("zh") == "zh"
    assert _map_sensevoice_language("zh-cn") == "zh"
    assert _map_sensevoice_language("en") == "en"
    assert _map_sensevoice_language("ja") == "ja"
    assert _map_sensevoice_language("ko") == "ko"
    assert _map_sensevoice_language("yue") == "yue"


def test_map_language_unknown_falls_back_auto():
    assert _map_sensevoice_language("fr") == "auto"
    assert _map_sensevoice_language("xx") == "auto"


def test_make_segment_with_timestamps():
    seg = _make_segment(["你", "好", "。"], [1.0, 1.5, 2.0], offset=10.0)
    assert seg == Segment(11.0, 12.0, "你好。")


def test_make_segment_no_timestamps_uses_chunk_range():
    seg = _make_segment(["你", "好"], [], offset=0.0)
    assert seg.start == 0.0
    assert seg.end > 0.0
    assert seg.text == "你好"


def test_make_segment_empty_text_returns_none():
    assert _make_segment([" ", " "], [1.0, 2.0], 0.0) is None


def test_tokens_to_segments_splits_on_punctuation():
    tokens = ["开", "饭", "时", "间", "。", "早", "上", "好", "！"]
    timestamps = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    segs = _tokens_to_segments(tokens, timestamps, offset=30.0)
    assert len(segs) == 2
    assert segs[0].text == "开饭时间。"
    assert segs[0].start == 30.1 and segs[0].end == 30.5
    assert segs[1].text == "早上好！"
    assert segs[1].start == 30.6 and segs[1].end == 30.9


def test_tokens_to_segments_no_boundary_keeps_whole():
    tokens = ["你", "好", "世", "界"]
    timestamps = [0.1, 0.2, 0.3, 0.4]
    segs = _tokens_to_segments(tokens, timestamps, offset=0.0)
    assert len(segs) == 1
    assert segs[0].text == "你好世界"


def test_tokens_to_segments_ts_mismatch_falls_back():
    """时间戳与 token 数不一致时不应崩溃(调用方会清空 timestamps)。"""
    segs = _tokens_to_segments(["a", "b", "c"], [0.1], offset=0.0)
    assert len(segs) == 1
    assert segs[0].text == "abc"


def test_tokens_to_segments_empty():
    assert _tokens_to_segments([], [], 0.0) == []


def _mk_sensevoice(root, name="SenseVoiceSmall") -> "Path":
    d = root / name
    d.mkdir(parents=True)
    (d / "model.int8.onnx").write_bytes(b"x")
    (d / "tokens.txt").write_text("x")
    return d


def test_load_loads_model_once_under_concurrent_workers(tmp_path, monkeypatch):
    """回归:并发首次转写只加载一次 SenseVoice 模型(避免双倍内存峰值 + 浪费算力)。

    修复前 load() 完全不在锁内,两个 worker 会各加载一次。修复后
    double-checked locking 保证只有一个 worker 真正调用 from_sense_voice。
    """
    _mk_sensevoice(tmp_path)
    import sherpa_onnx
    counter = {"n": 0}
    counter_lock = threading.Lock()

    def _fake_from_sense_voice(**kw):
        with counter_lock:
            counter["n"] += 1
        return object()

    monkeypatch.setattr(
        sherpa_onnx.OfflineRecognizer, "from_sense_voice", staticmethod(_fake_from_sense_voice)
    )
    t = SenseVoiceTranscriber(model_dir=str(tmp_path), device="cpu")

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
