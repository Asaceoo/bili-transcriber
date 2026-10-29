"""MCP 协议层测试:initialize/tools 列表/工具调度/stdio 分帧。"""

import io
import json

import pytest

from app.headless import ServiceError
from app.mcp_server import MCPServer, TOOLS, serve


class FakeService:
    def __init__(self):
        self.calls = []

    def submit(self, source, **kw):
        self.calls.append(("submit", source, kw))
        return {"source": source, "kind": "url",
                "jobs": [{"media_id": "BV1x_p1", "title": "T", "status": "queued"}],
                "total": 1, "skipped": 0}

    def wait(self, ids, timeout_sec=600):
        self.calls.append(("wait", ids, timeout_sec))
        return {"jobs": [{"media_id": ids[0], "status": "done", "terminal": True}],
                "all_done": True, "timed_out": False, "waited_sec": 0.1}

    def status(self, media_id):
        if media_id == "missing":
            raise ServiceError("任务不存在:missing")
        return {"media_id": media_id, "status": "done"}

    def list_jobs(self, status="", search="", limit=20):
        return [{"media_id": "a", "status": "done", "search": search, "limit": limit}]

    def transcript(self, media_id, fmt="md", max_chars=12000):
        return {"media_id": media_id, "format": fmt, "content": "正文"}

    def cancel(self, media_id):
        return {"media_id": media_id, "status": "cancelled"}

    def prewarm(self):
        return {"started": True}

    def get_settings(self):
        return {"engine": "whisper", "model_size": "large-v3-turbo"}

    def set_settings(self, **kw):
        return {"saved": True, "settings": kw}

    def search(self, keyword, limit=10, context_chars=240, fmt="md"):
        if not (keyword or "").strip():
            raise ServiceError("缺少参数:keyword")
        return {"keyword": keyword, "count": 0, "hits": []}

    def close(self):
        pass


@pytest.fixture
def server():
    fake = FakeService()
    srv = MCPServer(service_factory=lambda: fake)
    srv._fake = fake
    return srv


def _call(srv, name, args=None, msg_id=1):
    return srv.handle({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                       "params": {"name": name, "arguments": args or {}}})


def _payload(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def test_initialize_negotiates_supported_protocol(server):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-03-26"}})
    assert resp["result"]["protocolVersion"] == "2025-03-26"
    assert resp["result"]["serverInfo"]["name"] == "bili-transcriber"
    assert "tools" in resp["result"]["capabilities"]


def test_initialize_unknown_protocol_falls_back(server):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "1999-01-01"}})
    assert resp["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_schema_valid(server):
    resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    assert len(tools) == len(TOOLS)
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_notification_has_no_response(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_returns_error(server):
    # resources/list、prompts/list 已按"空清单"兼容处理,这里用真正未实现的方法
    resp = server.handle({"jsonrpc": "2.0", "id": 3, "method": "sampling/createMessage"})
    assert resp["error"]["code"] == -32601


def test_tools_call_success(server):
    resp = _call(server, "transcribe", {"source": "https://x", "wait": True})
    assert resp["result"]["isError"] is False
    payload = _payload(resp)
    assert payload["jobs"][0]["media_id"] == "BV1x_p1"
    assert payload["wait"]["all_done"] is True
    assert server._fake.calls[0][0] == "submit"


def test_tools_call_service_error_is_tool_error(server):
    resp = _call(server, "job_status", {"media_id": "missing"})
    assert resp["result"]["isError"] is True
    assert "任务不存在" in _payload(resp)["error"]


def test_tools_call_unknown_tool(server):
    resp = _call(server, "no_such_tool")
    assert resp["result"]["isError"] is True
    assert "未知工具" in _payload(resp)["error"]


def test_tools_call_bad_arguments(server):
    resp = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "list_jobs", "arguments": "oops"}})
    assert resp["result"]["isError"] is True


def test_tool_exception_is_captured(server):
    def boom(**kw):
        raise RuntimeError("内部炸了")
    server._fake.get_settings = boom
    resp = _call(server, "get_settings")
    assert resp["result"]["isError"] is True
    assert "内部炸了" in _payload(resp)["error"]


def test_update_settings_passes_through(server):
    resp = _call(server, "update_settings", {"engine": "sensevoice"})
    assert _payload(resp)["saved"] is True


def test_list_jobs_limit_int(server):
    resp = _call(server, "list_jobs", {"limit": "5", "search": "abc"})
    payload = _payload(resp)
    assert payload["jobs"][0]["limit"] == 5
    assert payload["jobs"][0]["search"] == "abc"


