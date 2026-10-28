# AI 集成:CLI + MCP

bili-transcriber 提供两种无界面调用方式,供 WorkBuddy、DeepSeek-Honey、Claude Desktop、
脚本/自动化流水线等直接调用本机转写能力:

| 方式 | 入口 | 适用场景 |
|------|------|----------|
| CLI | `bili-transcriber-cli ...` / `bili-transcriber.exe --cli ...` | Shell 脚本、CI、快速手工调用 |
| MCP | `bili-transcriber-mcp` / `bili-transcriber.exe --mcp` | 支持 MCP 的 AI 客户端(stdio 传输) |

两者与桌面端**共享同一份数据**:`data/history.db`(历史/任务状态)与
`data/settings.json`(设置)。桌面端已转写过的视频,CLI/MCP 再提交会直接返回
`skipped`,不会重复下载与转写。

---

## 一、CLI

### 1.1 入口

```bash
# 源码 / 开发环境(在项目根目录执行)
python -m app.cli --version

# 安装 wheel 后(控制台脚本)
bili-transcriber-cli --version

# 桌面安装版 exe(任意目录)
"C:\Program Files\Bili Note\bili-transcriber.exe" --cli --version
```

### 1.2 常用命令

```bash
# 转写 B站链接(等待完成,输出 JSON)
bili-transcriber-cli "https://www.bilibili.com/video/BV1xx411c7mD" --json

# 只给 BV 号也可以(自动补全为视频地址)
bili-transcriber-cli BV1xx411c7mD --json

# 转写本地文件,指定引擎与语言
bili-transcriber-cli D:\media\talk.mp4 --engine sensevoice --language zh --json

# 只提交不等待(适合后台批处理)
bili-transcriber-cli "https://www.bilibili.com/video/BV1xx411c7mD" --no-wait --json

# 查任务 / 列历史 / 读正文
bili-transcriber-cli --status BV1xx411c7mD_p1 --json
bili-transcriber-cli --list --limit 10 --json
bili-transcriber-cli --list --search 关键词 --status-filter done --json
bili-transcriber-cli --result BV1xx411c7mD_p1 --format md --max-chars 0

# 设置(持久化到 data/settings.json,桌面端同样生效)
bili-transcriber-cli --settings --json
bili-transcriber-cli --set engine=whisper --set model_size=large-v3-turbo --set notes=true

# 预加载模型 / 取消任务
bili-transcriber-cli --prewarm
bili-transcriber-cli --cancel BV1xx411c7mD_p1
```

### 1.3 参数

| 参数 | 说明 |
|------|------|
| `source` | 视频链接 / 裸 BV 号 / 本地文件路径 |
| `--json` | 机器可读输出(单个 JSON 对象) |
| `--no-wait` | 只提交不等待(默认等待) |
| `--timeout N` | 等待上限秒数,默认 600 |
| `--output-dir DIR` | 覆盖输出目录 |
| `--engine` | `whisper` / `qwen3-asr` / `sensevoice` |
| `--model` | Whisper 模型,如 `large-v3-turbo` |
| `--device` | `auto` / `cuda` / `cpu` |
| `--compute-type` | `auto` / `float16` / `int8_float16` / `int8` |
| `--language` | `zh` / `en` / 空=自动 |
| `--notes` / `--no-notes` | 是否生成图文讲义 |
| `--status ID` / `--list` / `--result ID` | 查询类 |
| `--format md\|txt\|srt\|notes` | 配合 `--result` |
| `--max-chars N` | 返回正文上限,`<=0` 返回全文 |
| `--settings` / `--set K=V` | 读取/修改设置(`--set` 可重复) |
| `--prewarm` / `--cancel ID` | 预热模型 / 取消任务 |
| `--mcp` | 以 MCP stdio 服务运行 |

退出码:`0` 成功;`1` 任务失败或执行错误;`2` 参数错误。

### 1.4 JSON 输出要点

```jsonc
{
  "source": "...", "kind": "url",
  "jobs": [{"media_id": "BV..._p1", "title": "...", "status": "queued"}],
  "total": 1, "skipped": 0,
  "wait": {                       // 默认等待时出现
    "jobs": [{
      "media_id": "...", "status": "done",
      "paths": {"srt": "...", "txt": "...", "md": "...", "notes": ""}
    }],
    "all_done": true, "timed_out": false, "waited_sec": 32.4
  }
}
```

失败时输出 `{"ok": false, "error": "可读错误"}`。

---

## 二、MCP

### 2.1 传输与协议

- 传输:stdio,换行分隔 JSON-RPC 2.0(标准 MCP stdio)。
- 协议版本:支持 `2025-06-18` / `2025-03-26` / `2024-11-05`(自动协商)。
- 能力:仅 `tools`(无 resources/prompts)。对客户端的 `resources/list` / `prompts/list` 请求回**空清单**而非 `-32601`,避免个别客户端把服务标成不可用。
- 所有日志写 stderr 与 `%LOCALAPPDATA%\Bili Note\logs\app.log`,stdout 只输出协议数据。**协议实现层做了 stdout 隔离**:`sys.stdout` 被改指向 stderr,协议句柄单独持有,任何遗留 `print` / 第三方库输出都不会插进 JSON 帧。
- 通道开关:桌面端「设置 → AI 调用与命令行」可关闭 MCP。关闭时进程不进入协议循环,退出码 1,原因写 stderr,stdout 保持干净。

### 2.2 客户端配置

**WorkBuddy**(`~/.workbuddy/mcp.json`):

```json
{
  "mcpServers": {
    "bili-transcriber": {
      "command": "D:\\bilibili\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "cwd": "D:\\bilibili"
    }
  }
}
```

