# B站音频本地转写桌面工具 (bili-transcriber)

粘贴 B 站视频链接(支持单 P / 多 P / 合集),自动完成:

**yt-dlp 下载音频 → FFmpeg 转码 → faster-whisper GPU 本地转写 → 输出 SRT / TXT / Markdown**

纯本地处理,不上传任何数据。

## 环境要求

- Windows + Python 3.11+(开发环境为 3.14)
- FFmpeg(需在 PATH 中,或手动放到项目目录)
- NVIDIA GPU(可选,CPU 也能跑,只是慢)

## 安装

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
```

首次转写会自动从 Hugging Face 下载 `large-v3-turbo` 模型(约 1.6 GB),保存在用户缓存目录,只需下载一次。

## 运行

```powershell
.venv\Scripts\python -m app.main
```

或安装后直接:

```powershell
.venv\Scripts\bili-transcriber
```

## 输出

```
output/{BV号}_{标题}/
├── {分P标题}.srt      # 带时间戳字幕
├── {分P标题}.txt      # 纯文本
├── {分P标题}.md       # 带时间戳的 Markdown
└── {分P标题}.m4a      # 原始音频(可在设置中关闭保留)
```

## 常见问题

- **下载/解析失败**:B 站接口变动导致,升级 yt-dlp:`pip install -U yt-dlp`
- **转写很慢**:确认设置中设备为 cuda;首次运行需下载模型
- **显存不足**:设置中把计算精度从 float16 改为 int8,或换用更小的模型
- **唱歌/纯音乐视频**:Silero VAD 只识别人声说话,遇到此类内容会自动关闭 VAD 重试,属正常现象

## 技术说明

- **模型下载**:大陆网络默认走 `hf-mirror.com` 镜像并禁用 Xet 协议;需要官方源时设置环境变量 `HF_ENDPOINT=https://huggingface.co`
- **CUDA 库**:cuBLAS/cuDNN 通过 pip 轮子(`nvidia-cublas-cu12`、`nvidia-cudnn-cu12`)安装,启动时自动注册 DLL 路径,无需手动装 CUDA Toolkit
- **环境自愈**:启动时自动清理指向不存在文件的 `SSL_CERT_FILE` 等证书环境变量(Anaconda 常见问题)
- **断点续跑**:下载的音频与转码的 wav 都会缓存,失败后重跑不会重复下载/转码

## 文档

| 文档 | 链接 |
|------|------|
| 用户手册（中文） | [docs/user-guide-zh.md](docs/user-guide-zh.md) |
| User Guide (English) | [docs/user-guide-en.md](docs/user-guide-en.md) |
| 技术手册（中文） | [docs/technical-manual-zh.md](docs/technical-manual-zh.md) |
| Technical Manual (English) | [docs/technical-manual-en.md](docs/technical-manual-en.md) |

## 开源许可

本项目基于 [MIT License](LICENSE) 开源。纯本地处理,音频与转写文本均不上传,可放心用于隐私敏感或离线批量场景。

## 贡献

欢迎提交 Issue / Pull Request。开发入口 `python -m app.main`,测试 `pytest -q`,一键发布 `python scripts/release.py`。
