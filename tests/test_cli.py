"""CLI 入口测试:参数解析、JSON 输出、退出码(注入 FakeService)。"""

import json

import pytest

import app.cli as cli
from app.headless import ServiceError


class FakeService:
    def __init__(self):
        self.closed = False
        self.set_calls = {}
        self.submit_calls = {}

    def submit(self, source, **kw):
        self.submit_calls = {"source": source, **kw}
        return {"source": source, "kind": "url",
                "jobs": [{"media_id": "BV1_p1", "title": "标题", "status": "queued"}],
                "total": 1, "skipped": 0}

    def wait(self, ids, timeout_sec=600):
        return {"jobs": [{"media_id": ids[0], "title": "标题", "status": "done",
                          "paths": {"md": r"D:\out\x.md"}}],
                "all_done": True, "timed_out": False, "waited_sec": 1.0}

    def status(self, media_id):
        if media_id == "missing":
            raise ServiceError("任务不存在:missing")
        return {"media_id": media_id, "status": "done", "title": "标题", "paths": {}}

    def list_jobs(self, status="", search="", limit=20):
        return [{"media_id": "a", "status": "done", "title": "甲", "paths": {}}]

    def transcript(self, media_id, fmt="md", max_chars=12000):
        return {"media_id": media_id, "format": fmt, "path": "x.md",
                "chars": 2, "truncated": False, "content": "正文"}

    def cancel(self, media_id):
        return {"media_id": media_id, "status": "cancelled", "title": "标题", "paths": {}}

    def prewarm(self, wait=False):
        self.prewarm_wait = wait
        return {"started": True, "engine": "whisper", "model": "large-v3-turbo",
                "loaded": True, "waited_sec": 0.1}

    def get_settings(self):
        return {"engine": "whisper"}

    def set_settings(self, **kw):
        self.set_calls = kw
        return {"saved": True, "settings": kw}

    def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    svc = FakeService()
    monkeypatch.setattr(cli, "SERVICE_FACTORY", lambda: svc)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    return svc


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip()


def test_no_args_shows_help(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_list_json(fake, capsys):
    assert cli.main(["--list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1


def test_status_missing_exit_1(fake, capsys):
    assert cli.main(["--status", "missing", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "任务不存在" in payload["error"]


def test_result_prints_content(fake, capsys):
    assert cli.main(["--result", "a"]) == 0
    assert "正文" in capsys.readouterr().out


def test_set_settings(fake, capsys):
    assert cli.main(["--set", "engine=sensevoice", "--set", "notes=true"]) == 0
    assert fake.set_calls == {"engine": "sensevoice", "notes": True}


def test_set_invalid_pair(fake, capsys):
    assert cli.main(["--set", "oops", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_submit_no_wait(fake, capsys):
    assert cli.main(["https://x", "--no-wait", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["jobs"][0]["status"] == "queued"
    assert fake.submit_calls["source"] == "https://x"
    assert fake.closed is True


def test_submit_wait_success(fake, capsys):
    assert cli.main(["https://x", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wait"]["all_done"] is True


def test_submit_with_overrides(fake):
    cli.main(["https://x", "--engine", "sensevoice", "--language", "zh", "--no-wait"])
    assert fake.submit_calls["engine"] == "sensevoice"
    assert fake.submit_calls["language"] == "zh"


def test_failed_job_exit_1(fake, capsys):
    def wait_failed(ids, timeout_sec=600):
        return {"jobs": [{"media_id": ids[0], "title": "标题", "status": "failed",
                          "error": "boom", "paths": {}}],
                "all_done": True, "timed_out": False, "waited_sec": 1.0}
    fake.wait = wait_failed
    assert cli.main(["https://x", "--json"]) == 1


def test_cancel(fake, capsys):
    assert cli.main(["--cancel", "a", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"


def test_prewarm_blocks_and_reports_loaded(fake, capsys):
    """CLI 是一次性进程:--prewarm 必须等加载完再退出,否则等于空操作。"""
    assert cli.main(["--prewarm", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["loaded"] is True
    assert fake.prewarm_wait is True


class _GbkOut:
    """模拟打包版窗口程序的 stdout:系统 ANSI 代码页 GBK,遇到 emoji 就炸。"""

    encoding = "gbk"

    def __init__(self):
        self.chunks = []

    def write(self, text):
        text.encode("gbk")   # 装不下 -> UnicodeEncodeError(真实行为)
        self.chunks.append(text)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.chunks)


def test_json_output_survives_gbk_stdout(monkeypatch):
    """B站标题含 emoji 时,GBK 流必须降级为纯 ASCII 的合法 JSON(不能崩)。"""
    out = _GbkOut()
    monkeypatch.setattr(cli.sys, "stdout", out)
    cli._emit({"title": "标题🎉", "ok": True}, as_json=True)
    payload = json.loads(out.text)          # 必须是可解析 JSON
    assert payload["title"] == "标题🎉"


def test_set_workers_non_integer_reports_error(fake, capsys):
    assert cli.main(["--set", "workers=很多", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "整数" in payload["error"]


def test_cli_json_sanitizes_nan():
    """CLI 的 JSON 输出同样不能出现裸 NaN(客户端会解析失败)。"""
    text = cli._dump_json({"duration": float("nan"), "title": "标题"})
    assert "NaN" not in text
    assert json.loads(text)["duration"] is None
