# B站音频本地转写工具 — 用户手册（中文）

> 项目名：**bili-transcriber**
> 一句话功能：粘贴 B 站视频链接，本地自动完成「下载音频 → 转码 → GPU 转写 → 导出字幕」。

本工具**完全本地运行**，音频与字幕不会上传任何服务器，适合对隐私敏感或需要离线批量处理的场景。

---

## 1. 功能简介

- 支持 **单 P / 多 P / 合集** 链接，自动展开为多个转写任务。
- 流水线：**yt-dlp 下载音频 → FFmpeg 转码 → faster-whisper 本地 GPU 转写 → 输出 SRT / TXT / Markdown**。
- 纯本地推理，数据不上传。
- 断点续跑：失败重跑不重复下载 / 转码。
- 桌面窗口（pywebview + NiceGUI），也支持浏览器访问。

---

## 2. 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows（已在 Windows 10/11 验证） |
| Python | 3.11 及以上（开发环境为 3.14） |
| FFmpeg | 需在 `PATH` 中，或手动放到项目目录 |
| GPU（可选） | NVIDIA 显卡 + CUDA 12.x；无 GPU 也能跑，仅速度更慢 |

> 若没有 NVIDIA 显卡，程序会自动回退到 CPU 推理，只需在「设置」中把设备改为 `cpu` 或 `auto`。

---

## 3. 安装

```powershell
# 1. 进入项目目录
cd bili-transcriber

# 2. 创建虚拟环境
python -m venv .venv

# 3. 安装依赖（可编辑模式）
.venv\Scripts\pip install -e .
```

首次转写时，程序会自动从 Hugging Face 镜像（`hf-mirror.com`）下载 `large-v3-turbo` 模型（约 1.6 GB），保存在用户缓存目录，**仅需下载一次**。

> 需要切换到官方源时，设置环境变量：
> ```powershell
> $env:HF_ENDPOINT = "https://huggingface.co"
> ```

---

## 4. 启动应用

**方式一：开发入口（推荐日常使用）**

```powershell
.venv\Scripts\python -m app.main
```

**方式二：安装后直接运行入口**

```powershell
.venv\Scripts\bili-transcriber
```

启动后：

- 若当前为可见桌面会话，会弹出标题为「B站音频本地转写」的原生窗口。
- 同时提供 Web 界面，浏览器打开 `http://127.0.0.1:8765` 即可使用。
- 可用环境变量 `BILI_PORT` 修改监听端口（例如 `BILI_PORT=9000 python -m app.main`），用于规避端口冲突。

---

## 5. 使用流程

界面分为三个页签：

### 任务（Tasks）
1. 在输入框粘贴 B 站链接（视频 / 多 P / 合集均可）。
2. 点击「添加」入队。系统先用 yt-dlp 探测，自动展开为若干条目。
3. 流水线逐条处理，状态按以下状态机推进：

   ```
   queued → downloading → converting → transcribing → saving → done / failed
   ```

4. 任务列表实时显示进度与状态徽章，可单独查看或重新运行失败项。

### 上传本地视频 / 音频
除 B 站链接外，本工具也支持直接上传本地媒体文件进行转写：

1. 在「任务」页下方的 **上传本地视频 / 音频** 区域点击「选择文件」。
2. 支持格式：mp4 / mkv / mov / avi / webm / flv / wmv / mp3 / m4a / wav / ogg / opus / flac / aac。
3. 文件会先保存到本机 `uploads/` 目录，然后与 B 站任务相同的本地流水线转写，输出 SRT / TXT / MD。

> 说明：上传的文件属于用户资产，**不会被自动删除**（仅清理中间临时 WAV）；同名 / 同内容文件再次上传会自动跳过已完成任务。

### 历史（History）
- 已处理任务及其产出路径的索引（存储在本地 SQLite）。
- 可从这里重新打开输出文件或重新运行任务。

### 设置（Settings）
- 修改模型、设备、精度、语言、保留音频、VAD、输出目录等。
- 提供「恢复默认」按钮一键还原。
- 设置保存在本地 `data/settings.json`。

---

## 6. 设置项详解

| 设置 | 说明 | 建议 |
|------|------|------|
| 模型大小 | 转写模型，默认 `large-v3-turbo` | 准确率与速度平衡；显存小可换 `small`/`medium` |
| 设备 | `cuda` / `cpu` / `auto` | 有 N 卡选 `cuda` 或 `auto` |
| 计算精度 | `float16` / `int8` | 显存不足改 `int8`（精度略降、占用减半） |
| 语言 | 自动检测 / 指定中文等 | 中文视频指定 `zh` 可提速并降错 |
| 保留音频 | 是否保留下载的原始音频 | 关闭可节省磁盘 |
| VAD | 语音活动检测（Silero） | 默认开启；纯音乐 / 唱歌场景会自动关闭重试 |
| 输出目录 | 字幕落盘位置 | 默认 `output/`，可改绝对路径 |

---

## 7. 输出文件格式

每个视频（或分 P）在输出目录下生成一个子文件夹：

```
output/{BV号}_{标题}/
├── {分P标题}.srt      # 带时间戳字幕（可直接导入剪辑软件）
├── {分P标题}.txt      # 纯文本稿
├── {分P标题}.md       # 带时间戳的 Markdown
└── {分P标题}.m4a      # 原始音频（可在设置中关闭保留）
```

---

## 8. 常见问题（FAQ）

**Q1：下载 / 解析失败？**
B 站接口偶有变动，优先升级下载器：
```powershell
.venv\Scripts\pip install -U yt-dlp
```

**Q2：转写很慢？**
确认设置中设备为 `cuda`；首次运行需下载模型（约 1.6 GB），之后不再下载。

**Q3：显存不足（CUDA out of memory）？**
在设置中把计算精度从 `float16` 改为 `int8`，或换用更小的模型（如 `medium`）。

**Q4：唱歌 / 纯音乐视频没有字幕？**
Silero VAD 只识别人声说话。遇到此类内容程序会**自动关闭 VAD 重试**，属正常现象；若仍为空，多半是视频本身无人声。

**Q5：端口被占用（启动报错）？**
用 `BILI_PORT` 环境变量换一个端口，例如 `BILI_PORT=9000 python -m app.main`。

---

## 9. 故障排查

- **日志**：运行时控制台输出即主要日志；脚本方式可用 `nohup` 重定向到文件查看。
- **端口冲突**：见 FAQ Q5。
- **模型下载慢**：确认走的是 `hf-mirror.com` 镜像；需要官方源时设置 `HF_ENDPOINT`。
- **SSL 证书报错**：程序启动时已内置「环境自愈」，会自动清理指向不存在文件的 `SSL_CERT_FILE` 等变量（常见于 Anaconda 环境），一般无需手动处理。

---

## 10. 隐私说明

所有下载、转码、转写均在本地完成，不依赖任何外部 API（除首次模型下载与视频本身的公开链接外），字幕内容不会离开你的机器。

---

*更多技术细节见 [技术手册（中文）](technical-manual-zh.md) / [Technical Manual (English)](technical-manual-en.md)。*
