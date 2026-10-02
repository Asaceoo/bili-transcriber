from pathlib import Path

import pytest

import app.converter as converter
from app.downloader import MediaInfo
from app.pipeline import Pipeline
from app.store import Job, Settings, Store
from app.task_control import CancelledError
from app.transcriber import Segment

INFO = MediaInfo(
    media_id="BV1pipe_p1", bv="BV1pipe", title="管道测试", uploader="UP",
    duration=10.0, url="https://www.bilibili.com/video/BV1pipe", part=1, total_parts=1,
    playlist_title="合集X",
)


class FakeDownloader:
    def __init__(self):
        self.probe_calls = 0
        self.audio_ext = ".m4a"   # 模拟下载产物扩展名(.m4a=纯音频,.mp4=含画面)
        self.video_calls = 0

    def probe(self, url):
        self.probe_calls += 1
        return [INFO]

    def download_audio(self, info, dest_dir, progress=None, want_video=False):
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / f"管道测试{self.audio_ext}"
        p.write_bytes(b"fake")
        info.audio_path = p
        if progress:
            progress(0.5)
            progress(1.0)
        return p

    def download_video(self, info, dest_dir, progress=None):
        self.video_calls += 1
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / "管道测试.video.mp4"
        p.write_bytes(b"fake-video")
        if progress:
            progress(1.0)
        return p


class FakeTranscriber:
    def __init__(self, fail=False):
        self.fail = fail
        # 与真实 Transcriber 对齐的公开接口:pipeline 读取 resolved_device 标注任务设备
        self.resolved_device = "cuda"

    def transcribe(self, wav, duration=0.0, progress=None):
        if self.fail:
            raise RuntimeError("GPU 爆炸")
        segs = [Segment(0.0, 1.0, "你好"), Segment(1.0, 2.0, "世界")]
        if progress:
            progress(0.5)
            progress(1.0)
        return segs

    def _resolve(self):
        return ("cuda", "float16")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "db" / "history.db")
    settings = Settings(output_dir="out", keep_audio=False)
    events: list[str] = []
    pipe = Pipeline(downloader=FakeDownloader(), store=store, settings=settings,
                    base_dir=tmp_path, on_event=events.append)
    monkeypatch.setattr(converter, "to_wav16k_mono",
                        lambda src, dst: dst.write_bytes(b"wav") or dst)
    return pipe, store, settings, events, tmp_path


def _fake_transcriber(pipe: Pipeline, **kw):
    pipe._transcriber = FakeTranscriber(**kw)
    pipe._model_key = ("locked",)
    pipe._get_transcriber = lambda: pipe._transcriber


def test_full_flow_produces_outputs(env):
    pipe, store, settings, events, tmp = env
    _fake_transcriber(pipe)

    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "done"
    out_dir = tmp / "out" / "BV1pipe_合集X"
    assert (out_dir / "管道测试.srt").exists()
    assert (out_dir / "管道测试.txt").exists()
    assert (out_dir / "管道测试.md").exists()
    assert (out_dir / "管道测试.m4a").exists() is settings.keep_audio  # False → 不保留
    assert not list((tmp / "cache").glob("*.wav"))  # 中间 wav 已清理
    assert any("完成" in e for e in events)


def test_failure_marks_job_failed(env):
    pipe, store, _, events, _ = env
    _fake_transcriber(pipe, fail=True)

    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "failed" and "GPU 爆炸" in job.error
    assert any("失败" in e for e in events)


# ---------- 图文讲义(可选输出) ----------

def _fake_extract_frames(video, frames_dir, progress=None, **kw):
    from app.keyframes import KeyFrame

    frames_dir.mkdir(parents=True, exist_ok=True)
    kfs = []
    for i, t in enumerate([0.0, 6.0]):
        p = frames_dir / f"slide_{i + 1:03d}.jpg"
        p.write_bytes(b"jpg")
        kfs.append(KeyFrame(time=t, image=p))
    if progress:
        progress(1.0)
    return kfs


def test_notes_generated_when_enabled(env, monkeypatch):
    monkeypatch.setattr("app.pipeline.keyframes.extract_frames", _fake_extract_frames)
    pipe, store, settings, events, tmp = env
    settings.notes = True
    pipe.downloader.audio_ext = ".mp4"  # 开讲义时下载产物为含画面 mp4
    _fake_transcriber(pipe)

    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "done"
    out_dir = tmp / "out" / "BV1pipe_合集X"
    assert job.notes_path.endswith("管道测试.notes.md")
    assert (out_dir / "管道测试.notes.md").exists()
    assert (out_dir / "管道测试_frames" / "slide_001.jpg").exists()
    text = (out_dir / "管道测试.notes.md").read_text(encoding="utf-8")
    assert "![第 1 页](管道测试_frames/slide_001.jpg)" in text
    assert "**[00:00:00]** 你好" in text
    assert pipe.downloader.video_calls == 0  # 复用下载的 mp4,无需补下视频


