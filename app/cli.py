"""命令行入口:供脚本/自动化/AI Agent 在无界面下调用转写能力。

用法示例:
  bili-transcriber-cli "https://www.bilibili.com/video/BVxxxx" --json
  bili-transcriber-cli .\\talk.mp4 --engine sensevoice --json
  bili-transcriber-cli --list --json
  bili-transcriber-cli --status BV1xx_p1 --json
  bili-transcriber-cli --result BV1xx_p1 --format md --max-chars 0
  bili-transcriber-cli --set engine=whisper --set model_size=large-v3-turbo
  bili-transcriber-cli --mcp        # 以 MCP stdio 服务运行

退出码:0 成功;1 任务失败/执行错误;2 参数错误;3 通道已在设置页关闭。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from app import APP_VERSION
from app.headless import (
    Service, ServiceError, channel_block_reason, json_safe,
)
from app.logging_setup import setup_logging

SERVICE_FACTORY = Service  # 测试可替换
EXIT_CHANNEL_DISABLED = 3  # 设置页关闭了 CLI 通道;与"任务失败(1)/参数错(2)"区分开


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bili-transcriber-cli",
        description="B站/快手音频本地转写 —— 无界面命令行",
    )
    p.add_argument("source", nargs="?", help="视频链接或本地音视频文件路径")
    p.add_argument("--version", action="store_true", help="显示版本")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--no-wait", action="store_true",
                   help="只提交不等待(任务留在队列,下次启动桌面端会自动续跑)")
    p.add_argument("--timeout", type=float, default=600, help="等待上限秒数(默认 600)")
    p.add_argument("--output-dir", help="覆盖输出目录")
    p.add_argument("--engine", choices=["whisper", "qwen3-asr", "sensevoice"])
    p.add_argument("--model", dest="model_size", help="Whisper 模型(如 large-v3-turbo)")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"])
    p.add_argument("--compute-type", dest="compute_type")
    p.add_argument("--language", help="zh / en / 空=自动")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--notes", dest="notes", action="store_true", default=None,
                       help="生成图文讲义")
    group.add_argument("--no-notes", dest="notes", action="store_false",
                       help="不生成图文讲义")

    p.add_argument("--status", metavar="MEDIA_ID", help="查询单个任务")
    p.add_argument("--list", action="store_true", help="列出任务")
    p.add_argument("--limit", type=int, default=20, help="--list 返回条数(默认 20)")
    p.add_argument("--status-filter", default="", help="--list 按状态过滤")
    p.add_argument("--search", default="", help="--list 关键字过滤")
    p.add_argument("--result", metavar="MEDIA_ID", help="读取任务产物文本")
    p.add_argument("--format", default="md", choices=["md", "txt", "srt", "notes"])
    p.add_argument("--max-chars", type=int, default=12000,
                   help="--result 最大字符数(<=0 全文,默认 12000)")
    p.add_argument("--settings", action="store_true", help="打印当前设置")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=[],
                   help="修改并持久化设置,可重复")
    p.add_argument("--cancel", metavar="MEDIA_ID", help="取消任务")
    p.add_argument("--prewarm", action="store_true", help="后台预加载模型")
    p.add_argument("--mcp", action="store_true", help="以 MCP stdio 服务运行")
    return p


def _stream_encoding() -> str:
    return getattr(sys.stdout, "encoding", None) or "utf-8"


def _dump_json(payload: Any, indent: int | None = 2) -> str:
    """序列化并确保目标流编码装得下。

    打包版是窗口程序(console=False),PyInstaller 复用父进程管道时 stdout 的
    编码取自系统 ANSI 代码页(中文 Windows = GBK)。B站标题含 emoji 时
    `print` 会抛 UnicodeEncodeError,任务白跑完却拿不到输出。降级为
    ensure_ascii=True 得到纯 ASCII 的合法 JSON,任何代码页都能写。
    """
    safe = json_safe(payload)
    text = json.dumps(safe, ensure_ascii=False, indent=indent, allow_nan=False)
    try:
        text.encode(_stream_encoding())
    except (UnicodeEncodeError, LookupError):
        text = json.dumps(safe, ensure_ascii=True, indent=indent, allow_nan=False)
    return text


def _write(text: str) -> None:
    """写一行到 stdout;流缺失(无控制台/下游已关闭)时静默跳过。

    `bili-transcriber-cli ... | head -1` 这类管道用法会让 stdout 提前关闭,
    Python 默认抛 BrokenPipeError 打出 traceback —— 对命令行工具是噪音,
    且退出码会变成 1,脚本会误判成"任务失败"。
    """
    out = sys.stdout
    if out is None:
        return
    try:
        out.write(text + "\n")
        out.flush()
    except UnicodeEncodeError:
        out.write(text.encode(_stream_encoding(), "backslashreplace").decode(
            _stream_encoding(), "replace") + "\n")
        out.flush()
    except (BrokenPipeError, OSError, ValueError):
        # 下游已关闭 / 句柄失效:把 fd 重定向到 devnull,避免解释器退出时
        # 再次 flush 报 "Exception ignored in: <_io.TextIOWrapper>"
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            sys.stdout = None  # type: ignore[assignment]
    except AttributeError:
        pass


def _emit(payload: dict, as_json: bool, human: str = "") -> None:
    if as_json:
        _write(_dump_json(payload))
    else:
        _write(human or _dump_json(payload, indent=None))


def _parse_sets(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise ServiceError(f"--set 需要 KEY=VALUE 形式:{item}")
        key, value = item.split("=", 1)
        key, value = key.strip(), value.strip()
        if key in {"notes", "vad", "keep_audio"}:
            out[key] = value.lower() in ("1", "true", "yes", "on")
        elif key == "workers":
            try:
                out[key] = int(value)
            except ValueError as exc:
                raise ServiceError(f"--set workers 需要整数:{value}") from exc
        else:
            out[key] = value
    return out


def _job_line(job: dict) -> str:
    name = job.get("title") or job.get("media_id")
    line = f"[{job.get('status')}] {name} ({job.get('media_id')})"
    if job.get("error"):
        line += f" 错误:{job['error']}"
    paths = job.get("paths") or {}
    if paths.get("md"):
        line += f"\n  → {paths['md']}"
    return line


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.version:
        # --version 不受通道开关影响:它只是一行版本号,留着便于排查
        _write(APP_VERSION)
        return 0
    if args.mcp:
        from app.mcp_server import main as mcp_main
        return mcp_main()
    # 通道门禁:放在构造 Service 之前 —— 关闭时不该去开数据库/建 pipeline
    blocked = channel_block_reason("cli")
    if blocked:
        _emit({"ok": False, "error": blocked, "channel": "cli", "enabled": False},
              args.json, f"错误:{blocked}")
        return EXIT_CHANNEL_DISABLED

    setup_logging()
    service: Service | None = None
    try:
        service = SERVICE_FACTORY()
        # ---- 只读/配置类子命令 ----
        if args.settings or args.set:
            if args.set:
                payload = service.set_settings(**_parse_sets(args.set))
                _emit(payload, args.json, "设置已保存")
                return 0
            payload = service.get_settings()
            _emit(payload, args.json,
                  "\n".join(f"{k} = {v}" for k, v in payload.items()))
            return 0
        if args.prewarm:
            payload = service.prewarm(wait=True)
            state = "已加载" if payload.get("loaded") else "未完成"
            _emit(payload, args.json,
                  f"模型{state}:{payload['engine']}/{payload['model']}"
                  f"({payload.get('waited_sec', 0)}s)\n"
                  "注:预加载只对当前进程有效,常驻进程(MCP/桌面端)才有加速收益。")
            return 0 if payload.get("loaded") else 1
        if args.list:
            jobs = service.list_jobs(status=args.status_filter, search=args.search,
                                     limit=args.limit)
            _emit({"jobs": jobs, "count": len(jobs)}, args.json,
                  "\n".join(_job_line(j) for j in jobs) or "(无任务)")
            return 0
        if args.status:
            job = service.status(args.status)
            _emit(job, args.json, _job_line(job))
            return 0
        if args.cancel:
            job = service.cancel(args.cancel)
            _emit(job, args.json, _job_line(job))
            return 0
        if args.result:
            payload = service.transcript(args.result, fmt=args.format,
                                         max_chars=args.max_chars)
            _write(_dump_json(payload) if args.json else payload["content"])
            return 0
        if not args.source:
            if sys.stderr is not None:
                _build_parser().print_help(sys.stderr)
            return 2
        # ---- 转写 ----
        payload = service.submit(
            args.source, output_dir=args.output_dir, engine=args.engine,
            model_size=args.model_size, device=args.device,
            compute_type=args.compute_type, language=args.language,
            notes=args.notes,
        )
        if not args.no_wait:
            ids = [j["media_id"] for j in payload["jobs"]]
            wait = service.wait(ids, timeout_sec=args.timeout)
            payload["wait"] = wait
        failed = [j for j in payload["jobs"] if j["status"] in ("failed", "cancelled")]
        human = "\n".join(
            f"[{j['status']}] {j['title']} ({j['media_id']})" for j in payload["jobs"]
        )
        if payload.get("wait"):
            human += "\n" + "\n".join(_job_line(j) for j in payload["wait"]["jobs"])
            failed = [j for j in payload["wait"]["jobs"]
                      if j["status"] in ("failed", "cancelled")]
            timed_out = payload["wait"]["timed_out"]
        else:
            timed_out = False
        _emit(payload, args.json, human)
        return 1 if failed or timed_out else 0
    except ServiceError as exc:
        _emit({"ok": False, "error": str(exc)}, args.json, f"错误:{exc}")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