def test_serve_framing_and_parse_error():
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        "\n"
        "not-json\n"
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    out = io.StringIO()
    srv = MCPServer(service_factory=lambda: FakeService())
    serve(srv, stdin, out)
    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    assert len(lines) == 3  # initialize / parse error / tools list(通知无响应)
    assert lines[0]["id"] == 1
    assert lines[1]["error"]["code"] == -32700
    assert lines[2]["id"] == 2 and lines[2]["result"]["tools"]


def test_lazy_service_not_created_for_handshake():
    created = {"n": 0}

    def factory():
        created["n"] += 1
        return FakeService()

    srv = MCPServer(service_factory=factory)
    srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert created["n"] == 0
    srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "get_settings", "arguments": {}}})
    assert created["n"] == 1


def test_transcribe_missing_source_is_error(server):
    resp = _call(server, "transcribe", {})
    assert resp["result"]["isError"] is True
    assert "source" in _payload(resp)["error"]


def test_wait_jobs_bad_type_is_error(server):
    resp = _call(server, "wait_jobs", {"media_ids": {"a": 1}})
    assert resp["result"]["isError"] is True
    assert "media_ids" in _payload(resp)["error"]


def test_job_status_missing_id_is_error(server):
    resp = _call(server, "job_status", {})
    assert resp["result"]["isError"] is True
    assert "media_id" in _payload(resp)["error"]


def test_bad_numeric_arg_is_readable_error(server):
    resp = _call(server, "list_jobs", {"limit": "很多"})
    assert resp["result"]["isError"] is True
    assert "limit" in _payload(resp)["error"]


def test_batch_request_is_flattened(server):
    """批量请求里再套批量不能产生嵌套数组(协议不允许)。"""
    resp = server.handle([
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        [{"jsonrpc": "2.0", "id": 2, "method": "ping"}],
    ])
    assert isinstance(resp, list)
    assert all(isinstance(item, dict) for item in resp)
    assert [item["id"] for item in resp] == [1, 2]


def test_tool_result_sanitizes_nan_and_infinity(server):
    """NaN/Infinity 不是合法 JSON:出站前必须净化成 null,否则客户端 JSON.parse 直接崩。"""
    resp = MCPServer._tool_result({"duration": float("nan"), "progress": float("inf"),
                                   "nested": [float("-inf")], "ok": 1})
    text = resp["content"][0]["text"]
    assert "NaN" not in text and "Infinity" not in text
    payload = json.loads(text)          # 严格解析必须通过
    assert payload["duration"] is None
    assert payload["progress"] is None
    assert payload["nested"] == [None]


# ---------- 互操作与通道开关 ----------

def test_resources_and_prompts_list_return_empty(server):
    """只声明 tools 能力,但部分客户端会无条件拉清单:回空清单而非 -32601。"""
    for method, key in (("resources/list", "resources"),
                        ("resources/templates/list", "resourceTemplates"),
                        ("prompts/list", "prompts")):
        resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": method})
        assert resp["result"][key] == [], method
    assert server.handle({"jsonrpc": "2.0", "method": "logging/setLevel",
                          "params": {"level": "info"}}) is None


def test_client_config_matches_runtime_mode():
    from app.mcp_server import client_config
    frozen = client_config(r"C:/apps/bili-transcriber.exe", frozen=True)
    assert frozen["mcpServers"]["bili-transcriber"] == {
        "command": r"C:/apps/bili-transcriber.exe", "args": ["--mcp"]}
    source = client_config("C:/py/python.exe", frozen=False)
    assert source["mcpServers"]["bili-transcriber"]["args"] == ["-m", "app.mcp_server"]


def test_main_refuses_when_mcp_disabled(tmp_path, monkeypatch, capsys):
    """设置页关掉 MCP 后,服务必须退出码 1 且把原因写 stderr(不能污染 stdout)。"""
    import app.mcp_server as mcp
    from app.store import Settings, save_settings

    monkeypatch.setattr(mcp, "setup_logging", lambda: None)
    monkeypatch.setattr("app.headless.default_data_dir", lambda: tmp_path)
    save_settings(Settings(enable_mcp=False), tmp_path / "settings.json")
    assert mcp.main() == 1
    err = capsys.readouterr().err
    assert "enable_mcp" in err and "设置" in err


