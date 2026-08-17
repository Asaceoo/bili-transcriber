# Bilibili Audio Local Transcriber — User Guide (English)

> Project name: **bili-transcriber**
> One-liner: Paste a Bilibili video link and the tool locally performs *download audio → transcode → GPU transcription → export subtitles*.

This tool runs **entirely on your machine**. Audio and transcripts are never uploaded to any server, making it suitable for privacy-sensitive or offline batch processing.

---

## 1. Overview

- Supports **single-part / multi-part / collection** links, automatically expanded into multiple transcription jobs.
- Pipeline: **yt-dlp download audio → FFmpeg transcode → faster-whisper local GPU transcription → output SRT / TXT / Markdown**.
- Fully local inference, no data upload.
- Resumable: a failed job re-runs without re-downloading / re-transcoding.
- Desktop window (pywebview + NiceGUI), also accessible from a browser.

---

## 2. Requirements

| Item | Requirement |
|------|-------------|
| OS | Windows (verified on Windows 10/11) |
| Python | 3.11 or newer (dev environment is 3.14) |
| FFmpeg | Must be on `PATH`, or placed manually in the project folder |
| GPU (optional) | NVIDIA GPU + CUDA 12.x; CPU-only also works, just slower |

> Without an NVIDIA GPU, the program falls back to CPU inference automatically — just set the device to `cpu` or `auto` in Settings.

---

## 3. Installation

```powershell
# 1. Enter the project directory
cd bili-transcriber

# 2. Create a virtual environment
python -m venv .venv

# 3. Install dependencies (editable)
.venv\Scripts\pip install -e .
```

On first transcription, the program automatically downloads the `large-v3-turbo` model (≈1.6 GB) from the Hugging Face mirror (`hf-mirror.com`) into your user cache directory. **This happens only once.**

> To switch to the official source, set the environment variable:
> ```powershell
> $env:HF_ENDPOINT = "https://huggingface.co"
> ```

---

## 4. Launch

**Option A — dev entry (recommended for daily use)**

```powershell
.venv\Scripts\python -m app.main
```

**Option B — installed console script**

```powershell
.venv\Scripts\bili-transcriber
```

After launch:

- If a visible desktop session is available, a native window titled "B站音频本地转写" (Bilibili Audio Local Transcriber) pops up.
- A web UI is also served; open `http://127.0.0.1:8765` in a browser.
- Override the listening port with the `BILI_PORT` env var (e.g. `BILI_PORT=9000 python -m app.main`) to avoid port conflicts.

---

## 5. Usage

The UI has three tabs:

### Tasks
1. Paste a Bilibili link (video / multi-part / collection) into the input box.
2. Click **Add** to enqueue. yt-dlp probes the link and expands it into entries.
3. The pipeline processes each entry sequentially. Status advances through the state machine:

   ```
   queued → downloading → converting → transcribing → saving → done / failed
   ```

4. The task list shows live progress and a status badge; you can inspect or re-run individual failed items.

### Upload local video / audio
Besides Bilibili links, the tool can also transcribe local media files you upload:

1. In the **Upload local video / audio** area at the bottom of the Tasks tab, click **Choose file**.
2. Supported formats: mp4 / mkv / mov / avi / webm / flv / wmv / mp3 / m4a / wav / ogg / opus / flac / aac.
3. The file is first saved to the local `uploads/` directory, then transcribed by the same local pipeline used for Bilibili tasks, producing SRT / TXT / MD.

> Note: Uploaded files are user assets and are **never deleted automatically** (only the intermediate WAV cache is cleaned). Re-uploading the same file/content skips already-completed jobs.

### History
- Index of processed jobs and their output paths (stored in a local SQLite database).
- Re-open output files or re-run jobs from here.

### Settings
- Adjust model, device, compute type, language, keep-audio, VAD, output directory, etc.
- A **Reset to default** button restores defaults in one click.
- Settings are persisted in local `data/settings.json`.

---

## 6. Settings Reference

| Setting | Description | Suggestion |
|---------|-------------|------------|
| Model size | Transcription model, default `large-v3-turbo` | Balanced accuracy/speed; use `small`/`medium` on low VRAM |
| Device | `cuda` / `cpu` / `auto` | Pick `cuda` or `auto` if you have an NVIDIA GPU |
| Compute type | `float16` / `int8` | Use `int8` when VRAM is tight (slightly lower accuracy, ~half memory) |
| Language | auto-detect / specify (e.g. `zh`) | Specifying `zh` for Chinese videos speeds up and reduces errors |
| Keep audio | Whether to keep the downloaded raw audio | Turn off to save disk |
| VAD | Voice activity detection (Silero) | On by default; auto-disabled and retried for music-only content |
| Output directory | Where subtitles are written | Defaults to `output/`; an absolute path is allowed |

---

## 7. Output Format

Each video (or part) gets a subfolder under the output directory:

```
output/{BV id}_{title}/
├── {part title}.srt      # Timestamped subtitles (import into editors)
├── {part title}.txt      # Plain text transcript
├── {part title}.md       # Markdown with timestamps
└── {part title}.m4a      # Raw audio (keep-audio setting)
```

---

## 8. FAQ

**Q1: Download / parse failed?**
Bilibili's interface changes occasionally. Upgrade the downloader first:
```powershell
.venv\Scripts\pip install -U yt-dlp
```

**Q2: Transcription is slow?**
Make sure the device is `cuda`; the first run downloads the model (≈1.6 GB) and won't repeat.

**Q3: Out of CUDA memory?**
Change compute type from `float16` to `int8` in Settings, or use a smaller model (e.g. `medium`).

**Q4: Music / singing videos have no subtitles?**
Silero VAD only detects human speech. For such content the tool **auto-disables VAD and retries** — this is expected. If it's still empty, the video likely has no speech.

**Q5: Port already in use (launch error)?**
Use the `BILI_PORT` env var to switch ports, e.g. `BILI_PORT=9000 python -m app.main`.

---

## 9. Troubleshooting

- **Logs**: console output is the primary log; when launched via script, redirect to a file with `nohup`.
- **Port conflict**: see FAQ Q5.
- **Slow model download**: confirm the `hf-mirror.com` mirror is used; set `HF_ENDPOINT` for the official source.
- **SSL certificate errors**: the app has built-in "self-healing" that clears stale `SSL_CERT_FILE` vars pointing to missing files (common in Anaconda) at startup — usually no manual action needed.

---

## 10. Privacy

All downloading, transcoding, and transcription happen locally. No external API is used except the one-time model download and the public video link itself; transcript contents never leave your machine.

---

*For more technical details see [技术手册（中文）](technical-manual-zh.md) / [Technical Manual (English)](technical-manual-en.md).*
