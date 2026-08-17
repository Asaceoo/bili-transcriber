"""converter 模块的回归测试:转码超时与缺失 ffmpeg 的错误处理。"""

import subprocess

import pytest

import app.converter as conv


def test_to_wav16k_mono_timeout_raises(monkeypatch, tmp_path):
    """ffmpeg 僵死(超时)应转为 ConverterError,而非永久阻塞。"""
    def _slow(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1800)

    monkeypatch.setattr(conv.subprocess, "run", _slow)
    src = tmp_path / "src.wav"
    src.write_bytes(b"dummy")
    with pytest.raises(conv.ConverterError):
        conv.to_wav16k_mono(src, tmp_path / "out.wav")


def test_to_wav16k_mono_missing_ffmpeg(monkeypatch, tmp_path):
    """ffmpeg 不存在应给出明确错误。"""
    def _missing(*_a, **_k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(conv.subprocess, "run", _missing)
    src = tmp_path / "src.wav"
    src.write_bytes(b"dummy")
    with pytest.raises(conv.ConverterError):
        conv.to_wav16k_mono(src, tmp_path / "out.wav")
