"""视频关键帧提取:检测画面切换(PPT 翻页)并截取关键帧,用于生成图文讲义。

双后端:优先外部/随包 ffmpeg(多线程 C 级场景检测,长视频显著快于 PyAV 逐帧),
不可用或失败时回退 PyAV(随包内置,不依赖 ffmpeg 二进制)。两后端灵敏度等价
(纯色翻转实测标定:PyAV 灰度 MAD 39 ↔ ffmpeg scene score 0.29,线性换算)。

算法:逐帧与前一帧比较画面差异,超过阈值判定为切换;
截取点 = 首帧 + 每次切换后约 0.4 秒(等翻页动画稳定)。
经验值:说话人头部视频 MAD 通常 < 5,PPT 整屏翻页通常 > 30。
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]

SCENE_THRESHOLD = 15.0         # PyAV 后端:灰度 MAD 阈值(0-255)
FFMPEG_SCENE_THRESHOLD = 0.11  # ffmpeg 后端:scene score 阈值(0-1),与 MAD=15 等价
MIN_GAP = 5.0                  # 两次截帧的最小间隔(秒),防渐变/动画重复截帧
SETTLE_DELAY = 0.4             # 检测到切换后等待动画稳定的延迟(秒)
MAX_FRAMES = 400               # 截帧上限,防闪光类视频刷爆磁盘
PROBE_SIZE = (64, 36)          # PyAV 场景检测用的缩略图尺寸(越小越快,精度足够)
JPEG_QUALITY = 85              # PyAV 后端 JPEG 质量
FFMPEG_JPEG_Q = 2              # ffmpeg 后端 JPEG 质量(-q:v,2≈高质量)

_PTS_TIME_RE = re.compile(r"pts_time:([0-9.]+)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")

# 判断一个文件是否是"含画面的视频"(讲义抽帧的可用源)
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".m4v", ".ts", ".wmv"}


class KeyframeError(RuntimeError):
    pass


@dataclass
class KeyFrame:
    time: float   # 截取时刻(秒)
    image: Path   # JPEG 路径


def extract_frames(
    video: Path,
    frames_dir: Path,
    min_gap: float = MIN_GAP,
    max_frames: int = MAX_FRAMES,
    progress: ProgressCallback | None = None,
) -> list[KeyFrame]:
    """提取关键帧;文件缺失/无视频流抛 KeyframeError。ffmpeg 优先,失败回退 PyAV。"""
    from app.ffmpeg_bin import resolve_ffmpeg
    from app.task_control import CancelledError

    if not video.exists():
        raise KeyframeError(f"视频文件不存在: {video}")
    exe = resolve_ffmpeg()
    if exe is not None:
        try:
            return _extract_ffmpeg(video, frames_dir, exe, min_gap, max_frames, progress)
        except CancelledError:
            raise  # 用户取消必须传播,不能被"回退"吞掉
        except KeyframeError:
            raise  # 已判定的问题(无视频流/截帧失败),回退只会重复同样结论
        except Exception as exc:  # noqa: BLE001 — 二进制缺失/解析异常等回退 PyAV
            logger.warning("ffmpeg 关键帧提取失败(%s),回退 PyAV: %s", exc, video)
    return _extract_pyav(video, frames_dir, min_gap, max_frames, progress)


# ---------------- ffmpeg 后端 ----------------

def _extract_ffmpeg(
    video: Path, frames_dir: Path, exe: str,
    min_gap: float, max_frames: int, progress: ProgressCallback | None,
) -> list[KeyFrame]:
    duration, has_video = _probe_video(video)
    if has_video is False:
        raise KeyframeError(f"未检测到视频流: {video}")
    frames_dir.mkdir(parents=True, exist_ok=True)
    scene_times = _ffmpeg_scene_times(video, exe, duration, progress)
    captures = _plan_captures(scene_times, min_gap, max_frames)
    results: list[KeyFrame] = []
    for i, t in enumerate(captures):
        img = frames_dir / f"slide_{i + 1:03d}.jpg"
        _ffmpeg_grab(video, exe, t, img)
        results.append(KeyFrame(time=t, image=img))
        if progress is not None:
            progress(min(0.9 + 0.1 * (i + 1) / len(captures), 1.0))
    logger.info("关键帧提取完成(ffmpeg):%d 帧(阈值 %.2f / 最小间隔 %.1fs): %s",
                len(results), FFMPEG_SCENE_THRESHOLD, min_gap, video)
    return results


def _ffmpeg_scene_times(
    video: Path, exe: str, duration: float, progress: ProgressCallback | None,
) -> list[float]:
    """检测画面切换时刻。select='gte(scene,0)' 让所有帧通过并计算 scene_score,
    metadata=print 把每帧的 pts_time + 分数打到 stderr,Python 流式解析——
    这样阈值判断在 Python 侧(两后端语义统一),且逐帧回调支撑进度/取消。"""
    cmd = [
        exe, "-nostdin", "-hide_banner",
        "-i", str(video), "-an", "-sn", "-dn",
        "-vf", "select='gte(scene,0)',metadata=print",
        "-f", "null", "-",
    ]
    times: list[float] = []
    cur_time: float | None = None
    last_report = -1.0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            m = _PTS_TIME_RE.search(line)
            if m:
                cur_time = float(m.group(1))
                continue
            m = _SCORE_RE.search(line)
            if m and cur_time is not None:
                if float(m.group(1)) > FFMPEG_SCENE_THRESHOLD:
                    times.append(cur_time)
                if (progress is not None and duration > 0
                        and cur_time - last_report >= max(1.0, duration / 100)):
                    last_report = cur_time
                    progress(min(cur_time / duration * 0.9, 0.9))
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        proc.stderr.close()
    rc = proc.wait()
    if rc != 0:
        raise KeyframeError(f"ffmpeg 场景检测失败(exit {rc}): {video}")
    return times


def _ffmpeg_grab(video: Path, exe: str, t: float, out: Path) -> None:
    """快照指定时刻的帧(-ss 前置快速定位 + 精确解码一帧)。"""
    cmd = [
        exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{t:.3f}", "-i", str(video),
        "-frames:v", "1", "-q:v", str(FFMPEG_JPEG_Q), str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0 or not out.exists():
        raise KeyframeError(f"ffmpeg 截帧失败({t:.2f}s): {proc.stderr.strip()[:300]}")


def _probe_video(video: Path) -> tuple[float, bool | None]:
    """(时长秒, 是否含视频流);探测失败返回 (0.0, None) 交给 ffmpeg 判定。"""
    try:
        import av

        with av.open(str(video)) as c:
            dur = c.duration / 1_000_000.0 if c.duration else 0.0
            return dur, bool(c.streams.video)
    except Exception:  # noqa: BLE001 — 探测失败不阻断,ffmpeg 自会报错
        return 0.0, None


def _plan_captures(scene_times: list[float], min_gap: float, max_frames: int) -> list[float]:
    """把切换时刻规划为截取时刻:首帧必截,每个切换 +SETTLE_DELAY,
    min_gap 去重,max_frames 封顶。纯函数便于单测。"""
    captures = [0.0]
    for t in sorted(scene_times):
        due = t + SETTLE_DELAY
        if due - captures[-1] >= min_gap:
            captures.append(due)
        if len(captures) >= max_frames:
            logger.warning("关键帧数达上限 %d,忽略后续画面切换", max_frames)
            break
    return captures


# ---------------- PyAV 后端(回退) ----------------

def _extract_pyav(
    video: Path, frames_dir: Path,
    min_gap: float, max_frames: int, progress: ProgressCallback | None,
) -> list[KeyFrame]:
    import av

    try:
        container = av.open(str(video))
    except Exception as exc:  # noqa: BLE001 — 打开失败统一转 KeyframeError
        raise KeyframeError(f"无法打开视频: {exc}") from exc
    try:
        if not container.streams.video:
            raise KeyframeError(f"未检测到视频流: {video}")
        stream = container.streams.video[0]
        duration = _stream_duration(container, stream)
        frames_dir.mkdir(parents=True, exist_ok=True)

        results: list[KeyFrame] = []
        pending: list[float] = []   # 已预约、尚未到期的截取时刻
        last_capture = 0.0          # 最近一次(已预约的)截取时刻;首帧必截视作 0
        prev_gray = None            # 上一帧灰度缩略图
        last_report = -1.0

        for frame in container.decode(video=0):
            t = _frame_time(frame, stream)
            if progress is not None and duration > 0 and t - last_report >= max(1.0, duration / 100):
                last_report = t
                progress(min(t / duration, 1.0))

            if not results:
                # 首帧必截(第一页 PPT 出现在开头)
                results.append(_save_frame(frame, t, frames_dir, len(results)))
                last_capture = t
            else:
                # 到期截帧:取到期后的第一帧(最接近期望时刻的完整画面)
                while pending and t >= pending[0]:
                    pending.pop(0)
                    results.append(_save_frame(frame, t, frames_dir, len(results)))
                    last_capture = t
                if len(results) >= max_frames:
                    logger.warning("关键帧数达上限 %d,提前结束检测: %s", max_frames, video)
                    break

            gray = frame.reformat(width=PROBE_SIZE[0], height=PROBE_SIZE[1], format="gray").to_image()
            if prev_gray is not None and len(results) < max_frames:
                due = t + SETTLE_DELAY
                if due - last_capture >= min_gap and _mad(prev_gray, gray) > SCENE_THRESHOLD:
                    pending.append(due)
            prev_gray = gray

        if progress is not None:
            progress(1.0)
        logger.info("关键帧提取完成(PyAV):%d 帧(阈值 %.0f / 最小间隔 %.1fs): %s",
                    len(results), SCENE_THRESHOLD, min_gap, video)
        return results
    finally:
        container.close()


def _save_frame(frame, t: float, frames_dir: Path, index: int) -> KeyFrame:
    path = frames_dir / f"slide_{index + 1:03d}.jpg"
    frame.to_image().save(path, format="JPEG", quality=JPEG_QUALITY)
    return KeyFrame(time=max(t, 0.0), image=path)


def _mad(a, b) -> float:
    """两张同尺寸灰度图的平均绝对差(0-255)。"""
    from PIL import ImageChops, ImageStat

    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def _frame_time(frame, stream) -> float:
    if frame.time is not None:
        return float(frame.time)
    if frame.pts is not None and stream.time_base:
        return float(frame.pts * stream.time_base)
    return 0.0


def _stream_duration(container, stream) -> float:
    try:
        if container.duration:
            return container.duration / 1_000_000.0
        if stream.duration:
            return float(stream.duration * stream.time_base)
    except Exception:  # noqa: BLE001 — 时长仅用于进度显示,失败无所谓
        pass
    return 0.0
