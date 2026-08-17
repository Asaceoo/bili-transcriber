# bili-transcriber 技术手册（中文）

> 面向开发者与维护者。说明架构、模块职责、数据流、线程模型、关键机制与构建发布流程。

## 1. 系统概览

bili-transcriber 是一个 **纯本地的 B站音频转写桌面工具**。核心链路：

```
B站链接 → yt-dlp 下载最佳音频流 → FFmpeg 转码 16kHz 单声道 WAV
        → faster-whisper 本地推理 → 输出 SRT / TXT / Markdown
```

设计原则：数据不上传、断点续跑、GPU 资源单点串行、UI 与计算线程解耦。

## 2. 技术栈

| 层 | 选型 |
|----|------|
| 语言 | Python ≥ 3.11（开发环境 3.14） |
| 下载 | yt-dlp |
| 转码 | FFmpeg（subprocess 调用） |
| 推理 | faster-whisper + ctranslate2（CUDA 后端） |
| 语音活动检测 | Silero VAD（faster-whisper 内建） |
| UI | NiceGUI（Quasar 组件） + pywebview（原生窗口） |
| 存储 | SQLite（history.db 历史索引）+ JSON（settings.json） |
| 构建 | hatchling（wheel）+ PyInstaller（便携 exe）+ InnoSetup（安装包） |

## 3. 目录结构

```
bili-transcriber/
├── app/                  # 应用源码（8 模块）
│   ├── main.py           # NiceGUI 三页签 UI + 事件循环
│   ├── pipeline.py       # 核心编排：状态机 + 工作线程
│   ├── downloader.py     # yt-dlp 封装：probe + 下载
│   ├── transcriber.py    # faster-whisper 封装：懒加载 + GPU 优先
│   ├── converter.py      # FFmpeg 封装：转码为 16kHz 单声道 WAV
│   ├── writers.py        # SRT / TXT / Markdown 落盘
│   ├── store.py          # SQLite + JSON 设置
│   └── __init__.py       # 版本/路径常量 + frozen 模式 DLL 搜索
├── build/
│   ├── bili-transcriber.spec   # PyInstaller 配置
│   ├── installer.iss          # InnoSetup 安装包脚本
│   └── build.bat              # 一键打包入口
├── scripts/
│   ├── e2e_check.py    # 端到端链路检查（probe/下载）
│   └── release.py      # 一键发布：bump 版本 + wheel + 便携 + 安装包
├── tests/              # pytest 套件（19~20 用例）
├── data/              # settings.json + history.db（运行时）
├── output/            # 转写产物（运行时）
└── pyproject.toml
```

## 4. 模块职责

### 4.1 `main.py` — UI 与事件循环
- 三页签：**任务 / 历史 / 设置**。
- UI 主线程每 **0.6s** 轮询 `Store` 并消费任务事件 `deque`，刷新界面。
- `BILI_PORT` 环境变量可覆盖监听端口（默认 8765），用于避开 Windows 下 `TIME_WAIT` 端口冲突（`uvicorn` 默认未启用 `SO_REUSEADDR`）。
- `frozen` 模式（PyInstaller 打包后）下 `BASE_DIR` 指向 `sys._MEIPASS`，用于定位 nvidia DLL 与资源。

### 4.2 `pipeline.py` — 核心编排
- **单工作线程串行**：所有条目在同一线程内顺序 `[下载 → 转码 → 转写 → 落盘]`，避免并发抢占 GPU 显存。
- **状态机**：`queued → downloading → converting → transcribing → saving → done / failed`。
- **转写全程持锁**：`transcriber.transcribe()` 期间持有锁，确保同一时刻只有一个转写任务使用模型/显存。
- **断点续跑**：
  - 已下载的音频路径缓存于 `job.audio_path`；重跑时若文件仍存在则跳过下载。
  - 已转码的 WAV 缓存于 `job.wav_path`；重跑时若文件仍存在则跳过转码。
  - 任务失败重跑时，`progress` 重置、状态复位，但不重复下载/转码。
- **缓存清理容错**（v0.1.1 修复）：落盘后删除中间产物（WAV）与可选原始音频时，若删除被环境安全策略（如沙箱回收站不可用）拦截抛出 `OSError`，**降级为 warning 日志，任务仍标记 `done`**，不再误判 `failed`。

### 4.3 `downloader.py` — yt-dlp 封装
- `probe(url)`：用 yt-dlp 探测链接，自动展开 **单 P / 多 P / 合集** 为条目列表（每个条目含 `media_id`、标题、URL）。
- `download(entry, dest)`：下载最佳音频流（优先 `m4a`/`bestaudio`），返回本地路径。
- 失败时抛出结构化异常，由 pipeline 捕获并标记 `failed`。

### 4.4 `transcriber.py` — faster-whisper 封装
- **模型懒加载**：首次调用才加载模型；加载后常驻内存。
- **缓存键**：`(model_size, device, compute_type, language, vad)`；仅当键变化时才重新加载，避免重复加载约 1.6 GB 模型。
- **GPU 优先**：`device=auto` 时检测 CUDA 可用性，不可用则回退 CPU。
- **VAD 自愈**：Silero VAD 滤除全部语音（如纯音乐/唱歌场景）导致零片段时，自动 **关闭 VAD 重试** 一次。

