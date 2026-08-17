"""任务编排:URL 入队 → probe 展开 → 下载 → 转码 → 转写 → 落盘,状态写入 Store。"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from app import converter
from app.downloader import Downloader, DownloadError, MediaInfo, _safe_name
from app.store import Job, Settings, Store
from app.transcriber import Segment, Transcriber
from app import writers

logger = logging.getLogger(__name__)

UpdateCallback = Callable[[Job], None]  # 类型别名:任务更新回调(job) -> None


@dataclass
class Task:
    """队列任务描述符。kind 决定后续处理分支。"""
    kind: str            # 'url' = B站链接;'local' = 本地上传文件
    url: str = ""
    media_id: str = ""   # rerun 时携带,用于把"解析失败"回写到已存在的 job


class Pipeline:
    """单工作线程串行处理;下载/转写不会并发占用 GPU。"""

    def __init__(self, downloader: Downloader, store: Store, settings: Settings,
                 base_dir: Path, on_event=None):
        self.downloader = downloader
        self.store = store
        self.settings = settings
        self.base_dir = base_dir
        self.cache_dir = base_dir / "cache"
        self.uploads_dir = base_dir / "uploads"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event  # (message: str) -> None,工作线程回调
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._transcriber: Transcriber | None = None
        self._model_key: tuple | None = None
        self._stop = threading.Event()
        self._progress_throttle = 0.03  # 进度写库的最小步进

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pipeline-worker", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def submit(self, url: str) -> None:
        self._queue.put(Task(kind="url", url=url.strip()))
        self.start()

    def submit_local(self, path: Path) -> None:
        """上传的本地文件:保存元数据并入队,跳过下载阶段。"""
        path = Path(path)
        if not path.exists():
            self._emit(f"本地文件不存在:{path}")
            return
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        media_id = "local_" + self._local_hash(path)
        duration = converter.media_duration(path)
        job = self.store.get_job(media_id)
        if job and job.status == "done" and job.md_path and Path(job.md_path).exists():
            self._emit(f"已存在,跳过:{job.title}")
            return
        job = job or Job(
            media_id=media_id, bv="", title=path.stem, uploader="本地文件",
            duration=duration, url="", source_type="local",
        )
        job.title, job.duration = path.stem, duration
        job.audio_path = str(path)
        job.status, job.progress, job.error, job.finished_at = "queued", 0.0, "", ""
        self.store.upsert_job(job)
        self._queue.put(Task(kind="local", media_id=media_id))
        self.start()

    @staticmethod
    def _local_hash(path: Path) -> str:
        import hashlib
        h = hashlib.sha1()
        h.update(str(path.resolve()).encode("utf-8"))
        h.update(f"{path.stat().st_size}".encode("utf-8"))
        h.update(f"{path.stat().st_mtime:.3f}".encode("utf-8"))
        return h.hexdigest()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings

    def rerun(self, media_id: str) -> None:
        job = self.store.get_job(media_id)
        if not job:
            return
        job.status = "queued"
        job.progress = 0.0
        job.error = ""
        job.finished_at = ""
        self.store.upsert_job(job)
        if job.source_type == "local":
            self._queue.put(Task(kind="local", media_id=media_id))
            self.start()
        else:
            self._queue.put(Task(kind="url", url=job.url, media_id=media_id))
            self.start()

    # ---------- 内部 ----------

    def _emit(self, message: str) -> None:
        if self.on_event:
            try:
                self.on_event(message)
            except Exception:  # 回调异常不影响任务
                logger.exception("on_event 回调失败")

    def _get_transcriber(self) -> Transcriber:
        key = (self.settings.model_size, self.settings.device,
               self.settings.compute_type, self.settings.language, self.settings.vad)
        if self._transcriber is None or key != self._model_key:
            self._transcriber = Transcriber(
                model_size=self.settings.model_size,
                device=self.settings.device,
                compute_type=self.settings.compute_type,
                language=self.settings.language,
                vad=self.settings.vad,
            )
            self._model_key = key
        return self._transcriber

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                if item.kind == "url":
                    self._process_url(item.url, item.media_id)
                elif item.kind == "local":
                    self._process_local(item.media_id)
            except Exception:
                logger.exception("处理任务失败: %s", item)
                self._emit(f"处理任务失败:{item}")
            finally:
                self._queue.task_done()

    def _process_url(self, url: str, media_id: str = "") -> None:
        self._emit(f"正在解析链接:{url}")
        try:
            entries = self.downloader.probe(url)
        except DownloadError as exc:
            self._emit(f"解析失败:{exc}")
            # 二次转写(rerun)时若链接已失效,需把已存在的任务标记为失败,
            # 否则会一直停在"排队中"且无任何反馈。
            if media_id:
                job = self.store.get_job(media_id)
                if job:
                    job.status, job.error = "failed", str(exc)[:500]
                    job.finished_at = datetime.now().isoformat(timespec="seconds")
                    self.store.upsert_job(job)
            return
        self._emit(f"共 {len(entries)} 个视频待处理")
        for info in entries:
            existing = self.store.get_job(info.media_id)
            if existing and existing.status == "done" and existing.md_path and Path(existing.md_path).exists():
                self._emit(f"已存在,跳过:{existing.title}")
                continue
            job = existing or Job(
                media_id=info.media_id, bv=info.bv, title=info.title,
                uploader=info.uploader, duration=info.duration, url=info.url,
                part=info.part, total_parts=info.total_parts,
            )
            job.title, job.duration = info.title, info.duration  # probe 可能拿到更新信息
            job.status, job.progress, job.error, job.finished_at = "queued", 0.0, "", ""
            self.store.upsert_job(job)
            try:
                self._process_job(job, info)
            except Exception as exc:
                logger.exception("任务失败: %s", job.media_id)
                job.status, job.error = "failed", str(exc)[:500]
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                self.store.upsert_job(job)
                self._emit(f"失败:{job.title}({exc})")

    def _process_local(self, media_id: str) -> None:
        job = self.store.get_job(media_id)
        if not job:
            return
        info = MediaInfo(
            media_id=job.media_id, bv="", title=job.title, uploader=job.uploader,
            duration=job.duration, url="", part=1, total_parts=1,
        )
        try:
            self._process_job(job, info)
        except Exception as exc:
            # 本地任务(源文件缺失/损坏、转码或转写失败)必须落库为 failed,
            # 否则会停在"排队中"且无任何错误反馈。
            logger.exception("本地任务失败: %s", media_id)
            job.status, job.error = "failed", str(exc)[:500]
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            self.store.upsert_job(job)
            self._emit(f"失败:{job.title}({exc})")

    def _job_dir(self, info: MediaInfo, unique_suffix: str = "") -> Path:
        folder = getattr(info, "playlist_title", "") or info.title
        name = _safe_name(folder) if not info.bv else f"{info.bv}_{_safe_name(folder)}"
        if unique_suffix:
            name = f"{name}_{_safe_name(unique_suffix)}"
        d = self.settings.resolve_output_dir(self.base_dir) / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _process_job(self, job: Job, info: MediaInfo) -> None:
        job_dir = self._job_dir(info, unique_suffix=job.media_id if job.source_type == "local" else "")
        wav = self.cache_dir / f"{_safe_name(info.media_id) or info.media_id}.wav"

        # 1. 获取音频源(本地上传文件跳过下载阶段)
        if job.source_type == "local":
            audio = Path(job.audio_path) if job.audio_path and Path(job.audio_path).exists() else None
            if audio is None:
                raise FileNotFoundError(f"本地音频源缺失:{job.audio_path}")
        else:
            self._set_stage(job, "downloading")
            audio = Path(job.audio_path) if job.audio_path and Path(job.audio_path).exists() else None
            if audio is None:
                audio = self.downloader.download_audio(
                    info, job_dir, progress=lambda p: self._set_stage(job, "downloading", p)
                )
            job.audio_path = str(audio)

        # 2. 转码(已有 wav 则复用,支持断点续跑)
        if not wav.exists():
            self._set_stage(job, "converting")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            converter.to_wav16k_mono(audio, wav)

        # 3. 转写
        self._set_stage(job, "transcribing")
        transcriber = self._get_transcriber()
        segments: list[Segment] = transcriber.transcribe(
            wav, duration=job.duration or info.duration,
            progress=lambda p: self._set_stage(job, "transcribing", p),
        )

        # 4. 落盘
        self._set_stage(job, "saving")
        stem = _safe_name(info.title) or info.media_id
        meta = writers.NoteMeta(
            title=info.title, uploader=info.uploader, duration=info.duration,
            bv=info.bv, url=info.url,
            model=f"{self.settings.model_size} ({transcriber.resolved_device})",
        )
        job.srt_path = str(writers.write_srt(segments, job_dir / f"{stem}.srt"))
        job.txt_path = str(writers.write_txt(segments, job_dir / f"{stem}.txt"))
        job.md_path = str(writers.write_md(meta, segments, job_dir / f"{stem}.md"))

        # 中间产物清理：在回收站不可用的受限环境（沙箱）中，删除操作会被
        # safe-delete 拦截并抛 OSError；清理失败不应让已成功转写的任务被判失败。
        try:
            wav.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理中间 WAV 缓存失败（已忽略，文件保留）: %s | %s", wav, exc)
        if not self.settings.keep_audio and job.source_type != "local":
            try:
                audio.unlink(missing_ok=True)
                job.audio_path = ""
            except OSError as exc:
                logger.warning("清理原始音频缓存失败（已忽略，文件保留）: %s | %s", audio, exc)
        # 本地上传文件属于用户资产,始终保持,不在此处删除

        job.status, job.progress = "done", 1.0
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        self.store.upsert_job(job)
        self._emit(f"完成:{job.title}")

    def _set_stage(self, job: Job, status: str, progress: float | None = None) -> None:
        stage_changed = job.status != status
        dirty = stage_changed or (
            progress is not None and abs(progress - job.progress) >= self._progress_throttle
        ) or progress in (0.0, 1.0)
        if not dirty:
            return
        job.status = status
        if progress is not None:
            job.progress = round(progress, 3)
        elif stage_changed:
            job.progress = 0.0
        self.store.upsert_job(job)