def test_notes_not_generated_when_disabled(env):
    pipe, store, _, _, tmp = env
    _fake_transcriber(pipe)

    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "done" and job.notes_path == ""
    assert not (tmp / "out" / "BV1pipe_合集X" / "管道测试.notes.md").exists()


def test_notes_failure_does_not_fail_job(env, monkeypatch):
    """讲义是附加产物:关键帧提取失败时任务仍算成功。"""
    def boom(*a, **kw):
        raise RuntimeError("关键帧爆炸")

    monkeypatch.setattr("app.pipeline.keyframes.extract_frames", boom)
    pipe, store, settings, events, tmp = env
    settings.notes = True
    pipe.downloader.audio_ext = ".mp4"
    _fake_transcriber(pipe)

    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "done" and job.notes_path == ""
    assert any("完成" in e for e in events)


def test_notes_legacy_audio_rerun_downloads_temp_video(env, monkeypatch):
    """旧任务重跑(缓存纯音频 m4a):补下临时视频截帧,成功后删除。"""
    monkeypatch.setattr("app.pipeline.keyframes.extract_frames", _fake_extract_frames)
    pipe, store, settings, events, tmp = env
    settings.notes = True
    _fake_transcriber(pipe)

    pipe._process_url("https://x")  # audio_ext 默认 .m4a,模拟旧缓存

    job = store.get_job("BV1pipe_p1")
    assert job.status == "done"
    out_dir = tmp / "out" / "BV1pipe_合集X"
    assert (out_dir / "管道测试.notes.md").exists()
    assert pipe.downloader.video_calls == 1
    assert not (out_dir / "管道测试.video.mp4").exists()  # 临时视频已清理


def test_notes_cancel_during_extraction_not_swallowed(env, monkeypatch):
    """截帧阶段取消必须向上传播(不能被"讲义失败不致命"吞掉)。"""
    def boom(*a, **kw):
        raise CancelledError()

    monkeypatch.setattr("app.pipeline.keyframes.extract_frames", boom)
    pipe, store, settings, events, tmp = env
    settings.notes = True
    pipe.downloader.audio_ext = ".mp4"
    _fake_transcriber(pipe)

    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "cancelled"


def test_notes_local_upload_uses_original_file(env, monkeypatch):
    """本地上传视频:直接用 uploads 原文件抽帧,不触发任何下载。"""
    monkeypatch.setattr("app.pipeline.keyframes.extract_frames", _fake_extract_frames)
    pipe, store, settings, events, tmp = env
    settings.notes = True
    src = tmp / "uploads" / "lecture.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"fake-mp4")
    job = Job(media_id="local_test", bv="", title="lecture", uploader="本地文件",
              duration=0.0, url="", source_type="local", audio_path=str(src))
    store.upsert_job(job)
    _fake_transcriber(pipe)

    pipe._process_local("local_test")

    got = store.get_job("local_test")
    assert got.status == "done"
    assert got.notes_path.endswith("lecture.notes.md")
    assert pipe.downloader.video_calls == 0
    assert src.exists()  # 上传资产永不删除


def test_done_job_skipped_on_resubmit(env):
    pipe, store, _, events, tmp = env
    _fake_transcriber(pipe)
    pipe._process_url("https://x")
    probe_before = pipe.downloader.probe_calls

    pipe._process_url("https://x")  # 再来一次

    assert pipe.downloader.probe_calls == probe_before + 1  # probe 总会执行
    assert any("跳过" in e for e in events)


def test_rerun_resets_and_reprocesses(env):
    pipe, store, _, _, tmp = env
    _fake_transcriber(pipe)
    pipe._process_url("https://x")

    md = Path(store.get_job("BV1pipe_p1").md_path)
    md.unlink()  # 破坏产物
    pipe.rerun("BV1pipe_p1")
    pipe._process_url("https://www.bilibili.com/video/BV1pipe")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "done" and md.exists()


