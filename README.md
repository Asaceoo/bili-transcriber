# B站音频本地转写桌面工具 (bili-transcriber)

粘贴 B 站视频链接(支持单 P / 多 P / 合集),自动完成:

**yt-dlp 下载音频 → FFmpeg 转码 → faster-whisper GPU 本地转写 → 输出 SRT / TXT / Markdown**

纯本地处理,不上传任何数据。除 B 站链接外,**也支持直接上传本地视频 / 音频文件**进行转写(数据不出本机)。

可选开启**图文讲义**:检测画面切换(如 PPT 翻页)自动截取关键帧,配对应时段字幕,输出"看图说话"式 `{标题}.notes.md`,页标题时间戳可点击跳回视频对应位置——适合网课 / 公开课复习。

## 环境要求

- Windows + Python 3.11+(开发环境为 3.14)
- FFmpeg **无需安装**:内置随包静态版(imageio-ffmpeg);PATH 上有完整版时优先使用
- NVIDIA GPU(可选,CPU 也能跑,只是慢)

## 安装

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
```

首次转写会自动从 Hugging Face 下载 `large-v3-turbo` 模型(约 1.6 GB),保存在用户缓存目录,只需下载一次。

## 模型外置(可选)

安装包**不含模型权重**。除首次在线自动下载外,也支持**用户自行下载模型包放入模型目录**(适合大陆网络慢 / 离线环境):

1. 打开设置页 →「打开模型目录」(默认 `%LOCALAPPDATA%\Bili Note\models`)
2. 从 Hugging Face 下载 `Systran/faster-whisper-large-v3-turbo` 仓库,解压后整体放入模型目录
3. 设置页点击「刷新检测」,出现 `large-v3-turbo(whisper)` 即识别成功;后续转写优先从本地加载,不再联网

> 模型目录需为 faster-whisper 的 ctranslate2 格式(`model.bin` + `config.json`),不是 openai 原版 `.pt` 权重。
> 目录名可保持官方 `faster-whisper-{模型名}`,也可直接叫 `{模型名}`。

## 识别引擎

设置页可切换识别引擎(模型均外置,自放模型目录):

| 引擎 | 模型 | 特点 |
|------|------|------|
| **Whisper**(默认) | `large-v3-turbo` 等 | GPU/CPU 通用,模型 1.6GB |
| **Qwen3-ASR** | `Qwen3-ASR-0.6B` / `1.7B` | 中文/方言强,0.6B 可 CPU 跑,52 语言方言 |

Qwen3-ASR 模型从 ModelScope 下载:

```powershell
# 0.6B(CPU/低配机平衡点)
modelscope download --model Qwen/Qwen3-ASR-0.6B --local_dir "%LOCALAPPDATA%\Bili Note\models\Qwen3-ASR-0.6B"
# 1.7B(精度更高,但更慢、更吃内存)
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir "%LOCALAPPDATA%\Bili Note\models\Qwen3-ASR-1.7B"
```

- 建议同时放入 `Qwen3-ForcedAligner-0.6B` 以获得精确字级时间戳;未放置时自动降级为按句估算
- 1.7B 精度更高但更慢、更吃内存;0.6B 是 CPU/低配机的平衡点

## 运行

```powershell
.venv\Scripts\python -m app.main
```

或安装后直接:

```powershell
.venv\Scripts\bili-transcriber
```

## 本地文件转写

除粘贴 B 站链接外,「任务」页提供两种提交本机文件的方式:

- **选择文件(推荐)**:弹出系统文件框,选定后**直接转写原文件**(零拷贝、秒提交,大文件首选)。
- **浏览器上传**:适合用手机/其他电脑访问本机网页时使用;该通道按 base64 传输,大文件较慢。上传副本保存在本机 `uploads/` 目录(支持 mp4 / mkv / mov / avi / webm / flv / wmv / mp3 / m4a / wav / ogg / opus / flac / aac),属用户资产不会被自动删除。

两种方式都走与 B 站相同的本地转写流程,输出 SRT / TXT / MD(开启讲义时另有 notes.md + 截图)。

- 仅清理中间的临时 WAV 缓存
- 同名 / 同内容文件再次提交会自动跳过已完成的任务
- 时长由 ffprobe(缺失时回退 ffmpeg)探测,用于界面展示

## 输出

```
output/{BV号}_{标题}/
├── {分P标题}.srt      # 带时间戳字幕
├── {分P标题}.txt      # 纯文本
├── {分P标题}.md       # 带时间戳的 Markdown
├── {分P标题}.m4a      # 原始音频(可在设置中关闭保留)
├── {分P标题}.notes.md            # 图文讲义(可选,设置开启后生成)
└── {分P标题}_frames/slide_*.jpg  # 讲义关键帧截图(与 notes.md 同级,需一起移动)
```

## 图文讲义(可选)

设置页开启「生成图文讲义」后,任务在转写完成后追加一步:

- ffmpeg 场景检测找出画面切换(如 PPT 翻页),在切换后截取全分辨率 JPEG;无 ffmpeg 时自动回退 PyAV(随包内置,永不缺)
- 帧时间戳与字幕段落对齐,每页输出「时间戳标题 + 截图 + 该页期间的字幕」
- B 站任务此时**直接下载视频流**(音频转码阶段从视频抽取,同一内容不会下载两遍);本地上传 / 快手任务直接用原视频,无额外流量
- 讲义生成失败只记日志,不影响转写任务本身;纯音频文件自动跳过

## 常见问题

- **下载/解析失败**:B 站接口变动导致,升级 yt-dlp:`pip install -U yt-dlp`
- **转写很慢**:确认是否放置了 GPU 加速包(见下);CPU 模式建议切换 Qwen3-ASR 引擎或换小模型
- **显存不足**:设置中把计算精度从 float16 改为 int8,或换用更小的模型
- **唱歌/纯音乐视频**:Silero VAD 只识别人声说话,遇到此类内容会自动关闭 VAD 重试,属正常现象
- **讲义没有生成**:确认设置已开启且视频源含画面;失败原因见「运行日记」日志,不影响字幕输出

## GPU 加速(可选)

v0.1.15 起安装包**不再内置 CUDA 运行时**(核心包体积大幅缩小)。需要 GPU 加速的用户:

1. 下载 `bili-transcriber-gpu-<ver>.zip`(与安装包同目录发布)
2. 解压到 `%LOCALAPPDATA%\Bili Note\gpu\`(保持 `nvidia/` 目录结构)
3. 软件启动自动检测并启用 CUDA;未放置则回退 CPU,不影响使用

> 单文件版(`*-single-*.exe`)自 v0.1.16 起同样支持 GPU 加速包;更早版本的单文件版仅 CPU。

## 技术说明

- **模型下载**:大陆网络默认走 `hf-mirror.com` 镜像并禁用 Xet 协议;需要官方源时设置环境变量 `HF_ENDPOINT=https://huggingface.co`;Qwen3-ASR 模型用 ModelScope(`modelscope download --model Qwen/Qwen3-ASR-0.6B`)
- **CUDA 库**:v0.1.15 起不再内置,通过独立 GPU 加速包外置(`app/gpu_runtime.py` 检测 `%LOCALAPPDATA%/Bili Note/gpu` 并注册 DLL)
- **ffmpeg 二进制**(`app/ffmpeg_bin.py`):PATH 完整版优先,否则用 imageio-ffmpeg 随包静态版——用于讲义场景检测(yt-dlp 合并分离流 / 关键帧提取),转码主力仍是 PyAV
- **关键帧提取**(`app/keyframes.py`):ffmpeg 场景检测优先(流式解析 scene score,多线程 C 级速度),失败自动回退 PyAV 缩略图灰度帧差,两后端灵敏度等价标定
- **环境自愈**:启动时自动清理指向不存在文件的 `SSL_CERT_FILE` 等证书环境变量(Anaconda 常见问题)
- **断点续跑**:下载的音频与转码的 wav 都会缓存,失败后重跑不会重复下载/转码

## 文档

| 文档 | 链接 |
|------|------|
| 用户手册（中文） | [docs/user-guide-zh.md](docs/user-guide-zh.md) |
| User Guide (English) | [docs/user-guide-en.md](docs/user-guide-en.md) |
| 技术手册（中文） | [docs/technical-manual-zh.md](docs/technical-manual-zh.md) |
| Technical Manual (English) | [docs/technical-manual-en.md](docs/technical-manual-en.md) |

> 上表为**滚动更新的最新版**手册。每次发布时 `scripts/release.py` 会自动生成带版本号后缀的快照（如 `docs/user-guide-zh-0.1.32.md`），版本号取自 `pyproject.toml` 单一来源。

## 开源许可

本项目基于 [MIT License](LICENSE) 开源。纯本地处理,音频与转写文本均不上传,可放心用于隐私敏感或离线批量场景。

## 贡献

欢迎提交 Issue / Pull Request。开发入口 `python -m app.main`,测试 `pytest -q`,一键发布 `python scripts/release.py`。
