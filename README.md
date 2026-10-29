<div align="center">

<img src="docs/assets/hero.svg" alt="bili-transcriber —— 把 B 站视频变成可读的文字" width="100%" />

# bili-transcriber

**把 B 站（以及抖音 / 小红书 / 快手 / YouTube）的视频变成可检索、可引用、可交给 AI 的文字。**

下载 → 转码 → 本地 GPU 转写 → SRT / TXT / Markdown。全程本机运行，音频不上传。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![UI](https://img.shields.io/badge/UI-NiceGUI-5898d4)](https://nicegui.io)
[![Engine](https://img.shields.io/badge/ASR-Whisper%20%7C%20Qwen3%20%7C%20SenseVoice-6f42c1)](#三个识别引擎)
[![MCP](https://img.shields.io/badge/MCP-stdio%20%2B%20HTTP-000000)](#ai-集成cli--mcp)
[![Tests](https://img.shields.io/badge/tests-544%20passed-2ea44f)](tests)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4)](#系统要求)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

> 对应版本 **v0.1.45** · 版本号由发布脚本自动递增，平时只改这一处

</div>

---

## 它到底解决什么问题

你看了一个 40 分钟的 B 站技术讲座，想引用第 23 分钟那段话，却只能反复拖进度条。

bili-transcriber 把它变成一份带时间戳的 Markdown：搜关键词、跳回原片、整段引给 AI，都行。
所有计算都在你的机器上完成 —— 音频文件不会离开本机。

> 一句话：**粘贴链接 → 等一下 → 得到可检索的文字稿。**

## 目录

| 章节 | 你会看到 |
|---|---|
| [一句话的旅程](#一句话的旅程) | 一次任务里数据怎么流动 |
| [功能一览](#功能一览) | 支持哪些平台、三个识别引擎、还有什么 |
| [安装](#安装) | 四种交付形态怎么选 |
| [快速开始](#快速开始) | 三条命令跑起来 |
| [使用说明](#使用说明) | 三个页签 + 每个设置项 |
| [20 个使用实例](#20-个使用实例) | 直接抄走的真实场景 |
| [命令行参考](#命令行参考) | 全部 34 个选项 |
| [AI 集成（CLI / MCP）](#ai-集成cli--mcp) | 八种客户端怎么接 |
| [输出结构](#输出结构) | 产物长什么样、放在哪 |
| [Cookie 与网络](#cookie-与网络国内网络必读) | 国内网络与登录态的坑 |
| [常见问题](#常见问题) | 出问题先看这里 |
| [开发与测试](#开发与测试) | 跑测试、发版本 |

## 一句话的旅程

<img src="docs/assets/pipeline.svg" alt="从链接到文字稿的数据流" width="100%" />

1. **解析** —— yt-dlp 展开链接：单 P、多 P 合集、短链、搜索页弹窗……统一成「待处理条目」。
2. **下载** —— 只拉音频流（要图文讲义时才拉视频流）；支持断点续跑，中间文件处理完即删。
3. **转码** —— FFmpeg 统一转成 16 kHz 单声道 WAV，三个引擎都吃这个格式。
4. **转写** —— 本地 GPU 上的三个引擎之一；模型按 (模型, 设备, 精度, 语言, VAD) 缓存，改设置才重载。
5. **写盘** —— SRT / TXT / Markdown 三件套，标题、UP 主、时长、时间戳齐全，落在你指定的目录。

一个后台工作线程串行跑完全程（状态机 queued → downloading → converting → transcribing → saving → done/failed），
转写阶段全程持锁保护 GPU。

## 功能一览

### 支持平台

| 平台 | 支持度 | 说明 |
|---|---|---|
| **B 站** | ✅ 完整 | 单 P / 多 P / 合集自动展开；大会员与高清需要 Cookie |
| **快手** | ✅ 完整 | 走分享页自研解析通道，免登录 |
| **抖音** | ⚠️ 需 Cookie | 精选内容需配浏览器 Cookie 或 cookies.txt |
| **小红书** | ⚠️ 需 Cookie | 登录后才返回完整视频地址 |
| **YouTube** | ⚠️ 需代理 | 境内必须配代理；年龄限制/会员视频需 Cookie |
| **本地文件** | ✅ 完整 | 直接把本地音视频交给任务页 |

### 三个识别引擎

| 引擎 | 后端 | 特点 | 适用 |
|---|---|---|---|
| **Whisper**（默认） | faster-whisper + ctranslate2 | 稳、生态成熟；CUDA 下支持批量推理 | 通用首选 |
| **Qwen3-ASR** | qwen-asr（transformers）+ ForcedAligner | 中文与中英混说更准，字级时间戳 | 中文讲座、访谈 |
| **SenseVoice** | sherpa-onnx（int8） | 极轻量、启动快、CPU 友好 | 没独显、只要草稿 |

切换引擎在设置页，或命令行加 `--engine whisper|qwen3-asr|sensevoice`。

### 其他

- **网络与 Cookie 一键检测** —— 自动扫出浏览器登录态、本机 cookies.txt 和可用代理，点「测试连接」当场验证能不能真用。
- **图文讲义** —— 从视频关键帧 + 转写段落生成带截图的讲义（`--notes`）。
- **全文检索** —— `--grep 关键词` 在所有已完成任务的转写稿里搜，返回命中片段与上下文。
- **AI 可调用** —— 同一套服务层暴露成 CLI 与 MCP（stdio + HTTP），八种 AI 客户端一键配置。
- **单实例保护** —— 重复双击只会把已有窗口提到前台，不会抢任务、不会闪退。
## 安装

### 系统要求

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | Windows 10 / 11（x64） | 安装版与便携版都在此测试 |
| 显卡 | 可选 NVIDIA GPU | 有则 GPU 加速；没有也能跑（CPU 慢 3~10 倍） |
| 内存 | 8 GB 起 | Whisper large-v3-turbo 约需 3 GB |
| 磁盘 | 3 GB 起 | 模型 + 产物；模型默认放用户目录 |
| Python | 3.11+（仅源码模式） | 开发环境实测 3.14 |

### 四种形态

| 形态 | 文件 | 适合谁 | 特点 |
|---|---|---|---|
| **安装版** | `bili-transcriber-setup-x.y.z.exe` | 绝大多数人 | 有开始菜单、卸载项，可覆盖升级 |
| **便携版** | `bili-transcriber-portable-x.y.z.zip` | 不想装、放 U 盘 | 解压即用，绿包 |
| **单文件版** | `bili-transcriber-single-x.y.z.exe` | 临时用一次 | 一个 exe 拷走就跑，启动略慢 |
| **wheel** | `bili_transcriber-x.y.z-py3-none-any.whl` | 开发者 / 装进自己的环境 | `pip install` 后得到 `bili-transcriber` 等三个入口 |

> 双击 exe 默认进**独立窗口**（内置 WebView2）。想退回浏览器界面：设环境变量 `BILI_FORCE_BROWSER=1`。
> 窗口态程序没有控制台，诊断信息都写进 `%LOCALAPPDATA%\Bili Note\logs\app.log`（2 MB 轮转，留 3 份）。

### GPU 加速包（可选）

`bili-transcriber-gpu-x.y.z.zip` 里是 NVIDIA 运行库（cuBLAS / cuDNN），解压覆盖到便携版目录即可。
安装版与 wheel 版已把依赖声明进包，通常无需额外操作。

## 快速开始

`B`powershell
# 1. 装（源码模式）
git clone https://github.com/Asaceoo/bili-transcriber && cd bili-transcriber
python -m venv .venv && .venv\Scripts\pip install -e .

# 2. 跑：桌面窗口
.venv\Scripts\python -m app.main

# 3. 或者命令行直接转一条
.venv\Scripts\python -m app.cli "https://www.bilibili.com/video/BV1xx411c7mD"
`B`

首次转写会自动下载模型（走 hf-mirror 镜像，约 1.5 GB），之后离线可用。
产物默认落在设置里的 `output_dir`。

> 冷启动要 10~20 秒：导入 torch / transformers 是主要开销。窗口会先出来，再逐段渲染。

## 使用说明

界面三个页签，加一个设置页。左侧步骤条把「该干什么」写死，不用记。

### 任务（Tasks）

- **粘贴链接或拖入文件** → 点「开始」。单 P、多 P 合集、短链、本地音视频都走同一个入口。
- 队列串行：下载/转码可并行（设置里的「并发任务数」1~6），**转写始终串行**保护 GPU。
- 每行显示阶段、进度、耗时；失败行点开看真实报错（不是"转写失败"四个字）。
- 暂停 / 继续 / 取消都在行内；暂停的任务重启程序会自动续跑。
- 下方「运行日志」实时滚动，排障先看它。

### 历史（History）

- 所有任务落 SQLite（`data/history.db`），主键是平台内媒体 ID，重跑会覆盖同一条。
- 可按标题 / UP 主 / 平台筛；点开能直接读产物、重新打开输出目录。
- 搜索关键词命中的是**标题**；要搜转写稿正文用 `--grep` 或 MCP 的 `search_transcript`。

### 设置（Settings）

按「会不会影响转写结果」排序，逐个说明：

| 设置 | 取值 | 影响 |
|---|---|---|
| 识别引擎 | whisper / qwen3-asr / sensevoice | 决定用哪个模型；切换会重新加载模型 |
| Whisper 模型 | tiny ~ large-v3-turbo | 越大越准越慢；默认 large-v3-turbo |
| 设备 | auto / cuda / cpu | auto 优先 CUDA，不可用回落 CPU |
| 计算精度 | auto / float16 / int8_float16 / int8 | 显存不够就降精度 |
| 语言 | auto / zh / en | 指定语言更快更准；auto 自动检测 |
| 保留音频 | 开 / 关 | 留原始音频备查，或转完即删 |
| VAD 静音过滤 | 开 / 关 | 跳过静音段，长视频提速明显 |
| 图文讲义 | 开 / 关 | 打开后 B 站任务会下视频流做关键帧截图 |
| 模型目录 | 路径 | 默认 `%LOCALAPPDATA%\Bili Note\models` |
| 浏览器 Cookie | 不使用 / Chrome / Edge / Firefox / Brave / Chromium / Opera / Vivaldi | 抖音精选、部分 YouTube 需要 |
| cookies.txt | 文件路径 | **优先于**浏览器 Cookie；不受浏览器开关影响 |
| 网络代理 | 如 `http://127.0.0.1:7890` | YouTube 等境外平台需要；点「探测」自动找 |
| 输出目录 | 路径 | 产物落地处；相对路径会被解析成绝对路径 |
| 并发任务数 | 1~6 | 下载/转码并行度 |
| 允许 CLI 调用 | 开 / 关 | 关掉后 `--cli` 直接拒绝（退出码 3） |
| 允许 MCP 调用 | 开 / 关 | 关掉后 `--mcp` / `--mcp-http` 被拦 |

> 通道开关**只能在桌面端点**：CLI 与 MCP 自己改不了。这是刻意的 —— 防止被提示注入的 AI 打开或关掉自己的通道。
## 20 个使用实例

从「最常用」到「进阶自动化」，每条都能直接抄。命令里的 `.venv\Scripts\` 前缀是源码模式；
用 exe 的话把 `.venv\Scripts\python -m app.cli` 换成 `bili-transcriber.exe --cli`。

### 入门三例

**1. 转一条普通 B 站视频**

```powershell
.venv\Scripts\python -m app.cli "https://www.bilibili.com/video/BV1xx411c7mD"
```

等它跑完，产物在输出目录：`标题.srt` / `标题.txt` / `标题.md`。

**2. 整个合集 / 多 P 一次性转完**

```powershell
.venv\Scripts\python -m app.cli "https://space.bilibili.com/123456/channel/collectiondetail?sid=789" --json
```

合集自动展开成多个条目，同一合集共享输出目录，靠 `_P01` `_P02` 后缀区分单 P。

**3. 本地录好的音频也能转**

```powershell
.venv\Scripts\python -m app.cli "D:\录音\周会.m4a" --language zh
```

本地文件不过网络，直接进转码与转写。

### 日常四例

**4. 只要文字稿，不要字幕文件**

```powershell
.venv\Scripts\python -m app.cli "https://www.bilibili.com/video/BV1xx411c7mD" --format txt
```

**5. 用 CPU 跑（机器没有独显）**

```powershell
.venv\Scripts\python -m app.cli "BV1xx411c7mD" --device cpu --model small --compute-type int8
```

**6. 中文技术讲座，换 Qwen3-ASR 更准**

```powershell
.venv\Scripts\python -m app.cli "BV1xx411c7mD" --engine qwen3-asr --language zh
```

**7. 顺手产出图文讲义**

```powershell
.venv\Scripts\python -m app.cli "BV1xx411c7mD" --notes
```

会额外下视频流做关键帧截图，得到 `标题.notes.md` + `标题_frames\slide_*.jpg`。

### 进阶五例

**8. 批量：把一个目录里的录音全丢给 CLI**

```powershell
Get-ChildItem D:\录音\*.m4a | ForEach-Object {
    .venv\Scripts\python -m app.cli $_.FullName --json --timeout 3600
}
```

**9. 只提交不等待，让桌面端后台跑**

```powershell
.venv\Scripts\python -m app.cli "BV1xx411c7mD" --no-wait
```

任务留在队列，下次启动桌面端自动续跑。

**10. 在所有已完成的转写稿里全文检索**

```powershell
.venv\Scripts\python -m app.cli --grep "注意力机制" --context 400 --limit 5
```

返回命中任务、标题与上下文片段 —— 比逐个打开 md 快得多。

**11. 读某条任务已生成的产物（不重新转）**

```powershell
.venv\Scripts\python -m app.cli --result BV1xx411c7mD --format md --max-chars 0
```

`--max-chars 0` 是全文；默认截断到 12000 字符，适合喂给上下文有限的 AI。

**12. 查看队列与状态**

```powershell
.venv\Scripts\python -m app.cli --list --status-filter running
.venv\Scripts\python -m app.cli --status BV1xx411c7mD
```

### 平台三例

**13. 抖音精选（要 Cookie）**

```powershell
.venv\Scripts\python -m app.cli --set cookies_from_browser=chrome
.venv\Scripts\python -m app.cli "https://v.douyin.com/xxxxx"
```

Chrome 开着时 Cookie 库被独占，先在设置页点「测试连接」看提示；或导出 cookies.txt 后 `--set cookie_file=...`。

**14. 小红书笔记视频**

```powershell
.venv\Scripts\python -m app.cli "https://www.xiaohongshu.com/explore/xxxxxxxx"
```

登录后才返回完整视频地址 —— 配浏览器 Cookie 或 cookies.txt。

**15. 快手分享链（免登录）**

```powershell
.venv\Scripts\python -m app.cli "https://v.kuaishou.com/xxxxx"
```

走自研分享页解析，不需要 Cookie。

### AI 集成三例

**16. 让 AI 客户端一条命令接上（stdio）**

```powershell
.venv\Scripts\python -m app.cli --doctor                      # 先看本机装了哪些客户端
.venv\Scripts\python -m app.cli --agent-config codex --write  # 写进 ~/.codex/config.toml（自动备份 .bak）
```

**17. 走 HTTP，给不能拉子进程的客户端**

```powershell
.venv\Scripts\python -m app.cli --mcp-http 8765
# 客户端配 http://127.0.0.1:8765/mcp
```

服务只绑 127.0.0.1，带 Origin 白名单；要鉴权就设 `BILI_MCP_HTTP_TOKEN`。

**18. 在 AI 对话里让它直接转写并检索**

```text
你：用 check_network 看看我的 Cookie 和代理现在能不能用
AI：（调 check_network mode=test）→ 逐平台返回延迟与登录态

你：转写这个视频，并找出讲"分布式一致性"的那段
AI：（transcribe → wait_jobs → search_transcript）→ 返回时间戳与原文
```

### 排障两例

**19. 转写报错，先看真实原因**

```powershell
.venv\Scripts\python -m app.cli --doctor
Get-Content "$env:LOCALAPPDATA\Bili Note\logs\app.log" -Tail 50
```

桌面端还有「运行日志」面板实时滚动，比翻日志文件快。

**20. 端口被占，换一个**

```powershell
$env:BILI_PORT = "9000"
.venv\Scripts\python -m app.main
```

或者：程序检测到已有实例在跑，会直接把现有窗口提到前台然后退出 —— 这是正常的单实例保护。
## 命令行参考

`bili-transcriber-cli` / `python -m app.cli` 共用同一套参数。打包后的 exe 需要 `--cli` 才进命令行模式。

### 提交任务

| 参数 | 说明 |
|---|---|
| `source` | 视频链接或本地音视频路径（位置参数） |
| `--no-wait` | 只提交不等待，任务留在队列 |
| `--timeout` | 等待上限秒数，默认 600 |
| `--output-dir` | 覆盖输出目录 |
| `--engine` | `whisper` / `qwen3-asr` / `sensevoice` |
| `--model` | Whisper 模型名，如 `large-v3-turbo` |
| `--device` | `auto` / `cuda` / `cpu` |
| `--compute-type` | `auto` / `float16` / `int8_float16` / `int8` |
| `--language` | `zh` / `en` / 省略=自动 |
| `--notes` / `--no-notes` | 本次是否生成图文讲义（互斥） |
| `--json` | 输出机器可读 JSON |

### 查询与管理

| 参数 | 说明 |
|---|---|
| `--list` | 列出任务 |
| `--limit` | 返回条数，默认 20（`--list` / `--grep` 共用） |
| `--status-filter` | 按状态过滤；支持 `running` 别名 |
| `--search` | 按标题关键字过滤 |
| `--grep KEYWORD` | 在已完成任务的**转写稿正文**里全文检索 |
| `--context` | `--grep` 的上下文字符数，默认 240 |
| `--status MEDIA_ID` | 查单个任务 |
| `--result MEDIA_ID` | 读取产物文本 |
| `--format` | `md` / `txt` / `srt` / `notes` |
| `--max-chars` | 产物截断长度，`<=0` 为全文，默认 12000 |
| `--cancel MEDIA_ID` | 取消任务 |

### 设置与其他

| 参数 | 说明 |
|---|---|
| `--settings` | 打印当前设置 |
| `--set KEY=VALUE` | 改设置并持久化，可重复；非法值直接拒绝 |
| `--prewarm` | 预加载模型并等到就绪才退出 |
| `--mcp` | 以 MCP stdio 服务运行 |
| `--mcp-http [PORT]` | 以 MCP HTTP 服务运行；省略端口从 8765 起自动找空位 |
| `--doctor` | AI 接入自检：本机客户端、配置路径、常见错误一次说清 |
| `--agent-config CLIENT` | 打印某客户端接入配置（8 种） |
| `--agent-list` | 列出本机检测到的 AI 客户端与注册状态 |
| `--transport` | `stdio`（默认）/ `http` |
| `--mcp-port` | `--transport http` 用的端口，默认 8765 |
| `--write` | 把配置写进客户端文件（先备份 `.bak`；DSH 由界面托管，拒绝直写） |
| `--version` | 显示版本 |

> 输出编码：被管道/重定向接管时**一律 UTF-8**（AI 客户端按 UTF-8 解析不会乱码），
> 留在终端里则沿用控制台代码页，避免 `cmd.exe` 花屏。

## AI 集成（CLI / MCP）

<img src="docs/assets/ai-integration.svg" alt="CLI 与 MCP 两条通道共用同一套服务层" width="100%" />

<img src="docs/assets/mcp-channels.svg" alt="一个服务层，三条通道接入任意 AI Agent" width="100%" />

三条通道共用同一个服务层（`app/headless.py`），行为完全一致 —— 只是传输方式不同：

- **CLI** —— 脚本、批处理、CI 里调用，参数进 / JSON 出。
- **MCP stdio** —— 能自己拉起子进程的客户端（WorkBuddy、Codex、Claude Code 等）。
- **MCP HTTP** —— 只能连 URL 的客户端；只绑 `127.0.0.1`，带 Origin 白名单与可选 Bearer token。

### 一条命令接入（`--agent-config`）

```powershell
bili-transcriber-cli --doctor                          # 自检：装了哪些客户端、缺什么
bili-transcriber-cli --agent-config workbuddy --write   # 写进 ~/.workbuddy/mcp.json
bili-transcriber-cli --agent-config codex --transport http --write
```

| 客户端 | 自动写入 | 说明 |
|---|---|---|
| WorkBuddy | ✅ | JSON `mcpServers`，已有条目原样保留 |
| DSH | ❌ 手动粘贴 | 连接表由界面托管，**拒绝直写**（写坏会让 DSH 起不来） |
| Codex | ✅ | TOML，只写文档化键 + `startup_timeout_sec=300` |
| Claude / Cursor / VS Code / Tare / 通用 | ✅ / 粘贴 | 见 `--agent-list` |

写入前自动备份 `.bak`，只动 `bili-transcriber` 一个条目。

### MCP 的 11 个工具

| 工具 | 作用 |
|---|---|
| `transcribe` | 提交转写任务（链接或本地文件） |
| `wait_jobs` | 等一批任务完成（超时夹紧 86400 秒） |
| `job_status` | 查单个任务状态与进度 |
| `list_jobs` | 列任务 |
| `get_transcript` | 取产物文本（md / txt / srt / notes） |
| `search_transcript` | 在转写稿正文里全文检索 |
| `check_network` | 网络与 Cookie 只读体检（quick / full / test） |
| `cancel_job` | 取消任务 |
| `prewarm_model` | 预加载模型 |
| `get_settings` | 读设置 |
| `update_settings` | 改设置（通道开关不可改） |

`check_network` 的 `test` 档会**读你当前设置**（浏览器 Cookie / cookies.txt / 代理）逐平台实测，
所以 AI 拿到的结论和你在设置页点「测试连接」是同一个。

### 通道开关（设置页可关）

设置页「AI 调用与命令行」两个开关落盘。关掉后：

- `--cli` / `bili-transcriber-cli` 立即退出，退出码 3；
- `--mcp` / `--mcp-http` 写一条 stderr 并返回 1，stdout 保持空（不污染协议）。

门禁在构造服务层**之前**判断，不白占 SQLite 锁。
## 输出结构

一次 B 站任务（`BV1xx411c7mD`，标题「分布式共识入门」）在输出目录里长这样：

```text
分布式共识入门.md            ← 带元信息与时间戳的正文（推荐阅读）
分布式共识入门.txt           ← 纯文本，喂给 AI 最省 token
分布式共识入门.srt           ← 标准字幕，可直接给播放器
分布式共识入门.notes.md      ← 只有开了「图文讲义」才有
分布式共识入门_frames\       ← 讲义配图（slide_001.jpg …）
```

`.md` 的头部是这样的：

```markdown
# 分布式共识入门

- 平台：B 站
- UP 主：某某某
- 时长：42:17
- 来源：https://www.bilibili.com/video/BV1xx411c7mD
- 引擎：whisper large-v3-turbo
- 生成时间：2026-10-29 21:40

## 正文

[00:00] 大家好，今天聊分布式共识……
[00:12] 先从两阶段提交说起……
```

字幕与正文都是**逐段**的：一段一个时间戳，拖动与引用都按段走。

## Cookie 与网络（国内网络必读）

### Chrome 127+ / Edge 的 Cookie 是「应用绑定加密」

Chrome 127 起把 Cookie 换成了 **App-Bound Encryption（v20）**：密钥由浏览器的 elevation 服务包裹，
**只有浏览器本体、在原始配置路径上**才能解开。第三方解密实现（yt-dlp 等）只认老的 v10，
遇到 v20 一律报解密失败。

本程序的做法是让浏览器自己解密：

- 复制 `Local State` + Cookie 库到临时目录，起一个**无窗口临时实例**（用完立即杀掉、目录删掉），
  经调试协议取回明文；你的浏览器窗口不受影响。
- **下载**时把明文落成一份临时 `cookies.txt` 交给 yt-dlp（10 分钟内复用缓存），
  这样"测试通过"和"下载成功"才是同一件事。

| 你会看到 | 含义 | 该做什么 |
|---|---|---|
| 绿点「已登录」+「值加密读不到」 | 登录态在，但这台机器上值取不出 | 关掉浏览器再点「测试连接」；或点「导出」走 cookies.txt |
| 锁图标「读取失败」 | Cookie 库被独占，连名字/有效期都读不到 | 关掉浏览器重测，或改用 cookies.txt |
| 「配置文件不存在」 | `cookie_file` 填的路径错了 | 改路径，或清空改用浏览器 Cookie |

> 实测：Chrome 运行时 Cookie 库在 Windows 上**完全不可复制**（`PermissionError 32`，
> 30 秒 60 次尝试全失败）。这不是程序卡住，是 Chrome 的安全设计 —— 关掉浏览器一次就成。

### 国内网络适配

| 问题 | 现象 | 本程序的处理 |
|---|---|---|
| HuggingFace 访问慢/失败 | 模型下载卡住 | 走 `hf-mirror` 镜像；SSL 证书异常时自愈重试 |
| 系统代理劫持回环 | `127.0.0.1` 的请求被代理出去 | 内部 urllib 一律 `ProxyHandler({})`，绝不走代理 |
| CUDA 轮子装不上 | `pip install` 报找不到 | 发布流程注入 CUDA 轮子源 |
| 境外平台不通 | YouTube 一直超时 | 设置页「探测」自动找本机代理端口并验证 |

## 常见问题

**Q：双击后开了浏览器而不是独立窗口？**
初次启动要导入 torch/transformers（10~20 秒），期间没窗口会让人以为没反应，再双击就进了"已有实例"分支。
现在默认走原生窗口，并在后台唤回；要强制浏览器界面设 `BILI_FORCE_BROWSER=1`。

**Q：开着几分钟自己退出了？**
旧版本（≤0.1.40）的已知问题：pywebview 的临时目录被回收导致渲染进程崩溃。0.1.41 起固定 WebView2 数据目录，已修。

**Q：B 站只能下 480P？**
B 站高清需要登录态。配浏览器 Cookie 或 cookies.txt（见上一节）。

**Q：转写很慢？**
先确认真的用上了 GPU：设置页把「设备」设 `cuda`，跑 `--doctor` 看设备探测。
CPU 上 large-v3-turbo 大约慢 3~10 倍；换 `small` 或 SenseVoice 明显更快。

**Q：多 P 合集里某一 P 失败，会全部重来吗？**
不会。每条独立状态，失败条目单独重试；已完成的不再下载。

**Q：产物在哪？**
设置里的「输出目录」。源码模式默认 `output/`；安装版建议指到自己的盘。

**Q：能不能只装命令行，不装界面？**
可以。`pip install bili_transcriber-*.whl` 后直接用 `bili-transcriber-cli`；
或用 MCP 让 AI 代劳。

**Q：数据会上传吗？**
不会。唯一的外部请求是"下载视频/音频"和"下载模型"。日志、历史、产物都在本机。

## 数据与隐私

| 数据 | 位置 | 说明 |
|---|---|---|
| 设置 | `data/settings.json` | 输出目录、引擎、Cookie 来源、代理等 |
| 历史 | `data/history.db` | SQLite，主键为媒体 ID |
| 模型 | `%LOCALAPPDATA%\Bili Note\models` | 安装版无写权限，故放用户目录 |
| 日志 | `%LOCALAPPDATA%\Bili Note\logs\app.log` | 2 MB 轮转，保留 3 份 |
| 产物 | 设置里的 output_dir | 音频（若保留）、字幕、文本、讲义图 |
| 导出的 cookies.txt | `data/cookies.txt` | **含登录凭据**，不要分享或提交 |

> Cookie 只在下载那一刻交给 yt-dlp，不进日志、不打包、不发往任何第三方。
> 通道开关落盘不可被 CLI/MCP 修改 —— 防止被提示注入的 AI 自行打开通道。

## 开发与测试

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q      # 全量 544 passed
.venv\Scripts\python.exe -m app.cli --doctor      # 自检
.venv\Scripts\python.exe scripts\release.py       # 一键发布（自动 bump 版本 + 手册快照）
```

- **动手前读** [CLAUDE.md](CLAUDE.md)：发布流程的四个必炸坑、质量基线、冻结版分发开关。
- **架构与模块** [docs/technical-manual-zh.md](docs/technical-manual-zh.md)。
- **用户手册** [docs/user-guide-zh.md](docs/user-guide-zh.md)（英文版 `-en`）。
- **AI 接入细节** [docs/ai-integration.md](docs/ai-integration.md)。
- **对抗性审查清单与方法论** [docs/adversarial-review.md](docs/adversarial-review.md)。

## 许可

MIT —— 见 [LICENSE](LICENSE)。
