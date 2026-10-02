# Bilibili Audio Local Transcriber — User Guide (English)

> Project: **bili-transcriber**
> One-liner: paste a video link (Bilibili / Douyin / Xiaohongshu / Kuaishou / YouTube), and the tool locally completes "download → convert → local AI transcription → export subtitles".

Everything runs **100% locally** — audio and subtitles never leave your machine. Ideal for privacy-sensitive or offline batch workflows.

> 发布版本快照 **v0.1.34** — 由 `scripts/release.py` 自动生成,版本号取自 `pyproject.toml` 单一来源。

---

## 1. Feature Overview

- **Single-part / multi-part / collection** links are auto-expanded into tasks.
- **Multi-platform**: Bilibili, Douyin (TikTok CN), Xiaohongshu, Kuaishou (login-free direct-link parsing), YouTube (proxy required). WeChat Channels links are intercepted and redirected to local-file upload.
- **Three transcription engines** (switchable in Settings):
  | Engine | Models | Notes |
  |--------|--------|-------|
  | Whisper (default) | large-v3-turbo / large-v3 / medium / small / base / tiny | Best accuracy, GPU accelerated |
  | Qwen3-ASR | Qwen3-ASR-0.6B / 1.7B | Alibaba Qwen, strong on Chinese |
  | SenseVoice | SenseVoiceSmall | Small, fast, runs on CPU (~30× realtime) |
- Resume-safe: re-running a failed task skips completed download/convert steps.
- Optional **illustrated notes**: auto-captures slide-change keyframes into a Markdown handout.
- **System tray**: closing the window keeps the service running.

---

## 2. Requirements

| Item | Requirement |
|------|-------------|
| OS | Windows 10/11 (verified) |
| GPU (optional) | NVIDIA + CUDA 12.x; falls back to CPU automatically |
| FFmpeg | **Not required**: static build bundled |
| Network | Only for first-run model download (CN mirror); fully offline afterwards |

---

## 3. Installation (choose one)

### Option 1: Setup installer (recommended)
Run `bili-transcriber-setup-0.1.34.exe` and follow the wizard.

### Option 2: Portable package
Unzip `bili-transcriber-portable-0.1.34.zip` anywhere and run `bili-transcriber.exe`. No registry writes — USB-drive friendly.

### Option 3: Single-file build
Run `bili-transcriber-single-0.1.34.exe` (CPU mode). Slower first launch due to self-extraction.

### Option 4: Development mode
```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m app.main
```

### GPU Acceleration Pack (optional)
1. Download `bili-transcriber-gpu-0.1.34.zip` (~1.3 GB of CUDA runtime DLLs).
2. Extract into `%LOCALAPPDATA%\Bili Note\gpu\`.
3. Restart the app — CUDA libraries are **auto-detected and registered**.

---

## 4. Launching

- Default **browser mode**: opens `http://127.0.0.1:8765` automatically (no WebView2 dependency).
- Closing the window minimizes to the **system tray** (right-click: open window / exit).
- Port conflict: set `BILI_PORT=9000`.
- Native window (requires WebView2): set `BILI_FORCE_NATIVE=1`.

The UI has **four tabs**: Tasks / History / Diary / Settings.

---

## 5. Workflow

### Tasks
1. Paste a video link and click "Add". The pipeline expands it into entries.
2. State machine: `queued → downloading → converting → transcribing → extracting(optional) → saving → done / failed`.
3. Each row offers: open output folder, re-run, pause/resume, cancel.
4. A live run log (last 100 lines) sits at the top of the Tasks tab.
5. Short links (Douyin `v.douyin.com`, Xiaohongshu `xhslink.com`) are auto-resolved with safety checks.

### Transcribing Local Files
- **Pick file (recommended)**: opens a native Windows file dialog; the original path is referenced directly (zero-copy). Supports multi-select.
- **Browser upload**: for phone/remote access; base64 transfer is slower for large files. Copies are kept in `uploads/`. Upload cap: **10 GB**.

Supported: mp4 / mkv / mov / avi / webm / flv / wmv / mp3 / m4a / wav / ogg / opus / flac / aac.

### History
- Index of processed tasks (local SQLite) with search by **title / uploader / video ID**.
- Per-row: open folder, re-run, delete record (optionally deleting disk artifacts too).
- "Clear finished" removes done/failed/cancelled records at once (running tasks unaffected; optional disk cleanup).

### Diary
- A dedicated tab logging every operation and error, with **pagination** (50/100/200/500 lines per page).
- "Open log directory" jumps straight to the log files.

### Settings
Saved to `data/settings.json`; "Restore defaults" button available. See next section.

---

## 6. Settings Reference

| Setting | Values | Notes |
|---------|--------|-------|
| Engine | whisper / qwen3-asr / sensevoice | Model list follows the engine |
| Model | engine-specific | Whisper default `large-v3-turbo` |
| Device | auto / cuda / cpu | `auto` picks GPU when available |
| Compute type | auto / float16 / int8_float16 / int8 | `int8` halves VRAM usage |
| Language | empty=auto / zh / en | Pin `zh` for Chinese content |
| Keep audio | on/off | Keep downloaded source audio |
| VAD | on/off | Auto-disabled retry for music-only videos |
| Illustrated notes | on/off | Bilibili tasks switch to video stream download |
| Model directory | path | Default `%LOCALAPPDATA%\Bili Note\models`; integrity check built in |
| Browser cookie | none / Chrome / Edge / Firefox / Brave / Opera / Vivaldi | Needed by Douyin & some YouTube videos |
| cookies.txt path | file path | Netscape format; **overrides** browser cookie |
| Proxy | e.g. `http://127.0.0.1:7890` | Required for YouTube |
| Output directory | path | Default `output/` |

