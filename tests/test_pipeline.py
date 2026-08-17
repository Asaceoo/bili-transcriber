from pathlib import Path

import pytest

import app.converter as converter
from app.downloader import MediaInfo
from app.pipeline import Pipeline
from app.store import Settings, Store
from app.transcriber import Segment

INFO = MediaInfo(
    media_id="BV1pipe_p1", bv="BV1pipe", title="管道测试", uploader="UP",
    duration=10.0, url="https://www.bilibili.com/video/BV1pipe", part=1, total_parts=1,
    playlist_title="合集X",
)


class FakeDownloader:
    def __init__(self):
        self.probe_calls = 0

    def probe(self, url):
        self.probe_calls += 1
        return [INFO]

    def download_audio(self, info, dest_dir, progress=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / "管道测试.m4a"
        p.write_bytes(b"fake")
        info.audio_path = p
        if progress:
            progress(0.5)
            progress(1.0)
        return p


class FakeTranscriber:
    def __init__(self, fail=False):
        self.fail = fail

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
    out_dir = tmp / "out" / "我的视频"
    assert (out_dir / "我的视频.srt").exists()
    assert (out_dir / "我的视频.txt").exists()
    assert (out_dir / "我的视频.md").exists()
    assert video.exists(), "本地上传的原文件不应被删除"
    assert any("完成" in e for e in events)


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
