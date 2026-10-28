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
from typing import Any, TextIO

from app import APP_VERSION
from app.headless import Service, ServiceError, json_safe
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
)

_ID = {"type": "string", "description": "任务 ID(提交时返回的 media_id)"}

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


def _arg_num(args: dict, key: str, default: float, minimum: float = 0.0) -> float:
    raw = _pick(args, key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"参数 {key} 需要数字,收到:{raw!r}") from exc
    return max(minimum, value)


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
        timeout = _arg_num(args, "timeout_sec", 600.0, minimum=1.0)
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
        return self.service.wait(ids, _arg_num(args, "timeout_sec", 600.0, minimum=1.0))

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

    def close(self) -> None:
        if self._service is not None:
            self._service.close()
            self._service = None


def serve(server: MCPServer, stdin: TextIO, stdout: TextIO) -> None:
    """阻塞读 stdin 的换行分隔 JSON-RPC,逐条响应写 stdout。"""
    if stdin is None or stdout is None:
        logger.error("MCP stdio 通道缺失,拒绝启动(需由客户端提供管道)")
        return
    for raw in iter(stdin.readline, ""):
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
    missing = [name for name in ("stdin", "stdout") if getattr(sys, name) is None]
    for name in missing:
        try:
            setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w",
                                    encoding="utf-8"))
        except OSError:
            pass
    if missing:
        logger.error("MCP 启动失败:%s 不可用(客户端必须通过管道启动,"
                     "不要直接双击 exe 或脱离管道运行)", "/".join(missing))
        return False
    return True


def main() -> int:
    setup_logging()
    if not _reopen_null_streams():
        return 1
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass
    logger.info("MCP stdio 服务启动(protocol=%s)", DEFAULT_PROTOCOL)
    server = MCPServer()
    try:
        serve(server, sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        logger.info("MCP stdio 服务退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
