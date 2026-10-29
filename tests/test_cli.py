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


def test_configure_output_encoding_forces_utf8_on_pipe():
    """被管道/重定向(stdout 非 tty)时必须改写成 UTF-8。

    打包版不继承 PYTHONUTF8,中文 Windows 下 stdout 是 cp936,JSON 里的中文
    会按 GBK 写出,按 UTF-8 解析的 AI 客户端直接解码失败。
    """
    calls = []

    class FakeStream:
        encoding = "cp936"

        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

        def reconfigure(self, **kw):
            calls.append(kw)

    out, err = FakeStream(False), FakeStream(False)
    cli._configure_output_encoding([out, err])
    assert calls == [{"encoding": "utf-8", "errors": "replace"}] * 2


def test_configure_output_encoding_keeps_console_codepage():
    """留在终端时不动:cmd.exe 默认代码页写 UTF-8 会花屏。"""
    calls = []

    class TtyStream:
        def isatty(self):
            return True

        def reconfigure(self, **kw):
            calls.append(kw)

    cli._configure_output_encoding([TtyStream()])
    assert calls == []


def test_configure_output_encoding_tolerates_odd_streams():
    """已关闭/被包装的流不能把 CLI 拖崩(reconfigure 可能抛 ValueError/AttributeError)。"""

    class Broken:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    cli._configure_output_encoding([Broken(), None])


# ---------- 通道开关(设置页关闭 CLI 后必须直接拒绝) ----------

def _write_settings(tmp_path, monkeypatch, **flags):
    from app.store import Settings, save_settings
    monkeypatch.setattr("app.headless.default_data_dir", lambda: tmp_path)
    save_settings(Settings(**flags), tmp_path / "settings.json")


def test_cli_disabled_exits_3(tmp_path, monkeypatch, capsys):
    _write_settings(tmp_path, monkeypatch, enable_cli=False)
    assert cli.main(["--list", "--json"]) == cli.EXIT_CHANNEL_DISABLED == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["channel"] == "cli"
    assert "enable_cli" in payload["error"]


def test_cli_disabled_still_reports_version(tmp_path, monkeypatch, capsys):
    """通道关了也要能查版本,否则排查问题时连版本都拿不到。"""
    _write_settings(tmp_path, monkeypatch, enable_cli=False)
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip()


def test_cli_enabled_passes_gate(tmp_path, monkeypatch, fake, capsys):
    _write_settings(tmp_path, monkeypatch, enable_cli=True)
    assert cli.main(["--list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_cli_does_not_touch_database_when_disabled(tmp_path, monkeypatch):
    """拒绝要发生在建 Service 之前:否则会白开一次 SQLite、白建一个 pipeline。"""
    _write_settings(tmp_path, monkeypatch, enable_cli=False)
    called: list[int] = []
    monkeypatch.setattr(cli, "SERVICE_FACTORY", lambda: called.append(1) or None)
    assert cli.main(["--list"]) == 3
    assert called == []


def test_write_ignores_broken_pipe(monkeypatch):
    """`cli ... | head -1` 提前关闭下游时,不能抛 BrokenPipeError 也不能改退出码。"""
    class Broken:
        encoding = "utf-8"

        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr("sys.stdout", Broken())
    cli._write("hello")            # 不应抛出
    assert cli._write.__name__ == "_write"



# ---------------------------------------------------------------- v0.1.43:grep / agent 接入

def test_grep_json(fake, capsys):
    cli.SERVICE_FACTORY = lambda: fake
    fake.search = lambda keyword, limit=10, context_chars=240, fmt="md": {
        "keyword": keyword, "count": 1,
        "hits": [{"media_id": "a_p1", "title": "T", "path": "a.md",
                  "position": 3, "snippet": "...注意力机制..."}]}
    try:
        assert cli.main(["--grep", "注意力机制", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 1
        assert payload["hits"][0]["media_id"] == "a_p1"
    finally:
        cli.SERVICE_FACTORY = cli.Service


def test_grep_human_output(fake, capsys):
    cli.SERVICE_FACTORY = lambda: fake
    fake.search = lambda keyword, limit=10, context_chars=240, fmt="md": {
        "keyword": keyword, "count": 0, "hits": []}
    try:
        assert cli.main(["--grep", "不存在"]) == 0
        assert "未命中" in capsys.readouterr().out
    finally:
        cli.SERVICE_FACTORY = cli.Service


def test_grep_requires_service(fake, capsys):
    # 无 --json 时缺 Service 也能安全走完(搜索引擎缺产物 → 未命中)
    cli.SERVICE_FACTORY = lambda: fake
    fake.search = lambda *a, **k: {"keyword": "x", "count": 0, "hits": []}
    try:
        assert cli.main(["--grep", "x"]) == 0
    finally:
        cli.SERVICE_FACTORY = cli.Service


def test_agent_config_workbuddy_stdout(capsys):
    assert cli.main(["--agent-config", "workbuddy"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "bili-transcriber" in data["mcpServers"]


def test_agent_config_codex_http(capsys):
    assert cli.main(["--agent-config", "codex", "--transport", "http",
                     "--mcp-port", "9001"]) == 0
    out = capsys.readouterr().out
    assert "[mcp_servers.bili-transcriber]" in out
    assert "http://127.0.0.1:9001/mcp" in out


def test_agent_config_unknown_client_exit_2(capsys):
    assert cli.main(["--agent-config", "nope"]) == 2


def test_agent_config_http_for_unsupported_client_exit_2(capsys):
    assert cli.main(["--agent-config", "tare", "--transport", "http"]) == 2


def test_agent_config_write_creates_file(tmp_path, monkeypatch, capsys):
    import app.agent_setup as A

    monkeypatch.setattr(A, "default_config_path",
                        lambda spec, home=None: tmp_path / "mcp.json")
    assert cli.main(["--agent-config", "workbuddy", "--write"]) == 0
    text = (tmp_path / "mcp.json").read_text(encoding="utf-8")
    assert "bili-transcriber" in text


def test_agent_list_reports_clients(capsys):
    assert cli.main(["--agent-list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    keys = {c["key"] for c in payload["clients"]}
    assert {"workbuddy", "dsh", "codex"} <= keys


def test_doctor_reports_stdio_and_http(capsys):
    assert cli.main(["--doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stdio"]["args"] == ["-m", "app.mcp_server"]
    assert payload["http"]["url"].endswith("/mcp")
    assert "clients" in payload


def test_mcp_http_gated_by_channel(tmp_path, monkeypatch, capsys):
    """设置页关闭 MCP 时 --mcp-http 也要被拦,退出码 3。"""
    monkeypatch.setattr(cli, "channel_block_reason", lambda ch: "MCP 已关闭")
    assert cli.main(["--mcp-http", "8123", "--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"] == "mcp"


def test_agent_config_dsh_write_rejected(capsys):
    assert cli.main(["--agent-config", "dsh", "--write"]) == 1
    assert "界面托管" in capsys.readouterr().out
