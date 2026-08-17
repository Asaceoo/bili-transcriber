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