The Settings tab also provides:
- **External models**: drop an unpacked model folder into the model directory for auto-detection. SenseVoice: `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`; Qwen3-ASR: `Qwen/Qwen3-ASR-0.6B` or `Qwen3-ASR-1.7B` from ModelScope.
- **Hardware detection**: one-click check of CPU/GPU/CUDA availability and VRAM, with device & compute-type recommendations.

---

## 7. Output Files

```
output/{platform_id}_{title}/
├── {title}.srt              # timestamped subtitles
├── {title}.txt              # plain text
├── {title}.md               # Markdown with timestamps
├── {title}.m4a              # source audio (optional)
├── {title}.notes.md         # illustrated notes (optional)
└── {title}_frames/slide_*.jpg
```

Notes pages contain a clickable timestamp title, a keyframe screenshot, and the subtitles spoken during that frame. Notes failure never affects transcription success; pure-audio files skip notes automatically.

---

## 8. FAQ

**Q1: Download/parse failure?** Upgrade yt-dlp: `.venv\Scripts\pip install -U yt-dlp`.

**Q2: Douyin/YouTube login wall?** Set **Browser Cookie** (log in first) or provide a `cookies.txt`; YouTube also needs **Proxy**.

**Q3: Transcription slow?** Install the GPU pack; first run downloads the model once (~1.6 GB). Qwen3-ASR-1.7B on CPU is ~3x slower than 0.6B (the app warns you). For maximum speed choose SenseVoice (~30× realtime on CPU).

**Q4: CUDA out of memory?** Switch compute type to `int8` or a smaller model.

**Q5: No subtitles for music/singing?** VAD detects speech only; the app auto-retries without VAD. Empty result usually means no speech.

**Q6: "Detect models" reports missing files?** The model package is incomplete — delete and re-download.

**Q7: Port occupied?** `BILI_PORT=9000`.

**Q8: Do tasks survive closing the window?** Yes — the tray keeps the service running.

**Q9: How to transcribe WeChat Channels links?** They are intercepted — upload the file locally instead (no public parse API exists).

---

## 9. Troubleshooting

- **Run diary**: a dedicated paginated tab logs every step and error.
- **Slow model download**: default mirror is `hf-mirror.com`; switch via `HF_ENDPOINT=https://huggingface.co`.
- **SSL errors**: auto-healed at startup (stale `SSL_CERT_FILE` cleanup).
- **GPU pack inactive**: verify DLLs sit directly under `%LOCALAPPDATA%\Bili Note\gpu\`, then restart.

---

## 10. Privacy

All processing is local. Only outbound traffic: model download (first run) and the public video platforms themselves. Transcripts never leave your machine.

---

*See also: [技术手册（中文）](technical-manual-zh.md) / [Technical Manual (English)](technical-manual-en.md).*


---

## 11. Companion AI Agent Skills (Universal)

This tool covers "link → subtitles/transcript". The knowledge-building step afterwards is handled by the companion **universal AI agent skills** (open-source repo: [Asaceoo/bili-note-skills](https://github.com/Asaceoo/bili-note-skills)):

| Skill | Purpose | Input → Output |
|---|---|---|
| `bili-note` | Fetch Bilibili subtitles / AI subtitles / comments; archive raw materials and evidence indexes | Bilibili URL → transcript + full archive |
| `bili-content-enhance` | Turn a transcript into 6 learning artifacts (summary / glossary / knowledge map / animated HTML / extras / deep understanding) with a quality gate | transcript → learning pack (6 files) |

**Universal edition (v1.1.0)**: the two skills are **not tied to any AI agent** — Claude Code, Cursor, Codex CLI, WPS AI, WorkBuddy, Cline and Gemini CLI can all use them as-is; assistants without a skills directory just paste `SKILL.md` into the system prompt; and they also run from a plain shell with no agent at all. Scripts have zero third-party dependencies (bili-note uses the Python stdlib only; the gate script uses Node built-ins only), and the logged-in route only needs a generic CDP interface (`GET /targets` and `GET /eval`, default `http://localhost:3456`).

Install (pick the line for your platform):

```bash
git clone https://github.com/Asaceoo/bili-note-skills.git
cp -r bili-note-skills/skills/bili-note ~/.claude/skills/            # WorkBuddy: ~/.workbuddy/skills/; Codex: ~/.codex/skills/
cp -r bili-note-skills/skills/bili-content-enhance ~/.claude/skills/
```

Typical chain: export a `.md` / `.txt` / `.srt` transcript with this tool → hand the file to any AI agent → say "generate a learning pack" → receive 6 artifacts → run `node scripts/validate.js <learning-pack-dir>` as the quality gate.

Full documentation: [English manual](https://github.com/Asaceoo/bili-note-skills/blob/main/docs/manual-en.md).
