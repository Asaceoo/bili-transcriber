# bili-transcriber 技术手册（中文）

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
