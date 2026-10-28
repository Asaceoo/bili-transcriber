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
    resp = server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
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
