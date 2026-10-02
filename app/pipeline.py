"""任务编排:URL 入队 → probe 展开 → 下载 → 转码 → 转写 → 落盘,状态写入 Store。"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from app import converter
from app.downloader import Downloader, DownloadError, MediaInfo, _safe_name, output_stem
from app import keyframes
from app.store import Job, Settings, Store
from app.task_control import CancelledError, TaskControl
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
    """多工作线程并发处理:并行下载/转码/落盘,转写真核串行(共享锁)。

    并发度 WORKERS 个线程同时从队列取任务,提升吞吐(一个任务在下载/转码时,
    另一个可并行转写)。转写因共享同一 transcriber(内部持锁)仍串行,
    避免多模型并发抢占 GPU/内存导致 OOM。
    """

    WORKERS = 2  # 并发工作线程数

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
        self._workers: list[threading.Thread] = []
        self._transcriber: Transcriber | None = None
        self._model_key: tuple | None = None
        self._model_lock = threading.Lock()  # 保护 _transcriber 的懒加载/替换
        self._stop = threading.Event()
        self._progress_throttle = 0.03  # 进度写库的最小步进
        self._control = TaskControl()
        self._paused_prev: dict[str, str] = {}  # 暂停前阶段,恢复时还原
        self._last_activity: dict[str, float] = {}  # 各任务最近一次进度更新时间(卡顿检测)
        self._recovered_paused = False
        # 启动即同步设置(含 Cookie 配置:抖音/部分 YouTube 视频必需)
        self.apply_settings(settings)

    # ---------- 生命周期 ----------

    def start(self) -> None:
        alive = [t for t in self._workers if t.is_alive()]
        if alive:
            return
        self._stop.clear()
        if not self._recovered_paused:
            # 上次会话遗留的"已暂停"任务:恢复为排队并重新入队(控制状态不跨进程持久化)
            self._recovered_paused = True
            for job in self.store.list_jobs():
                if job.status == "paused":
                    job.status, job.progress, job.error = "queued", 0.0, ""
                    self.store.upsert_job(job)
                    self._queue.put(Task(
                        kind="local" if job.source_type == "local" else "url",
                        url=job.url, media_id=job.media_id,
                    ))
        self._workers = [
            threading.Thread(target=self._run, name=f"pipeline-worker-{i}",
                             daemon=True)
            for i in range(self.WORKERS)
        ]
        for t in self._workers:
            t.start()

    @property
    def alive_workers(self) -> int:
        return sum(1 for t in self._workers if t.is_alive())

    def shutdown(self) -> None:
        self._stop.set()
        # 每个工作线程各投一个哨兵,保证全部线程都退出
        for _ in range(len(self._workers)):
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
        """真实待处理数 = 数据库中 queued 状态的任务数。

        不能用队列 qsize():取消的任务仍留在队列里等 worker 消费(计数虚高、
        永不归零);URL 任务 probe 展开前也只算 1 项(展开后才是真实任务数)。
        """
        return self.store.count_jobs("queued")

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        # Cookie/代理配置同步给下载器(getattr 防御:测试可能注入无 configure 的替身)
        configure = getattr(self.downloader, "configure", None)
        if callable(configure):
            configure(
                cookies_from_browser=getattr(settings, "cookies_from_browser", ""),
                cookie_file=getattr(settings, "cookie_file", ""),
                proxy=getattr(settings, "proxy", ""),
            )

    # ---------- 任务控制(暂停/恢复/取消) ----------

    def pause(self, media_id: str) -> None:
        # 先设置控制状态(即使任务尚未落库,入队后也会立即生效)
        self._control.pause(media_id)
        job = self.store.get_job(media_id)
        if not job or job.status in ("done", "failed", "cancelled", "paused"):
            return
        self._paused_prev[media_id] = job.status
        job.status = "paused"
        self.store.upsert_job(job)
        self._emit(f"已暂停:{job.title}")

    def resume(self, media_id: str) -> None:
        job = self.store.get_job(media_id)
        if not job:
            return
        self._control.resume(media_id)
        if job.status == "paused":
            job.status = self._paused_prev.pop(media_id, "transcribing")
            self.store.upsert_job(job)
        self._emit(f"已恢复:{job.title}")

    def cancel(self, media_id: str) -> None:
        # 先设置控制状态(即使任务尚未落库,入队后也会立即生效)
        self._control.cancel(media_id)
        self._paused_prev.pop(media_id, None)
        job = self.store.get_job(media_id)
        if not job:
            return
        if job.status not in ("done", "failed", "cancelled"):
            job.status, job.error = "cancelled", "已取消"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            self.store.upsert_job(job)
        self._emit(f"已取消:{job.title}")

    def stalled(self, media_id: str, threshold: float = 60.0) -> bool:
        """任务是否长时间无进度更新(可能卡住/仍在加载模型)。"""
        job = self.store.get_job(media_id)
        if not job or job.status not in ("transcribing", "downloading", "converting"):
            return False
        last = self._last_activity.get(media_id)
        if last is None:
            return False
        return (time.monotonic() - last) > threshold

    def rerun(self, media_id: str) -> None:
        job = self.store.get_job(media_id)
        if not job:
            return
        self._control.clear(media_id)  # 清除历史取消/暂停状态,允许重新执行
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
        # 进度消息同时写入文件日志(运行日记页签可见),便于排查问题
        logger.info("%s", message)
        if self.on_event:
            try:
                self.on_event(message)
            except Exception:  # 回调异常不影响任务
                logger.exception("on_event 回调失败")

    def _get_transcriber(self):
        engine = self.settings.engine or "whisper"
        if engine == "qwen3-asr":
            key = (engine, self.settings.model_size, self.settings.device,
                   self.settings.compute_type, self.settings.language,
                   self.settings.model_dir)
            with self._model_lock:
                if self._transcriber is None or key != self._model_key:
                    from app.qwen_transcriber import QwenASRTranscriber
                    self._transcriber = QwenASRTranscriber(
                        model_size=self.settings.model_size,
                        device=self.settings.device,
                        compute_type=self.settings.compute_type,
                        language=self.settings.language,
                        vad=self.settings.vad,
                        model_dir=self.settings.model_dir,
                        on_event=self._emit,
                    )
                    self._model_key = key
            return self._transcriber
        if engine == "sensevoice":
            key = (engine, self.settings.model_size, self.settings.language,
                   self.settings.model_dir)
            with self._model_lock:
                if self._transcriber is None or key != self._model_key:
                    from app.sensevoice_transcriber import SenseVoiceTranscriber
                    self._transcriber = SenseVoiceTranscriber(
                        model_size=self.settings.model_size,
                        device=self.settings.device,
                        compute_type=self.settings.compute_type,
                        language=self.settings.language,
                        vad=self.settings.vad,
                        model_dir=self.settings.model_dir,
                        on_event=self._emit,
                    )
                    self._model_key = key
            return self._transcriber
        if engine != "whisper":
            raise ValueError(
                f"识别引擎 {engine!r} 尚未接入(当前支持 whisper / qwen3-asr / sensevoice);"
                f"请先在设置中改回"
            )
        key = (engine, self.settings.model_size, self.settings.device,
               self.settings.compute_type, self.settings.language, self.settings.vad,
               self.settings.model_dir)
        with self._model_lock:
            if self._transcriber is None or key != self._model_key:
                self._transcriber = Transcriber(
                    model_size=self.settings.model_size,
                    device=self.settings.device,
                    compute_type=self.settings.compute_type,
                    language=self.settings.language,
                    vad=self.settings.vad,
                    model_dir=self.settings.model_dir,
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
            # 终态保护:已取消的任务不得被复活为 queued(此前会先覆盖成 queued 再被
            # wait_if_paused 的 CancelledError 改回 cancelled,UI 抓拍到"排队中"闪烁)。
            if self._control.check(job.media_id) == "cancelled":
                job.status, job.error = "cancelled", "已取消"
                if not job.finished_at:
                    job.finished_at = datetime.now().isoformat(timespec="seconds")
                self.store.upsert_job(job)
                self._control.clear(job.media_id)
                self._emit(f"已取消:{job.title}")
                continue
            # 排队中被暂停的任务:状态显示为已暂停(worker 会在 wait_if_paused 阻塞)
            ctrl = self._control.check(job.media_id)
            job.status = "paused" if ctrl == "paused" else "queued"
            job.progress, job.error, job.finished_at = 0.0, "", ""
            self.store.upsert_job(job)
            try:
                # 排队中被暂停/取消的任务:在此等待或中止
                self._control.wait_if_paused(job.media_id)
                self._process_job(job, info)
            except CancelledError:
                job.status, job.error = "cancelled", "已取消"
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                self.store.upsert_job(job)
                self._control.clear(job.media_id)
                self._emit(f"已取消:{job.title}")
            except Exception as exc:
                logger.exception("任务失败: %s", job.media_id)
                job.status, job.error = "failed", str(exc)[:500]
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                self.store.upsert_job(job)
                self._control.clear(job.media_id)
                self._emit(f"失败:{job.title}({exc})")

    def _process_local(self, media_id: str) -> None:
        job = self.store.get_job(media_id)
        if not job:
            return
        # 终态保护:排队中被取消的任务,worker 消费到时直接落 cancelled 并跳过,
        # 不进入转码/转写流程(wait_if_paused 也能拦截,这里早退少做无效工作)。
        if self._control.check(media_id) == "cancelled":
            job.status, job.error = "cancelled", "已取消"
            if not job.finished_at:
                job.finished_at = datetime.now().isoformat(timespec="seconds")
            self.store.upsert_job(job)
            self._control.clear(media_id)
            self._emit(f"已取消:{job.title}")
            return
        info = MediaInfo(
            media_id=job.media_id, bv="", title=job.title, uploader=job.uploader,
            duration=job.duration, url="", part=1, total_parts=1,
        )
        # 排队中被暂停的任务:状态显示为已暂停(worker 会在 wait_if_paused 阻塞)
        if self._control.check(media_id) == "paused":
            job.status = "paused"
            self.store.upsert_job(job)
        try:
            # 排队中被暂停/取消的任务:在此等待或中止
            self._control.wait_if_paused(media_id)
            self._process_job(job, info)
        except CancelledError:
            job.status, job.error = "cancelled", "已取消"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            self.store.upsert_job(job)
            self._control.clear(media_id)
            self._emit(f"已取消:{job.title}")
        except Exception as exc:
            # 本地任务(源文件缺失/损坏、转码或转写失败)必须落库为 failed,
            # 否则会停在"排队中"且无任何错误反馈。
            logger.exception("本地任务失败: %s", media_id)
            job.status, job.error = "failed", str(exc)[:500]
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            self.store.upsert_job(job)
            self._control.clear(media_id)
            self._emit(f"失败:{job.title}({exc})")

    def _job_dir(self, info: MediaInfo, unique_suffix: str = "") -> Path:
        folder = getattr(info, "playlist_title", "") or info.title
        name = _safe_name(folder) if not info.bv else f"{info.bv}_{_safe_name(folder)}"
        if unique_suffix:
            name = f"{name}_{_safe_name(unique_suffix)}"
        d = self.settings.resolve_output_dir(self.base_dir) / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _checked_progress(self, job: Job, status: str, p: float) -> None:
        """进度回调包装:先检查暂停/取消,再写进度。取消时抛 CancelledError。"""
        self._control.wait_if_paused(job.media_id)
        self._set_stage(job, status, p)

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
                # 讲义开启时直接下载含画面的格式(音频转码阶段从视频抽取),
                # 避免音频 + 视频同一内容下载两遍
                audio = self.downloader.download_audio(
                    info, job_dir,
                    progress=lambda p: self._checked_progress(job, "downloading", p),
                    want_video=self.settings.notes,
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
            progress=lambda p: self._checked_progress(job, "transcribing", p),
        )

        # 4. 落盘
        self._set_stage(job, "saving")
        stem = output_stem(info)
        meta = writers.NoteMeta(
            title=info.title, uploader=info.uploader, duration=info.duration,
            bv=info.bv, url=info.url,
            model=f"{self.settings.model_size} ({transcriber.resolved_device})",
        )
        job.srt_path = str(writers.write_srt(segments, job_dir / f"{stem}.srt"))
        job.txt_path = str(writers.write_txt(segments, job_dir / f"{stem}.txt"))
        job.md_path = str(writers.write_md(meta, segments, job_dir / f"{stem}.md"))

        # 5. 图文讲义(可选):关键帧截图 + 字幕对齐;失败只记日志,不影响任务成败
        self._generate_notes(job, info, job_dir, stem, meta, segments)

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
        self._control.clear(job.media_id)
        self._last_activity.pop(job.media_id, None)
        self._emit(f"完成:{job.title}")

    def _video_source(self, job: Job, info: MediaInfo, job_dir: Path) -> tuple[Path | None, Path | None]:
        """取讲义用视频源,返回 (视频路径, 临时视频路径)。

        优先复用已有文件(本地上传原文件 / 开讲义后下载的合一 mp4 / 快手 mp4);
        仅旧任务重跑(纯音频已缓存)才补下临时视频,用完由调用方删除。
        本地上传与合一 mp4 属于用户可见产物,永不在此删除。
        """
        audio = Path(job.audio_path) if job.audio_path else None
        if audio is not None and audio.exists() and audio.suffix.lower() in keyframes.VIDEO_EXTS:
            return audio, None
        if job.source_type == "local":
            return None, None  # 本地纯音频文件,无画面可用
        self._set_stage(job, "downloading")
        video = self.downloader.download_video(
            info, job_dir,
            progress=lambda p: self._checked_progress(job, "downloading", p),
        )
        return video, video

    def _generate_notes(self, job: Job, info: MediaInfo, job_dir: Path, stem: str,
                        meta: writers.NoteMeta, segments: list[Segment]) -> None:
        if not self.settings.notes:
            return
        video_temp: Path | None = None
        try:
            video, video_temp = self._video_source(job, info, job_dir)
            if video is None:
                logger.info("无可用视频源(纯音频文件),跳过图文讲义: %s", job.title)
                return
            frames = keyframes.extract_frames(
                video, job_dir / f"{stem}_frames",
                progress=lambda p: self._checked_progress(job, "extracting", p),
            )
            if not frames:
                logger.info("未检测到画面切换,跳过图文讲义: %s", job.title)
                return
            sections = writers.build_slide_sections(
                segments, [(f.time, f.image) for f in frames], job_dir,
            )
            job.notes_path = str(writers.write_notes_md(
                meta, sections, job_dir / f"{stem}.notes.md",
            ))
            logger.info("图文讲义完成:%d 页 → %s", len(frames), job.notes_path)
        except CancelledError:
            raise  # 用户取消不能被"讲义失败不致命"吞掉
        except Exception as exc:  # noqa: BLE001 — 讲义是附加产物,失败不拖垮转写
            logger.warning("图文讲义生成失败(转写结果不受影响): %s | %s", job.title, exc)
        finally:
            if video_temp is not None:
                try:
                    video_temp.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("清理临时视频失败(已忽略,文件保留): %s | %s", video_temp, exc)

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
        self._last_activity[job.media_id] = time.monotonic()
