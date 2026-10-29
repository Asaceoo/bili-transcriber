"""MCP HTTP 传输测试:会话表、安全头、路由与端到端真实 socket。"""

import json
import urllib.error
import urllib.request

import pytest

from app.mcp_http import (
    MAX_BODY,
    McpHttpServer,
    _Sessions,
    _origin_allowed,
)


# ---------------------------------------------------------------- 会话表


def test_sessions_create_touch_drop():
    s = _Sessions(ttl=60.0)
    sid = s.create()
    assert s.touch(sid) is True
    assert s.touch("nope") is False
    assert s.drop(sid) is True
    assert s.drop(sid) is False
    assert s.count() == 0


def test_sessions_expire_by_ttl(monkeypatch):
    s = _Sessions(ttl=10.0)
    sid = s.create()
    # 时间直接快进 11 秒
    import app.mcp_http as m

    now = m.time.monotonic() + 11.0
    monkeypatch.setattr(m.time, "monotonic", lambda: now)
    assert s.touch(sid) is False  # 过期即视为不存在
    assert s.count() == 0


def test_sessions_evict_oldest_when_full():
    s = _Sessions(ttl=999.0, limit=2)
    a, b = s.create(), s.create()
    s.touch(a)  # a 变新,b 最旧
    c = s.create()
    assert s.touch(b) is False  # b 被淘汰
    assert s.touch(a) and s.touch(c)


def test_sessions_drop_by_delete_endpoint():
    srv = McpHttpServer(port=0)
    srv.start()
    try:
        sid = srv.sessions.create()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/mcp", method="DELETE",
            headers={"Mcp-Session-Id": sid})
        with opener.open(req, timeout=10) as r:
            assert r.status == 204
        assert srv.sessions.touch(sid) is False
    finally:
        srv.stop()


# ---------------------------------------------------------------- 安全规则


@pytest.mark.parametrize("origin,ok", [
    (None, True),
    ("", True),
    ("http://127.0.0.1:5500", True),
    ("http://localhost:3000", True),
    ("https://evil.example", False),
    ("http://127.0.0.1.evil.example", False),
    ("not a url", False),
])
def test_origin_allowlist(origin, ok):
    assert _origin_allowed(origin) is ok


def test_bearer_token_required_when_set():
    srv = McpHttpServer(port=0, token="s3cret")
    srv.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()

        def req(headers):
            return urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/mcp", data=payload,
                headers={"Content-Type": "application/json", **headers}, method="POST")

        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req({}), timeout=10)
        assert ei.value.code == 401
        # token 错也 401
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req({"Authorization": "Bearer wrong"}), timeout=10)
        assert ei.value.code == 401
        # token 对 → 200
        with opener.open(req({"Authorization": "Bearer s3cret"}), timeout=10) as r:
            assert r.status == 200
            assert json.loads(r.read())["result"] == {}
    finally:
        srv.stop()


def test_rejects_unknown_protocol_version():
    srv = McpHttpServer(port=0)
    srv.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
            headers={"Content-Type": "application/json",
                     "MCP-Protocol-Version": "1999-01-01"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req, timeout=10)
        assert ei.value.code == 400
    finally:
        srv.stop()


# ---------------------------------------------------------------- 路由


def test_get_mcp_returns_405_and_healthz_ok():
    srv = McpHttpServer(port=0)
    srv.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(f"http://127.0.0.1:{srv.port}/mcp", timeout=10)
        assert ei.value.code == 405
        with opener.open(f"http://127.0.0.1:{srv.port}/healthz", timeout=10) as r:
            body = json.loads(r.read())
            assert body["ok"] is True
            assert body["server"] == "bili-transcriber"
        # 未知路径 404
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(f"http://127.0.0.1:{srv.port}/other", timeout=10)
        assert ei.value.code == 404
    finally:
        srv.stop()


