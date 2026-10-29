"""agent_setup 测试:客户端解析、配置渲染、合并写入、本机探测。"""

import json
import tempfile
from pathlib import Path

import pytest

from app import agent_setup as A
from app.agent_setup import (
    CLIENTS,
    SERVER_KEY,
    ClientSpec,
    detect_clients,
    http_url,
    merge_json,
    merge_toml,
    render,
    render_toml,
    resolve_client,
    resolve_executable,
    server_entry,
    summary,
    write_config,
)


# ---------------------------------------------------------------- 客户端解析


def test_resolve_client_by_key_and_alias():
    assert resolve_client("workbuddy").key == "workbuddy"
    assert resolve_client("DSH").key == "dsh"
    assert resolve_client("claude-code").key == "claude"
    assert resolve_client("code").key == "vscode"


def test_resolve_client_unknown_lists_options():
    with pytest.raises(KeyError) as ei:
        resolve_client("nope")
    assert "workbuddy" in str(ei.value)


# ---------------------------------------------------------------- 条目与渲染


def test_server_entry_stdio_source_mode():
    entry = server_entry("stdio", frozen=False)
    assert entry["command"].endswith("python.exe") or entry["command"] == "python"
    assert entry["args"] == ["-m", "app.mcp_server"]


def test_server_entry_stdio_frozen_mode():
    entry = server_entry("stdio", exe="C:/x/bili.exe", frozen=True)
    assert entry == {"command": "C:/x/bili.exe", "args": ["--mcp"]}


def test_server_entry_http():
    assert server_entry("http", port=9999) == {
        "type": "http", "url": "http://127.0.0.1:9999/mcp"}
    assert http_url(8765) == "http://127.0.0.1:8765/mcp"


def test_render_workbuddy_json():
    text = render(CLIENTS["workbuddy"], frozen=False)
    data = json.loads(text)
    entry = data["mcpServers"][SERVER_KEY]
    assert entry["args"] == ["-m", "app.mcp_server"]


def test_render_vscode_uses_servers_key():
    text = render(CLIENTS["vscode"], frozen=False)
    data = json.loads(text)
    assert "mcpServers" not in data
    assert SERVER_KEY in data["servers"]


def test_render_dsh_stdio_and_http():
    stdio = json.loads(render(CLIENTS["dsh"], frozen=False))
    assert stdio["transport"] == "stdio"
    assert stdio["command"] and stdio["args"] == ["-m", "app.mcp_server"]
    assert stdio["enabled"] is True

    http = json.loads(render(CLIENTS["dsh"], transport="http", port=9000))
    assert http["transport"] == "streamable-http"
    assert http["url"] == "http://127.0.0.1:9000/mcp"
    assert "Accept" in http["headers"]


def test_render_codex_toml():
    text = render(CLIENTS["codex"], frozen=False)
    assert f"[mcp_servers.{SERVER_KEY}]" in text
    assert "command = " in text
    assert "args = ['-m', 'app.mcp_server']" in text
    assert "startup_timeout_sec = 300" in text


def test_render_codex_http_toml():
    text = render(CLIENTS["codex"], transport="http", port=8765)
    assert "url = 'http://127.0.0.1:8765/mcp'" in text
    assert "command" not in text


def test_render_rejects_http_for_clients_without_support():
    with pytest.raises(ValueError):
        render(CLIENTS["vscode"], transport="http")
    with pytest.raises(ValueError):
        render(CLIENTS["tare"], transport="http")


def test_render_toml_quotes_windows_paths():
    text = render_toml(CLIENTS["codex"],
                       {"command": "C:\\Program Files\\bili\\x.exe", "args": ["--mcp"]})
    assert "'C:\\Program Files\\bili\\x.exe'" in text  # 单引号字面量


def test_toml_str_escapes_single_quotes():
    assert A._toml_str("it's") == json.dumps("it's")


# ---------------------------------------------------------------- 合并写入


def test_merge_json_preserves_other_servers():
    existing = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    spec = CLIENTS["workbuddy"]
    entry = {"command": "p", "args": ["-m", "app.mcp_server"]}
    merged = json.loads(merge_json(existing, spec, entry))
    assert merged["mcpServers"]["other"] == {"command": "x"}  # 别人的没被动
    assert merged["mcpServers"][SERVER_KEY]["command"] == "p"


def test_merge_json_rejects_non_object_root():
    with pytest.raises(ValueError):
        merge_json("[]", CLIENTS["workbuddy"], {"command": "p"})


def test_merge_json_rejects_non_object_bucket():
    with pytest.raises(ValueError):
        merge_json('{"mcpServers": 3}', CLIENTS["workbuddy"], {"command": "p"})


def test_merge_toml_replaces_same_section_keeps_others():
    existing = "\n".join([
        "[providers.openai]", "model = 'gpt'", "",
        f"[mcp_servers.{SERVER_KEY}]", "command = 'old'", "args = ['x']", "",
        "[providers.other]", "key = 'keep'",
    ])
    block = "[mcp_servers.bili-transcriber]\ncommand = 'new'\nargs = []"
    out = merge_toml(existing, block)
    assert "[providers.openai]" in out and "model = 'gpt'" in out
    assert "[providers.other]" in out and "key = 'keep'" in out
    assert "command = 'new'" in out
    assert "old" not in out  # 旧条目被整段替换


def test_merge_toml_appends_when_absent():
    out = merge_toml("[a.b]\nx = 1\n", "[mcp_servers.bili-transcriber]\ncommand = 'n'")
    assert out.index("[a.b]") < out.index("bili-transcriber")
    assert "command = 'n'" in out


