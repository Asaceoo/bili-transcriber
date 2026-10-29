# CLAUDE.md — bili-transcriber AI 协作规则

B站音频本地转写桌面工具：yt-dlp 下载 → FFmpeg 转码 → 三引擎本地 GPU 转写 → SRT/TXT/Markdown。纯本地、数据不上传。当前 v0.1.44，测试 29 文件 / 525 passed。

## 速查命令

| 任务 | 命令 |
|---|---|
| 跑全量测试 | `.venv/Scripts/python.exe -m pytest tests/ -q` |
| 源码起 CLI | `.venv/Scripts/python.exe -m app.cli <参数>`（**不需要** `--cli`） |
| 源码起 MCP stdio | `python -m app.cli --mcp` |
| 源码起 MCP HTTP | `python -m app.cli --mcp-http [PORT]` |
| 自检 | `python -m app.cli --doctor [--json]` |
| 一键发布 | `.venv/Scripts/python.exe scripts/release.py`（`--wheel-only` / `--bin-only` / `--single-only` / `--setup-only`） |
| 上传 release | `python scripts/github_release_upload.py <tag> <assets...> --notes <md>` |
| 冻结包冒烟 | `dist/bili-transcriber/bili-transcriber.exe --cli --doctor`（exe 必须 `--cli`） |

Entry points（pyproject）：`bili-transcriber`=GUI / `bili-transcriber-cli`=CLI / `bili-transcriber-mcp`=MCP stdio。

## 架构速览（app/ 31 文件）

- **main.py** NiceGUI 三页签 UI；**pipeline.py** 单工作线程串行编排（状态机 queued→downloading→converting→transcribing→saving→done/failed）
- **cli.py / headless.py** 无头 CLI + Service 层；**mcp_server.py** MCP 11 工具（JSON-RPC stdio）；**mcp_http.py** Streamable HTTP（仅绑 127.0.0.1）
- **agent_setup.py** 8 客户端配置生成（workbuddy/dsh/codex/claude/cursor/vscode/tare/generic）
- 三引擎 transcriber/qwen_transcriber/sensevoice_transcriber，`load()` 均 double-checked locking
- 详细模块说明见 docs/technical-manual-zh.md（深入文档指针见文末表）

## 发布流程四大坑（违反必炸）

1. **沙箱钩子**：`scripts/release.py` 内已设 `CODEBUDDY_SAFE_DELETE_SANDBOX=0`。绕过脚本手动跑 PyInstaller 必须自己设，否则收尾 `os.remove` 被拦死、构建卡死退出码 1。
2. **版本三处同步**：release.py 自动 bump `pyproject.toml` / `build/installer.iss` / `app/__init__.py APP_VERSION`。手改版本必须三处一起改。
3. **snapshot_docs 滚动快照**：发布时生成 `docs/*-{ver}.md` 并删旧快照。**常驻文档（ai-integration.md、adversarial-review.md）不在快照列表，严禁当旧快照删除**（README 3 处 + 手册 2 处引用）。
4. **dist/build 清理**：旧产物移入待删目录（如 `dist/_trash_YYYYMMDD/`）而非 rm，避开 safe-delete 钩子。

## 质量基线与审查方法论

- 全量 525 passed 是底线；任何改动后跑全量。
- **测试全绿 ≠ 真机可用**：组装层（CLI 入口、HTTP 服务、冻结包）必须子进程级 e2e；render_cli 等文档片段的消费方是打包 exe，真机验证必须覆盖交付形态。
- 对抗性审查三视角（实现者/审查者/AI 使用方），工具闭环拿真实报错，清单兜底；历史清单在 docs/adversarial-review.md。
- 代码规范：配对操作同防（POST/DELETE 都要校验）；路径入盘必须 `resolve()`；空串 ≠ 默认值（set_settings 已统一守卫）；token 比较用 `hmac.compare_digest`。
- 中文注释、报错信息必须可行动（告诉用户下一步做什么，别裸抛 traceback）。

## 冻结版（exe）分发开关

`app/main.py` 入口分发：**exe 默认进 GUI，必须 `--cli` 才进 CLI**；源码 `python -m app.cli` 天然是 CLI、不需要该开关。改 `render_cli` / agent_setup 片段时，frozen 分支必须保留 `--cli`（第四轮曾误删，已用双向回归锁定：冻结版必含 / 源码版必不含）。

## 环境变量（代码实际读取）

| 变量 | 作用 |
|---|---|
| `BILI_DEVICE` / `BILI_COMPUTE_TYPE` | 覆盖设备/精度（auto/cuda/cpu） |
| `BILI_MCP_HTTP_TOKEN` | MCP HTTP Bearer token（未设则不校验） |
| `BILI_PORT` | 服务端口 |
| `BILI_TEST_IMPORTS` | 测试导入开关 |
| `CODEBUDDY_SAFE_DELETE_SANDBOX` | 构建沙箱钩子开关（见四大坑 #1） |

## 数据与配置位置

- 设置：`data/settings.json`（output_dir / model_size / device / language / keep_audio / vad）
- 历史：`data/history.db`（SQLite，主键 media_id）
- 模型外置：`%LOCALAPPDATA%\Bili Note\models\`（安装版无写权限，走用户目录）
- 产物输出：默认 `data/settings.json` 的 output_dir（当前 `D:\个人用`）

## 大陆网络适配（改下载/模型逻辑前必读）

hf-mirror 镜像、SSL 证书自愈、CUDA pip 轮子注入；**urllib 客户端必须 `ProxyHandler({})`**——Clash 等系统代理会把 127.0.0.1 请求也代理出去导致本机回环挂死。

## 深入文档指针

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 功能、33 参数、20 实例、概念图 |
| [docs/technical-manual-zh.md](docs/technical-manual-zh.md) | 架构与模块详解（中/英） |
| [docs/user-guide-zh.md](docs/user-guide-zh.md) | 用户手册（中/英） |
| [docs/ai-integration.md](docs/ai-integration.md) | CLI/MCP 参数、返回结构、8 客户端接入 |
| [docs/adversarial-review.md](docs/adversarial-review.md) | 五轮审查完整清单与方法论 |
| [docs/release-notes/](docs/release-notes/) | 各版本变更 |
| [.workbuddy/memory/MEMORY.md](.workbuddy/memory/MEMORY.md) | 项目长期笔记（会话记忆） |

<!-- neat-freak: initialized at 2026-10-29 -->