def test_post_body_guards():
    srv = McpHttpServer(port=0)
    srv.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f"http://127.0.0.1:{srv.port}/mcp"

        # 空体 → 400 parse error
        req = urllib.request.Request(url, data=b"   ",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req, timeout=10)
        assert ei.value.code == 400

        # 坏 JSON → 400 -32700
        req = urllib.request.Request(url, data=b"{oops",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req, timeout=10)
        payload = json.loads(ei.value.read())
        assert payload["error"]["code"] == -32700

        # 超长体 → 400(不读完整内容,只声明长度)
        req = urllib.request.Request(
            url, data=b"x", method="POST",
            headers={"Content-Type": "application/json",
                     "Content-Length": str(MAX_BODY + 1)})
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req, timeout=10)
        assert ei.value.code == 400
    finally:
        srv.stop()


def test_initialize_returns_session_and_accepts_sse():
    srv = McpHttpServer(port=0)
    srv.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f"http://127.0.0.1:{srv.port}/mcp"
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18",
                                      "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}}})

        req = urllib.request.Request(url, data=init.encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with opener.open(req, timeout=10) as r:
            assert r.status == 200
            sid = r.headers.get("Mcp-Session-Id")
            assert sid

        # 带 Accept: text/event-stream → SSE 帧
        req = urllib.request.Request(
            url, data=json.dumps({"jsonrpc": "2.0", "id": 2,
                                  "method": "tools/list"}).encode(),
            headers={"Content-Type": "application/json", "Mcp-Session-Id": sid,
                     "Accept": "application/json, text/event-stream"},
            method="POST")
        with opener.open(req, timeout=10) as r:
            assert r.status == 200
            assert "text/event-stream" in r.headers.get("Content-Type", "")
            body = r.read().decode()
        assert body.startswith("event: message\ndata: ")
        event = json.loads(body.split("data: ", 1)[1].split("\n", 1)[0])
        assert len(event["result"]["tools"]) >= 11

        # 未知 session → 404,提示重新 initialize
        req = urllib.request.Request(
            url, data=json.dumps({"jsonrpc": "2.0", "id": 3,
                                  "method": "tools/list"}).encode(),
            headers={"Content-Type": "application/json",
                     "Mcp-Session-Id": "bogus"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req, timeout=10)
        assert ei.value.code == 404
    finally:
        srv.stop()


def test_notification_returns_202():
    srv = McpHttpServer(port=0)
    srv.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/mcp",
            data=json.dumps({"jsonrpc": "2.0",
                             "method": "notifications/initialized"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with opener.open(req, timeout=10) as r:
            assert r.status == 202
            assert r.read() == b""
    finally:
        srv.stop()


def test_start_stop_lifecycle_and_port_backfill():
    srv = McpHttpServer(port=0)
    assert srv._httpd is None
    srv.start()
    assert srv._httpd is not None and srv.port != 0
    srv.stop()
    assert srv._httpd is None
    # 重复 stop 安全
    srv.stop()


def test_context_manager():
    with McpHttpServer(port=0) as srv:
        assert srv._httpd is not None
    assert srv._httpd is None


def test_serve_cli_subprocess_end_to_end():
    """回归：serve_cli 曾因缺 import sys 在真实 --mcp-http 路径崩 NameError。

    用子进程真跑 CLI 入口，验证 stdout 端点行 + POST initialize 全通。
    """
    import subprocess
    import sys as _sys
    import time as _time

    proc = subprocess.Popen(
        [_sys.executable, "-m", "app.cli", "--mcp-http", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(_sys.path[0]) if _sys.path[0] else ".",
        text=True, encoding="utf-8")
    try:
        # 读到 stdout 端点行（serve_cli 的契约）
        endpoint = None
        deadline = _time.time() + 20
        while _time.time() < deadline:
            line = proc.stdout.readline()
            if line.startswith("MCP HTTP listening on "):
                endpoint = line.strip().split()[-1]
                break
            if proc.poll() is not None:
                break
        assert endpoint, "serve_cli 未在 stdout 打印端点行（进程可能已崩溃）"
        assert endpoint.startswith("http://127.0.0.1:")

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            endpoint,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2024-11-05",
                                        "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "0"}}}).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            method="POST")
        with opener.open(req, timeout=10) as r:
            body = r.read().decode()
            assert r.status == 200 and "bili-transcriber" in body
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