使用桌面安装版 exe(无需 Python):

```json
{
  "mcpServers": {
    "bili-transcriber": {
      "command": "C:\\Program Files\\Bili Note\\bili-transcriber.exe",
      "args": ["--mcp"]
    }
  }
}
```

**DeepSeek-Honey / 其他 MCP 客户端**:凡是支持 stdio MCP 的客户端,配置项等价于
`command + args`(如上);不支持 stdio 的客户端需要其自带 stdio 桥接。

写入配置后,需要在 WorkBuddy 的连接器管理页对新建的 MCP 服务点「信任」才会加载。

### 2.3 工具清单

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `transcribe` | 提交链接/本地文件 | `source`(必填)、`wait`、`timeout_sec`、`output_dir`、`engine`、`model_size`、`language`、`notes` |
| `wait_jobs` | 等待任务到终态 | `media_ids`(数组)、`timeout_sec` |
| `job_status` | 单任务状态/进度/产物路径 | `media_id` |
| `list_jobs` | 列历史,可按状态/关键字筛选 | `status`、`search`、`limit` |
| `get_transcript` | 读产物正文(默认截断 12000 字) | `media_id`、`format`、`max_chars` |
| `cancel_job` | 取消任务 | `media_id` |
| `prewarm_model` | 后台预加载模型 | — |
| `get_settings` | 读当前设置 | — |
| `update_settings` | 改并持久化设置 | `engine`、`model_size`、`output_dir`、`notes`、`workers` 等 |

工具返回统一包含 `content[0].text`(JSON 字符串)与 `structuredContent`(同结构对象);
业务错误通过 `isError: true` + `{"ok": false, "error": "..."}` 返回,不会中断服务。

### 2.4 典型调用序列

```text
initialize
  → tools/call transcribe {"source": "https://www.bilibili.com/video/BV...", "wait": true}
  → tools/call get_transcript {"media_id": "BV..._p1", "format": "md", "max_chars": 0}
```

或异步:

```text
transcribe(wait=false) → 拿 media_id
wait_jobs([media_id], timeout_sec=600)
get_transcript(media_id)
```

首次转写会加载模型(1~2 分钟,GPU 加速包已放置时更快);可先调 `prewarm_model`,
再提交任务。

---

## 三、语义与限制

1. **参数覆盖 vs 持久化**
   - `transcribe` 的 `engine/model_size/...` 只在当前进程内生效,**不写** settings.json;
   - `update_settings`(或 CLI `--set`)才持久化,桌面端下次启动生效。
2. **设置快照**:MCP 服务进程启动后持有设置快照;若桌面端随后改了设置,
   需重启 MCP 服务(或调用 `update_settings`)才会同步。
3. **阻塞语义**:`transcribe(wait=true)` 与 `wait_jobs` 会在处理请求期间阻塞该服务的
   其他调用(单线程读循环);长任务建议 `wait=false` + 轮询。
4. **并发**:CLI/MCP 与桌面端可同时运行(共享 SQLite WAL),但**不要两边同时提交同一个
   视频**,否则会重复下载/转写(主键相同,最终产物互相覆盖)。
5. **暂停任务**:无界面进程不会自动恢复桌面端「已暂停」的任务,避免后台擅自开工。
6. **登录态**:需要登录的视频请在设置里配置 `cookie_file`(见用户手册 cookie 章节),
   MCP 可用 `update_settings {"cookie_file": "D:\\bilibili\\bili-cookies.txt"}`。
7. **取消**:`notifications/cancelled` 不会中断已开始的 `wait_jobs`;请用 `cancel_job`。

---

## 四、故障排查

| 现象 | 排查 |
|------|------|
| 客户端显示 MCP 连接失败 | 手动执行 `bili-transcriber-mcp`,看 stderr 报错;确认 command 路径存在 |
| 调用工具无响应 | 查看 `%LOCALAPPDATA%\Bili Note\logs\app.log`,确认模型是否在下载/加载 |
| `任务不存在` | media_id 拼写错误;用 `list_jobs` 查真实 ID |
| `解析失败:...` | 链接失效/需要登录/网络不可达;检查 cookies 与代理设置 |
| `尚无 md 产物` | 任务尚未完成或失败;先 `job_status` 看 `status`/`error` |
| 中文乱码 | 客户端需按 UTF-8 解析 stdio;服务端已强制 UTF-8 输出 |
| 启动即退出,stderr 说「通道已在桌面端「设置」中关闭」 | 设置页关掉了 MCP 开关,勾回来保存即可(或把 `settings.json` 的 `enable_mcp` 改 `true`) |
| 日志里出现 `database is locked` | 0.1.39 及更早:桌面端与 MCP 同时写 `history.db` 撞锁。0.1.40 起 `busy_timeout=5000`,升级即可 |
| 客户端把整段会话断开 | 旧版遇到非 UTF-8 字节会崩;0.1.40 起降级为 `-32700` 解析错误并继续 |

---

## 五、实现位置(给维护者)

| 文件 | 职责 |
|------|------|
| `app/headless.py` | 无界面服务层(提交/等待/查询/设置,与 GUI 共享数据) |
| `app/cli.py` | 命令行入口(argparse + JSON 输出 + 退出码) |
| `app/mcp_server.py` | MCP stdio 服务(JSON-RPC 2.0 子集 + 9 个工具) |
| `app/paths.py` | 运行根目录/数据目录解析(GUI/CLI/MCP 共用) |
| `app/pipeline.py#submit_prepared` | 预解析提交:避免 worker 二次 probe |

测试:`tests/test_cli.py`、`tests/test_mcp_server.py`、`tests/test_headless.py`,
以及 `tests/test_pipeline.py` 中 `submit_prepared` 相关用例。
