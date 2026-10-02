"""模型目录扫描与本地加载测试。"""

from pathlib import Path

from app import models
from app.models import (
    DetectedModel, ENGINE_QWEN3_ASR, ENGINE_SENSEVOICE, ENGINE_WHISPER,
    find_whisper_model, resolve_model_dir, scan_models,
)


def _mk_whisper(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"x")
    (d / "config.json").write_text("{}")
    return d


def test_default_model_dir_uses_localappdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\tester\AppData\Local")
    assert models.default_model_dir() == Path(r"C:\Users\tester\AppData\Local\Bili Note\models")


def test_default_model_dir_fallback_home(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert models.default_model_dir() == Path.home() / "Bili Note" / "models"


def test_resolve_model_dir_blank_uses_default(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\appdata")
    assert resolve_model_dir("") == Path(r"C:\appdata\Bili Note\models")
    assert resolve_model_dir("   ") == Path(r"C:\appdata\Bili Note\models")


def test_resolve_model_dir_absolute(tmp_path: Path):
    assert resolve_model_dir(str(tmp_path)) == tmp_path


def test_scan_detects_whisper_model(tmp_path: Path):
    _mk_whisper(tmp_path, "faster-whisper-large-v3-turbo")
    found = scan_models(tmp_path)
    assert len(found) == 1
    m = found[0]
    assert m.engine == ENGINE_WHISPER
    assert m.name == "large-v3-turbo"  # 前缀被剥离
    assert m.path.name == "faster-whisper-large-v3-turbo"


def test_scan_detects_qwen3_asr(tmp_path: Path):
    d = tmp_path / "Qwen3-ASR-0.6B"
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"x")
    (d / "config.json").write_text("{}")
    found = scan_models(tmp_path)
    assert len(found) == 1
    assert found[0].engine == ENGINE_QWEN3_ASR
    assert found[0].name == "Qwen3-ASR-0.6B"


def test_scan_detects_sensevoice(tmp_path: Path):
    d = tmp_path / "SenseVoiceSmall"
    d.mkdir()
    (d / "config.yaml").write_text("{}")
    (d / "model.pt").write_bytes(b"x")
    found = scan_models(tmp_path)
    assert len(found) == 1
    assert found[0].engine == ENGINE_SENSEVOICE


def test_scan_detects_sensevoice_sherpa_onnx(tmp_path: Path):
    """sherpa-onnx 格式(model.int8.onnx + tokens.txt)也应被识别为 SenseVoice。"""
    d = tmp_path / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    d.mkdir()
    (d / "model.int8.onnx").write_bytes(b"x")
    (d / "tokens.txt").write_text("a\nb\n")
    found = scan_models(tmp_path)
    assert len(found) == 1
    assert found[0].engine == ENGINE_SENSEVOICE
    assert found[0].valid is True


def test_find_sensevoice_model(tmp_path: Path):
    from app.models import find_sensevoice_model
    d = tmp_path / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    d.mkdir()
    (d / "model.int8.onnx").write_bytes(b"x")
    (d / "tokens.txt").write_text("a\n")
    assert find_sensevoice_model(tmp_path, "SenseVoiceSmall") == d
    assert find_sensevoice_model(tmp_path, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17") == d
    assert find_sensevoice_model(tmp_path / "nope", "SenseVoiceSmall") is None


def test_find_sensevoice_model_renamed(tmp_path: Path):
    """用户自定义目录名(含 sensevoice 关键字)也能命中。"""
    from app.models import find_sensevoice_model
    d = tmp_path / "my-sensevoice-copy"
    d.mkdir()
    (d / "model.int8.onnx").write_bytes(b"x")
    (d / "tokens.txt").write_text("a\n")
    assert find_sensevoice_model(tmp_path, "SenseVoiceSmall") == d


def test_scan_ignores_unrecognized_dirs(tmp_path: Path):
    (tmp_path / "random").mkdir()
    (tmp_path / "random" / "foo.txt").write_text("x")
    assert scan_models(tmp_path) == []


def test_scan_empty_or_missing(tmp_path: Path):
    assert scan_models(tmp_path) == []            # 空目录
    assert scan_models(tmp_path / "nope") == []   # 不存在


def test_find_whisper_model_official_name(tmp_path: Path):
    _mk_whisper(tmp_path, "faster-whisper-small")
    hit = find_whisper_model(tmp_path, "small")
    assert hit is not None and hit.name == "faster-whisper-small"


def test_find_whisper_model_renamed_dir(tmp_path: Path):
    _mk_whisper(tmp_path, "small")
    assert find_whisper_model(tmp_path, "small") == tmp_path / "small"


def test_find_whisper_model_missing(tmp_path: Path):
    _mk_whisper(tmp_path, "faster-whisper-medium")
    assert find_whisper_model(tmp_path, "large-v3") is None


def test_describe_models_summary(tmp_path: Path):
    _mk_whisper(tmp_path, "faster-whisper-small")
    summary = models.describe_models(tmp_path)
    assert "small" in summary and ENGINE_WHISPER in summary
    assert "✓" in summary
    missing_dir = tmp_path / "empty"
    missing_summary = models.describe_models(missing_dir)
    assert "未检测到模型包" in missing_summary or "目录不存在" in missing_summary


def test_detected_model_dataclass():
    m = DetectedModel(engine="whisper", name="small", path=Path("/x"))
    assert m.engine == "whisper" and m.name == "small"


def test_scan_detects_incomplete_qwen(tmp_path: Path):
    """缺少权重文件时应标记为不完整,帮助用户排查模型包放错。"""
    d = tmp_path / "Qwen3-ASR-0.6B"
    d.mkdir()
    (d / "config.json").write_text("{}")
    found = scan_models(tmp_path)
    assert len(found) == 1
    assert found[0].engine == ENGINE_QWEN3_ASR
    assert found[0].valid is False
    assert any("safetensors" in m for m in found[0].missing)


def test_find_qwen_asr_model(tmp_path: Path):
    from app.models import find_qwen_asr_model
    d = tmp_path / "Qwen3-ASR-0.6B"
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"x")
    (d / "config.json").write_text("{}")
    assert find_qwen_asr_model(tmp_path, "Qwen3-ASR-0.6B") == d
    assert find_qwen_asr_model(tmp_path, "0.6B") == d
    assert find_qwen_asr_model(tmp_path, "1.7B") is None
    assert find_qwen_asr_model(tmp_path / "nope", "0.6B") is None


def test_find_qwen_asr_model_17b(tmp_path: Path):
    """Qwen3-ASR-1.7B 目录应能被按全名与版本号命中。"""
    from app.models import find_qwen_asr_model
    d = tmp_path / "Qwen3-ASR-1.7B"
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"x")
    (d / "config.json").write_text("{}")
    assert find_qwen_asr_model(tmp_path, "Qwen3-ASR-1.7B") == d
    assert find_qwen_asr_model(tmp_path, "1.7B") == d