### 4.5 `converter.py` — FFmpeg 封装
- `to_wav16k_mono(src, dst)`：调用 FFmpeg 将任意音频转码为 **16kHz 单声道 16-bit WAV**（faster-whisper 的最佳输入格式）。
- 通过 subprocess 调用，捕获 stderr 用于错误诊断。

### 4.6 `writers.py` — 产物落盘
- `write_srt / write_txt / write_md`：分别输出时间戳字幕、纯文本、带时间戳 Markdown。
- 输出目录结构：`{output_dir}/{BV id}_{title}/{part title}.{ext}`。

### 4.7 `store.py` — 存储
- **SQLite**（`data/history.db`）：历史任务索引，`UPSERT` 主键为 `media_id`，记录状态、进度、输出路径。
- **JSON**（`data/settings.json`）：用户设置（模型、设备、精度、语言、keep_audio、vad、输出目录）。

## 5. 数据流

```
URL
 └─ downloader.probe → [entry, entry, ...]          # 展开单P/多P/合集
      └─ for entry in entries (串行):
           1. downloader.download → audio_path       # 命中缓存则跳过
           2. converter.to_wav16k_mono → wav_path    # 命中缓存则跳过
           3. transcriber.transcribe(wav) → segments # 持锁；GPU 优先
           4. writers.write_* → 落盘 SRT/TXT/MD
           5. 清理中间产物（WAV，可选原始音频）        # 失败降级为 warning
           └─ store.update(job: done)
 UI 线程 0.6s 轮询 Store + 消费事件 deque → 刷新界面
```

## 6. 线程模型

```
┌─────────────┐     Store/DB      ┌──────────────────┐
│  UI 线程    │ ─── 只读轮询 ───▶ │                  │
│ (NiceGUI)   │ ◀── 事件 deque ── │   SQLite + JSON   │
└─────────────┘                   │   (Store)        │
                                  └──────────────────┘
                                        ▲ 只写
                                        │
┌─────────────┐                   ┌──────────────────┐
│ 工作线程    │ ─── 写 Store ───▶ │  Pipeline         │
│ (1 个串行)  │                   │  download→conv→   │
└─────────────┘                   │  transcribe→write │
                                  └──────────────────┘
```

设计要点：
- 工作线程只写 Store / 事件队列；UI 线程只读并轮询。两者不直接共享可变状态，避免竞态。
- 转写为 GPU 重负载，单线程串行 + 转写持锁，规避显存争用与 OOM。

## 7. 大陆网络优化

README 与代码内置以下适配（便于国内环境开箱即用）：
- **模型镜像**：首次下载走 `hf-mirror.com`（Hugging Face 国内镜像），仅一次。可经 `HF_ENDPOINT` 切换官方源。
- **SSL 自愈**：启动清理由 Anaconda 等残留的、指向缺失文件的 `SSL_CERT_FILE`，避免握手失败。
- **CUDA 库注入**：nvidia 系列库经 pip 轮子（而非系统 PATH）注入，打包后由 `__init__.py` 的 `sys._MEIPASS` 定位。
- **禁用 Xet**：避免 git/Git LFS 相关网络开销。

## 8. 构建与发布

### 8.1 版本管理
- **单一来源**：`pyproject.toml` 的 `version`（如 `0.1.1`）。
- `scripts/release.py` 自动 `bump`（默认 patch +1），并同步 `build/installer.iss` 的 `AppVersion`。
- 三产物文件名均带版本后缀，符合「每次发布必须带版本号」规范。

### 8.2 三产物
| 产物 | 命令/流程 | 输出 |
|------|-----------|------|
| Wheel | `python -m build` (hatchling) | `dist/bili_transcriber-{ver}-py3-none-any.whl` |
| 便携版 | PyInstaller + `zipfile` 进程内打包 | `dist/bili-transcriber-portable-{ver}.zip` |
| 安装包 | InnoSetup (`build/installer.iss`) | `dist/bili-transcriber-setup-{ver}.exe` |

> 便携版使用 Python `zipfile` 进程内压缩，规避 PowerShell `Compress-Archive` 在某些沙箱被安全删除钩子拦截而中断整个进程树的问题。`do_portable` 已加固：PyInstaller 失败但 `dist/bili-transcriber/` 已存在时，自动复用现有构建继续打 zip。

### 8.3 打包命令
```powershell
# 一键（bump + wheel + 便携 + 安装包）
python scripts/release.py

# 仅 wheel
python scripts/release.py --wheel-only
```

## 9. 测试

`tests/` 共 19~20 个用例，覆盖：

| 模块 | 用例数 | 策略 |
|------|--------|------|
| store | 5 | 真实 SQLite 临时库 |
| downloader | 5 | mock yt-dlp |
| writers | 3 | 比对落盘内容 |
| pipeline | 5 | `FakeDownloader` / `FakeTranscriber` mock 外部边界；`converter.to_wav16k_mono` 替换为写文件 lambda |
| ui | 1 | NiceGUI `user_plugin` 注入 |
| + 回归 | 1 | `test_safe_delete_failure_keeps_job_done`：模拟 safe-delete 拦截，验证任务仍 `done` |

运行：
```powershell
.venv\Scripts\pytest -q
```

## 10. 已知限制

- 仅验证 Windows；macOS/Linux 需自行验证 pywebview 原生窗口。
- 应用代码当前不读取/显示内部版本号（仅文件名带版本后缀）。
- 沙箱/受限环境下 PyInstaller 二次重跑可能被安全删除策略拦截，需在本机环境完成最终 exe 构建。
