"""MCP Streamable HTTP 传输(2025-03-26 / 2025-06-18 规范的最小实现)。

为什么需要它:stdio 要求客户端**自己拉起子进程**并接管管道。有些调用方做不到 ——
DSH 的 streamable-http 连接、浏览器内的 Agent、容器里跑的应用、只能发 HTTP 的
工作流引擎。这条通道让它们用 `POST /mcp` 就能调同一套工具。

协议处理完全复用 `MCPServer.handle`,stdio 与 HTTP 两条通道行为一致(同一份
tools/list、同一份错误语义),差别只在传输层。

路由:
  POST   /mcp        提交 JSON-RPC(单条或批量);回 JSON 或 SSE(看 Accept)
  GET    /mcp        未实现服务端主动推送 → 405(规范允许,且明确优于挂死)
  DELETE /mcp        结束会话 → 204
  GET    /healthz    存活探针,给容器/脚本用

安全默认值(都不是可选项而是默认,需要时显式放开):
  - 只绑 127.0.0.1:这个服务能读写用户本机文件,绑 0.0.0.0 等于把磁盘交出去
  - 校验 Origin:只接受本机来源/无 Origin 头,挡掉 DNS rebinding(网页通过
    浏览器把 127.0.0.1 当成自己的 origin 来打本机服务)
  - 默认不发 CORS 头:没有 CORS,浏览器页面读不到响应,也就无法驱动工具
  - 可选 Bearer token(`BILI_MCP_HTTP_TOKEN`),给确实要跨进程/跨用户隔离的场景
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from app import APP_VERSION
from app.mcp_server import MCPServer

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PROTOCOL_VERSION = "2025-06-18"
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"
SESSION_TTL = 1800.0  # 30 分钟无活动就回收,避免长跑进程攒出无限 session
MAX_SESSIONS = 64
MAX_BODY = 8 * 1024 * 1024  # 单条 JSON-RPC 最多 8MB(全文转录稿走文件,不走这里)

# 只允许本机来源。空 Origin(命令行工具、curl、MCP 客户端)直接放行,
# 因为浏览器一定会带 Origin —— 这条规则专治浏览器。
_LOCAL_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class HttpProtocolError(RuntimeError):
    """请求本身不合法(不是 JSON、超长、会话无效)。"""


class _Sessions:
    """会话表:initialize 建、DELETE 删、闲置过期回收。

    MCP 的会话只是"客户端逻辑连接"的标识,不保存业务状态(业务状态在
    Service/数据库里),所以这里可以随时回收而不影响正确性。
    """

    def __init__(self, ttl: float = SESSION_TTL, limit: int = MAX_SESSIONS):
        self._lock = threading.Lock()
        self._items: dict[str, float] = {}
        self.ttl = ttl
        self.limit = limit

    def create(self) -> str:
        sid = uuid.uuid4().hex
        with self._lock:
            self._expire_locked()
            if len(self._items) >= self.limit:
                # 满了就淘汰最久没动的,拒绝新连接会让客户端以为服务挂了
                oldest = min(self._items, key=self._items.get)
                self._items.pop(oldest, None)
                logger.warning("MCP 会话数达上限 %d,淘汰最旧会话 %s", self.limit, oldest[:8])
            self._items[sid] = time.monotonic()
        return sid

    def touch(self, sid: str) -> bool:
        with self._lock:
            self._expire_locked()  # 过期即视为不存在;否则 touch 会把死会话复活
            if sid not in self._items:
                return False
            self._items[sid] = time.monotonic()
            return True

    def drop(self, sid: str) -> bool:
        with self._lock:
            return self._items.pop(sid, None) is not None

    def count(self) -> int:
        with self._lock:
            self._expire_locked()
            return len(self._items)

    def _expire_locked(self) -> None:
        now = time.monotonic()
        for sid in [k for k, t in self._items.items() if now - t > self.ttl]:
            self._items.pop(sid, None)


class _QuietHTTPServer(ThreadingHTTPServer):
    """跳过 getfqdn 的 HTTP 服务。

    BaseHTTPRequestHandler 默认的 server_bind 会调 `socket.getfqdn(host)` 做
    反向 DNS —— 本机 DNS 被代理/防火墙接管时(大陆网络很常见),对 127.0.0.1
    的反向解析能挂住几十秒,表现为"服务还没监听就卡死"。server_name 只用于
    日志与默认 Host 头,拿字面 IP 足够。
    """

    def server_bind(self) -> None:  # noqa: D102 — 覆盖标准库行为
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def _origin_allowed(origin: str | None) -> bool:
    """没有 Origin(命令行/原生客户端)放行;有就必须是本机来源。"""
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    return host in _LOCAL_ORIGIN_HOSTS


class McpHttpServer:
    """把 MCPServer 挂到 HTTP 上;线程安全由 handle_lock 串行化保证。

    Service 内部的 SQLite 连接是 check_same_thread=False 建的,并不等于可以
    并发写。stdio 天然是串行的,HTTP 是多线程 —— 这里补一把锁,让两条通道
    的并发语义一致(工具调用本来就是毫秒级,不需要并行)。
    """

    def __init__(self, server: MCPServer | None = None, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT, token: str | None = None,
                 allow_cors: bool = False):
        self.mcp = server or MCPServer()
        self.host = host
        self.port = port
        self.token = token if token is not None else os.environ.get("BILI_MCP_HTTP_TOKEN", "")
        self.allow_cors = allow_cors
        self.sessions = _Sessions()
        self._handle_lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> "McpHttpServer":
        """起 HTTP 服务并立即返回(accept 循环跑在守护线程)。

        之前只构造了 server 对象就返回 —— 没人跑 serve_forever,连接进了
        内核 backlog 却永远没人 accept,客户端表现为"请求挂死无响应"。
        这是工具闭环测试(真实 POST)抓出来的,单靠看代码很难注意。
        """
        handler = _make_handler(self)
        self._httpd = _QuietHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        # port=0 时由系统分配,回填真实端口(测试与"随便找个空端口"都靠这个)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
            name=f"mcp-http-{self.port}")
        self._thread.start()
        logger.info("MCP HTTP 服务监听 http://%s:%d/mcp", self.host, self.port)
        return self

    def serve_forever(self) -> None:
        """阻塞当前线程跑 accept 循环(CLI/独立进程入口用)。

        start() 已在守护线程里跑了一个循环;这里再把主线程挂进同一个
        server 的循环会抢请求,所以语义改为"等待后台线程结束"。
        """
        if self._httpd is None:
            self.start()
        thread = self._thread
        if thread is not None:
            thread.join()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
            self._thread = None
        self.mcp.close()

    def __enter__(self) -> "McpHttpServer":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # ---------- 请求处理(供 handler 调用) ----------

    def check_auth(self, header: str | None) -> bool:
        if not self.token:
            return True
        if not header:
            return False
        parts = header.split(None, 1)
        return len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip() == self.token

    def handle_message(self, payload: Any) -> tuple[Any, str | None]:
        """处理一条 JSON-RPC 消息;返回 (响应, 新建的 session id)。

        会话只在 `initialize` 时创建。客户端如果不带 session 直接 tools/call,
        也放行 —— 有些实现(含部分 CLI 的 --mcp-http 探测器)会跳过握手,
        为此报 400 只会让人以为服务坏了。
        """
        with self._handle_lock:
            resp = self.mcp.handle(payload)
        sid = None
        if isinstance(payload, dict) and payload.get("method") == "initialize":
            sid = self.sessions.create()
        return resp, sid


def _make_handler(owner: McpHttpServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"bili-transcriber/{APP_VERSION}"
        protocol_version = "HTTP/1.1"

        # ---------- 基础工具 ----------

        def _cors(self) -> None:
            if owner.allow_cors:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers",
                                 f"Content-Type, Authorization, {SESSION_HEADER}, "
                                 f"{PROTOCOL_HEADER}")
                self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
                self.send_header("Access-Control-Expose-Headers", SESSION_HEADER)

        def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # 客户端提前断开:不是错误,别刷 traceback

        def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json", extra)

        def _jsonrpc_error(self, status: int, code: int, message: str,
                           request_id: Any = None) -> None:
            self._json(status, {"jsonrpc": "2.0", "id": request_id,
                                "error": {"code": code, "message": message}})

        def _read_body(self) -> bytes:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                # 有些客户端用 chunked;httplib 不解析,直接按空体处理会静默丢请求,
                # 报 411 让问题暴露在客户端一侧,比"看起来成功但没执行"好。
                raise HttpProtocolError("缺少 Content-Length(不支持 chunked 上传)")
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise HttpProtocolError(f"Content-Length 非法:{raw_len!r}") from exc
            if length < 0:
                raise HttpProtocolError("Content-Length 为负数")
            if length > MAX_BODY:
                raise HttpProtocolError(f"请求体过大({length} 字节,上限 {MAX_BODY})")
            return self.rfile.read(length) if length else b""

        # ---------- 路由 ----------

        def do_OPTIONS(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 约定
            if not owner.allow_cors:
                self._send(405, b"", "text/plain")
                return
            self._send(204)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/healthz":
                self._json(200, {"ok": True, "server": "bili-transcriber",
                                 "version": APP_VERSION, "sessions": owner.sessions.count()})
                return
            if path != "/mcp":
                self._send(404, b'{"error":"not found"}')
                return
            # 规范:不提供服务端主动推送时,GET /mcp 必须回 405
            self._send(405, b'{"error":"this server does not offer a GET stream; '
                             b'use POST /mcp"}', "application/json",
                       {"Allow": "POST, DELETE, OPTIONS"})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/")
            if path != "/mcp":
                self._send(404, b'{"error":"not found"}')
                return
            sid = self.headers.get(SESSION_HEADER)
            if sid:
                owner.sessions.drop(sid)
            self._send(204)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/")
            if path != "/mcp":
                self._send(404, b'{"error":"not found"}')
                return
            if not _origin_allowed(self.headers.get("Origin")):
                # DNS rebinding:页面把请求发到 127.0.0.1,Origin 却是外部域名
                self._jsonrpc_error(403, -32600, "拒绝跨站请求(Origin 非本机)")
                return
            if not owner.check_auth(self.headers.get("Authorization")):
                self._jsonrpc_error(401, -32600, "缺少或错误的 Bearer token")
                return
            version = self.headers.get(PROTOCOL_HEADER)
            if version and version not in (PROTOCOL_VERSION, "2025-03-26", "2024-11-05"):
                self._jsonrpc_error(400, -32600, f"不支持的 MCP-Protocol-Version:{version}")
                return
            try:
                body = self._read_body()
            except HttpProtocolError as exc:
                self._jsonrpc_error(400, -32600, str(exc))
                return
            if not body.strip():
                self._jsonrpc_error(400, -32700, "空请求体")
                return
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                self._jsonrpc_error(400, -32700, f"Parse error: {exc.msg}")
                return

            # 带 session 的请求必须是已知 session;未知就等于会话被回收/服务重启,
            # 回 404 让客户端重新 initialize(MCP 规范约定的行为)。
            sid_in = self.headers.get(SESSION_HEADER)
            if sid_in and not owner.sessions.touch(sid_in):
                self._jsonrpc_error(404, -32600,
                                    "会话不存在或已过期,请重新 initialize")
                return

            try:
                resp, new_sid = owner.handle_message(payload)
            except Exception as exc:  # noqa: BLE001 — 协议层异常不能把服务打挂
                logger.exception("处理 MCP 请求失败")
                self._jsonrpc_error(500, -32603, f"内部错误:{type(exc).__name__}: {exc}")
                return

            extra = {}
            if new_sid:
                extra[SESSION_HEADER] = new_sid
                extra[PROTOCOL_HEADER] = PROTOCOL_VERSION
            if resp is None:  # 纯通知
                self._send(202, b"", "application/json", extra)
                return
            if "text/event-stream" in (self.headers.get("Accept") or ""):
                self._send_sse(resp, extra)
                return
            self._json(200, resp, extra)

        def _send_sse(self, resp: Any, extra: dict[str, str]) -> None:
            """把响应包成 SSE:每个 JSON-RPC 消息一个 event: message。"""
            items = resp if isinstance(resp, list) else [resp]
            chunks = [
                "event: message\ndata: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                for item in items
            ]
            body = "".join(chunks).encode("utf-8")
            self._send(200, body, "text/event-stream", extra)

        def log_message(self, fmt: str, *args: Any) -> None:
            # 默认实现往 stderr 打访问日志;这里转进 logger,避免污染
            logger.debug("MCP HTTP %s - %s", self.address_string(), fmt % args)

    return Handler


def serve_cli(port: int | None = None) -> int:
    """CLI 的 `--mcp-http` 入口。port=None 时从 8765 起自动找空位。

    返回 0=正常退出;1=端口全被占用;3=MCP 通道在设置页被关闭。
    """
    from app.headless import channel_block_reason
    from app.logging_setup import setup_logging

    setup_logging()
    blocked = channel_block_reason("mcp")
    if blocked:
        logger.error("MCP 通道已关闭:%s", blocked)
        return 3

    host = DEFAULT_HOST
    candidates = [port] if port else list(range(DEFAULT_PORT, DEFAULT_PORT + 20))
    server: McpHttpServer | None = None
    for candidate in candidates:
        server = McpHttpServer(host=host, port=int(candidate))
        try:
            server.start()
            break
        except OSError as exc:
            logger.debug("端口 %d 不可用:%s", candidate, exc)
            server = None
    if server is None:
        logger.error("MCP HTTP 启动失败:%s 起连续 %d 个端口都被占用",
                     candidates[0], len(candidates))
        return 1
    url = f"http://{host}:{server.port}/mcp"
    logger.info("MCP HTTP 已就绪:%s(POST JSON-RPC;GET /healthz 探活)", url)
    # stdout 是自由的(HTTP 模式协议走 POST,不走 stdout):显式打一行端点,
    # 自动选端口/写脚本拉起进程的调用方靠这行拿到真实地址。
    out = sys.stdout
    if out is not None:
        try:
            out.write(f"MCP HTTP listening on {url}\n")
            out.flush()
        except (OSError, ValueError):
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def main(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> int:
    """独立进程入口:`python -m app.mcp_http [--port N]`。"""
    from app.logging_setup import setup_logging

    setup_logging()
    server = McpHttpServer(host=host, port=port)
    try:
        server.start()
    except OSError as exc:
        logger.error("MCP HTTP 启动失败(端口 %s 是否被占用?):%s", port, exc)
        return 1
    logger.info("MCP HTTP 已就绪:http://%s:%d/mcp", host, server.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="bili-transcriber MCP HTTP 服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=DEFAULT_HOST)
    args = ap.parse_args()
    raise SystemExit(main(port=args.port, host=args.host))
