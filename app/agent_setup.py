"""各 AI 客户端的接入配置生成器。

同一个 MCP 服务,不同客户端的配置文件格式不一样 —— 这不是能靠"文档写一句
自己填"糊过去的地方,填错一个键客户端就静默不加载。这里把已知格式固化下来:

| 客户端 | 格式 | 键位 | 本机路径 |
|---|---|---|---|
| WorkBuddy | JSON | `mcpServers.<name>.{command,args}` | `~/.workbuddy/mcp.json` |
| DSH | JSON(连接条目) | `{transport,command,args}` 或 `{transport,url}` | `~/.dsh/storages/mcp_connector.json`(由界面托管) |
| Codex | TOML | `[mcp_servers.<name>] command/args` | `~/.codex/config.toml` |
| Claude Code / Desktop | JSON | `mcpServers.<name>` | `~/.claude.json` / `%APPDATA%\\Claude\\...` |
| Cursor | JSON | `mcpServers.<name>` | `~/.cursor/mcp.json` |
| VS Code | JSON | `servers.<name>` | `%APPDATA%\\Code\\User\\mcp.json` |
| Tare / 其它 | JSON | `mcpServers.<name>` | 按软件说明 |

设计原则:**只生成、不擅自改用户配置**。写入必须显式 `--write`,写之前留
`.bak`,并且只动 `bili-transcriber` 这一个条目 —— 用户的其它 MCP 服务不该被
我们的"一键配置"碰掉。
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import APP_VERSION

SERVER_KEY = "bili-transcriber"
DEFAULT_HTTP_PORT = 8765


@dataclass(frozen=True)
class ClientSpec:
    key: str
    label: str
    fmt: str                      # json | toml
    style: str = "mcp_servers"    # mcp_servers | vscode | dsh
    config_path: str = ""         # 相对用户主目录;空=需用户自己找
    http: bool = True             # 是否支持 streamable-http
    writable: bool = True         # False=配置由客户端界面托管,禁止 --write 直写
    note: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


CLIENTS: dict[str, ClientSpec] = {
    "workbuddy": ClientSpec(
        "workbuddy", "WorkBuddy", "json", "mcp_servers", ".workbuddy/mcp.json",
        note="设置页「MCP 客户端配置」里也能直接复制同一份 JSON"),
    "dsh": ClientSpec(
        "dsh", "DSH", "json", "dsh", ".dsh/storages/mcp_connector.json",
        writable=False,
        note="DSH 的连接表由界面托管(--write 会破坏它的 tables 结构),"
             "请在 DSH 里「添加 MCP 连接」后粘贴下面的条目"),
    "codex": ClientSpec(
        "codex", "Codex CLI/IDE", "toml", "mcp_servers", ".codex/config.toml",
        note="追加到 config.toml 末尾后重启 Codex;`startup_timeout_sec` 给足首次加载时间"),
    "claude": ClientSpec(
        "claude", "Claude Code / Desktop", "json", "mcp_servers", ".claude.json",
        aliases=("claude-code", "claude-desktop"),
        note="Claude Desktop 的用户级配置在 %APPDATA%\\Claude\\claude_desktop_config.json"),
    "cursor": ClientSpec(
        "cursor", "Cursor", "json", "mcp_servers", ".cursor/mcp.json"),
    "vscode": ClientSpec(
        "vscode", "VS Code (Copilot)", "json", "vscode", "", http=False,
        aliases=("code",),
        note="VS Code 用 `servers` 键;也可放到工作区 .vscode/mcp.json"),
    "tare": ClientSpec(
        "tare", "Tare", "json", "mcp_servers", ".tare/mcp.json", http=False,
        note="Tare 各版本配置位置可能不同,把下面的 JSON 粘进它的 MCP 服务列表即可"),
    "generic": ClientSpec(
        "generic", "通用 / 其它客户端", "json", "mcp_servers", "",
        note="任何认 `mcpServers` 的客户端都能用这一段"),
}


def resolve_client(name: str) -> ClientSpec:
    """按 key 或别名找客户端;找不到给出可选清单(比 KeyError 友好)。"""
    key = (name or "").strip().lower()
    if key in CLIENTS:
        return CLIENTS[key]
    for spec in CLIENTS.values():
        if key in spec.aliases:
            return spec
    options = "、".join(CLIENTS)
    raise KeyError(f"未知客户端:{name}(可选:{options})")


def default_config_path(spec: ClientSpec, home: Path | None = None) -> Path | None:
    if not spec.config_path:
        return None
    base = home or Path.home()
    return base / spec.config_path


def resolve_executable(exe: str | None = None, frozen: bool | None = None) -> tuple[str, list[str]]:
    """算出 stdio 启动命令。

    打包版:`<exe> --mcp`;源码版:`<python> -m app.mcp_server`。
    只有冻结进程能靠 `--mcp` 参数直接进 MCP —— 源码下 `python -m app.mcp_server`
    才是入口,写成 `python --mcp` 客户端会直接报"未知参数"。
    """
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return exe or sys.executable or "bili-transcriber.exe", ["--mcp"]
    return exe or sys.executable or "python", ["-m", "app.mcp_server"]


def http_url(port: int = DEFAULT_HTTP_PORT, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}/mcp"


def server_entry(transport: str = "stdio", *, exe: str | None = None,
                 args: list[str] | None = None, port: int = DEFAULT_HTTP_PORT,
                 frozen: bool | None = None) -> dict[str, Any]:
    """客户端配置里的那个 `bili-transcriber` 条目。"""
    if transport == "http":
        return {"type": "http", "url": http_url(port)}
    cmd, cmd_args = resolve_executable(exe, frozen)
    return {"command": cmd, "args": list(args if args is not None else cmd_args)}


def render_json(spec: ClientSpec, entry: dict[str, Any]) -> str:
    """JSON 客户端的完整片段(带外层键)。"""
    if spec.style == "dsh":
        return json.dumps(_dsh_entry(entry), ensure_ascii=False, indent=2)
    key = "servers" if spec.style == "vscode" else "mcpServers"
    payload: dict[str, Any] = {key: {SERVER_KEY: entry}}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _dsh_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """DSH 的连接条目:transport 二选一,其余字段按它自己的 schema 给默认值。"""
    if "url" in entry:
        return {
            "key": SERVER_KEY, "connectorId": SERVER_KEY, "kind": "manual",
            "name": "Bili Transcriber", "serverKey": SERVER_KEY,
            "serverName": SERVER_KEY, "transport": "streamable-http",
            "url": entry["url"],
            "headers": {"Accept": "application/json, text/event-stream"},
            "enabled": True,
        }
    return {
        "key": SERVER_KEY, "connectorId": SERVER_KEY, "kind": "manual",
        "name": "Bili Transcriber", "serverKey": SERVER_KEY,
        "serverName": SERVER_KEY, "transport": "stdio",
        "command": entry["command"], "args": entry.get("args", []),
        "env": {}, "cwd": "", "enabled": True,
    }


def _toml_str(value: str) -> str:
    """TOML 字符串:Windows 路径里全是反斜杠,必须用单引号字面量原样保留。"""
    if "'" not in value:
        return f"'{value}'"
    return json.dumps(value)  # 含单引号时退回双引号并转义


def render_toml(spec: ClientSpec, entry: dict[str, Any]) -> str:
    lines = [f"[mcp_servers.{SERVER_KEY}]"]
    if "url" in entry:
        lines.append(f"url = {_toml_str(entry['url'])}")
    else:
        lines.append(f"command = {_toml_str(entry['command'])}")
        args = entry.get("args") or []
        rendered = ", ".join(_toml_str(str(a)) for a in args)
        lines.append(f"args = [{rendered}]")
    # 只写文档化的键:startup_timeout_sec 给首次模型加载(1~2 分钟)留时间。
    # 别加想当然的键 —— Codex 对未知键报解析错,整个 config.toml 起不来,
    # 用户其它 provider 配置跟着陪葬。超时建议写在注释里。
    lines.append("startup_timeout_sec = 300")
    lines.append("# 长转写建议在客户端侧放宽工具超时(如 Codex 的 tool 超时配置)")
    return "\n".join(lines) + "\n"


def render_cli(*, exe: str | None = None, frozen: bool | None = None) -> str:
    """只能执行 shell 的 Agent 用的命令片段(POSIX + PowerShell 两版)。"""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        # 冻结版入口是 main.py 的 GUI 分发器:必须显式 --cli 才进 CLI
        # (源码版 python -m app.cli 本身就是 CLI,不带 --cli)。
        # v0.1.44 前曾误删 —— 真机冒烟 `bili-transcriber.exe --doctor`
        # 直接起了 GUI,才还原真相。
        base = f'"{exe or "bili-transcriber.exe"}" --cli'
    else:
        base = "python -m app.cli"
    return (
        "# POSIX / Git Bash\n"
        f'{base} "https://www.bilibili.com/video/BV1xx411c7mD" --json\n'
        f'{base} --list --json --limit 5\n'
        f'{base} --status BV1xx411c7mD_p1 --json\n'
        f'{base} --result BV1xx411c7mD_p1 --format md --max-chars 0\n'
        f'{base} --doctor --json\n'
        "\n"
        "# PowerShell(注意 `&` 调用运算符)\n"
        f'{base.replace("python -m app.cli", "python -m app.cli")} '
        '"https://www.bilibili.com/video/BV1xx411c7mD" --json\n'
    )


def render(spec: ClientSpec, *, transport: str = "stdio", exe: str | None = None,
           args: list[str] | None = None, port: int = DEFAULT_HTTP_PORT,
           frozen: bool | None = None) -> str:
    """渲染某个客户端在某传输方式下的配置片段。"""
    if transport == "http" and not spec.http:
        raise ValueError(f"{spec.label} 这一版建议用 stdio;"
                         f"若它已支持 HTTP,把 url 填进同名条目即可")
    entry = server_entry(transport, exe=exe, args=args, port=port, frozen=frozen)
    if spec.fmt == "toml":
        return render_toml(spec, entry)
    return render_json(spec, entry)


# ---------------------------------------------------------------- 落地到磁盘


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = int(path.stat().st_mtime)
    bak = path.with_suffix(path.suffix + f".bak-{stamp}")
    if not bak.exists():
        shutil.copy2(path, bak)
    return bak


def merge_json(existing_text: str, spec: ClientSpec, entry: dict[str, Any]) -> str:
    """把条目并进现有 JSON;保留用户的其它服务与注释掉的内容。"""
    key = "servers" if spec.style == "vscode" else "mcpServers"
    data: dict[str, Any] = {}
    if existing_text.strip():
        data = json.loads(existing_text)
        if not isinstance(data, dict):
            raise ValueError("现有配置文件顶层不是对象,拒绝覆盖")
    bucket = data.get(key)
    if bucket is None:
        bucket = {}
        data[key] = bucket
    if not isinstance(bucket, dict):
        raise ValueError(f"现有配置里的 {key} 不是对象,拒绝覆盖")
    bucket[SERVER_KEY] = entry
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


_TOML_HEADER = f"[mcp_servers.{SERVER_KEY}]"


def merge_toml(existing_text: str, block: str) -> str:
    """替换同名 section;没有就追加。

    只认得 `[mcp_servers.bili-transcriber]` 这一段(到下一个行首 `[` 或文件尾),
    其它内容原样保留 —— 用户的 config.toml 里有 provider/model 等关键配置,
    整体重写等于删库。
    """
    lines = existing_text.splitlines()
    out: list[str] = []
    skipping = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        head = stripped.split("#", 1)[0].strip()  # section 头行没有字符串字面量,按 # 截断安全
        is_any_header = head.startswith("[") and head.endswith("]")
        if not skipping and head == _TOML_HEADER:
            skipping = True
            if not replaced:
                out.extend(block.rstrip("\n").splitlines())
                replaced = True
            continue
        if skipping:
            if is_any_header:
                skipping = False  # 进入下一个 section(含带注释的头),开始照抄
            else:
                continue
        out.append(line)
    text = "\n".join(out).rstrip("\n")
    if not replaced:
        text = (text + "\n\n" if text else "") + block.rstrip("\n")
    return text + "\n"


def write_config(spec: ClientSpec, snippet: str, *, path: Path | None = None,
                 home: Path | None = None) -> dict[str, Any]:
    """把片段写进配置文件(先备份)。返回结果描述,便于 CLI 打印。"""
    if not spec.writable:
        raise ValueError(
            f"{spec.label} 的配置由客户端界面托管,不支持 --write 直写"
            "(写坏它的内部结构会导致客户端起不来);请按说明在客户端里手动添加")
    target = path or default_config_path(spec, home)
    if target is None:
        raise ValueError(f"{spec.label} 没有默认配置路径,请手动粘贴")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if spec.fmt == "toml":
        merged = merge_toml(existing, snippet)
    else:
        entry = _entry_from_snippet(spec, snippet)
        try:
            merged = merge_json(existing, spec, entry)
        except json.JSONDecodeError as exc:
            # Cursor/VS Code 的 mcp.json 常是 JSONC(带 // 注释),json 解析会炸;
            # 报"往哪粘"比裸 traceback 可行动 —— 原文件一个字节都没动。
            raise ValueError(
                f"{target} 不是纯 JSON(常见原因:文件里有 // 或 /* 注释),"
                f"为避免改坏你的注释,请手动粘贴下面的片段:\n{snippet}") from exc
    backup = _backup(target)
    target.write_text(merged, encoding="utf-8")
    return {"ok": True, "client": spec.key, "path": str(target),
            "backup": str(backup) if backup else "", "created": not bool(existing)}


def _entry_from_snippet(spec: ClientSpec, snippet: str) -> dict[str, Any]:
    """从渲染出的片段里取回条目,保证写入与展示完全一致(不重复构造)。"""
    data = json.loads(snippet)
    if spec.style == "dsh":
        return {k: v for k, v in data.items()
                if k in ("transport", "command", "args", "url", "headers")}
    key = "servers" if spec.style == "vscode" else "mcpServers"
    entry = (data.get(key) or {}).get(SERVER_KEY)
    if not isinstance(entry, dict):
        raise ValueError("片段缺少 bili-transcriber 条目")
    return entry


# ---------------------------------------------------------------- 本机探测


def detect_clients(home: Path | None = None) -> list[dict[str, Any]]:
    """本机装了哪些客户端、配置在哪、是否已经注册过本服务。

    只读目录与配置文本,不写。`installed` 判断依据是配置目录存在 ——
    比去注册表/安装目录找 exe 稳,也跨平台。
    """
    base = home or Path.home()
    result: list[dict[str, Any]] = []
    for spec in CLIENTS.values():
        path = default_config_path(spec, base)
        if path is None:
            result.append({"key": spec.key, "label": spec.label, "installed": False,
                           "path": "", "registered": False, "note": spec.note})
            continue
        installed = path.parent.exists()
        registered = False
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                registered = SERVER_KEY in text
            except OSError:
                registered = False
        result.append({"key": spec.key, "label": spec.label, "installed": installed,
                       "path": str(path), "registered": registered, "note": spec.note})
    return result


def summary(entry: dict[str, Any]) -> str:
    """一行描述,用于 CLI 人类可读输出。"""
    state = "已注册" if entry.get("registered") else ("未注册" if entry.get("installed")
                                                     else "未检测到")
    where = entry.get("path") or "(无默认路径)"
    return f"{entry['label']:<22} {state:<8} {where}"


def doctor(exe: str | None = None, frozen: bool | None = None,
           port: int = DEFAULT_HTTP_PORT) -> dict[str, Any]:
    """自检:AI 接入前最常错的几件事,一次说清。"""
    from app.paths import data_dir

    cmd, args = resolve_executable(exe, frozen)
    clients = detect_clients()
    issues: list[str] = []
    if not any(c["installed"] for c in clients):
        issues.append("没有检测到任何已知 AI 客户端的配置目录")
    # data_dir() 同时覆盖源码版(项目/data)与打包版(AppData/Local/Bili Note),
    # 写死任何一个都会在另一种形态下误报"没初始化"
    settings = data_dir() / "settings.json"
    if not settings.exists():
        issues.append("还没生成 settings.json:先启动一次桌面端或跑任意一次 CLI")
    return {
        "ok": not issues,
        "version": APP_VERSION,
        "python": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)) if frozen is None else frozen,
        "stdio": {"command": cmd, "args": args},
        "http": {"url": http_url(port)},
        "clients": clients,
        "settings_path": str(settings),
        "issues": issues,
    }
