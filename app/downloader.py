"""yt-dlp 封装:解析 B 站 URL(单 P / 多 P / 合集)并下载音频流。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yt_dlp

ProgressCallback = Callable[[float], None]


@dataclass
class MediaInfo:
    """一个待处理的最小单元(单 P 视频或合集中的一个条目)。"""

    media_id: str  # yt-dlp 条目 id,多 P 形如 BV1xx411c7mD_p2,唯一
    bv: str
    title: str
    uploader: str
    duration: float  # 秒
    url: str  # 该条目的网页地址
    part: int = 1  # 第几 P
    total_parts: int = 1
    playlist_title: str = ""  # 合集/多 P 的整体标题(决定输出目录)
    audio_path: Path | None = field(default=None, repr=False)


def _bilibili_id(raw_id: str) -> str:
    """yt-dlp 对多 P 的 id 形如 'BV1xx411c7mD_p2',BV 号取下划线前部分。"""
    return raw_id.split("_")[0]


class DownloadError(RuntimeError):
    pass


class Downloader:
    """串行使用;每个实例内部加锁,便于多线程共享。"""

    def __init__(self, ydl_factory: Callable[[dict], "yt_dlp.YoutubeDL"] | None = None):
        # ydl_factory 仅供测试注入 mock;生产走 yt_dlp.YoutubeDL
        self._ydl_factory = ydl_factory or (lambda opts: yt_dlp.YoutubeDL(opts))
        self._lock = threading.Lock()

    def probe(self, url: str) -> list[MediaInfo]:
        """展开 URL 为待处理条目列表,不下载。"""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "skip_download": True,
            "extract_flat": False,
            # 网络健壮性:解析阶段也设超时,避免僵死连接永久阻塞工作线程
            "socket_timeout": 30,
            "retries": 3,
        }
        with self._lock:
            with self._ydl_factory(opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                except yt_dlp.utils.DownloadError as exc:
                    raise DownloadError(f"解析链接失败: {exc}") from exc
        if info is None:
            raise DownloadError("未获取到视频信息")
        entries = list(info.get("entries") or []) if info.get("_type") == "playlist" else [info]
        # 合集/多 P 展开时个别条目可能拿不到完整信息,过滤掉
        entries = [e for e in entries if e and e.get("id")]
        if not entries:
            raise DownloadError("链接中没有可处理的视频")
        total = len(entries)
        playlist_title = str(info.get("title") or "") if info.get("_type") == "playlist" else ""
        result: list[MediaInfo] = []
        for idx, e in enumerate(entries, start=1):
            raw_id = str(e["id"])
            webpage = e.get("webpage_url") or url
            result.append(
                MediaInfo(
                    media_id=raw_id,
                    bv=_bilibili_id(raw_id),
                    title=e.get("title") or raw_id,
                    uploader=e.get("uploader") or e.get("channel") or "",
                    duration=float(e.get("duration") or 0.0),
                    url=webpage,
                    part=idx,
                    total_parts=total,
                    playlist_title=playlist_title,
                )
            )
        return result

    def download_audio(self, info: MediaInfo, dest_dir: Path, progress: ProgressCallback | None = None) -> Path:
        """下载最佳音频流到 dest_dir,返回文件路径。"""
        dest_dir.mkdir(parents=True, exist_ok=True)

        def hook(d: dict) -> None:
            if progress is None:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                if total:
                    progress(min(done / total, 1.0))

        outtmpl = str(dest_dir / f"{_safe_name(info.title) or info.media_id}.%(ext)s")
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "overwrites": True,
            "progress_hooks": [hook],
            "nopart": True,
            # 网络健壮性:下载设超时与重试,避免僵死连接永久阻塞工作线程
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "retry_sleep": 3,
        }
        with self._lock:
            with self._ydl_factory(opts) as ydl:
                try:
                    ydl.download([info.url])
                except yt_dlp.utils.DownloadError as exc:
                    raise DownloadError(f"下载音频失败: {exc}") from exc
        # outtmpl 用 %(ext)s,下载成功后按标题前缀找回文件
        prefix = _safe_name(info.title) or info.media_id
        candidates = [p for p in dest_dir.iterdir() if p.is_file() and p.stem == prefix]
        audio_exts = {".m4a", ".opus", ".ogg", ".mp3", ".aac", ".webm", ".flac"}
        audio = [p for p in candidates if p.suffix.lower() in audio_exts]
        if not audio:
            raise DownloadError(f"下载完成但未找到音频文件: {dest_dir / prefix}.*")
        path = max(audio, key=lambda p: p.stat().st_size)
        info.audio_path = path
        if progress is not None:
            progress(1.0)
        return path


def _safe_name(name: str) -> str:
    """Windows 文件名非法字符替换,并限长。"""
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad else ch for ch in name).strip().rstrip(".")
    return cleaned[:80]
