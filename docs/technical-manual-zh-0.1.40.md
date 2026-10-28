# bili-transcriber 技术手册（中文）

> 面向开发者与维护者。讲清架构分层、并发模型、状态机、模块职责、数据契约、安全边界、打包坑位与测试体系。
> 发布版本快照 **v0.1.40** — 由 `scripts/release.py` 自动生成,版本号取自 `pyproject.toml` 单一来源。

---

## 目录

1. [系统概览](#1-系统概览)
2. [分层架构](#2-分层架构)
3. [技术栈](#3-技术栈)
4. [目录结构](#4-目录结构)
5. [并发与线程模型](#5-并发与线程模型)
6. [状态机与任务恢复](#6-状态机与任务恢复)
7. [模块职责](#7-模块职责)
8. [数据契约](#8-数据契约)
9. [安全与健壮性设计](#9-安全与健壮性设计)
10. [打包兼容性坑位](#10-打包兼容性坑位)
11. [构建与发布](#11-构建与发布)
12. [测试体系](#12-测试体系)
13. [已知限制](#13-已知限制)

---

## 1. 系统概览

bili-transcriber 是一个**纯本地的多平台视频 / 音频转写工具**，有三种驱动方式（GUI / CLI / MCP）共用同一套服务层。

核心链路：

```
输入：链接(B站/抖音/小红书/快手/YouTube) 或 本地音视频文件
  → 探测展开（平台识别 / 短链展开 / 单P·多P·合集 → N 条任务）
  → 下载最佳音频流（开启讲义时改下视频流）
  → FFmpeg 转码 16kHz 单声道 WAV
  → 本地推理（faster-whisper / Qwen3-ASR / SenseVoice 三选一）
  → 可选：关键帧提取 → 图文讲义
  → 落盘 SRT / TXT / Markdown
```

**设计原则（贯穿全项目）：**

| 原则 | 落地方式 |
|---|---|
| 数据不上传 | 无任何外部 AI API 调用；只有下载视频和拉模型两次出站 |
| 断点续跑 | 音频 / WAV / 视频路径缓存复用；启动时恢复遗留进行中任务 |
| GPU 不并发争用 | 转写全程持引擎锁，同一时刻只有一个任务占卡 |
| UI 与计算解耦 | 工作线程只写 Store / 事件队列，UI 线程只读轮询 |
| 单一来源 | 版本号只在 `pyproject.toml`；状态集合只在 `pipeline.py` |

---

## 2. 分层架构

```
┌──────────────────────────────────────────────────────────┐
│  驱动层                                                   │
│  app/main.py (GUI)   app/cli.py (CLI)   app/mcp_server.py │
└───────────────┬──────────────┬───────────────┬───────────┘
                │              │               │
                │       ┌──────┴───────┐       │
                │       │ app/headless │◀──────┘  服务层(唯一业务入口)
                │       │   Service    │
                │       └──────┬───────┘
                ▼              ▼
┌──────────────────────────────────────────────────────────┐
│  编排层  app/pipeline.py (Task / 状态机 / 工作线程池)      │
└──────────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────┐
│  能力层                                                   │
│  downloader / kuaishou / converter / transcriber /       │
│  qwen_transcriber / sensevoice_transcriber / keyframes / │
│  writers / models / gpu_runtime / hardware / ffmpeg_bin  │
└──────────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────┐
│  基础层  paths / store / task_control / native_dialog /   │
│          tray / logging_setup                            │
└──────────────────────────────────────────────────────────┘
```

**关键点：CLI 与 MCP 不直接碰 pipeline**，一律经 `app/headless.py` 的 `Service`。这样：

- 两条 AI 通道行为完全一致，只有传输方式不同。
- 参数校验、JSON 序列化、错误包装只有一处实现。
- 加新通道（HTTP / 消息队列）时不用改业务逻辑。

---

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python ≥ 3.11（开发环境 3.14） |
| 下载 | yt-dlp（B站 / 抖音 / 小红书 / YouTube）+ 自研快手 SSR 解析 |
| 转码 | FFmpeg（subprocess；内置 imageio-ffmpeg 静态版，`app/ffmpeg_bin.py` 解析） |
| 推理 1 | faster-whisper + ctranslate2（CUDA 后端，默认） |
| 推理 2 | qwen-asr（Qwen3-ASR 0.6B / 1.7B，torch 后端） |
| 推理 3 | sherpa-onnx（SenseVoiceSmall，ONNX int8） |
| 关键帧 | FFmpeg scene 检测（优先）/ PyAV 逐帧 MAD（回退），双后端 |
| UI | NiceGUI（Quasar 组件），默认浏览器模式；pywebview 原生窗口可选 |
| 托盘 | pystray（失败静默降级） |
| 原生对话框 | ctypes comdlg32（Windows 文件选择框） |
| 单实例 | ctypes `CreateMutexW` |
| 存储 | SQLite（history.db）+ JSON（settings.json） |
| 构建 | hatchling（wheel）+ PyInstaller（onedir / onefile）+ InnoSetup |

---

## 4. 目录结构

```
bili-transcriber/
├── app/                        # 应用源码（28 个模块，约 6700 行）
│   ├── __init__.py             # APP_VERSION + frozen 模式 DLL 搜索路径
│   ├── paths.py                # 运行时路径解析（GUI/CLI/MCP 共用同一份 data/）
│   ├── main.py                 # 启动入口：单实例 / 端口 / 托盘 / UI 装配
│   ├── pipeline.py             # 核心编排：状态机 / 工作线程池 / 引擎锁
│   ├── headless.py             # 服务层：CLI 与 MCP 的唯一业务入口
│   ├── cli.py                  # 命令行驱动
│   ├── mcp_server.py           # MCP stdio 服务端（JSON-RPC 2.0 子集）
│   ├── downloader.py           # 下载编排：平台识别 / 短链 / SSR / Cookie / 代理
│   ├── kuaishou.py             # 快手分享页 SSR 解析（免登录提取 mp4 直链）
│   ├── converter.py            # FFmpeg → 16kHz 单声道 WAV
│   ├── transcriber.py          # 引擎 1：faster-whisper
│   ├── qwen_transcriber.py     # 引擎 2：Qwen3-ASR（分块转写 + 对齐器）
│   ├── sensevoice_transcriber.py # 引擎 3：SenseVoice（token→段时间戳）
│   ├── keyframes.py            # 关键帧提取（ffmpeg scene / PyAV 双后端）
│   ├── writers.py              # SRT / TXT / MD / notes 落盘（原子写）
│   ├── models.py               # 模型目录扫描 / 检测 / 定位（三引擎）
│   ├── gpu_runtime.py          # 外置 GPU 加速包 DLL 注册
│   ├── ffmpeg_bin.py           # ffmpeg 二进制解析（PATH 优先，内置兜底）
│   ├── hardware.py             # 硬件探测（GPU 型号 / 显存 / CUDA 可用性）
│   ├── store.py                # SQLite + JSON 设置（14 项定义）
│   ├── task_control.py         # 任务级暂停 / 取消（线程安全）
│   ├── native_dialog.py        # Windows 原生文件选择框（ctypes comdlg32）
│   ├── tray.py                 # 系统托盘（pystray，失败静默降级）
│   ├── logging_setup.py        # 日志初始化
│   └── ui/                     # UI 层（从 main.py 拆分）
│       ├── state.py            # 共享单例：store / settings / pipeline / events
│       ├── pages.py            # 四个页签的布局构建
│       └── helpers.py          # 动作函数：开文件夹 / 任务操作 / 上传落盘
├── build/
│   ├── bili-transcriber.spec         # PyInstaller onedir
│   ├── bili-transcriber-onefile.spec # PyInstaller onefile
│   ├── preload_transformers.py       # 运行时钩子（transformers / nagisa 兼容）
│   ├── installer.iss                 # InnoSetup 脚本
│   └── smoke_*.py / verify_*.py      # 打包冒烟与验证
├── scripts/
│   ├── release.py              # 一键发布：bump + 五产物 + 文档快照
│   ├── e2e_check.py            # 端到端链路检查
│   └── bdelete/ clean_*/ move_old/ sh_delete.py  # 沙箱安全删除工具
├── tests/                      # pytest（24 文件 / 372 用例）
├── docs/                       # 双语手册 + assets/ 概念图
├── data/                       # settings.json + history.db（运行时；日志在 %LOCALAPPDATA%\Bili Note\logs）
├── output/                     # 转写产物（运行时）
└── pyproject.toml              # 版本单一来源
```

---

## 5. 并发与线程模型

### 5.1 三层结构

```
┌──────────────┐   Store / SQLite      ┌──────────────────┐
│   UI 线程     │ ─── 只读轮询 0.6s ──▶ │  SQLite + JSON   │
│  (NiceGUI)   │ ◀── 事件 deque ────── │     (Store)      │
└──────────────┘     maxlen=200        └──────────────────┘
                                              ▲ 只写
┌─────────────────────────────┐               │
│  工作线程池 (默认 2，可配 1~6) │ ──────────────┘
│  download / convert 可重叠     │
│  transcribe 串行（引擎锁）     │
└─────────────────────────────┘
```

### 5.2 为什么下载并发、转写串行

下载和转码是 **I/O 与 CPU 密集混合**，多路并行能显著提升吞吐：任务 A 在转写时，任务 B 可以同时下载，A 结束后 B 的音频已就绪，几乎无缝。

转写必须串行：多个任务同时往同一块显卡塞数据会打满显存触发 `CUDA out of memory`，全部失败。串行在总吞吐上反而更优。

### 5.3 三层锁

| 锁 | 位置 | 保护对象 |
|---|---|---|
| `_model_lock` | `Pipeline` | `_transcriber` 实例的懒加载 / 替换（设置变更时重建） |
| 引擎 `self._lock` | 各 transcriber | `transcribe()` 串行化，确保同一时刻只有一个任务占模型 / 显存 |
| double-checked lock | 各引擎 `load()` | 判空 + 加载整体在锁内，杜绝冷启动双加载 |

`_model_lock` 只在**拿到引擎引用**时短暂持有；in-flight 转写持有的是自己的引用，因此设置变更不会打断正在跑的任务。

### 5.4 队列去重机制

```python
self._scheduled: set[str] = set()   # media_id 已在队列中
```

- `_enqueue(task)` 统一入队，入队时 `add`。
- worker 取到任务时 `discard`（表示"已不在队"）。
- 三处入队（`submit_local` / `submit_prepared` / `rerun`）全部走 `_enqueue`。

**解决的真实 bug**：曾出现「submit_prepared → start()」与「submit_prepared 自身入队」双重入队，同一个 media_id 被两个 worker 同时处理，一个删了 wav 另一个就 `FileNotFoundError`。`_scheduled` 是唯一裁决点。

### 5.5 单实例保护

两个进程同时处理同一批任务会互相删中间文件。保护机制：

```python
name = f"Local\\bili-transcriber-{sha1(str(DATA_DIR))[:12]}"
h = ctypes.windll.kernel32.CreateMutexW(None, False, name)
if ctypes.get_last_error() == ERROR_ALREADY_EXISTS (183):
    webbrowser.open(existing_url)   # 打开已有窗口
    return                          # 自己退出
```

命名里带 `DATA_DIR` 的哈希，因此**不同数据目录可以并行跑多个实例**（比如一个正式版一个测试版），同目录则互斥。

**子进程必须排除在外**。NiceGUI 会 spawn 子进程并重跑模块的 `__mp_main__` 分支，子进程再走一次 `main()` 必然抢不到互斥体，然后按"已有实例"分支**又开一个浏览器标签页**（真机日志每次启动都多一行 `__mp_main__: 已有实例在运行,本进程退出`）。因此判定顺序里先查进程身份：

```python
if sys.platform != "win32" or multiprocessing.current_process().name != "MainProcess":
    return True      # 子进程不是"第二个实例",既不抢互斥体也不开浏览器
```

### 5.6 模型空闲卸载

`MODEL_IDLE_UNLOAD_SEC = 600`。队列空且超过 10 分钟无任务时卸载模型，释放显存 / 内存。下次任务提交自动重新加载（懒加载）——只损失一次冷启动时间。

对 CLI 一次性进程无所谓，对长时间挂机的桌面端能省下 1.6 GB 内存。

---

## 6. 状态机与任务恢复

### 6.1 状态集合（单一来源）

```python
# app/pipeline.py
ACTIVE_STATUSES = frozenset({
    "queued", "downloading", "converting", "transcribing",
    "extracting", "saving", "paused",
})
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
```

`app/ui/state.py` 直接 `from app.pipeline import ACTIVE_STATUSES` 再导出。以前两处各写一份，改了一处忘了另一处就会让「进行中」判断漂移——现在物理上不可能。

### 6.2 状态流转

```
queued
  └→ downloading ──→ converting ──→ transcribing ──┬→ saving → done
                                     │              │
                                     └→ extracting ─┘   (开启讲义时)
                     任意阶段 ──→ failed / cancelled
                     任意阶段 ──→ paused ──→ (resume) 回到原阶段
```

### 6.3 启动恢复

`Pipeline.start()` 除了拉起 worker，还做**遗留任务恢复**：

```python
for job in self.store.list_jobs():
    if job.media_id in self._scheduled:      # 已在队，别重复
        continue
    if job.status not in ACTIVE_STATUSES:    # 只看进行中
        continue
    job.status, job.progress, job.error, job.finished_at = "queued", 0.0, "", ""
    self.store.upsert_job(job)
    self._enqueue(Task(kind=..., url=job.url, media_id=job.media_id))
    recovered += 1
```

**为什么必须覆盖全部 ACTIVE_STATUSES 而不只是 queued / paused**：实测 `--no-wait` 提交后 CLI 进程退出，任务永久停在 `transcribing`，桌面端再也不理它。修成恢复全部进行中状态后，同样的场景能自动续跑成功。

### 6.4 重复提交的语义

`submit_prepared` / `submit_local` 遇到已在处理中的 media_id 时，**不重新入队**，返回：

```python
{"media_id": ..., "title": ..., "status": "running", "stage": <当前状态>}
```

调用方（CLI / MCP / UI）据此可以区分「新提交」和「已经在跑」。这也是防止双 worker 抢同一任务的第一道闸。

---

## 7. 模块职责

### 7.1 `paths.py` — 路径解析

```python
def app_root() -> Path   # frozen = exe 所在目录；源码 = 项目根
def data_dir() -> Path   # app_root()/data
```

**CLI / MCP 必须与 GUI 指向同一份 `data/`**，否则 AI 调用看不到桌面端的历史和设置，会读写两套互相独立的数据库。

### 7.2 `main.py` — 启动与 UI 装配

`main()` 的执行顺序（顺序即正确性）：

1. `setup_logging()`
2. 解析端口（`BILI_PORT`，默认 8765）
3. **单实例检查** → 失败则 `webbrowser.open(已有地址)` 并 `return`
4. `pipeline.start()`（含遗留任务恢复）
5. 托盘、UI、浏览器打开

单实例检查必须在 `pipeline.start()` **之前** —— 否则第二个实例会先把任务标成 queued 再被踢，留下脏状态。

UI 线程每 **0.6s** 轮询 `Store` 并消费事件 `deque(maxlen=200)`。

### 7.3 `pipeline.py` — 核心编排

| 方法 | 职责 |
|---|---|
| `submit(url)` | 提交链接（内部 probe 后转 `submit_prepared`） |
| `submit_local(path)` | 提交本地文件（内容哈希作 media_id） |
| `submit_prepared(entries)` | 提交已探测的条目列表，返回每条的受理结果 |
| `_enqueue(task)` | 唯一入队点，维护 `_scheduled` |
| `rerun(media_id)` | 重置状态并重新入队 |
| `pause/resume/cancel` | 转调 `task_control` |
| `apply_settings(settings)` | 热应用设置；worker 数变化时重建线程池 |
| `prewarm()` | 后台线程加载模型 |
| `maybe_unload_model()` | 空闲卸载 |
| `stalled(media_id)` | 判断任务是否卡住（默认 60s 无进度） |

### 7.4 `headless.py` — 服务层

对外 API：`submit` / `status` / `list_jobs` / `wait` / `transcript` / `cancel` / `prewarm` / `get_settings` / `set_settings` / `close`。

**三个关键设计：**

**① `json_safe(value)`** — 递归把 `NaN` / `±Infinity` 转成 `None`。所有出站 JSON 都过它，且 `json.dumps(..., allow_nan=False)`。默认的 `allow_nan=True` 会写出裸 `NaN`，JS 侧 `JSON.parse` 直接报错。

**② `_validate_settings(changes)`** — 白名单校验 `engine` / `device` / `compute_type`，`output_dir` 做**写探针**（实际创建目录 + 写临时文件）。硬错误抛 `ServiceError`；`cookie_file` / `model_dir` 不存在只返回 warning（可能稍后才创建）。

**③ `_has_orphan(ids)`** — `wait()` 里检测「无存活 worker + 队列空 + 存在 queued」的组合，命中则返回 `{"stalled": True, "hint": "..."}` 而不是死等超时。这对应「CLI 提交了任务但本进程没在调度它」的真实场景。

**④ `channel_block_reason(channel)`** — 读 `settings.json` 判断 `enable_cli` / `enable_mcp`，被关时返回一段可直接展示给用户的中文原因（含"怎么改回来"）。只读文件、不建 `Service`。

**⑤ 已持久化 vs 本次生效** — `Service` 同时持有 `_persisted`（磁盘真相）与 `settings`（叠加了本次进程的临时覆盖）。`get_settings()` 报前者，并把临时覆盖单列在 `session_overrides` 里：

```python
data = asdict(self._persisted)
if self._overrides:
    data["session_overrides"] = dict(self._overrides)
```

原因：`transcribe(engine="sensevoice")` 只在本进程生效、不写盘。以前 `get_settings()` 回显被覆盖后的对象，AI 会据此告诉用户"我已把你的引擎改成 sensevoice"，其实磁盘上没变。

`set_settings()` 改成了**读-改-写**：先重新读一遍磁盘，再套用本次改动的键。否则长驻的 MCP 进程会用启动时那份内存副本把用户在设置页刚改的开关覆盖回去 —— 这是"通道开关"这类安全语义字段上最不能容忍的失步。

### 7.5 `cli.py` — 命令行

**编码自适应（重要）**：

```python
def _dump_json(payload, indent=2):
    safe = json_safe(payload)
    text = json.dumps(safe, ensure_ascii=False, indent=indent, allow_nan=False)
    try:
        text.encode(_stream_encoding())      # 探测 stdout 能否表示
    except (UnicodeEncodeError, LookupError):
        text = json.dumps(safe, ensure_ascii=True, indent=indent, allow_nan=False)
    return text
```

`_write()` 再兜一层 `backslashreplace`。

**为什么必须这么做**：PyInstaller `console=False` 打包出的 exe，复用父进程管道，`sys.stdout.encoding` 取系统 ANSI 代码页 —— 中文 Windows 是 **GBK**。此时 `print('🎉')` 直接抛 `UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f389'`，CLI 崩在最后一步。

测法：`PYTHONIOENCODING=gbk` 跑一遍，确认输出仍是合法 JSON（ASCII 转义也能被 `JSON.parse` 解析）。

**通道门禁与退出码**。`main()` 的顺序被刻意安排成：`--version` → `--mcp` 转交 → **门禁** → `setup_logging()` → 建 `Service`。门禁放在建 Service 之前，是因为通道关闭时不该去开数据库（否则白拿一把 SQLite 锁、白建一个 pipeline），测试 `test_cli_does_not_touch_database_when_disabled` 直接断言工厂没被调用。

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 任务失败 / 执行错误（含参数值非法） |
| 2 | 参数错误（缺值、互斥冲突） |
| 3 | 通道已在设置页关闭 |

`--version` 不受门禁影响 —— 排查问题时至少还能拿到版本号。

**断管不抛栈**。`cli ... | head -1` 会让 stdout 提前关闭，Python 默认抛 `BrokenPipeError`，退出码变 1，脚本误判成"任务失败"。`_write()` 捕获 `(BrokenPipeError, OSError, ValueError)` 后把 `sys.stdout` 指向 `devnull`，同时避免解释器退出时再 flush 一次报 `Exception ignored in: <_io.TextIOWrapper>`。

**输出被管道接管时强制 UTF-8**。`main()` 的第一行是 `_configure_output_encoding()`：`stdout`/`stderr` 若不是 tty（被重定向或作为子进程管道），就 `reconfigure(encoding="utf-8", errors="replace")`；仍是终端则保持控制台代码页。

为什么非改不可：打包版自带运行时，**不继承调用方的 `PYTHONUTF8` / `PYTHONIOENCODING`**，中文 Windows 下 stdout 编码取系统 ANSI 代码页 `cp936`。此时 JSON 里的中文按 GBK 写出 —— 本机 `subprocess.run(..., text=True)` 用 locale 解码能读，但按 UTF-8 解析的客户端（Node `child_process`、显式 `encoding="utf-8"` 的 Python 调用方）会得到乱码甚至 `UnicodeDecodeError`，而这正是 CLI 通道的主要使用者。

早期实现只在"编码装不下"时降级 `ensure_ascii=True`（`_dump_json`），但 **GBK 装得下常用汉字，降级分支根本不会触发** —— 输出于是长期是"人能看、机器读错"的 GBK。改流之后 `_stream_encoding()` 返回 `utf-8`，两条路径终于一致。

终端不动是有意的：`cmd.exe` 默认代码页直接写 UTF-8 会花屏，人读的那一份得顺着控制台。

### 7.6 `mcp_server.py` — MCP 服务端

手写 JSON-RPC 2.0 子集，不引第三方 SDK（降低打包体积与依赖风险）。

支持方法：`initialize` / `tools/list` / `tools/call` / `notifications/initialized` / `ping`。

**健壮性处理：**

| 问题 | 处理 |
|---|---|
| 参数类型不可靠（AI 常把 `limit` 传成字符串） | `_arg_int` / `_arg_num` / `_pick` 容错转换；`"10"` 能解析，`"abc"` 给可读错误 |
| 客户端发批量请求（JSON 数组） | 逐条 `handle()` 后**展平**为单个响应数组 |
| 结果里有 NaN | `_tool_result()` 统一 `json_safe` + `allow_nan=False` |
| 被非管道方式启动（stdin/stdout 为 None） | `_reopen_null_streams()` 写日志并返回 `False` → `main()` 返回 1 |
| 客户端无条件拉 `resources/list` / `prompts/list` | 回空清单而不是 `-32601`：个别客户端拿到方法不存在会把整个服务标记为不可用 |
| 任何遗留 `print` / 第三方库输出 | `_isolate_stdout()`：协议句柄存局部变量，`sys.stdout` 改指 stderr |
| 客户端塞进非 UTF-8 字节 | stdin `errors="replace"`；`serve()` 捕获解码/读错误后结束会话而不是抛栈 |
| 通道被设置页关闭 | `main()` 在建 Service 之前拒绝，原因写 stderr，**stdout 保持干净**，退出码 1 |

**为什么 stdout 必须隔离**：MCP stdio 的帧就是"一行一个 JSON"。只要有任意一行非 JSON 混进 stdout，客户端就会 `JSON.parse` 失败或直接断开连接 —— 排查起来极难，因为是自己代码里一句遗留的 `print`。把协议通道单独摘出来是 LSP / MCP server 的通行做法：

```python
proto = sys.stdout          # 只给协议用
sys.stdout = sys.stderr     # 其余输出(含日志、库的 print)统统去 stderr
serve(server, sys.stdin, proto)
```

**为什么非法字节不能抛栈**：一行坏字节把进程打崩,客户端看到的是"服务意外退出",而真正原因(编码)藏在 traceback 里。实测:`serve()` 原来用 `iter(stdin.readline, "")`,遇到 `ÿþ` 直接 `UnicodeDecodeError`;改成显式 `readline()` + `try/except` 后,坏行降级成 `-32700 Parse error` 响应,会话继续。

### 7.7 `downloader.py` — 下载编排

- **平台识别**：按域名分流 `bilibili / youtube / douyin / xiaohongshu / kuaishou`，未知平台按 B 站兼容处理；media_id 前缀 `yt_` / `dy_` / `xhs_` / `ks_`。
- **短链安全**：b23.tv 等走**域名白名单** + **DNS-rebinding 防护**（解析 IP 复核）后才跟随重定向。
- **Cookie / 代理**：`cookiefile`（cookies.txt）优先于 `cookiesfrombrowser`；浏览器名不在 `SUPPORTED_BROWSERS` 时忽略，避免 YoutubeDL 构造抛错。
- **探测缓存**：`_reach_cache` 在代理变更后清空重测。
- **微信视频号主动拦截**：没有免登录直链，直接给出改用本地上传的提示，而不是让它下载失败。

### 7.8 三转写引擎

| 模块 | 后端 | 时间戳策略 | 语言映射 |
|---|---|---|---|
| `transcriber.py` | faster-whisper / ctranslate2 | 原生 | whisper 代码 |
| `qwen_transcriber.py` | qwen-asr (torch) + ForcedAligner | aligner 对齐；缺失时按时长降级估算 | `_map_qwen_language()` whisper→全名，未知回退 auto |
| `sensevoice_transcriber.py` | sherpa-onnx int8 | token 级时间戳聚合 `_tokens_to_segments()` | `_map_sensevoice_language()` |

共性：

- **懒加载 + 缓存键** `(model_size, device, compute_type, language, vad)`，变了才重载。
- **VAD 自愈**：Silero VAD 滤除全部语音（纯音乐）时自动关闭重试一次。
- **Qwen 分块**：长音频切 60s 块逐块推理，`on_event` 上报「第 i/n 块」进度。

### 7.9 `writers.py` — 落盘

```python
def _write_text_retry(path, text, attempts=6):
    tmp = path.with_name(f".{path.name}.tmp")
    for i in range(attempts):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)      # 原子替换
            return path
        except PermissionError:
            time.sleep(0.2 * (i + 1))  # 线性退避
    raise
```

先写 `.tmp` 再 `os.replace`，配 6 次 `PermissionError` 退避。解决两个真实问题：杀毒软件扫目录时短暂锁定文件、用户正用别的程序打开产物。**不会留下半截文件**。

### 7.10 `models.py` / `gpu_runtime.py`

- `default_model_dir()` → `%LOCALAPPDATA%\Bili Note\models`；`resolve_model_dir()` 支持自定义。
- `scan_models()` / `describe_models()` 按三引擎特征文件判断完整性，驱动设置页「检测模型」。
- `gpu_runtime` 在 `%LOCALAPPDATA%\Bili Note\gpu` 下按 `nvidia/{cublas,cudnn,cuda_runtime,cuda_nvrtc}/bin/` 找 17 个 CUDA DLL（`_dll_root()` 以 `cublas64_12.dll` 是否存在判定可用，兼容扁平布局），`add_dll_directory` + PATH 双注入后 ctranslate2 才能加载。
  - **为什么必须双注入**：ctranslate2 以 `CUDA_DYNAMIC_LOADING=ON` 编译，延迟加载 cuBLAS 不走 Windows 安全搜索模式，只注册 `add_dll_directory` 时 ctypes 能加载但转写仍失败。
  - **句柄不能丢**：`add_dll_directory` 返回的句柄被 GC 回收后搜索路径立即失效（表现为模型加载成功、转写时才崩），所以用 `_dll_handles` 长期持有。
  - **注册幂等**：`_registered_dirs` 防止每次转写都重复前置 PATH 导致无限膨胀。
- `ensure_gpu_or_cpu()` 缺失包时静默回退 CPU，并提前 `ctypes.WinDLL("cublas64_12.dll")` 验证可加载，避免转写中途才崩。

### 7.11 `keyframes.py` — 双后端

| 后端 | 判定方式 | 适用 |
|---|---|---|
| ffmpeg（优先） | `select='gte(scene,0)'` + `metadata=print` 流式解析每帧分数，阈值 0.11 | 快，几乎总能成功 |
| PyAV（回退） | 64×36 灰度 MAD > 15 | ffmpeg 不可用时 |

公共策略：首帧必截；切换后 0.4s 截取；最小间隔 5s；上限 400 帧。`KeyframeError` 与用户取消不回退，直接上抛。

### 7.12 `ui/` 三件套

- `state.py` — 模块首次导入时创建单例（store / settings / pipeline / events），与拆分前 `main.py` 行为一致。
- `pages.py` — 四页签布局。
- `helpers.py` — 纯动作函数，不含布局；`_launch_explorer` 等 UI 名字可被测试 monkeypatch。

**表格行级事件的坑**：NiceGUI `$emit('open-path', path)` 后 `e.args` 是 `[path]` 列表，需 `_first_arg(e)` 解包。explorer 的 `/select,"path"` 必须用引号包裹，否则含空格的路径被截断。

---

## 8. 数据契约

### 8.1 设置（16 项）

```jsonc
{
  "output_dir": "output",         // 相对 app_root 或绝对路径
  "model_size": "large-v3-turbo",
  "device": "auto",               // auto / cuda / cpu
  "compute_type": "auto",         // auto / float16 / int8_float16 / int8
  "language": "",                 // 空 = 自动
  "keep_audio": true,
  "vad": true,
  "notes": false,
  "engine": "whisper",            // whisper / qwen3-asr / sensevoice
  "model_dir": "",                // 空 = 默认目录
  "cookies_from_browser": "",
  "cookie_file": "",
  "proxy": "",
  "workers": 2,                   // 1~6
  "enable_cli": true,             // CLI 通道开关(仅桌面端可改)
  "enable_mcp": true              // MCP 通道开关(仅桌面端可改)
}
```

后两项**不在 `OVERRIDABLE_SETTINGS` 里**：`--set` 与 MCP 的 `update_settings` 都会拒绝它们。理由不是洁癖 —— 一旦允许写，一个被提示注入的 AI 就能先 `update_settings(enable_mcp=true)` 长期留在机器上，或反过来把自己锁死来制造"服务坏了"的假象。权限边界放在人这一侧。

读取方（`channel_block_reason`）直接读 `settings.json` 而不构造 `Service`：通道关闭时不该去开数据库、白建 pipeline。解析失败时按默认值放行 —— 这道开关用于收敛外部调用面，不是抗篡改边界（能改文件的人本就能改回来），而把配置搞坏的用户若连 CLI 都进不去反而更难自救。

### 8.2 任务记录（SQLite `jobs` 表）

主键 `media_id`，UPSERT 写入。字段：`media_id` / `url` / `title` / `status` / `progress` / `error` / `source_type` / `output_dir` / `created_at` / `finished_at`。

`source_type` 决定恢复时用 `local` 还是 `prepared` 任务类型 —— **恢复 prepared 而不是 url**，避免多 P 视频在恢复时被重新 probe 成整批，把用户单独跑的那一 P 又拉回队列。

### 8.3 输出目录与文件名规则

规律集中在两处，改任意一处都会影响全部产物命名：

**目录**（`Pipeline._job_dir`）：

```python
folder = info.playlist_title or info.title           # 合集优先用整体标题
name = _safe_name(folder) if not info.bv else f"{info.bv}_{_safe_name(folder)}"
if unique_suffix:                                     # 本地任务用 media_id 去重
    name = f"{name}_{_safe_name(unique_suffix)}"
```

- `info.bv` 由 `_bilibili_id()` 取下划线前部分，**已剥掉 `_p2` 后缀** —— 所以同一合集的所有分 P 落进同一个目录。
- 本地文件 `info.bv` 为空，`unique_suffix = media_id`（`local_<sha1>`），因此目录是 `{文件名}_{hash}`，同名不同内容不会撞。

**文件名主干**（`downloaders.output_stem`）：

```python
stem = _safe_name(info.title) or info.media_id
if info.total_parts > 1:
    stem = f"{stem}_P{info.part:02d}"
```

`total_parts > 1` 时才加 `_P01` 后缀，因为合集里各 P 标题常常重复——不加会互相覆盖。单 P 保持原名以兼容既有产物。

`_safe_name()` 把 `<>:"/\|?*` 替换成 `_`，去尾部空格与点，并截断到 80 字符。

### 8.4 服务层返回结构

```jsonc
// submit
{"accepted": 2, "running": 1, "skipped": 1, "jobs": [{"media_id": "...", "status": "queued"}]}

// wait
{"state": "done", "done": 3, "failed": 0, "jobs": [...], "waited_sec": 12.4}

// wait（异常场景）
{"state": "stalled", "stalled": true, "hint": "任务处于排队态但本进程未在调度它..."}

// transcript
{"media_id": "...", "format": "md", "chars": 4123, "truncated": false, "text": "..."}
```

---

## 9. 安全与健壮性设计

| 风险 | 措施 |
|---|---|
| SSRF / DNS rebinding（短链展开） | 域名白名单 + 解析 IP 复核后才跟随重定向 |
| 文件名注入 | `_safe_name()` 去非法字符 + 长度截断 |
| 非法设置导致启动崩溃 | 保存前白名单校验 + 输出目录写探针 |
| 产物半截 / 写入失败 | 原子写 + PermissionError 退避重试 |
| JSON 崩溃 JS 端 | 出站统一 `json_safe` + `allow_nan=False` |
| 打包 exe 编码崩溃 | stdout 编码探测 + `ensure_ascii` 降级 + `backslashreplace` |
| 双进程抢任务 | `CreateMutexW` 单实例 + `_scheduled` 去重 |
| 通道被随意开关 | 两个开关只在桌面端可写，`--set` / `update_settings` 拒绝 |
| 桌面端与 AI 同时写库 | `PRAGMA busy_timeout=5000`（WAL 单写者，撞锁改为等锁） |
| 第三方库 print 污染 MCP 协议 | `_isolate_stdout()` 把 `sys.stdout` 改指 stderr，协议句柄单独持有 |
| 客户端塞非 UTF-8 字节 | stdin `errors="replace"` + `serve()` 捕获解码/读错误，降级为 `-32700` 而非崩进程 |
| 僵尸任务 | 启动恢复全部 `ACTIVE_STATUSES` + `wait()` 的 stalled 检测 |
| Cookie 泄露 | 只在下载时交给 yt-dlp，不进日志、不打包 |
| 扫描/删除误伤 | `scripts/bdelete/` 等工具要求显式路径，不做通配删除 |

---

## 10. 打包兼容性坑位

| 问题 | 对策 |
|---|---|
| transformers 误检 sklearn → `GenerationMixin` 导入崩 | spec `excludes=["sklearn"]` + 钩子 `preload_transformers.py` 置 `_sklearn_available=False` |
| nagisa 裸导入 `prepro`（PEP 366）失败 | 钩子把 nagisa 包目录插入 `sys.path`（不能 `import nagisa.prepro`，会循环导入）；hiddenimports 补 `nagisa/dynet/_dynet` |
| 沙箱 safe-delete 钩子拦截 PyInstaller 清理 | `release.py` 先 `_evacuate()` 把旧 dist/build 改名撤离，`--noconfirm` 无物可删 |
| onefile 原生窗口白屏 | 默认浏览器模式；`BILI_FORCE_NATIVE=1` 显式开启 |
| CUDA DLL 定位 | `__init__.py` frozen 模式注入 `sys._MEIPASS` 搜索路径 + `gpu_runtime` 注册外置包 |
| **窗口态 exe 的 stdout 是 GBK** | CLI 编码探测 + `ensure_ascii` 降级；日志 handler 在 `sys.stderr is None` 时不挂 |
| **`console=False` 时 stdin/stdout 为 None** | `logging_setup` 判空；MCP `_reopen_null_streams()` 明确报错而非 `AttributeError` |
| NiceGUI `multiprocessing` 重跑模块 | 判断 `multiprocessing.current_process().name` 避免子进程重复建 UI |

---

## 11. 构建与发布

### 11.1 版本单一来源

`pyproject.toml` 的 `version` 是唯一真源。`release.py` 自动同步：

1. `app/__init__.py` 的 `APP_VERSION`
2. `build/installer.iss` 的 `AppVersion`
3. **四份手册头部声明** + 生成带版本后缀的快照
4. **README 头部声明**（只维护滚动版，不生成快照）

`tests/` 有版本三处一致性校验，漏同步会直接测试失败。

### 11.2 五产物

| 产物 | 输出 |
|---|---|
| Wheel | `dist/bili_transcriber-{ver}-py3-none-any.whl` |
| 便携包 | `dist/bili-transcriber-portable-{ver}.zip`（onedir 全量，约 6900 文件） |
| 安装包 | `dist/bili-transcriber-setup-{ver}.exe`（InnoSetup） |
| 单文件版 | `dist/bili-transcriber-single-{ver}.exe`（onefile，CPU 模式） |
| GPU 加速包 | `dist/bili-transcriber-gpu-{ver}.zip`（17 个 CUDA DLL） |

产物校验：zip `testzip()`、PE 头（`MZ` / `PE`）、`_built_fresh()` 时间戳防陈旧产物误发。

### 11.3 命令

```powershell
python scripts/release.py              # 一键：bump + 五产物 + 文档快照
python scripts/release.py --wheel-only # 快速交付
python scripts/release.py --bin-only   # 仅便携 + 安装包（版本已定）
python scripts/release.py --setup-only # 仅 onedir + 安装包
python scripts/release.py --single-only# 仅重建单文件版
```

---

## 12. 测试体系

`tests/` 共 **24 个文件 / 372 用例**，全绿约 1~3 分钟。

| 领域 | 文件 | 策略 |
|---|---|---|
| 存储 | `test_store.py` | 真实 SQLite 临时库 |
| 下载 | `test_downloader.py` / `test_kuaishou.py` | mock yt-dlp / SSR HTML 夹具 |
| 三引擎 | `test_transcriber` / `test_qwen_transcriber` / `test_sensevoice_transcriber` | FakeModel 注入；**并发 load() 回归**（`Barrier(2)` 断言只加载一次） |
| 编排 | `test_pipeline.py` | FakeDownloader / FakeTranscriber mock 外部边界；含「重复提交不重排」「遗留任务恢复」「半路被杀恢复」 |
| 服务层 | `test_headless.py` | running 语义 / stalled 检测 / prewarm / 4 项设置校验 / 状态别名 |
| CLI | `test_cli.py` | 参数解析、JSON 输出、**GBK stdout 回归**、非整数 `--set` |
| MCP | `test_mcp_server.py` | 协议握手、9 工具、非法参数容错、**批量展平**、**NaN 清洗** |
| 落盘 | `test_writers.py` | 原子写重试、无半截文件 |
| UI | `test_ui.py` / `test_main_helpers.py` | NiceGUI `user_plugin`；`_first_arg` / explorer 引号单测 |
| 发布 | `test_release_docs.py` | 文档版本头部自动同步 |
| 打包 | `test_nagisa_ascii.py` 等 | 运行时钩子与兼容性 |

### 12.1 对抗性审查方法论

本项目采用三视角迭代审查：

| 视角 | 关注点 | 典型产出 |
|---|---|---|
| **工具实现者** | 边界条件、资源清理、编码、线程安全 | `_scheduled` 去重、原子写、编码降级 |
| **审查者** | 契约一致性、错误路径、重复代码 | `ACTIVE_STATUSES` 单一来源、`json_safe` 统一出口 |
| **AI 使用方** | 参数是否好传、错误是否可读、状态是否可判 | `running` 语义、stalled 提示、`_arg_int` 容错 |

原则：**先拿到真实报错再改**。上面每一条修复都对应一次实际运行的失败输出，而不是"看起来可能有问题"。

### 12.2 运行

```powershell
.venv\Scripts\pytest -q                       # 全量
.venv\Scripts\pytest tests/test_cli.py -v     # 单文件
```

---

## 13. 已知限制

- **仅验证 Windows**。macOS / Linux 未验证：`native_dialog`（comdlg32）、`main.py` 单实例（`CreateMutexW`）、`ui/helpers` 的 explorer 调用都是 Windows 专属；其他平台会降级但未测试。
- **GPU 提速只覆盖 whisper**（ctranslate2）。Qwen3-ASR 走 torch，有卡会快但不是同一套优化；SenseVoice 走 onnxruntime CPU。
- **yt-dlp 无法在打包版内单独升级**。平台接口变更后需要发新版。
- **快手 SSR 解析依赖分享页结构**，站点改版需同步更新 `app/kuaishou.py`。
- **沙箱 / 受限环境下 PyInstaller 重跑可能被安全删除策略拦截**，最终 exe 构建需在本机完整权限下完成。
- **MCP 只实现 JSON-RPC 子集**，不支持 resources / prompts / sampling 等更上层能力。
- **讲义关键帧上限 400 帧**，超长视频会漏掉部分页面。