def test_isolate_stdout_keeps_protocol_channel_clean(monkeypatch):
    """任何遗留的 print 都不能落进 JSON-RPC 通道。"""
    import app.mcp_server as mcp

    proto, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", proto)
    monkeypatch.setattr("sys.stderr", err)
    proto_out = mcp._isolate_stdout()
    print("stray log line")           # 模拟第三方库/遗留调试输出
    proto_out.write('{"jsonrpc":"2.0"}\n')

    assert "stray log line" in err.getvalue()
    assert "stray log line" not in proto.getvalue()
    assert proto.getvalue() == '{"jsonrpc":"2.0"}\n'


def test_serve_tolerates_non_utf8_input():
    """客户端塞进非 UTF-8 字节时降级为解析错误,不能把整个会话拆掉。"""
    stream = io.TextIOWrapper(
        io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\xff\xfe\n'),
        encoding="utf-8", errors="replace")  # 与 main() 里 reconfigure 的配置一致
    out = io.StringIO()
    serve(MCPServer(service_factory=lambda: FakeService()), stream, out)
    lines = [json.loads(x) for x in out.getvalue().strip().splitlines()]
    assert lines[0]["result"] == {}
    assert lines[1]["error"]["code"] == -32700


# ---------------------------------------------------------------- v0.1.43 新工具

def test_tools_list_contains_new_tools(server):
    names = {t["name"] for t in TOOLS}
    assert {"search_transcript", "check_network"} <= names


def test_search_transcript_passes_args(server):
    captured = {}

    def fake_search(keyword, limit=10, context_chars=240, fmt="md"):
        captured.update(keyword=keyword, limit=limit, context_chars=context_chars)
        return {"keyword": keyword, "count": 0, "hits": []}

    server._fake.search = fake_search
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "search_transcript", "arguments": {
                              "keyword": "关键词", "limit": "3",
                              "context_chars": 500}}})
    sc = resp["result"]["structuredContent"]
    assert captured == {"keyword": "关键词", "limit": 3, "context_chars": 500}
    assert sc["count"] == 0


def test_search_transcript_missing_keyword_is_tool_error(server):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "search_transcript", "arguments": {}}})
    assert resp["result"]["isError"] is True
    assert "keyword" in resp["result"]["structuredContent"]["error"]


def test_check_network_quick_mode(server, monkeypatch):
    from app import netcheck as nc
    from app.netcheck import NetworkReport, PlatformProbe, ProxyCandidate

    def fake_probe(timeout=4.0):
        rep = NetworkReport()
        rep.direct["bilibili"] = PlatformProbe(platform="bilibili", ok=True, ms=100)
        rep.working_proxy = "http://127.0.0.1:7897"
        rep.system_proxy = "http://127.0.0.1:7897"
        rep.proxies = [ProxyCandidate(url="http://127.0.0.1:7897", source="系统代理",
                                      listening=True, verified=True)]
        return rep

    monkeypatch.setattr(nc, "probe_network", fake_probe)
    resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "check_network",
                                     "arguments": {"mode": "quick"}}})
    sc = resp["result"]["structuredContent"]
    assert sc["mode"] == "quick"
    assert sc["working_proxy"] == "http://127.0.0.1:7897"
    assert sc["direct"]["bilibili"]["ok"] is True


def test_check_network_full_includes_browser_scan(server, monkeypatch):
    from pathlib import Path

    from app import netcheck as nc
    from app.netcheck import (
        BrowserInfo, DetectionReport, NetworkReport,
    )

    monkeypatch.setattr(nc, "probe_network", lambda timeout=4.0: NetworkReport())

    def fake_browser():
        b = BrowserInfo(key="chrome", label="Chrome", running=True, error="x")
        b.cookie_db = Path("C:/fake/Cookies")
        return b

    monkeypatch.setattr(nc, "detect_browsers", lambda: [fake_browser()])
    monkeypatch.setattr(nc, "detect_cookie_files", lambda dirs, extra_files=(): [])
    monkeypatch.setattr(nc, "default_cookie_dirs", lambda base, data: [base])
    monkeypatch.setattr(
        nc, "build_report",
        lambda browsers, files, network, **kw: DetectionReport(
            browsers=browsers, cookie_files=files, network=network,
            suggested_browser="edge", suggested_cookie_file="",
            suggested_proxy="http://127.0.0.1:7897", notes=["n1"]))
    resp = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "check_network",
                                     "arguments": {"mode": "full"}}})
    sc = resp["result"]["structuredContent"]
    assert sc["suggested"]["browser"] == "edge"
    b = sc["browsers"][0]
    assert b["running"] is True
    assert b["readable"] is False          # property 也进了 JSON
    assert isinstance(b["cookie_db"], str)  # Path 字符串化
    assert sc["notes"] == ["n1"]


