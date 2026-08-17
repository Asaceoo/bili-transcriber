# B站音频本地转写桌面工具

## 目标

粘贴 B站视频链接(支持单 P / 多 P / 合集),自动完成:yt-dlp 下载音频 → FFmpeg 转码 → faster-whisper GPU 本地转写 → 输出带时间戳的 SRT / TXT / Markdown 三种格式。NiceGUI 桌面窗口管理任务队列与历史。

## 技术选型

- **Python 3.11+**,pyproject.toml 管理依赖
- **yt-dlp**:下载 B站音频流(bestaudio, m4s→m4a)
- **faster-whisper `large-v3-turbo`**(用户有 N 卡,走 CUDA int8/fp16;设置里可切换 small/medium 模型)
- **FFmpeg**:系统依赖,音频转 16kHz 单声道
- **NiceGUI(native 桌面模式)**:界面 + 后台任务队列
- **SQLite**:历史记录索引

## 项目结构

```
D:\bilibili\
├── pyproject.toml / README.md
├── app/
│   ├── main.py        # NiceGUI 桌面入口
│   ├── downloader.py  # yt-dlp 封装:URL 解析(单P/多P/合集)、音频下载、进度回调
│   ├── converter.py   # ffmpeg 转码 wav 16k mono
│   ├── transcriber.py # faster-whisper 封装:模型懒加载、VAD 分段、进度百分比
│   ├── writers.py     # SRT / TXT / Markdown 输出
│   ├── store.py       # SQLite 历史 + 输出目录管理
│   └── pipeline.py    # 任务队列(下载可并行、转写串行走 GPU)、状态机与重试
└── tests/             # pytest,Downloader 用 mock 测试
```

输出目录:`output/{BV号}_{标题}/` 下放 `audio.m4a`(可配置是否保留)和每个分 P 的 `.srt` / `.txt` / `.md`。同一 BV 号重复添加时命中缓存,不重复下载转写。

## 实施步骤

1. **脚手架**:pyproject + 依赖安装(yt-dlp、faster-whisper、nicegui),检测 FFmpeg 与 CUDA 可用性
2. **下载与转码**:downloader + converter,拿真实视频验证音频落盘
3. **转写**:transcriber 加载 large-v3-turbo(CUDA),VAD 分段,时间戳对齐,writers 输出三格式
4. **任务队列与历史**:pipeline 状态机(排队/下载中/转写中/完成/失败)、SQLite 记录、缓存去重
5. **桌面 UI**:URL 输入框 + 添加按钮、任务列表实时进度条、历史记录页(搜索/重新转写/打开文件夹)、设置页(输出目录/模型选择/是否保留音频/计算精度)
6. **端到端验收**:真实 B站视频跑通全链路,确认 UI 进度与产物正确;README 写清 FFmpeg 安装与首次模型下载说明

## 验收标准

粘贴一个 B站视频链接,界面显示下载与转写进度,完成后在输出目录得到带时间戳的 SRT/TXT/MD 文件,历史记录可查。