def test_wav_cache_reused_between_runs(env, monkeypatch):
    pipe, store, _, _, tmp = env
    _fake_transcriber(pipe)
    convert_calls: list[Path] = []
    real_convert = converter.to_wav16k_mono
    monkeypatch.setattr(converter, "to_wav16k_mono",
                        lambda src, dst: convert_calls.append(dst) or real_convert(src, dst))
    pipe._process_url("https://x")

    # 模拟失败后重跑:wav 缓存还在时不应再次转码
    job = store.get_job("BV1pipe_p1")
    job.status, job.md_path = "failed", ""
    store.upsert_job(job)
    (tmp / "cache" / "BV1pipe_p1.wav").write_bytes(b"cached-wav")
    pipe._process_url("https://x")
    assert len(convert_calls) == 1  # 第二轮没有重新转码


def test_safe_delete_failure_keeps_job_done(env, monkeypatch):
    """回归(safe-delete 拦截):缓存删除被环境拒绝抛 OSError 时,
    已成功转写的任务仍应标记 done,而非被误判 failed。"""
    pipe, store, settings, events, tmp = env
    _fake_transcriber(pipe)

    # 模拟沙箱 safe-delete:所有 unlink 抛 PermissionError(OSError 子类)
    def _blocked_unlink(self, missing_ok=False):
        raise PermissionError("[SAFE_DELETE_FAIL_CLOSED] recycle bin unavailable")
    monkeypatch.setattr(Path, "unlink", _blocked_unlink)

    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "done", f"删除被拦截不应判失败,实际: {job.error}"
    assert not job.error  # 删除失败已降级为 warning,error 字段应为空
    out_dir = tmp / "out" / "BV1pipe_合集X"
    assert (out_dir / "管道测试.md").exists()
    assert (out_dir / "管道测试.srt").exists()
    # 缓存因删除被拦而保留(预期行为)
    assert list((tmp / "cache").glob("*.wav"))


def test_local_file_processing_skips_download_and_preserves_source(env, monkeypatch):
    """本地上传文件:跳过下载阶段、下载器不被调用、产物生成、原文件保留。"""
    pipe, store, settings, events, tmp = env
    _fake_transcriber(pipe)
    monkeypatch.setattr(pipe, "start", lambda: None)  # 同步验证,不启动后台线程

    video = pipe.uploads_dir / "我的视频.mp4"
    video.write_bytes(b"fake-video-bytes")
    media_id = "local_" + pipe._local_hash(video)

    pipe.submit_local(video)       # 入队 + 创建 job(下载器未参与)
    pipe._process_local(media_id)  # 同步处理

    job = store.get_job(media_id)
    assert job is not None
    assert job.source_type == "local"
    assert job.uploader == "本地文件"
    assert job.status == "done", job.error
    assert pipe.downloader.probe_calls == 0  # 本地通道不调用下载器
    out_dir = tmp / "out" / f"我的视频_{media_id}"
    assert (out_dir / "我的视频.srt").exists()
    assert (out_dir / "我的视频.txt").exists()
    assert (out_dir / "我的视频.md").exists()
    assert video.exists(), "本地上传的原文件不应被删除"
    assert any("完成" in e for e in events)


def test_local_failure_marks_job_failed(env, monkeypatch):
    """本地任务处理失败(源文件损坏/缺失/转写异常)必须落库为 failed,而非停在排队中。"""
    pipe, store, settings, events, tmp = env
    _fake_transcriber(pipe, fail=True)
    monkeypatch.setattr(pipe, "start", lambda: None)

    video = pipe.uploads_dir / "broken.mp4"
    video.write_bytes(b"not-a-real-video")
    media_id = "local_" + pipe._local_hash(video)
    pipe.submit_local(video)
    pipe._process_local(media_id)

    job = store.get_job(media_id)
    assert job.status == "failed", f"本地失败任务应标记 failed,实际: {job.status}"
    assert "GPU 爆炸" in job.error
    assert any("失败" in e for e in events)


def test_url_rerun_of_dead_link_marks_failed(env, monkeypatch):
    """rerun 已存在任务时若链接失效(probe 抛错),应把该任务标记 failed。"""
    pipe, store, _, events, tmp = env
    _fake_transcriber(pipe)

    class DeadDownloader:
        def probe(self, url):
            from app.downloader import DownloadError
            raise DownloadError("视频不存在或已下架")
        def download_audio(self, *a, **k):
            raise AssertionError("不应走到下载阶段")

    pipe.downloader = DeadDownloader()
    monkeypatch.setattr(pipe, "start", lambda: None)

    job = Job(media_id="BVdead", bv="BVdead", title="消失的视频", uploader="UP",
              duration=1.0, url="https://x")
    store.upsert_job(job)
    pipe._process_url("https://x", "BVdead")  # 模拟 rerun 带 media_id 的解析失败分支

    j = store.get_job("BVdead")
    assert j.status == "failed", f"失效链接 rerun 应标记 failed,实际: {j.status}"
    assert "视频不存在" in j.error



