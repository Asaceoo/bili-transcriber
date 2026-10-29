"""无界面服务层(CLI/MCP 共用)测试:真实 Pipeline + 假下载器/转写器。"""

from pathlib import Path

import pytest

import app.converter as converter
from app.downloader import MediaInfo
from app.headless import Service, ServiceError, channel_block_reason
from app.pipeline import Pipeline
from app.store import Job, Settings, Store
from app.transcriber import Segment

INFO = MediaInfo(
    media_id="BV1svc_p1", bv="BV1svc", title="服务层测试", uploader="UP",
    duration=10.0, url="https://www.bilibili.com/video/BV1svc", part=1, total_parts=1,
    playlist_title="合集S",
)


class FakeDownloader:
    def __init__(self):
        self.probe_calls = 0
        self.configure_calls = 0

    def configure(self, **kwargs):
        self.configure_calls += 1

    def probe(self, url):
        self.probe_calls += 1
        if "broken" in url:
            raise RuntimeError("网络不可达")
        return [INFO]

    def download_audio(self, info, dest_dir, progress=None, want_video=False):
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / "服务层测试.m4a"
        p.write_bytes(b"fake")
        info.audio_path = p
        return p


class FakeTranscriber:
    resolved_device = "cpu"

    def transcribe(self, wav, duration=0.0, progress=None):
        return [Segment(0.0, 1.0, "服务层"), Segment(1.0, 2.0, "测试")]


