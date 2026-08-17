"""任务编排:URL 入队 → probe 展开 → 下载 → 转码 → 转写 → 落盘,状态写入 Store。"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from pathlib import Path

from app import converter
from app.downloader import Downloader, DownloadError, MediaInfo, _safe_name
from app.store import Job, Settings, Store
from app.transcriber import Segment, Transcriber
from app import writers

logger = logging.getLogger(__name__)

UpdateCallback = lambda job: None  # noqa: E731  仅用于类型说明


class Pipeline:
    """单工作线程串行处理;下载/转写不会并发占用 GPU。"""

    def __init__(self, downloader: Downloader, store: Store, settings: Settings,
                 base_dir: Path, on_event=None):
        self.downloader = downloader
        self.store = store
        self.settings = settings
        self.base_dir = base_dir
        self.cache_dir = base_dir / "cache"
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
        self._queue.put(url.strip())
        self.start()

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
        self.submit(job.url)

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
                self._process_url(item)
            except Exception:
                logger.exception("处理 URL 失败: %s", item)
                self._emit(f"处理链接失败:{item}")
            finally:
                self._queue.task_done()

    def _process_url(self, url: str) -> None:
        self._emit(f"正在解析链接:{url}")
        try:
            entries = self.downloader.probe(url)
        except DownloadError as exc:
            self._emit(f"解析失败:{exc}")
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

    def _job_dir(self, info: MediaInfo) -> Path:
        folder = getattr(info, "playlist_title", "") or info.title
        name = f"{info.bv}_{_safe_name(folder)}" if _safe_name(folder) else info.bv
        d = self.settings.resolve_output_dir(self.base_dir) / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _process_job(self, job: Job, info: MediaInfo) -> None:
        job_dir = self._job_dir(info)
        wav = self.cache_dir / f"{_safe_name(info.media_id) or info.media_id}.wav"

        # 1. 下载音频(已有则复用)
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
            model=f"{self.settings.model_size} ({transcriber._resolve()[0]})",
        )
        job.srt_path = str(writers.write_srt(segments, job_dir / f"{stem}.srt"))
        job.txt_path = str(writers.write_txt(segments, job_dir / f"{stem}.txt"))
        job.md_path = str(writers.write_md(meta, segments, job_dir / f"{stem}.md"))

        wav.unlink(missing_ok=True)  # 中间产物
        if not self.settings.keep_audio:
            audio.unlink(missing_ok=True)
            job.audio_path = ""

        job.status, job.progress = "done", 1.0
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        self.store.upsert_job(job)
        self._emit(f"完成:{job.title}")

    def _set_stage(self, job: Job, status: str, progress: float | None = None) -> None:
        dirty = job.status != status or (
            progress is not None and abs(progress - job.progress) >= self._progress_throttle
        ) or progress in (0.0, 1.0)
        if not dirty:
            return
        job.status = status
        if progress is not None:
            job.progress = round(progress, 3)
        self.store.upsert_job(job)