def test_unsupported_engine_raises_clear_error(env):
    """未接入的引擎应报清晰错误,而非静默用 whisper 加载。"""
    pipe, store, settings, _, _ = env
    settings.engine = "bogus-engine"
    with pytest.raises(ValueError, match="尚未接入"):
        pipe._get_transcriber()


def test_qwen_engine_returns_qwen_transcriber(env):
    """qwen3-asr 引擎应返回 QwenASRTranscriber 实例(懒加载,不真正加载模型)。"""
    pipe, store, settings, _, _ = env
    settings.engine = "qwen3-asr"
    t = pipe._get_transcriber()
    from app.qwen_transcriber import QwenASRTranscriber
    assert isinstance(t, QwenASRTranscriber)
    assert not t.loaded  # 尚未真正加载


def test_sensevoice_engine_returns_sensevoice_transcriber(env):
    """sensevoice 引擎应返回 SenseVoiceTranscriber 实例(懒加载,不真正加载模型)。"""
    pipe, store, settings, _, _ = env
    settings.engine = "sensevoice"
    t = pipe._get_transcriber()
    from app.sensevoice_transcriber import SenseVoiceTranscriber
    assert isinstance(t, SenseVoiceTranscriber)
    assert not t.loaded  # 尚未真正加载


def test_local_file_resubmit_skips_when_done(env, monkeypatch):
    """已完成的本地任务再次上传应跳过,不重复处理。"""
    pipe, store, _, events, tmp = env
    _fake_transcriber(pipe)
    monkeypatch.setattr(pipe, "start", lambda: None)

    video = pipe.uploads_dir / "clip.mp4"
    video.write_bytes(b"x")
    media_id = "local_" + pipe._local_hash(video)
    pipe.submit_local(video)
    pipe._process_local(media_id)
    events.clear()
    pipe.submit_local(video)  # 已完成应跳过
    assert any("跳过" in e for e in events)


def test_multipart_same_title_outputs_not_overwritten(env):
    """回归:多P标题相同时,各P输出文件名必须带 _P{part} 后缀,避免互相覆盖。"""
    pipe, store, _, _, tmp = env
    _fake_transcriber(pipe)

    multi = [
        MediaInfo(media_id="BV1m_p1", bv="BV1m", title="同名标题", uploader="UP",
                  duration=10.0, url="https://x", part=1, total_parts=2, playlist_title="合集M"),
        MediaInfo(media_id="BV1m_p2", bv="BV1m", title="同名标题", uploader="UP",
                  duration=10.0, url="https://x", part=2, total_parts=2, playlist_title="合集M"),
    ]

    class MultiDownloader(FakeDownloader):
        def probe(self, url):
            return multi

        def download_audio(self, info, dest_dir, progress=None, want_video=False):
            dest_dir.mkdir(parents=True, exist_ok=True)
            p = dest_dir / f"{info.media_id}.m4a"
            p.write_bytes(b"fake")
            info.audio_path = p
            if progress:
                progress(1.0)
            return p

    pipe.downloader = MultiDownloader()
    pipe._process_url("https://x")

    out_dir = tmp / "out" / "BV1m_合集M"
    assert (out_dir / "同名标题_P01.srt").exists()
    assert (out_dir / "同名标题_P02.srt").exists()
    assert (out_dir / "同名标题_P01.txt").exists()
    assert (out_dir / "同名标题_P02.txt").exists()
    assert (out_dir / "同名标题_P01.md").exists()
    assert (out_dir / "同名标题_P02.md").exists()


# ---------- 暂停 / 取消 / 卡顿检测 ----------

def test_cancel_before_process_marks_cancelled(env):
    """任务在排队中被取消:处理时应标记 cancelled 且不产出文件。"""
    pipe, store, _, events, tmp = env
    _fake_transcriber(pipe)

    pipe.cancel("BV1pipe_p1")
    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "cancelled"
    assert job.error == "已取消"
    assert any("已取消" in e for e in events)
    assert not (tmp / "out" / "BV1pipe_合集X" / "管道测试.md").exists()


