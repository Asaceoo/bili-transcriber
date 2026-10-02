"""converter 模块的回归测试:PyAV 优先转码、ffmpeg 回退与错误处理。"""

import subprocess
import wave

import pytest

import app.converter as conv


def _make_wav(path, rate=16000, seconds=0.5):
    """生成一个合法的单声道 16bit wav 测试文件。"""
    import numpy as np

    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    data = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return path


def test_to_wav16k_mono_pyav_success(tmp_path):
    """PyAV 应能把任意 wav 转成 16kHz 单声道 wav(不依赖外部 ffmpeg)。"""
    src = _make_wav(tmp_path / "src.wav", rate=44100)
    dst = tmp_path / "out.wav"
    result = conv.to_wav16k_mono(src, dst)
    assert result == dst
    assert dst.exists() and dst.stat().st_size > 0
    # 验证输出确实是 16kHz 单声道
    with wave.open(str(dst), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1


def test_to_wav16k_mono_pyav_ignores_crippled_ffmpeg(monkeypatch, tmp_path):
    """PATH 上 ffmpeg 缺 wav muxer(精简构建)时,PyAV 仍能完成转码。"""
    src = _make_wav(tmp_path / "src.wav", rate=44100)
    dst = tmp_path / "out.wav"

    def _crippled(*_a, **_k):
        # 模拟精简 ffmpeg:无 wav muxer
        return subprocess.CompletedProcess(
            ["ffmpeg", "-muxers"], 0, stdout=" E  mp4             MP4 (MPEG-4 Part 14)\n", stderr=""
        )

    monkeypatch.setattr(conv.subprocess, "run", _crippled)
    result = conv.to_wav16k_mono(src, dst)
    assert result == dst
    with wave.open(str(dst), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1


def test_to_wav16k_mono_missing_source(tmp_path):
    """源文件不存在应给出明确错误。"""
    with pytest.raises(conv.ConverterError, match="源文件不存在"):
        conv.to_wav16k_mono(tmp_path / "nope.wav", tmp_path / "out.wav")


def test_to_wav16k_mono_timeout_raises(monkeypatch, tmp_path):
    """PyAV 失败且 ffmpeg 探测超时,应转为 ConverterError,而非永久阻塞。"""

    def _slow(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1800)

    monkeypatch.setattr(conv.subprocess, "run", _slow)
    src = tmp_path / "src.wav"
    src.write_bytes(b"dummy")
    with pytest.raises(conv.ConverterError):
        conv.to_wav16k_mono(src, tmp_path / "out.wav")


def test_to_wav16k_mono_missing_ffmpeg(monkeypatch, tmp_path):
    """PyAV 失败且 ffmpeg 不存在,应给出明确错误。"""

    def _missing(*_a, **_k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(conv.subprocess, "run", _missing)
    src = tmp_path / "src.wav"
    src.write_bytes(b"dummy")
    with pytest.raises(conv.ConverterError):
        conv.to_wav16k_mono(src, tmp_path / "out.wav")


def test_ffmpeg_capable_detects_wav_muxer(monkeypatch):
    """_ffmpeg_capable 应识别带 wav muxer 的完整 ffmpeg。"""
    conv._ffmpeg_capable_cache.clear()

    def _full(*_a, **_k):
        return subprocess.CompletedProcess(
            ["ffmpeg", "-muxers"], 0,
            stdout=" E  wav             WAV / WAVE (Waveform Audio)\n E  mp4             MP4 (MPEG-4 Part 14)\n",
            stderr="",
        )

    monkeypatch.setattr(conv.subprocess, "run", _full)
    assert conv._ffmpeg_capable("ffmpeg") is True


def test_ffmpeg_capable_rejects_missing_wav(monkeypatch):
    """_ffmpeg_capable 应拒绝缺 wav muxer 的精简 ffmpeg。"""
    conv._ffmpeg_capable_cache.clear()

    def _crippled(*_a, **_k):
        return subprocess.CompletedProcess(
            ["ffmpeg", "-muxers"], 0,
            stdout=" E  mp4             MP4 (MPEG-4 Part 14)\n",
            stderr="",
        )

    monkeypatch.setattr(conv.subprocess, "run", _crippled)
    assert conv._ffmpeg_capable("ffmpeg") is False