def test_check_network_test_mode(server, monkeypatch):
    from app import netcheck as nc
    from app.netcheck import PlatformTest, TestReport

    monkeypatch.setattr(nc, "test_configuration", lambda timeout=4.0: TestReport(
        items=[PlatformTest(platform="bilibili", network_ok=True, ms=88,
                            cookie_state="verified", detail="d")],
        source="cookie_file", summary="OK", warnings=[], ok=True))
    resp = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "check_network",
                                     "arguments": {"mode": "test"}}})
    sc = resp["result"]["structuredContent"]
    assert sc["ok"] is True and sc["platforms"][0]["ms"] == 88


def test_check_network_timeout_is_clamped(server, monkeypatch):
    seen = {}
    from app import netcheck as nc

    def fake_probe(timeout=4.0):
        seen["timeout"] = timeout
        return NetworkReport()

    monkeypatch.setattr(nc, "probe_network", fake_probe)
    server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                   "params": {"name": "check_network",
                              "arguments": {"timeout": 99}}})
    assert seen["timeout"] == 15.0


# ---------------------------------------------------------------- Service.search

def _make_service_with_store(tmp_path):
    """借真 Store 造两个 done 任务,一个无产物。"""
    from app.store import Job, Store

    store = Store(tmp_path / "history.db")
    store.upsert_job(Job(media_id="a_p1", bv="BV1a", title="讲转写的视频",
                         uploader="u", duration=1.0, url="",
                         status="done", md_path=str(tmp_path / "a.md"),
                         txt_path=str(tmp_path / "a.txt")))
    (tmp_path / "a.md").write_text(
        "前文\n这里讲到 transformer 的注意力机制,非常重要\n后文", encoding="utf-8")
    (tmp_path / "a.txt").write_text("txt 版也有注意力机制", encoding="utf-8")
    store.upsert_job(Job(media_id="b_p1", bv="BV1b", title="另一个",
                         uploader="u", duration=1.0, url="", status="done"))

    class _S:
        pass

    svc = _S()
    svc.store = store
    from app.headless import Service

    svc.search = Service.search.__get__(svc)
    yield svc
    store.close()


def test_service_search_finds_keyword_with_context(tmp_path):
    for svc in _make_service_with_store(tmp_path):
        out = svc.search("注意力机制")
        assert out["count"] == 1
        hit = out["hits"][0]
        assert hit["media_id"] == "a_p1"
        assert "transformer" in hit["snippet"]
        assert hit["position"] > 0


def test_service_search_missing_keyword(tmp_path):
    for svc in _make_service_with_store(tmp_path):
        assert svc.search("不存在的词")["count"] == 0


def test_service_search_requires_keyword(tmp_path):
    for svc in _make_service_with_store(tmp_path):
        from app.headless import ServiceError

        with pytest.raises(ServiceError):
            svc.search("  ")
        with pytest.raises(ServiceError):
            svc.search("")


def test_service_search_skips_jobs_without_products(tmp_path):
    for svc in _make_service_with_store(tmp_path):
        out = svc.search("另一个")  # 标题不在正文检索范围,只搜 md/txt
        assert out["count"] == 0
        out2 = svc.search("注意力机制", fmt="txt")
        assert out2["searched_fmt"] == "txt"
        assert out2["hits"][0]["path"].endswith("a.txt")


def test_check_network_rejects_unknown_mode(server):
    resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                          "params": {"name": "check_network",
                                     "arguments": {"mode": "立刻"}}})
    assert resp["result"]["isError"] is True
    assert "quick/full/test" in resp["result"]["structuredContent"]["error"]


def test_transcribe_timeout_is_clamped(server, monkeypatch):
    seen = {}

    def fake_submit(source, **kw):
        seen["submitted"] = source
        return {"jobs": [{"media_id": "m1", "status": "queued"}], "total": 1,
                "skipped": 0}

    def fake_wait(ids, timeout_sec=600.0):
        seen["timeout"] = timeout_sec
        return {"jobs": [{"media_id": "m1", "status": "done", "terminal": True}],
                "all_done": True, "timed_out": False, "waited_sec": 0.1}

    server._fake.submit = fake_submit
    server._fake.wait = fake_wait
    server.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                   "params": {"name": "transcribe", "arguments": {
                       "source": "BV1x", "wait": True, "timeout_sec": 1e9}}})
    assert seen["timeout"] == 86400.0  # 1e9 被夹到一天,不会挂死服务