def test_pause_blocks_until_resume(env):
    """任务暂停时处理线程应阻塞;恢复后继续完成。"""
    import threading
    import time

    pipe, store, _, _, tmp = env
    _fake_transcriber(pipe)

    pipe.pause("BV1pipe_p1")
    result: list[str] = []

    def work():
        pipe._process_url("https://x")
        result.append("done")

    t = threading.Thread(target=work)
    t.start()
    time.sleep(0.2)
    assert not result  # 暂停中应阻塞,未完成
    assert store.get_job("BV1pipe_p1").status == "paused"

    pipe.resume("BV1pipe_p1")
    t.join(timeout=3)
    assert result == ["done"]
    assert store.get_job("BV1pipe_p1").status == "done"


def test_pause_restores_previous_stage_on_resume(env):
    """恢复时应还原暂停前的阶段(如 transcribing),而非固定值。"""
    pipe, store, _, _, tmp = env
    _fake_transcriber(pipe)
    job = Job(media_id="BV1pipe_p1", bv="BV1pipe", title="管道测试", uploader="UP",
              duration=10.0, url="https://x", status="transcribing")
    store.upsert_job(job)

    pipe.pause("BV1pipe_p1")
    assert store.get_job("BV1pipe_p1").status == "paused"
    pipe.resume("BV1pipe_p1")
    assert store.get_job("BV1pipe_p1").status == "transcribing"


def test_checked_progress_raises_on_cancel(env):
    """进度回调在任务取消后应抛 CancelledError。"""
    pipe, store, _, _, tmp = env
    job = store.get_job("BV1pipe_p1") or Job(
        media_id="BV1pipe_p1", bv="BV1pipe", title="管道测试", uploader="UP",
        duration=10.0, url="https://x",
    )
    pipe.cancel("BV1pipe_p1")
    with pytest.raises(CancelledError):
        pipe._checked_progress(job, "transcribing", 0.5)


def test_cancel_mid_transcription_marks_cancelled(env):
    """转写过程中取消:任务标记 cancelled,不误判 failed。"""
    pipe, store, _, events, tmp = env

    class CancelOnProgressTranscriber(FakeTranscriber):
        def transcribe(self, wav, duration=0.0, progress=None):
            if progress:
                progress(0.3)  # 第一次进度后触发取消
                pipe.cancel("BV1pipe_p1")
                progress(0.6)  # 第二次进度应抛 CancelledError
            return [Segment(0.0, 1.0, "你好")]

    pipe._transcriber = CancelOnProgressTranscriber()
    pipe._model_key = ("locked",)
    pipe._get_transcriber = lambda: pipe._transcriber

    pipe._process_url("https://x")

    job = store.get_job("BV1pipe_p1")
    assert job.status == "cancelled"
    assert job.error == "已取消"
    assert any("已取消" in e for e in events)


def test_stalled_detection(env):
    """长时间无进度更新的任务应被 stalled() 判定为卡顿。"""
    import time

    pipe, store, _, _, tmp = env
    _fake_transcriber(pipe)
    pipe._process_url("https://x")
    job = store.get_job("BV1pipe_p1")
    job.status = "transcribing"
    store.upsert_job(job)

    # 手动把最近活动时间拨到很久以前
    pipe._last_activity["BV1pipe_p1"] = time.monotonic() - 999
    assert pipe.stalled("BV1pipe_p1", threshold=60.0) is True

    # 更新活动时间后不再卡顿
    pipe._last_activity["BV1pipe_p1"] = time.monotonic()
    assert pipe.stalled("BV1pipe_p1", threshold=60.0) is False

    # 非活动状态/未知任务不算卡顿
    job.status = "done"
    store.upsert_job(job)
    assert pipe.stalled("BV1pipe_p1", threshold=60.0) is False
    assert pipe.stalled("no_such_job", threshold=60.0) is False


def test_start_recovers_paused_jobs_from_previous_session(env, monkeypatch):
    """上次会话遗留的 paused 任务:start() 应恢复为排队并重新入队。"""
    pipe, store, _, _, tmp = env
    job = Job(media_id="BVold", bv="BVold", title="旧任务", uploader="UP",
              duration=1.0, url="https://x", status="paused")
    store.upsert_job(job)
    monkeypatch.setattr(pipe, "_run", lambda: None)  # 不真正消费队列

    pipe.start()

    assert store.get_job("BVold").status == "queued"
    assert pipe.pending_count == 1
    item = pipe._queue.get_nowait()
    assert item.kind == "url" and item.media_id == "BVold"
    pipe.shutdown()
