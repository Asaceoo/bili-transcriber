# B站音频本地转写桌面工具 (bili-transcriber)
<img width="3200" height="1929" alt="521118ebd34554bd" src="https://github.com/user-attachments/assets/6e8d0614-d772-4300-94d0-6ddf5020dfef" />

[user-guide-zh-0.1.32.md](https://github.com/user-attachments/files/32939689/user-guide-zh-0.1.32.md)

# B站音频本地转写工具 — 用户手册（中文）

> 项目名：**bili-transcriber**
> 一句话功能：粘贴视频链接（B站 / 抖音 / 小红书 / 快手 / YouTube），本地自动完成「下载 → 转码 → 本地 AI 转写 → 导出字幕」。

本工具**完全本地运行**，音频与字幕不会上传任何服务器，适合对隐私敏感或需要离线批量处理的场景。

> 发布版本快照 **v0.1.32** — 由 `scripts/release.py` 自动生成,版本号取自 `pyproject.toml` 单一来源。

---

## 1. 功能简介

- 支持 **单 P / 多 P / 合集** 链接，自动展开为多个转写任务。
- **多平台**：B站、抖音、小红书、快手（免登录直链解析）、YouTube（需代理）。
- **三识别引擎**（设置页切换）：
  | 引擎 | 模型 | 特点 |
  |------|------|------|
  | Whisper（默认） | large-v3-turbo / large-v3 / medium / small / base / tiny | 准确率最高，支持 GPU 加速 |
  | Qwen3-ASR | Qwen3-ASR-0.6B / 1.7B | 阿里通义千问，中文表现好 |
  | SenseVoice | SenseVoiceSmall | 体积小、速度快，CPU 也能跑 |
- 纯本地推理，数据不上传。
- 断点续跑：失败重跑不重复下载 / 转码。
- 图文讲义（可选）：自动截取 PPT 翻页关键帧，生成带截图的讲义 Markdown。
- 系统托盘常驻：关窗后服务不退出，可随时重新打开窗口。

---

## 2. 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11（已验证） |
| GPU（可选） | NVIDIA 显卡 + CUDA 12.x；无 GPU 自动回退 CPU，仅速度更慢 |
| FFmpeg | **无需安装**：内置随包静态版 |
| 网络 | 首次使用需联网下载模型（走国内镜像）；之后完全离线可用 |

> 若没有 NVIDIA 显卡，程序会自动回退 CPU 推理，无需任何配置。

---

## 3. 安装（三种方式任选其一）

### 方式一：安装版（推荐普通用户）
双击 `bili-transcriber-setup-0.1.32.exe`，按向导完成安装。桌面/开始菜单生成快捷方式。

### 方式二：便携包
解压 `bili-transcriber-portable-0.1.32.zip` 到任意目录，双击其中的 `bili-transcriber.exe` 即可。**不写注册表**，适合 U 盘携带或多机使用。

### 方式三：单文件版
直接运行 `bili-transcriber-single-0.1.32.exe`（CPU 模式）。首次启动需解压，稍慢；追求 GPU 提速请用前两种。

### 方式四：开发模式（源码运行）
```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m app.main
```

### GPU 加速包（可选）
安装版 / 便携包默认为 CPU 推理。要让 Whisper 走 NVIDIA 显卡：

1. 下载 `bili-transcriber-gpu-0.1.32.zip`（约 1.3 GB，内含 CUDA 运行库）。
2. 解压到 `%LOCALAPPDATA%\Bili Note\gpu\` 目录。
3. 重启应用——程序**自动检测并注册** CUDA 库，无需配置环境变量。

---

## 4. 启动应用

- 双击快捷方式或 exe；开发模式用 `.venv\Scripts\python -m app.main`。
- 默认以**浏览器模式**启动并自动打开 `http://127.0.0.1:8765`（100% 兼容，不依赖 WebView2）。
- 关闭窗口后程序**退到系统托盘**继续运行：托盘图标右键可「打开主窗口」或「退出」。
- 端口被占用时：设环境变量 `BILI_PORT=9000` 换端口。
- 想要原生窗口（需已安装 WebView2）：设环境变量 `BILI_FORCE_NATIVE=1`。

---

## 5. 使用流程

### 任务（Tasks）
1. 在输入框粘贴视频链接（B站 / 抖音 / 小红书 / 快手 / YouTube 均可）。
2. 点击「添加」入队。系统自动探测并展开为若干条目。
3. 状态机推进：`queued → downloading → converting → transcribing → extracting(可选) → saving → done / failed`。
4. 任务列表实时刷新，**每行可单独**：打开所在文件夹、重新运行失败项、取消任务。

### 转写本地文件（两种入口）
- **选择文件（推荐）**：点「选择文件(推荐,立即转写)…」弹出**系统原生文件框**，直接引用原路径，零拷贝秒提交，大文件首选。
- **浏览器上传**：仅在用手机/其他电脑访问本机网页时使用；走 base64 传输，大文件明显偏慢属正常。上传副本保存在 `uploads/`，不会被自动删除。

支持格式：mp4 / mkv / mov / avi / webm / flv / wmv / mp3 / m4a / wav / ogg / opus / flac / aac。

### 历史（History）
- 已处理任务及产出路径索引（本地 SQLite 存储）。
- 每行可打开所在文件夹 / 重新运行。

### 设置（Settings）
所有设置保存在本地 `data/settings.json`，提供「恢复默认」一键还原。详见下节。

---

## 6. 设置项详解

| 设置 | 可选值 | 说明 |
|------|--------|------|
| 识别引擎 | whisper / qwen3-asr / sensevoice | 切换后模型列表自动跟随 |
| 转写模型 | 按引擎而定（见第 1 节表格） | whisper 默认 `large-v3-turbo` |
| 设备 | auto / cuda / cpu | 有 N 卡选 auto 即可 |
| 计算精度 | auto / float16 / int8_float16 / int8 | 显存不足改 int8（占用减半） |
| 语言 | 空=自动检测 / zh / en | 中文视频指定 zh 提速降错 |
| 保留音频 | 开/关 | 是否保留下载的原始音频 |
| VAD 语音检测 | 开/关 | 默认开启；纯音乐场景自动关闭重试 |
| 图文讲义 | 开/关 | 开启后 B站任务改下视频流（体积大），截 PPT 翻页帧生成讲义 |
| 模型目录 | 路径 | 默认 `%LOCALAPPDATA%\Bili Note\models`；可放手动下载的模型包，设置页一键检测完整性 |
| 浏览器 Cookie | 不使用 / Chrome / Edge / Firefox / Brave / Opera / Vivaldi | 抖音精选、部分 YouTube 视频需要 |
| cookies.txt 路径 | 文件路径 | Netscape 格式，**优先于**浏览器 Cookie |
| 网络代理 | 如 `http://127.0.0.1:7890` | YouTube 等境外平台需要 |
| 输出目录 | 路径 | 默认 `output/` |

---

## 7. 输出文件格式

每个视频在输出目录下生成一个子文件夹：

```
output/{平台ID}_{标题}/
├── {标题}.srt              # 带时间戳字幕（可直接导入剪辑软件）
├── {标题}.txt              # 纯文本稿
├── {标题}.md               # 带时间戳的 Markdown
├── {标题}.m4a              # 原始音频（设置中可关闭保留）
├── {标题}.notes.md         # 图文讲义（开启 notes 后生成）
└── {标题}_frames/slide_*.jpg  # 讲义关键帧截图（移动时需与 notes.md 一起）
```

### 图文讲义说明
- 每页 = 可点击时间戳标题 + 关键帧截图 + 该画面期间的字幕。
- 本地上传 / 快手任务直接用原视频抽帧；B站任务开启讲义后改为下载视频流（音频从视频抽取，不重复下载）。
- 讲义生成失败**不影响**转写结果；纯音频文件自动跳过讲义。

---

## 8. 常见问题（FAQ）

**Q1：下载 / 解析失败？**
B站/抖音接口偶有变动，优先升级下载器：`.venv\Scripts\pip install -U yt-dlp`（开发模式）。

**Q2：抖音 / YouTube 提示需要登录或访问受限？**
设置页配置**浏览器 Cookie**（先用对应浏览器登录目标网站），或导出 `cookies.txt` 填入路径；YouTube 另需配置**网络代理**。

**Q3：转写很慢？**
- 确认设备为 `auto`/`cuda` 并安装了 **GPU 加速包**（见第 3 节）。
- 首次运行需下载模型（whisper large-v3-turbo 约 1.6 GB），之后不再下载。
- Qwen3-ASR-1.7B 在 CPU 上约为 0.6B 的 3 倍耗时，程序会主动提示。

**Q4：显存不足（CUDA out of memory）？**
计算精度改 `int8`，或换更小模型（如 `medium` / Qwen3-ASR-0.6B）。

**Q5：唱歌 / 纯音乐视频没有字幕？**
VAD 只识别人声。程序遇此类内容会**自动关闭 VAD 重试**；若仍为空，多半是视频本身无人声。

**Q6：设置页「检测模型」提示缺失文件？**
模型包不完整。删除该模型目录后让程序重新下载，或从设置页提示的官方地址重新获取。

**Q7：端口被占用？**
`BILI_PORT=9000` 换端口启动。

**Q8：关了窗口任务还在跑吗？**
在。程序退到系统托盘继续转写，从托盘可重新打开窗口。

---

## 9. 故障排查

- **运行日记**：任务页下方实时滚动，报错原因与阶段一目了然。
- **模型下载慢 / 失败**：默认走 `hf-mirror.com` 国内镜像；需官方源时设 `HF_ENDPOINT=https://huggingface.co`。
- **SSL 证书报错**：程序启动时已内置环境自愈，一般无需处理。
- **GPU 包未生效**：确认解压路径为 `%LOCALAPPDATA%\Bili Note\gpu\`（其下直接是 cudnn/cublas 等 DLL），重启应用。

---

## 10. 隐私说明

所有下载、转码、转写均在本地完成，不依赖任何外部 AI API。仅两类网络访问：① 首次模型下载（镜像站）；② 视频本身来自公开平台。字幕内容不会离开你的机器。

---

*更多技术细节见 [技术手册（中文# bili-transcriber 技术手册（中文）

> 面向开发者与维护者。说明架构、模块职责、并发模型、数据流、安全机制、构建发布流程与测试体系。
> 发布版本快照 **v0.1.32** — 由 `scripts/release.py` 自动生成,版本号取自 `pyproject.toml` 单一来源。

## 1. 系统概览

bili-transcriber 是一个**纯本地的多平台视频/音频转写桌面工具**。核心链路：

```
链接(B站/抖音/小红书/快手/YouTube)
  → yt-dlp / 平台专用解析 下载最佳音频流（或视频流）
  → FFmpeg 转码 16kHz 单声道 WAV
  → 本地推理引擎（faster-whisper / Qwen3-ASR / SenseVoice 三选一）
  → 输出 SRT / TXT / Markdown（可选图文讲义 notes.md）
```

设计原则：数据不上传、断点续跑、GPU 资源锁保护、UI 与计算线程解耦。

## 2. 技术栈

| 层 | 选型 |
|----|------|
| 语言 | Python ≥ 3.11（开发环境 3.14） |
| 下载 | yt-dlp（B站/抖音/小红书/YouTube）+ 自研快手 SSR 解析 |
| 转码 | FFmpeg（subprocess；内置 imageio-ffmpeg 静态版，`app/ffmpeg_bin.py` 解析） |
| 推理引擎 1 | faster-whisper + ctranslate2（CUDA 后端，默认） |
| 推理引擎 2 | qwen-asr（Qwen3-ASR 0.6B/1.7B，torch 后端） |
| 推理引擎 3 | sherpa-onnx（SenseVoiceSmall，ONNX int8） |
| 关键帧 | FFmpeg scene 检测（优先）/ PyAV 逐帧 MAD（回退），双后端 |
| UI | NiceGUI（Quasar 组件），默认浏览器模式；pywebview 原生窗口可选 |
| 托盘 | pystray（系统托盘常驻） |
| 原生对话框 | ctypes comdlg32（Windows 文件选择框） |
| 存储 | SQLite（history.db 历史索引）+ JSON（settings.json 设置） |
| 构建 | hatchling（wheel）+ PyInstaller（onedir/onefile）+ InnoSetup（安装包） |

## 3. 目录结构

```
bili-transcriber/
├── app/                        # 应用源码（20 模块）
│   ├── main.py                 # NiceGUI 三页签 UI + 事件绑定 + 启动模式选择
│   ├── pipeline.py             # 核心编排：状态机 + WORKERS=2 线程池 + 引擎锁
│   ├── downloader.py           # 下载编排：平台识别/短链/SSR/Cookie/代理/DNS防护
│   ├── kuaishou.py             # 快手分享页 SSR 解析（免登录提取 mp4 直链）
│   ├── transcriber.py          # 引擎1：faster-whisper 封装
│   ├── qwen_transcriber.py     # 引擎2：Qwen3-ASR 封装（分块转写+对齐器）
│   ├── sensevoice_transcriber.py # 引擎3：SenseVoice 封装（token→段时间戳）
│   ├── converter.py            # FFmpeg 转码 16kHz 单声道 WAV
│   ├── keyframes.py            # 关键帧提取（ffmpeg scene / PyAV 双后端）
│   ├── writers.py              # SRT/TXT/MD/notes.md 落盘
│   ├── models.py               # 模型目录扫描/检测/定位（三引擎）
│   ├── gpu_runtime.py          # 外置 GPU 加速包 DLL 注册
│   ├── ffmpeg_bin.py           # ffmpeg 二进制解析（PATH 优先，内置兜底）
│   ├── hardware.py             # 硬件探测（GPU 型号/显存/CUDA 可用性）
│   ├── store.py                # SQLite + JSON 设置（含 14 项设置定义）
│   ├── task_control.py         # 任务级 暂停/取消 控制（线程安全）
│   ├── native_dialog.py        # Windows 原生文件选择框（ctypes comdlg32）
│   ├── tray.py                 # 系统托盘（pystray，失败静默降级）
│   ├── logging_setup.py        # 日志初始化
│   └── __init__.py             # APP_VERSION + frozen 模式 DLL 搜索路径
├── build/
│   ├── bili-transcriber.spec        # PyInstaller onedir 配置
│   ├── bili-transcriber-onefile.spec# PyInstaller onefile 配置
│   ├── preload_transformers.py      # 打包运行时钩子（transformers/nagisa 兼容）
│   ├── installer.iss                # InnoSetup 安装包脚本
│   └── smoke_*.py / verify_*.py     # 打包冒烟与验证脚本
├── scripts/
│   ├── release.py              # 一键发布：bump + wheel + 便携 + 安装包 + 单文件 + GPU 包
│   ├── e2e_check.py            # 端到端链路检查
│   └── bdelete/clean_*/move_old/sh_delete.py  # 沙箱安全删除工具
├── tests/                      # pytest 套件（21 文件 / 243 用例）
├── docs/                       # 中英双语用户手册 + 技术手册
├── data/                       # settings.json + history.db（运行时）
├── output/                     # 转写产物（运行时）
└── pyproject.toml              # 版本单一来源
```

## 4. 模块职责（要点）

### 4.1 `main.py` — UI 与事件循环
- 三页签：**任务 / 历史 / 设置**；UI 线程每 **0.6s** 轮询 `Store` 并消费事件 `deque(maxlen=200)`。
- 引擎切换联动：`model_select` 选项按引擎自动切换（`MODEL_OPTIONS` / `QWEN_MODEL_OPTIONS` / `SENSEVOICE_MODEL_OPTIONS`），非法组合自动纠正。
- 表格行级事件：NiceGUI `$emit('open-path', path)` → `e.args` 为 `[path]` 列表，经 `_first_arg(e)` 解包后 `_open_path()` 降级打开（文件缺失→父目录→输出目录）；explorer `/select,"path"` 引号包裹防空格断参。
- 启动模式：默认浏览器模式（不依赖 WebView2，100% 可靠）；`BILI_FORCE_NATIVE=1` 时尝试 pywebview 原生窗口；`BILI_PORT` 覆盖端口（默认 8765）。

### 4.2 `pipeline.py` — 核心编排
- **并发模型（v0.1.32）**：`WORKERS = 2` 工作线程并发取队列（下载/转码可重叠），**转写由三层锁保护**：
  - `_model_lock`（pipeline）：保护 transcriber 实例的懒加载/替换（设置变更时重建）。
  - 引擎内部 `self._lock`：`transcribe()` 串行化，确保同一时刻只有一个任务占用模型/显存。
  - 引擎 `load()` 为 **double-checked locking**：判空+加载整体在锁内，杜绝冷启动双加载（v0.1.32 修复，含三引擎并发回归测试）。
- **状态机**：`queued → downloading → converting → transcribing → extracting(可选) → saving → done / failed`；支持任务级暂停/取消（`task_control.py`）。
- **断点续跑**：`audio_path` / `wav_path` 缓存复用，重跑跳过已完成的下载/转码。
- **缓存清理容错**：删除中间产物被沙箱拦截抛 `OSError` 时降级 warning，任务仍 `done`。

### 4.3 `downloader.py` — 下载编排
- **平台识别**：按域名分流 `bilibili / youtube / douyin / xiaohongshu / kuaishou`，未知平台按 B站兼容处理；media_id 前缀 `yt_/dy_/xhs_/ks_`。
- **短链与安全**：b23.tv 等短链展开走**域名白名单**校验 + **DNS-rebinding 防护**（解析 IP 复核）后再跟随重定向；抖音分享链经 `modal_id` / 重定向两种正则提取视频 id。
- **Cookie/代理**：`cookiefile`（cookies.txt）优先于 `cookiesfrombrowser`（浏览器名不在 SUPPORTED_BROWSERS 时忽略，避免 YoutubeDL 构造抛错）；代理形如 `http://127.0.0.1:7890` / `socks5://…`。
- **网络探测缓存**：`_reach_cache` 在代理变更后清空重测。

### 4.4 三转写引擎
| 模块 | 后端 | 时间戳 | 语言映射 |
|------|------|--------|----------|
| `transcriber.py` | faster-whisper/ctranslate2 | 原生 | whisper 代码 |
| `qwen_transcriber.py` | qwen-asr(torch) + ForcedAligner | aligner 对齐；缺失时按时长降级估算 | `_map_qwen_language()` whisper→全名，未知回退 auto |
| `sensevoice_transcriber.py` | sherpa-onnx int8 | token 级时间戳聚合 `_tokens_to_segments()` | `_map_sensevoice_language()` |

- **模型懒加载 + 缓存键**：`(model_size, device, compute_type, language, vad)` 变化才重载。
- **VAD 自愈**：Silero VAD 滤除全部语音（纯音乐）时自动关闭重试一次。
- **Qwen 分块转写**：长音频切 60s 块逐块推理，`on_event` 上报「第 i/n 块」进度；1.7B 在 CPU 上主动提示慢。

### 4.5 `models.py` — 模型管理
- `default_model_dir()`：`%LOCALAPPDATA%\Bili Note\models`；`resolve_model_dir()` 支持用户自定义。
- `scan_models()` / `describe_models()`：按三引擎特征文件识别目录完整性（缺文件/版本匹配/单 qwen 目录回退），结果驱动设置页「检测模型」。
- `find_{whisper,qwen_asr,sensevoice}_model()`：从模型目录定位具体模型；在线下载经 `download_root` 落到统一模型目录。

### 4.6 `gpu_runtime.py` — 外置 GPU 加速包
- 检测 `%LOCALAPPDATA%\Bili Note\gpu` 下的 CUDA DLL（cudnn/cublas/cufft 等 17 个），`add_dll_directory` + PATH 注入后 ctranslate2 才能加载。
- `ensure_gpu_or_cpu()`：GPU 包缺失时静默回退 CPU，用户无感。

### 4.7 `keyframes.py` — 关键帧提取（双后端）
- **ffmpeg 后端（优先）**：`select='gte(scene,0)'` + `metadata=print` 流式解析每帧分数（阈值 0.11），切换点 `-ss` 快照；逐帧回调支撑进度/取消。
- **PyAV 后端（回退）**：64x36 灰度 MAD>15 判定切换。
- 公共策略：首帧必截；切换后 0.4s 截取；最小间隔 5s；上限 400 帧；`KeyframeError` 与用户取消不回退直接上抛。

### 4.8 `store.py` — 存储
- **SQLite**（`data/history.db`）：历史索引，UPSERT 主键 `media_id`。
- **JSON**（`data/settings.json`）14 项：`output_dir / model_size / device / compute_type / language / keep_audio / vad / notes / engine / model_dir / cookies_from_browser / cookie_file / proxy`。

## 5. 数据流

```
URL → downloader.probe（平台识别+短链展开）
      └─ for entry in entries（WORKERS=2 并发取队列）:
           1. download → audio_path          # 缓存命中跳过
           2. to_wav16k_mono → wav_path      # 缓存命中跳过
           3. transcriber.transcribe(wav)    # 引擎锁串行；GPU 优先
           4. (notes) keyframes → writers.write_notes_md
           5. writers.write_srt/txt/md → 落盘
           6. 清理中间产物                    # 失败降级 warning
           └─ store.update(job: done)
UI 线程 0.6s 轮询 Store + 消费事件 deque → 刷新界面
```

## 6. 线程模型

```
┌─────────────┐     Store/DB      ┌──────────────────┐
│  UI 线程    │ ─── 只读轮询 ───▶ │   SQLite + JSON   │
│ (NiceGUI)   │ ◀── 事件 deque ── │   (Store)        │
└─────────────┘                   └──────────────────┘
                                        ▲ 只写
┌────────────────────────┐              │
│ WORKERS=2 工作线程      │ ─────────────┘
│  download / convert 可重叠 │
│  transcribe 串行(引擎锁)   │
└────────────────────────┘
```

设计要点：工作线程只写 Store/事件队列，UI 只读轮询，无共享可变状态竞态；转写 GPU 重负载经引擎锁串行，规避显存争用与 OOM。

## 7. 打包兼容性（PyInstaller 坑位清单）

| 问题 | 对策 |
|------|------|
| transformers 误检 sklearn → GenerationMixin 导入崩 | spec `excludes=["sklearn"]` + 运行时钩子 `preload_transformers.py` 置 `_sklearn_available=False` |
| nagisa 裸导入 `prepro`（PEP366）失败 | 钩子将 nagisa 包目录 `sys.path` 插入（不可 `import nagisa.prepro`，会循环导入）；hiddenimports 补 `nagisa/dynet/_dynet` |
| 沙箱 safe-delete 钩子拦截 PyInstaller 清理 | `release.py` 先 `_evacuate()` 把旧 dist/build 目录改名撤离，`--noconfirm` 无物可删 |
| onefile 原生窗口白屏 | 默认浏览器模式；`BILI_FORCE_NATIVE=1` 显式开启 |
| CUDA DLL 定位 | `__init__.py` frozen 模式注入 `sys._MEIPASS` 搜索路径 + `gpu_runtime` 注册外置包 |

## 8. 构建与发布

### 8.1 版本管理
- **单一来源**：`pyproject.toml` 的 `version`。
- `scripts/release.py` 自动 bump 并同步 **三处**：`build/installer.iss` 的 `AppVersion`、`app/__init__.py` 的 `APP_VERSION`（tests 有三处一致性校验）。

### 8.2 五产物
| 产物 | 输出 |
|------|------|
| Wheel | `dist/bili_transcriber-{ver}-py3-none-any.whl` |
| 便携包 | `dist/bili-transcriber-portable-{ver}.zip`（onedir 全量，~6900 文件） |
| 安装包 | `dist/bili-transcriber-setup-{ver}.exe`（InnoSetup） |
| 单文件版 | `dist/bili-transcriber-single-{ver}.exe`（onefile，CPU 模式） |
| GPU 加速包 | `dist/bili-transcriber-gpu-{ver}.zip`（17 个 CUDA DLL，解压到 %LOCALAPPDATA%） |

产物进程内校验：zip `testzip`、PE 头（MZ/PE）、`_built_fresh()` 时间戳防陈旧产物误发。

### 8.3 命令
```powershell
python scripts/release.py            # 一键：bump + 全部产物
python scripts/release.py --wheel-only
python scripts/release.py --single-only
python scripts/release.py --setup-only
```

## 9. 测试

`tests/` 共 **21 个文件 / 243 用例**（v0.1.32 全绿），覆盖：

| 领域 | 文件（示例） | 策略 |
|------|--------------|------|
| 存储 | test_store.py | 真实 SQLite 临时库 |
| 下载 | test_downloader.py / test_kuaishou.py | mock yt-dlp / SSR HTML 夹具 |
| 三引擎 | test_transcriber / test_qwen_transcriber / test_sensevoice_transcriber | FakeModel 注入；**并发 load() 回归**（Barrier(2) 断言仅加载一次） |
| 编排 | test_pipeline.py | FakeDownloader/FakeTranscriber mock 外部边界 |
| UI | test_ui.py / test_main_helpers.py | NiceGUI user_plugin；`_first_arg`/explorer 引号单测 |
| 打包 | test_nagisa_ascii.py 等 | 运行时钩子与兼容性 |
| 讲义 | test_keyframes.py / test_writers.py | 双后端阈值标定与落盘内容 |

运行：`.venv\Scripts\pytest -q`

## 10. 已知限制

- 仅验证 Windows；macOS/Linux 未验证（pywebview 原生窗口与 comdlg32 均为 Windows 专属）。
- Qwen3-ASR/SenseVoice 的 Qwen 为 CPU 可跑但慢；GPU 提速仅覆盖 whisper 引擎（ctranslate2）。
- 沙箱/受限环境下 PyInstaller 重跑可能被安全删除策略拦截，最终 exe 构建需在本机完成。
- 快手 SSR 解析依赖分享页结构，站点改版需同步更新 `app/kuaishou.py`。
）](technical-manual-zh.md) / [Technical Manual (English)](technical-manual-en.md)。*
[technical-manual-zh-0.1.32.md](https://github.com/user-attachments/files/32939693/technical-manual-zh-0.1.32.md)