def test_write_config_backs_up_and_creates(tmp_path):
    spec = ClientSpec("t", "T", "json", "mcp_servers", "cfg/mcp.json")
    target = tmp_path / "cfg" / "mcp.json"
    target.parent.mkdir()
    target.write_text('{"mcpServers": {"keep": 1}}', encoding="utf-8")

    snippet = json.dumps({"mcpServers": {SERVER_KEY: {"command": "p", "args": []}}})
    result = write_config(spec, snippet, path=target)
    assert result["ok"] is True and result["created"] is False
    assert result["backup"]  # 有备份
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["mcpServers"]["keep"] == 1
    assert SERVER_KEY in merged["mcpServers"]

    # 新文件 → created=True, 无备份
    result2 = write_config(spec, snippet, path=tmp_path / "cfg2" / "mcp.json")
    assert result2["created"] is True and result2["backup"] == ""


def test_write_config_rejects_spec_without_path():
    spec = ClientSpec("t", "T", "json", "mcp_servers", "")
    with pytest.raises(ValueError):
        write_config(spec, "{}", path=None, home=None)


# ---------------------------------------------------------------- 探测与自检


def test_detect_clients_reports_registered(tmp_path):
    home = tmp_path
    (home / ".workbuddy").mkdir()
    (home / ".workbuddy" / "mcp.json").write_text(
        json.dumps({"mcpServers": {SERVER_KEY: {}}}), encoding="utf-8")
    rows = detect_clients(home=home)
    by_key = {r["key"]: r for r in rows}
    assert by_key["workbuddy"]["installed"] is True
    assert by_key["workbuddy"]["registered"] is True
    assert by_key["cursor"]["installed"] is False
    assert by_key["vscode"]["installed"] is False  # 无默认路径


def test_summary_line():
    line = summary({"label": "X", "registered": True, "installed": True, "path": "/a"})
    assert "已注册" in line and "X" in line and "/a" in line
    line2 = summary({"label": "Y", "registered": False, "installed": False, "path": ""})
    assert "未检测到" in line2 and "(无默认路径)" in line2


def test_doctor_flags_missing_settings(tmp_path, monkeypatch):
    report = A.doctor(frozen=True, exe="bili.exe", port=9000)
    assert report["version"]
    assert report["stdio"] == {"command": "bili.exe", "args": ["--mcp"]}
    assert report["http"]["url"] == "http://127.0.0.1:9000/mcp"
    assert isinstance(report["issues"], list)


def test_resolve_executable_source_vs_frozen():
    cmd, args = resolve_executable(frozen=False)
    assert args == ["-m", "app.mcp_server"]
    cmd, args = resolve_executable(exe="x.exe", frozen=True)
    assert (cmd, args) == ("x.exe", ["--mcp"])


def test_dsh_write_is_rejected(tmp_path):
    """DSH 的连接表由界面托管:--write 直写会破坏 tables 结构,必须拒绝。"""
    spec = CLIENTS["dsh"]
    assert spec.writable is False
    with pytest.raises(ValueError) as ei:
        write_config(spec, "{}", path=tmp_path / "mcp_connector.json")
    assert "界面托管" in str(ei.value)


def test_codex_toml_has_no_invented_keys():
    """只写文档化的键:Codex 对未知键解析失败,整个 config.toml 起不来。"""
    text = render(CLIENTS["codex"], frozen=False)
    assert "startup_timeout_sec = 300" in text
    assert "tool_timeout_sec =" not in text  # 想当然的键不允许落进配置


def test_merge_toml_replaces_section_with_trailing_comment():
    """回归:带行尾注释的旧 section 曾被漏匹配 → TOML 重复 table,Codex 全崩。"""
    from app.agent_setup import merge_toml, render_toml, CLIENTS
    import tomllib
    block = render_toml(CLIENTS["codex"], {"command": "X://new//b.exe", "args": ["--mcp"]})
    old = ("[mcp_servers.other]\ncommand = 'keep-me'\n\n"
           "[mcp_servers.bili-transcriber] # 用户注释\ncommand = 'old'\n\n"
           "[mcp_servers.third] # 别的注释\nmodel = 'keep-me-too'\n")
    merged = merge_toml(old, block)
    data = tomllib.loads(merged)  # 解析失败会抛异常
    assert data["mcp_servers"]["bili-transcriber"]["command"] == "X://new//b.exe"
    assert data["mcp_servers"]["other"]["command"] == "keep-me"
    assert data["mcp_servers"]["third"]["model"] == "keep-me-too"
    assert merged.count("[mcp_servers.bili-transcriber]") == 1


def test_write_config_jsonc_gives_actionable_error():
    """回归:JSONC(带注释)配置曾泄漏 JSONDecodeError traceback。"""
    from app.agent_setup import write_config, render, resolve_client
    spec = resolve_client("cursor")
    snippet = render(spec)
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "mcp.json"
        target.write_text("{\n  // 我的注释\n  \"mcpServers\": {}\n}\n", encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        with pytest.raises(ValueError) as ei:
            write_config(spec, snippet, path=target)
        assert "注释" in str(ei.value) and "mcpServers" in str(ei.value)
        assert target.read_text(encoding="utf-8") == before  # 原文件未动


def test_render_cli_frozen_has_no_cli_flag():
    """回归:frozen 片段曾生成非法 --cli 参数,客户端复制即报错。"""
    from app.agent_setup import render_cli
    text = render_cli(frozen=True, exe="bili-transcriber.exe")
    assert "--cli" not in text
    assert "bili-transcriber.exe" in text