@pytest.fixture
def service(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(converter, "to_wav16k_mono",
                        lambda src, dst: dst.write_bytes(b"wav") or dst)
    store = Store(tmp_path / "data" / "history.db")
    settings = Settings(output_dir="out", keep_audio=False)
    dl = FakeDownloader()
    svc = Service(downloader=dl, store=store, settings=settings, base_dir=tmp_path)
    svc.pipeline._transcriber = FakeTranscriber()
    svc.pipeline._model_key = ("locked",)
    svc.pipeline._get_transcriber = lambda: svc.pipeline._transcriber
    yield svc
    svc.close()


def test_submit_url_returns_media_ids_and_completes(service):
    result = service.submit("https://x/video/BV1svc")
    assert result["kind"] == "url"
    assert result["jobs"][0]["media_id"] == "BV1svc_p1"
    wait = service.wait(["BV1svc_p1"], timeout_sec=15)
    assert wait["all_done"] is True
    job = service.status("BV1svc_p1")
    assert job["status"] == "done"
    assert job["paths"]["md"].endswith(".md")
    assert service.downloader.probe_calls == 1


def test_submit_second_time_skips_done(service):
    service.submit("https://x/video/BV1svc")
    service.wait(["BV1svc_p1"], timeout_sec=15)
    again = service.submit("https://x/video/BV1svc")
    assert again["skipped"] == 1
    assert again["jobs"][0]["status"] == "skipped"


def test_submit_probe_error_is_readable(service):
    with pytest.raises(ServiceError) as exc:
        service.submit("https://broken/x")
    assert "解析失败" in str(exc.value)


def test_empty_source_rejected(service):
    with pytest.raises(ServiceError):
        service.submit("   ")


def test_local_submit_and_skip(service, tmp_path):
    media = tmp_path / "talk.m4a"
    media.write_bytes(b"fake-audio")
    first = service.submit(str(media))
    assert first["kind"] == "local"
    mid = first["jobs"][0]["media_id"]
    service.wait([mid], timeout_sec=15)
    assert service.status(mid)["status"] == "done"
    second = service.submit(str(media))
    assert second["skipped"] == 1


def test_overrides_are_session_only(service, tmp_path):
    service.submit("https://x/video/BV1svc", engine="sensevoice")
    assert service.settings.engine == "sensevoice"
    assert not (tmp_path / "data" / "settings.json").exists()


def test_unknown_override_rejected(service):
    with pytest.raises(ServiceError):
        service.submit("https://x/video/BV1svc", bogus=1)


def test_set_settings_persists(service, tmp_path):
    payload = service.set_settings(engine="sensevoice")
    assert payload["saved"] is True
    assert (tmp_path / "data" / "settings.json").exists()
    assert '"engine": "sensevoice"' in (tmp_path / "data" / "settings.json").read_text("utf-8")


def test_status_missing_raises(service):
    with pytest.raises(ServiceError):
        service.status("nope")


def test_transcript_before_done_raises(service):
    service.store.upsert_job(Job(media_id="q1", bv="", title="排队", uploader="",
                                 duration=0, url="", status="queued"))
    with pytest.raises(ServiceError) as exc:
        service.transcript("q1")
    assert "尚无" in str(exc.value)


def test_transcript_truncates(service, tmp_path):
    out = tmp_path / "out.md"
    out.write_text("字" * 100, encoding="utf-8")
    service.store.upsert_job(Job(media_id="d1", bv="", title="完成", uploader="",
                                 duration=0, url="", status="done",
                                 md_path=str(out)))
    payload = service.transcript("d1", fmt="md", max_chars=10)
    assert payload["truncated"] is True
    assert payload["content"].startswith("字" * 10)
    full = service.transcript("d1", fmt="md", max_chars=0)
    assert full["truncated"] is False
    assert len(full["content"]) == 100


def test_list_jobs_filters(service):
    service.store.upsert_job(Job(media_id="a", bv="", title="甲", uploader="",
                                 duration=0, url="", status="done"))
    service.store.upsert_job(Job(media_id="b", bv="", title="乙", uploader="",
                                 duration=0, url="", status="failed"))
    assert len(service.list_jobs()) == 2
    assert [j["media_id"] for j in service.list_jobs(status="failed")] == ["b"]
    assert [j["media_id"] for j in service.list_jobs(search="甲")] == ["a"]


def test_wait_timeout_reports_partial(service):
    service.store.upsert_job(Job(media_id="slow", bv="", title="慢", uploader="",
                                 duration=0, url="", status="transcribing"))
    wait = service.wait(["slow"], timeout_sec=0.3, poll_sec=0.05)
    assert wait["all_done"] is False
    assert wait["timed_out"] is True
    assert wait["jobs"][0]["status"] == "transcribing"


def test_wait_missing_ids_rejected(service):
    with pytest.raises(ServiceError):
        service.wait([])


def test_cancel_marks_cancelled(service):
    service.store.upsert_job(Job(media_id="c1", bv="", title="取消", uploader="",
                                 duration=0, url="", status="queued"))
    job = service.cancel("c1")
    assert job["status"] == "cancelled"


def test_wait_missing_id_fails_fast(service):
    import time as _t
    started = _t.monotonic()
    with pytest.raises(ServiceError) as exc:
        service.wait(["nope"], timeout_sec=30)
    assert "任务不存在" in str(exc.value)
    assert _t.monotonic() - started < 2  # 不白等 timeout


def test_bare_bv_is_normalized(service):
    result = service.submit("BV1xx411c7mD")
    assert result["source"] == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert result["jobs"][0]["media_id"] == "BV1svc_p1"


def test_directory_source_rejected(service, tmp_path):
    d = tmp_path / "folder"
    d.mkdir()
    with pytest.raises(ServiceError) as exc:
        service.submit(str(d))
    assert "目录" in str(exc.value)


def test_set_settings_coerces_types(service, tmp_path):
    payload = service.set_settings(workers="abc", vad="false", notes="true")
    settings = payload["settings"]
    assert settings["workers"] == Settings().workers  # 坏值回退默认
    assert settings["vad"] is False
    assert settings["notes"] is True


def test_list_jobs_bad_limit_falls_back(service):
    service.store.upsert_job(Job(media_id="a", bv="", title="甲", uploader="",
                                 duration=0, url="", status="done"))
    assert len(service.list_jobs(limit="abc")) == 1


def test_submit_running_job_returns_running(service):
    """重复提交正在执行的任务:返回 running,不再二次入队(AI 超时重试的典型场景)。"""
    service.store.upsert_job(Job(media_id="BV1svc_p1", bv="BV1svc", title="服务层测试",
                                 uploader="UP", duration=10.0, url="https://x",
                                 status="transcribing", progress=0.4))
    result = service.submit("https://www.bilibili.com/video/BV1svc")
    assert result["jobs"][0]["status"] == "running"
    assert result["jobs"][0]["stage"] == "transcribing"
    assert result["running"] == 1
    assert service.pipeline._queue.unfinished_tasks == 0
    assert service.store.get_job("BV1svc_p1").status == "transcribing"


def test_wait_orphan_queued_returns_stalled_fast(service):
    """上次会话遗留的 queued 任务:本进程不会调度它,必须快速返回 stalled 而非白等。"""
    import time as _t
    service.store.upsert_job(Job(media_id="orphan", bv="", title="遗留", uploader="",
                                 duration=1.0, url="https://x", status="queued"))
    started = _t.monotonic()
    payload = service.wait(["orphan"], timeout_sec=60)
    assert _t.monotonic() - started < 3
    assert payload["stalled"] is True
    assert payload["all_done"] is False
    assert "排队" in payload["hint"]


def test_prewarm_wait_reflects_loaded_state(service, monkeypatch):
    monkeypatch.setattr(service.pipeline, "prewarm", lambda: None)
    monkeypatch.setattr(service.pipeline, "_prewarm_thread", None, raising=False)
    service.pipeline._transcriber = type("T", (), {"loaded": True})()
    payload = service.prewarm(wait=True)
    assert payload["loaded"] is True
    assert payload["waited_sec"] >= 0


def test_set_settings_rejects_invalid_engine(service):
    with pytest.raises(ServiceError) as exc:
        service.set_settings(engine="whisper-large")
    assert "engine" in str(exc.value)
    assert service.get_settings()["engine"] != "whisper-large"


def test_set_settings_rejects_invalid_device(service):
    with pytest.raises(ServiceError):
        service.set_settings(device="gpu")


def test_set_settings_rejects_unusable_output_dir(service, tmp_path):
    """坏 output_dir 必须当场拒绝:否则转写跑完才在保存阶段失败,GPU 白烧。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")   # 用文件当父目录,必然无法创建
    with pytest.raises(ServiceError) as exc:
        service.set_settings(output_dir=str(blocker / "sub"))
    assert "output_dir" in str(exc.value)


def test_set_settings_warns_missing_cookie_file(service, tmp_path):
    payload = service.set_settings(cookie_file=str(tmp_path / "nope.txt"))
    assert payload["saved"] is True
    assert payload["warnings"] and "cookie_file" in payload["warnings"][0]


def test_override_with_bad_engine_fails_before_submit(service):
    with pytest.raises(ServiceError):
        service.submit("https://www.bilibili.com/video/BV1svc", engine="nope")
    assert service.downloader.probe_calls == 0  # 未触发下载/解析


def test_list_jobs_running_alias_covers_active_stages(service):
    """AI 习惯传 status="running":要映射到全部进行中状态,不能返回空。"""
    for mid, st in (("a", "downloading"), ("b", "transcribing"), ("c", "done")):
        service.store.upsert_job(Job(media_id=mid, bv="", title=mid, uploader="",
                                     duration=0, url="", status=st))
    assert {j["media_id"] for j in service.list_jobs(status="running")} == {"a", "b"}
    assert [j["media_id"] for j in service.list_jobs(status="done")] == ["c"]


# ---------- 通道开关(CLI/MCP 的开关由桌面端设置页维护) ----------

def test_channel_block_reason_default_allows(tmp_path: Path):
    """没有 settings.json(全新安装)时两个通道都可用。"""
    missing = tmp_path / "nope.json"
    assert channel_block_reason("cli", missing) == ""
    assert channel_block_reason("mcp", missing) == ""


def test_channel_block_reason_reports_disabled_channel(tmp_path: Path):
    from app.store import save_settings
    p = tmp_path / "settings.json"
    save_settings(Settings(enable_cli=False), p)
    msg = channel_block_reason("cli", p)
    assert "命令行" in msg and "enable_cli" in msg
    # 两个开关互不影响
    assert channel_block_reason("mcp", p) == ""


def test_channel_block_reason_ignores_unknown_channel(tmp_path: Path):
    assert channel_block_reason("carrier-pigeon", tmp_path / "x.json") == ""


def test_set_settings_rejects_channel_switches(service):
    """通道开关只能由桌面端改:被提示注入的 AI 不能给自己开门/关门。"""
    with pytest.raises(ServiceError) as ei:
        service.set_settings(enable_mcp=False)
    assert "桌面端设置页" in str(ei.value)


def test_set_settings_keeps_switch_changed_on_disk(service, tmp_path: Path):
    """设置的写入是读-改-写:长驻 MCP 进程不能把用户在设置页关掉的开关写回来。"""
    from dataclasses import replace

    from app.store import load_settings, save_settings

    service.set_settings(engine="whisper")  # 先落盘一份
    path = tmp_path / "data" / "settings.json"
    on_disk = load_settings(path)
    save_settings(replace(on_disk, enable_mcp=False), path)  # 模拟用户在设置页关掉

    service.set_settings(engine="sensevoice")  # 之后 AI 又调了一次 update_settings
    assert load_settings(path).enable_mcp is False
    assert service.get_settings()["enable_mcp"] is False
    assert load_settings(path).engine == "sensevoice"


def test_get_settings_reports_persisted_not_session_override(service):
    """传过 engine 覆盖后,get_settings 仍要报磁盘上的配置,并单列本次覆盖。"""
    service.submit("https://x/video/BV1svc", engine="sensevoice")
    data = service.get_settings()
    assert data["engine"] == "whisper"
    assert data["session_overrides"]["engine"] == "sensevoice"


def test_set_settings_rejects_empty_output_dir(service):
    """空 output_dir 会被解析成程序根目录,产物散落到源码树里,必须当场拒绝。"""
    with pytest.raises(ServiceError) as ei:
        service.set_settings(output_dir="   ")
    # 统一空串守卫(见 set_settings):文案含键名与"空串",拒绝语义不变
    assert "output_dir" in str(ei.value) and "空串" in str(ei.value)
