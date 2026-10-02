"""ffmpeg 二进制定位测试:PATH 优先 / 随包静态版回退 / 缓存重置。"""

from __future__ import annotations

import pytest

from app import ffmpeg_bin


@pytest.fixture(autouse=True)
def fresh_cache():
    ffmpeg_bin._reset_cache()
    yield
    ffmpeg_bin._reset_cache()


def test_resolve_prefers_path_ffmpeg(monkeypatch):
    monkeypatch.setattr(ffmpeg_bin.shutil, "which", lambda name: "C:/tools/ffmpeg.exe")
    monkeypatch.setattr(ffmpeg_bin, "bundled_ffmpeg", lambda: "D:/bundled/ffmpeg.exe")
    assert ffmpeg_bin.resolve_ffmpeg() == "C:/tools/ffmpeg.exe"
    assert ffmpeg_bin.using_bundled() is False


def test_resolve_falls_back_to_bundled(monkeypatch):
    monkeypatch.setattr(ffmpeg_bin.shutil, "which", lambda name: None)
    monkeypatch.setattr(ffmpeg_bin, "bundled_ffmpeg", lambda: "D:/bundled/ffmpeg.exe")
    assert ffmpeg_bin.resolve_ffmpeg() == "D:/bundled/ffmpeg.exe"
    assert ffmpeg_bin.using_bundled() is True


def test_resolve_returns_none_when_both_missing(monkeypatch):
    monkeypatch.setattr(ffmpeg_bin.shutil, "which", lambda name: None)
    monkeypatch.setattr(ffmpeg_bin, "bundled_ffmpeg", lambda: None)
    assert ffmpeg_bin.resolve_ffmpeg() is None
    assert ffmpeg_bin.using_bundled() is False


def test_resolve_result_is_cached(monkeypatch):
    calls = []

    def fake_which(name):
        calls.append(name)
        return "C:/x/ffmpeg.exe"

    monkeypatch.setattr(ffmpeg_bin.shutil, "which", fake_which)
    assert ffmpeg_bin.resolve_ffmpeg() == "C:/x/ffmpeg.exe"
    assert ffmpeg_bin.resolve_ffmpeg() == "C:/x/ffmpeg.exe"
    assert len(calls) == 1  # 第二次走缓存


def test_frozen_bundled_ffmpeg_found_in_meipass(monkeypatch, tmp_path):
    """冻结环境:imageio_ffmpeg 定位失败时,直查 _MEIPASS/imageio_ffmpeg/binaries/。"""
    binaries = tmp_path / "imageio_ffmpeg" / "binaries"
    binaries.mkdir(parents=True)
    exe = binaries / "ffmpeg-win-x86_64-v7.1.exe"
    exe.write_bytes(b"MZfake")

    class FakeSys:
        _MEIPASS = str(tmp_path)

    monkeypatch.setattr(ffmpeg_bin.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(ffmpeg_bin.shutil, "which", lambda name: None)
    # imageio_ffmpeg.get_ffmpeg_exe 抛错,模拟冻结下 importlib.resources 失效
    monkeypatch.setattr(ffmpeg_bin, "bundled_ffmpeg", ffmpeg_bin.bundled_ffmpeg)  # no-op 保真
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "imageio_ffmpeg":
            raise ModuleNotFoundError("frozen")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert ffmpeg_bin.bundled_ffmpeg() == str(exe)
    assert ffmpeg_bin.resolve_ffmpeg() == str(exe)


def test_frozen_bundled_ffmpeg_none_without_meipass(monkeypatch):
    monkeypatch.delattr(ffmpeg_bin.sys, "_MEIPASS", raising=False)
    assert ffmpeg_bin._frozen_bundled_ffmpeg() is None
