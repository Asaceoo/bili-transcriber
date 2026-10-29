"""MCP(Model Context Protocol)服务端:stdio + 换行分隔 JSON-RPC 2.0。

供 WorkBuddy / DeepSeek-Honey 等支持 MCP 的 AI 软件直接调用本机转写能力:
  initialize -> tools/list -> tools/call
不依赖第三方 mcp SDK(避免给桌面包引入额外依赖),实现标准子集:
initialize / notifications/* / ping / tools/list / tools/call。

启动方式:
  python -m app.mcp_server            (源码)
  bili-transcriber-mcp                (安装后的控制台入口)
  bili-transcriber.exe --mcp          (桌面打包版)

注意:stdout 只允许协议数据;所有日志走 stderr/文件。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from app import APP_VERSION
from app.headless import Service, ServiceError, channel_block_reason, json_safe
from app.logging_setup import setup_logging

logger = logging.getLogger(__name__)

SERVER_NAME = "bili-transcriber"
# 兼容的 MCP 协议版本(按新到旧);客户端请求命中的首个即为协商结果
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]

INSTRUCTIONS = (
    "bili-transcriber 是本机 B站/快手音频转写工具(纯本地,GPU 优先)。"
    "典型流程:transcribe(提交,可用 wait=true 等完成) -> 若未等待则轮询 wait_jobs "
    "-> get_transcript 取 Markdown/TXT/SRT。首次转写需加载模型(1~2 分钟),"
    "可先调 prewarm_model 预热。任务产物落盘到 output_dir,返回的 paths 为绝对路径。"
    "查历史内容用 search_transcript 按关键词检索已转写文本;网络/Cookie 排障用 "
    "check_network(只读检测,不改配置)。"
)

_ID = {"type": "string", "description": "任务 ID(提交时返回的 media_id)"}

# 只声明了 tools 能力,但部分客户端会无条件拉资源/提示词清单:回 -32601 会让
# 它把整个服务标记成不可用。这里回空清单,语义等同于"没实现,但确实没有"。
EMPTY_METHOD_RESULTS: dict[str, dict] = {
    "resources/list": {"resources": []},
    "resources/templates/list": {"resourceTemplates": []},
    "prompts/list": {"prompts": []},
    "logging/setLevel": {},
}

TOOLS: list[dict] = [
    {
        "name": "transcribe",
        "description": "提交 B站/快手链接或本地音视频文件转写。立即返回任务 ID;"
                       "wait=true 时阻塞到完成/超时(默认 false,推荐配合 wait_jobs)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string",
                           "description": "视频链接(单P/多P/合集)或本地文件绝对路径"},
                "wait": {"type": "boolean", "default": False,
                         "description": "是否等待全部任务结束再返回"},
                "timeout_sec": {"type": "number", "default": 600,
                                "description": "wait=true 的最长等待秒数"},
                "output_dir": {"type": "string", "description": "覆盖产物目录(绝对路径)"},
                "engine": {"type": "string", "enum": ["whisper", "qwen3-asr", "sensevoice"],
                           "description": "转写引擎(本次进程内生效,不改用户配置)"},
                "model_size": {"type": "string", "description": "Whisper 模型,如 large-v3-turbo"},
                "device": {"type": "string", "enum": ["auto", "cuda", "cpu"]},
                "compute_type": {"type": "string"},
                "language": {"type": "string", "description": "zh / en / 空=自动检测"},
                "notes": {"type": "boolean", "description": "是否生成图文讲义(需视频源)"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "wait_jobs",
        "description": "等待一组任务进入终态(完成/失败/取消)或超时,返回各自状态与产物路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_ids": {"type": "array", "items": {"type": "string"},
                              "description": "任务 ID 列表"},
                "timeout_sec": {"type": "number", "default": 600},
            },
            "required": ["media_ids"],
        },
    },
    {
        "name": "job_status",
        "description": "查询单个任务的阶段、进度、错误与产物路径。",
        "inputSchema": {"type": "object", "properties": {"media_id": _ID},
                        "required": ["media_id"]},
    },
    {
        "name": "list_jobs",
        "description": "列出历史/进行中任务(按创建时间倒序),可按状态或关键字筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "description": "queued/downloading/converting/transcribing/"
                                          "extracting/saving/done/failed/cancelled/paused"},
                "search": {"type": "string", "description": "标题/UP主/BV 号关键字"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "get_transcript",
        "description": "读取任务产物的文本内容(Markdown/TXT/SRT/图文讲义),默认截断到 12000 字符。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_id": _ID,
                "format": {"type": "string", "enum": ["md", "txt", "srt", "notes"],
                           "default": "md"},
                "max_chars": {"type": "integer", "default": 12000,
                              "description": "最大返回字符数;<=0 表示全文"},
            },
            "required": ["media_id"],
        },
    },
    {
        "name": "search_transcript",
        "description": "在已完成任务的转写稿里按关键词全文检索,返回命中任务的 media_id、标题与上下文片段。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "检索关键词(必填)"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50,
                          "description": "最多返回命中任务数"},
                "context_chars": {"type": "integer", "default": 240, "minimum": 40,
                                  "maximum": 2000, "description": "每个命中的上下文长度"},
                "fmt": {"type": "string", "enum": ["md", "txt"], "default": "md",
                        "description": "检索的文本格式"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "check_network",
        "description": "只读检测本机网络与登录 Cookie(不修改任何配置)。quick=直连探测+代理验证;"
                       "full=再扫浏览器/cookies.txt 登录态;test=按当前配置逐平台实测。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["quick", "full", "test"],
                         "default": "quick"},
                "timeout": {"type": "number", "default": 4.0, "minimum": 1.0,
                            "maximum": 15.0, "description": "单次探测超时秒数"},
            },
        },
    },
    {
        "name": "cancel_job",
        "description": "取消排队中/进行中的任务。",
        "inputSchema": {"type": "object", "properties": {"media_id": _ID},
                        "required": ["media_id"]},
    },
    {
        "name": "prewarm_model",
        "description": "后台预加载当前设置的转写模型,减少首次转写等待(立即返回)。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_settings",
        "description": "读取当前转写设置(引擎/模型/输出目录/Cookie/代理等)。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_settings",
        "description": "修改并持久化设置(写入 data/settings.json,桌面端同样生效)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string"},
                "engine": {"type": "string", "enum": ["whisper", "qwen3-asr", "sensevoice"]},
                "model_size": {"type": "string"},
                "device": {"type": "string", "enum": ["auto", "cuda", "cpu"]},
                "compute_type": {"type": "string"},
                "language": {"type": "string"},
                "notes": {"type": "boolean"},
                "vad": {"type": "boolean"},
                "keep_audio": {"type": "boolean"},
                "cookie_file": {"type": "string"},
                "proxy": {"type": "string"},
                "workers": {"type": "integer", "minimum": 1, "maximum": 6},
            },
        },
    },
]


def client_config(executable: str | None = None, frozen: bool | None = None) -> dict:
    """生成 MCP 客户端配置片段,供设置页展示/复制。

    打包版是 `bili-transcriber.exe --mcp`;源码模式是 `python -m app.mcp_server`。
    显式传参便于测试,不传则按当前进程推断。
    """
    exe = executable or sys.executable or "bili-transcriber.exe"
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    args = ["--mcp"] if is_frozen else ["-m", "app.mcp_server"]
    return {"mcpServers": {SERVER_NAME: {"command": exe, "args": args}}}


def _ok(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _arg_str(args: dict, key: str) -> str:
    """取必填字符串参数;缺失/空值给出可读错误(而不是把 None 当 ID 去查库)。"""
    value = args.get(key)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ServiceError(f"缺少参数:{key}")
    return text


def _pick(args: dict, key: str, default: Any) -> Any:
    raw = args.get(key, default)
    return default if raw is None or raw == "" else raw


def _dataclass_public(obj: Any) -> dict:
    """dataclass → JSON 友好 dict:补上 property(readable/coverage 等派生字段)、
    Path 转字符串。AI 使用方只看 JSON,拿不到 Python 属性,漏了就是信息缺失。"""
    out = {}
    for key, value in vars(obj).items():
        out[key] = str(value) if isinstance(value, Path) else value
    for name in dir(type(obj)):
        if name.startswith("_"):
            continue
        attr = getattr(type(obj), name)
        if isinstance(attr, property) and name not in out:
            try:
                out[name] = getattr(obj, name)
            except Exception:  # noqa: BLE001 — 派生字段失败不挡主体
                pass
    return out


def _arg_int(args: dict, key: str, default: int,
             minimum: int | None = None, maximum: int | None = None) -> int:
    """取整型参数并夹紧范围;类型错误给出可读提示(AI 客户端常传字符串)。"""
    raw = _pick(args, key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"参数 {key} 需要整数,收到:{raw!r}") from exc
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _arg_num(args: dict, key: str, default: float, minimum: float = 0.0,
             maximum: float | None = None) -> float:
    raw = _pick(args, key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"参数 {key} 需要数字,收到:{raw!r}") from exc
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


class MCPServer:
    """协议处理与工具调度;Service 懒创建(initialize/tools/list 不碰数据库)。"""

    def __init__(self, service_factory=None, version: str = APP_VERSION):
        self._service: Service | None = None
        self._service_factory = service_factory or Service
        self.version = version

    @property
    def service(self) -> Service:
        if self._service is None:
            self._service = self._service_factory()
        return self._service

    # ---------- 协议 ----------

    def handle(self, msg: Any) -> dict | list[dict] | None:
        """处理单条消息;返回响应(通知返回 None)。"""
        if isinstance(msg, list):  # 兼容批量请求(旧版协议)
            collected: list[dict] = []
            for item in msg:
                resp = self.handle(item)
                if resp is None:
                    continue
                # 批量里再套批量会把 list 当响应塞进数组(协议不允许),展平
                collected.extend(resp if isinstance(resp, list) else [resp])
            return collected or None
        if not isinstance(msg, dict):
            return _err(None, -32600, "Invalid Request")
        method = msg.get("method")
        if not isinstance(method, str):
            return None  # 客户端发来的响应消息,忽略
        request_id = msg.get("id")
        is_notification = "id" not in msg
        params = msg.get("params") or {}

        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            result = self._initialize(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = self._tools_call(params)
        elif method in EMPTY_METHOD_RESULTS:
            result = EMPTY_METHOD_RESULTS[method]
        else:
            return None if is_notification else _err(request_id, -32601,
                                                     f"Method not found: {method}")
        return None if is_notification else _ok(request_id, result)

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": self.version},
            "instructions": INSTRUCTIONS,
        }

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return self._tool_error("arguments 必须是对象")
        handler = getattr(self, f"_tool_{name}", None) if isinstance(name, str) else None
        if handler is None:
            return self._tool_error(f"未知工具:{name}")
        try:
            payload = handler(args)
        except ServiceError as exc:
            return self._tool_error(str(exc))
        except Exception as exc:  # noqa: BLE001 — 工具异常回给调用方,不中断服务
            logger.exception("工具执行失败: %s", name)
            return self._tool_error(f"{type(exc).__name__}: {exc}")
        return self._tool_result(payload)

    @classmethod
    def _tool_result(cls, payload: dict) -> dict:
        safe = json_safe(payload)  # NaN/Infinity -> null,保证是合法 JSON
        return {
            "content": [{"type": "text", "text": json.dumps(safe, ensure_ascii=False,
                                                            allow_nan=False)}],
            "structuredContent": safe,
            "isError": False,
        }

    @staticmethod
    def _tool_error(message: str) -> dict:
        payload = {"ok": False, "error": message}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": payload,
            "isError": True,
        }

    # ---------- 工具实现 ----------

    def _tool_transcribe(self, args: dict) -> dict:
        wait = bool(args.get("wait", False))
        # 上限一天:AI 会把超时传成 1e9 这类"数学上对、实际上挂死"的值
        timeout = _arg_num(args, "timeout_sec", 600.0, minimum=1.0, maximum=86400.0)
        result = self.service.submit(
            _arg_str(args, "source"),
            output_dir=args.get("output_dir"),
            engine=args.get("engine"),
            model_size=args.get("model_size"),
            device=args.get("device"),
            compute_type=args.get("compute_type"),
            language=args.get("language"),
            notes=args.get("notes"),
        )
        if wait:
            ids = [j["media_id"] for j in result["jobs"]]
            result["wait"] = self.service.wait(ids, timeout)
        return result

    def _tool_wait_jobs(self, args: dict) -> dict:
        ids = args.get("media_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, (list, tuple)):
            raise ServiceError("media_ids 必须是字符串数组")
        return self.service.wait(ids, _arg_num(args, "timeout_sec", 600.0,
                                               minimum=1.0, maximum=86400.0))

    def _tool_job_status(self, args: dict) -> dict:
        return self.service.status(_arg_str(args, "media_id"))

    def _tool_list_jobs(self, args: dict) -> dict:
        jobs = self.service.list_jobs(
            status=str(args.get("status", "") or ""),
            search=str(args.get("search", "") or ""),
            limit=_arg_int(args, "limit", 20, minimum=1, maximum=200),
        )
        return {"jobs": jobs, "count": len(jobs)}

    def _tool_get_transcript(self, args: dict) -> dict:
        return self.service.transcript(
            _arg_str(args, "media_id"),
            fmt=str(args.get("format", "md") or "md"),
            max_chars=_arg_int(args, "max_chars", 12000),
        )

    def _tool_cancel_job(self, args: dict) -> dict:
        return self.service.cancel(_arg_str(args, "media_id"))

    def _tool_prewarm_model(self, args: dict) -> dict:
        return self.service.prewarm()

    def _tool_get_settings(self, args: dict) -> dict:
        return self.service.get_settings()

    def _tool_update_settings(self, args: dict) -> dict:
        return self.service.set_settings(**args)

    def _tool_search_transcript(self, args: dict) -> dict:
        return self.service.search(
            str(args.get("keyword", "") or ""),
            limit=_arg_int(args, "limit", 10, minimum=1, maximum=50),
            context_chars=_arg_int(args, "context_chars", 240, minimum=40, maximum=2000),
            fmt=str(args.get("fmt", "md") or "md"),
        )

    def _tool_check_network(self, args: dict) -> dict:
        # 延迟导入:netcheck 会拉起探测线程,不用的场景(纯转写)不该付这份成本
        from app import netcheck

        # strip 必须和 lower 一起做:AI 常把 " test " 这种带空格的值塞进来,
        # 只 lower 不 strip 会让「大写能过、带空格不能过」变成不可预测的行为。
        mode = str(args.get("mode", "quick") or "quick").strip().lower()
        if mode not in ("quick", "full", "test"):
            raise ServiceError(
                f"mode 只支持 quick/full/test,收到:{args.get('mode')!r}")
        timeout = _arg_num(args, "timeout", 4.0, minimum=1.0)
        if timeout > 15.0:
            timeout = 15.0

        if mode == "test":
            # "按当前配置实测"必须真的读用户配置:不传 browser/cookie_file/proxy
            # 就永远只能拿到"未配置 Cookie",与工具说明自相矛盾(AI 使用方最容易踩)。
            settings = self.service.get_settings()
            report = netcheck.test_configuration(
                str(settings.get("cookies_from_browser", "") or ""),
                str(settings.get("cookie_file", "") or ""),
                str(settings.get("proxy", "") or ""),
                timeout=timeout,
            )
            return {
                "mode": mode,
                "ok": report.ok,
                "summary": report.summary,
                "source": report.source,
                "warnings": report.warnings,
                "platforms": [vars(item) for item in report.items],
            }

        network = netcheck.probe_network(timeout=timeout)
        payload: dict[str, Any] = {
            "mode": mode,
            "direct": {k: vars(v) for k, v in network.direct.items()},
            "system_proxy": network.system_proxy,
            "working_proxy": network.working_proxy,
            "proxies": [vars(p) for p in network.proxies],
        }
        if mode == "full":
            from app.paths import base_dir, data_dir

            browsers = netcheck.detect_browsers()
            settings = self.service.get_settings()
            dirs = netcheck.default_cookie_dirs(base_dir(), data_dir())
            extra: list[str] = []
            cur = str(settings.get("cookie_file", "") or "").strip()
            if cur:
                extra.append(cur)  # 用户手填的路径即使不在常见目录也要查
            cookie_files = netcheck.detect_cookie_files(dirs, extra_files=extra)
            report = netcheck.build_report(
                browsers, cookie_files, network,
                current_browser=str(settings.get("cookies_from_browser", "") or ""),
                current_cookie_file=cur,
                current_proxy=str(settings.get("proxy", "") or ""),
            )
            payload.update({
                "browsers": [_dataclass_public(b) for b in report.browsers],
                "cookie_files": [_dataclass_public(f) for f in report.cookie_files],
                "suggested": {
                    "browser": report.suggested_browser,
                    "cookie_file": report.suggested_cookie_file,
                    "proxy": report.suggested_proxy,
                },
                "platform_states": {
                    k: vars(v) for k, v in report.platform_states.items()},
                "notes": report.notes,
            })
        return payload

    def close(self) -> None:
        if self._service is not None:
            self._service.close()
            self._service = None


def serve(server: MCPServer, stdin: TextIO, stdout: TextIO) -> None:
    """阻塞读 stdin 的换行分隔 JSON-RPC,逐条响应写 stdout。"""
    if stdin is None or stdout is None:
        logger.error("MCP stdio 通道缺失,拒绝启动(需由客户端提供管道)")
        return
    while True:
        try:
            raw = stdin.readline()
        except (UnicodeDecodeError, ValueError, OSError) as exc:
            # 客户端塞了非 UTF-8 字节、管道被关或句柄失效:会话到此为止,
            # 但不该把整个进程带进 traceback(客户端只会看到"服务崩了")。
            logger.warning("读取 stdin 失败,结束会话: %s", exc)
            return
        if not raw:
            return
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            resp: Any = _err(None, -32700, f"Parse error: {exc.msg}")
        else:
            resp = server.handle(msg)
        if resp is None:
            continue
        try:
            for item in (resp if isinstance(resp, list) else [resp]):
                stdout.write(json.dumps(item, ensure_ascii=False) + "\n")
            stdout.flush()
        except (BrokenPipeError, OSError, AttributeError):
            # 客户端(如 WorkBuddy)关闭管道:安静退出,不打印 traceback
            logger.info("输出管道已关闭,MCP 服务退出")
            return


def _reopen_null_streams() -> bool:
    """确保 stdin/stdout 可用;返回 False 表示启动方没提供管道(配置错误)。

    打包为窗口程序(console=False)且被无管道的进程启动时,PyInstaller 会把
    sys.stdin/stdout 置为 None。继续跑只会让 `stdout.write` 抛 AttributeError,
    表现为"服务无响应";这里显式判定、写日志并以退出码 1 结束,便于定位。
    """
    missing = [name for name in ("stdin", "stdout", "stderr")
               if getattr(sys, name) is None]
    for name in missing:
        try:
            setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w",
                                    encoding="utf-8"))
        except OSError:
            pass
    # stderr 缺失不致命(只影响日志),stdin/stdout 缺失就无法跑协议了
    fatal = [n for n in missing if n in ("stdin", "stdout")]
    if fatal:
        logger.error("MCP 启动失败:%s 不可用(客户端必须通过管道启动,"
                     "不要直接双击 exe 或脱离管道运行)", "/".join(fatal))
        return False
    return True


def _isolate_stdout() -> TextIO | None:
    """把协议通道单独摘出来,sys.stdout 改指向 stderr。

    只做 `json.dumps + write` 是不够的:任何一处遗留的 print、第三方库的
    进度输出、甚至被测代码的调试语句,只要落到 stdout 就会在 JSON-RPC 里
    插进一行垃圾,客户端要么解析失败要么直接断开。协议通道握在本地变量里,
    其余输出统统一律去 stderr,是 LSP/MCP server 的通行做法。
    """
    proto = sys.stdout
    if proto is None:
        return None
    try:
        proto.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass
    stderr = sys.stderr
    if stderr is not None:
        sys.stdout = stderr  # type: ignore[assignment]
    return proto


def main() -> int:
    setup_logging()
    if not _reopen_null_streams():
        return 1
    # 通道门禁:桌面端设置页可关闭 MCP。放在建 Service 之前(别白开数据库),
    # 且必须在接管 stdout 之前 —— 拒绝信息要写 stderr,不能污染协议通道。
    blocked = channel_block_reason("mcp")
    if blocked:
        if sys.stderr is not None:
            print(f"[bili mcp] {blocked}", file=sys.stderr)
        logger.error("MCP 通道已关闭:%s", blocked)
        return 1
    proto_out = _isolate_stdout()
    for stream in (sys.stdin, proto_out):
        try:
            # errors="replace":客户端偶尔会塞进非 UTF-8 字节(复制粘贴的乱码),
            # 严格模式会直接抛 UnicodeDecodeError 把整个会话拆掉。
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass
    logger.info("MCP stdio 服务启动(protocol=%s)", DEFAULT_PROTOCOL)
    server = MCPServer()
    try:
        serve(server, sys.stdin, proto_out)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        logger.info("MCP stdio 服务退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
