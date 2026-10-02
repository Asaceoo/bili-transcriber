"""关键帧提取测试:用 PyAV 合成纯色切换视频,端到端验证场景检测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import keyframes
from app.keyframes import KeyFrame


def _encode_video(path: Path, scenes: list[tuple[str, float]],
                  size: tuple[int, int] = (128, 72), fps: int = 10) -> None:
    """合成纯色场景视频:scenes = [(颜色, 持续秒), ...]。"""
    av = pytest.importorskip("av")
    from PIL import Image

    colors = {"red": (210, 30, 30), "blue": (30, 30, 210)}
    container = av.open(str(path), "w")
    try:
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height = size
        stream.pix_fmt = "yuv420p"
        for color, dur in scenes:
            img = Image.new("RGB", size, colors[color])
            for _ in range(int(dur * fps)):
                frame = av.VideoFrame.from_image(img)
                for packet in stream.encode(frame):
                    container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


def _avg_rgb(image_path: Path) -> tuple[int, int, int]:
    from PIL import Image

    with Image.open(image_path) as im:
        return im.convert("RGB").resize((1, 1)).getpixel((0, 0))


def test_two_scene_video_yields_two_frames(tmp_path):
    pytest.importorskip("av")
    # 场景时长需大于默认最小间隔 5s,否则第二帧被防重规则抑制
    video = tmp_path / "v.mp4"
    _encode_video(video, [("red", 6.0), ("blue", 6.0)])

    frames = keyframes.extract_frames(video, tmp_path / "frames")

    assert len(frames) == 2
    assert all(isinstance(f, KeyFrame) for f in frames)
    # 首帧在开头;第二帧在 6s 切换 + 稳定延迟之后
    assert frames[0].time < 1.0
    assert 6.0 <= frames[1].time <= 7.5
    # 截图内容:第一页偏红,第二页偏蓝
    assert frames[0].image.exists() and frames[1].image.exists()
    r0, g0, b0 = _avg_rgb(frames[0].image)
    r1, g1, b1 = _avg_rgb(frames[1].image)
    assert r0 > b0
    assert b1 > r1


def test_single_scene_yields_one_frame(tmp_path):
    pytest.importorskip("av")
    video = tmp_path / "v.mp4"
    _encode_video(video, [("red", 3.0)])

    frames = keyframes.extract_frames(video, tmp_path / "frames")

    assert len(frames) == 1
    assert frames[0].time < 1.0


def test_rapid_flips_collapsed_by_min_gap(tmp_path):
    """1 秒一切换的快速翻页被最小间隔(默认 5s)合并,只保留首帧。"""
    pytest.importorskip("av")
    video = tmp_path / "v.mp4"
    _encode_video(video, [("red", 1.0), ("blue", 1.0), ("red", 1.0), ("blue", 1.0)])

    frames = keyframes.extract_frames(video, tmp_path / "frames")

    assert len(frames) == 1


def test_missing_file_and_non_video_raise(tmp_path):
    pytest.importorskip("av")
    with pytest.raises(keyframes.KeyframeError):
        keyframes.extract_frames(tmp_path / "nope.mp4", tmp_path / "frames")

    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not a video")
    with pytest.raises(keyframes.KeyframeError):
        keyframes.extract_frames(bad, tmp_path / "frames")


def test_progress_reaches_one(tmp_path):
    pytest.importorskip("av")
    video = tmp_path / "v.mp4"
    _encode_video(video, [("red", 6.0), ("blue", 6.0)])

    seen: list[float] = []
    frames = keyframes.extract_frames(video, tmp_path / "frames", progress=seen.append)

    assert frames
    assert seen and abs(seen[-1] - 1.0) < 1e-6
    assert all(0.0 <= p <= 1.0 for p in seen)


# ---------- 双后端:ffmpeg 优先 / PyAV 回退 ----------

def _no_ffmpeg(monkeypatch):
    from app import ffmpeg_bin
    monkeypatch.setattr(ffmpeg_bin, "resolve_ffmpeg", lambda: None)
    monkeypatch.setattr(ffmpeg_bin, "_checked", True, raising=False)


def test_pyav_backend_used_when_no_ffmpeg(tmp_path, monkeypatch):
    """PATH 与随包版都不可用时走 PyAV 后端,行为一致。"""
    pytest.importorskip("av")
    _no_ffmpeg(monkeypatch)
    video = tmp_path / "v.mp4"
    _encode_video(video, [("red", 6.0), ("blue", 6.0)])

    frames = keyframes.extract_frames(video, tmp_path / "frames")

    assert len(frames) == 2
    assert frames[0].time < 1.0 and 6.0 <= frames[1].time <= 7.5
    r0 = _avg_rgb(frames[0].image)
    r1 = _avg_rgb(frames[1].image)
    assert r0[0] > r0[2] and r1[2] > r1[0]


def test_broken_ffmpeg_falls_back_to_pyav(tmp_path, monkeypatch):
    """ffmpeg 二进制存在但无法启动时,自动回退 PyAV 完成提取。"""
    pytest.importorskip("av")
    from app import ffmpeg_bin
    monkeypatch.setattr(ffmpeg_bin, "resolve_ffmpeg", lambda: str(tmp_path / "missing.exe"))
    monkeypatch.setattr(ffmpeg_bin, "_checked", True, raising=False)
    video = tmp_path / "v.mp4"
    _encode_video(video, [("red", 6.0), ("blue", 6.0)])

    frames = keyframes.extract_frames(video, tmp_path / "frames")

    assert len(frames) == 2


def test_ffmpeg_backend_equivalent_results(tmp_path):
    """默认(ffmpeg 可用)走 ffmpeg 后端:合成视频检出 2 帧,颜色判定正确。"""
    video = tmp_path / "v.mp4"
    _encode_video(video, [("red", 6.0), ("blue", 6.0)])

    frames = keyframes.extract_frames(video, tmp_path / "frames")

    assert len(frames) == 2
    assert frames[0].time < 1.0 and 6.0 <= frames[1].time <= 7.5
    r0 = _avg_rgb(frames[0].image)
    r1 = _avg_rgb(frames[1].image)
    assert r0[0] > r0[2] and r1[2] > r1[0]


# ---------- 纯函数与解析逻辑 ----------

def test_plan_captures_gap_and_cap():
    # 切换 3s(3.4 距首帧不足 5s,跳过)、10s(10.4 保留)、11s(11.4 距 10.4 不足,跳过)
    assert keyframes._plan_captures([3.0, 10.0, 11.0], min_gap=5.0, max_frames=400) == [0.0, 10.4]
    # 上限 2:首帧 + 首个有效切换,其余丢弃
    assert keyframes._plan_captures([10.0, 20.0, 30.0], min_gap=5.0, max_frames=2) == [0.0, 10.4]
    # 无切换:仅首帧
    assert keyframes._plan_captures([], min_gap=5.0, max_frames=400) == [0.0]


def test_ffmpeg_scene_times_parses_and_filters(monkeypatch, tmp_path):
    """流式解析 pts_time/scene_score:低于阈值的不算,进度按播放时间推进。"""
    lines = [
        "[Parsed_metadata_1 @ 0x1] frame:0    pts:0       pts_time:0\n",
        "[Parsed_metadata_1 @ 0x1] lavfi.scene_score=0.000000\n",
        "[Parsed_metadata_1 @ 0x1] frame:30   pts:300      pts_time:3\n",
        "[Parsed_metadata_1 @ 0x1] lavfi.scene_score=0.050000\n",   # 低于阈值 0.11
        "[Parsed_metadata_1 @ 0x1] frame:60   pts:600      pts_time:6\n",
        "[Parsed_metadata_1 @ 0x1] lavfi.scene_score=0.290000\n",   # 命中
        "[Parsed_metadata_1 @ 0x1] frame:90   pts:900      pts_time:9\n",
        "[Parsed_metadata_1 @ 0x1] lavfi.scene_score=0.400000\n",   # 命中
    ]

    class FakeStream:
        def __init__(self, ls):
            self._it = iter(ls)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._it)

        def close(self):
            pass

    class FakeProc:
        stderr = FakeStream(lines)
        killed = False

        def kill(self):
            self.killed = True

        def wait(self):
            return 0

    monkeypatch.setattr(keyframes.subprocess, "Popen", lambda *a, **k: FakeProc())
    seen: list[float] = []
    times = keyframes._ffmpeg_scene_times(tmp_path / "v.mp4", "ffmpeg", 12.0, seen.append)

    assert times == [6.0, 9.0]
    # 进度按 播放时间/总时长×0.9 推进:0s、3s、6s、9s 各回调一次
    assert seen == pytest.approx([0.0, 3 / 12 * 0.9, 6 / 12 * 0.9, 9 / 12 * 0.9])


def test_ffmpeg_scene_times_nonzero_exit_raises(monkeypatch, tmp_path):
    class FakeStream:
        def __init__(self, ls):
            self._it = iter(ls)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._it)

        def close(self):
            pass

    class FakeProc:
        stderr = FakeStream(["[Parsed_metadata_1 @ 0x1] frame:0 pts:0 pts_time:0\n"])

        def kill(self):
            pass

        def wait(self):
            return 1

    monkeypatch.setattr(keyframes.subprocess, "Popen", lambda *a, **k: FakeProc())
    with pytest.raises(keyframes.KeyframeError, match="场景检测失败"):
        keyframes._ffmpeg_scene_times(tmp_path / "v.mp4", "ffmpeg", 0.0, None)
