# bili-transcriber Technical Manual (English)

> For developers and maintainers. Covers architecture, module responsibilities, data flow, threading model, key mechanisms, and the build/release process.

## 1. Overview

bili-transcriber is a **fully local Bilibili audio transcription desktop tool**. Core pipeline:

```
Bilibili URL → yt-dlp download best audio → FFmpeg transcode to 16kHz mono WAV
             → faster-whisper local inference → export SRT / TXT / Markdown
```

Design principles: no data upload, resumable jobs, single-serial GPU access, decoupled UI and compute threads.

## 2. Tech Stack

| Layer | Choice |
|-------|--------|
| Language | Python ≥ 3.11 (dev environment is 3.14) |
| Download | yt-dlp |
| Transcode | FFmpeg (subprocess) |
| Inference | faster-whisper + ctranslate2 (CUDA backend) |
| VAD | Silero VAD (built into faster-whisper) |
| UI | NiceGUI (Quasar components) + pywebview (native window) |
| Storage | SQLite (history.db index) + JSON (settings.json) |
| Build | hatchling (wheel) + PyInstaller (portable exe) + InnoSetup (installer) |

## 3. Directory Layout

```
bili-transcriber/
├── app/                  # Application source (8 modules)
│   ├── main.py           # NiceGUI 3-tab UI + event loop
│   ├── pipeline.py       # Core orchestration: state machine + worker thread
│   ├── downloader.py     # yt-dlp wrapper: probe + download
│   ├── transcriber.py    # faster-whisper wrapper: lazy load + GPU-first
│   ├── converter.py      # FFmpeg wrapper: transcode to 16kHz mono WAV
│   ├── writers.py        # SRT / TXT / Markdown writers
│   ├── store.py          # SQLite + JSON settings
│   └── __init__.py       # version/path constants + frozen-mode DLL search
├── build/
│   ├── bili-transcriber.spec   # PyInstaller config
│   ├── installer.iss          # InnoSetup installer script
│   └── build.bat              # one-click packaging entry
├── scripts/
│   ├── e2e_check.py    # end-to-end link check (probe/download)
│   └── release.py      # one-click release: bump + wheel + portable + installer
├── tests/              # pytest suite (19~20 cases)
├── data/              # settings.json + history.db (runtime)
├── output/            # transcripts (runtime)
└── pyproject.toml
```

## 4. Module Responsibilities

### 4.1 `main.py` — UI and Event Loop
- Three tabs: **Tasks / History / Settings**.
- The UI main thread polls `Store` and consumes the task event `deque` every **0.6s** to refresh the view.
- The `BILI_PORT` env var overrides the listening port (default 8765) to avoid Windows `TIME_WAIT` port conflicts (`uvicorn` does not enable `SO_REUSEADDR` by default).
- In `frozen` mode (after PyInstaller packaging) `BASE_DIR` points to `sys._MEIPASS` to locate nvidia DLLs and resources.

### 4.2 `pipeline.py` — Core Orchestration
- **Single serial worker thread**: all entries are processed sequentially `[download → transcode → transcribe → write]` to avoid concurrent GPU VRAM contention.
- **State machine**: `queued → downloading → converting → transcribing → saving → done / failed`.
- **Transcription lock**: `transcriber.transcribe()` holds a lock so only one job uses the model/VRAM at a time.
- **Resume**:
  - Downloaded audio path is cached in `job.audio_path`; re-run skips download if the file still exists.
  - Transcoded WAV is cached in `job.wav_path`; re-run skips transcode if the file still exists.
  - On re-run after failure, `progress` resets and status clears, but download/transcode are not repeated.
- **Cache-cleanup fault tolerance** (v0.1.1 fix): when deleting intermediate WAV and optional raw audio after writing, if deletion is blocked by an environment safety policy (e.g. sandbox recycle bin unavailable) raising `OSError`, it is **downgraded to a warning log and the job is still marked `done`** instead of being mislabeled `failed`.

### 4.3 `downloader.py` — yt-dlp Wrapper
- `probe(url)`: use yt-dlp to probe a link and automatically expand **single-part / multi-part / collection** into a list of entries (each with `media_id`, title, URL).
- `download(entry, dest)`: download the best audio stream (prefer `m4a`/`bestaudio`), return the local path.
- On failure, raises a structured exception caught by the pipeline and marked `failed`.

### 4.4 `transcriber.py` — faster-whisper Wrapper
- **Lazy model loading**: the model loads on first call and stays resident.
- **Cache key**: `(model_size, device, compute_type, language, vad)`; reload only when the key changes, avoiding reloading the ~1.6 GB model.
- **GPU-first**: with `device=auto`, CUDA availability is detected and falls back to CPU when unavailable.
- **VAD self-heal**: when Silero VAD filters out all speech (music-only / singing scenes) yielding zero segments, it **automatically retries once with VAD disabled**.

### 4.5 `converter.py` — FFmpeg Wrapper
- `to_wav16k_mono(src, dst)`: invoke FFmpeg to transcode any audio into **16kHz mono 16-bit WAV** (the optimal faster-whisper input format).
- Called via subprocess; stderr is captured for diagnostics.

### 4.6 `writers.py` — Output Writers
- `write_srt / write_txt / write_md`: emit timestamped subtitles, plain text, and timestamped Markdown respectively.
- Output layout: `{output_dir}/{BV id}_{title}/{part title}.{ext}`.

### 4.7 `store.py` — Storage
- **SQLite** (`data/history.db`): history index, `UPSERT` keyed on `media_id`, recording status, progress, and output paths.
- **JSON** (`data/settings.json`): user settings (model, device, compute type, language, keep_audio, vad, output directory).

## 5. Data Flow

```
URL
 └─ downloader.probe → [entry, entry, ...]          # expand single/multi/collection
      └─ for entry in entries (serial):
           1. downloader.download → audio_path       # skip if cache hit
           2. converter.to_wav16k_mono → wav_path    # skip if cache hit
           3. transcriber.transcribe(wav) → segments # locked; GPU-first
           4. writers.write_* → write SRT/TXT/MD
           5. clean intermediate products (WAV, optional raw audio)  # failure → warning
           └─ store.update(job: done)
 UI thread polls Store + consumes event deque every 0.6s → refresh view
```

## 6. Threading Model

```
┌─────────────┐     Store/DB      ┌──────────────────┐
│  UI thread  │ ─── read-only ──▶ │                  │
│ (NiceGUI)   │ ◀── event deque ─ │   SQLite + JSON   │
└─────────────┘                   │   (Store)        │
                                  └──────────────────┘
                                        ▲ writes only
                                        │
┌─────────────┐                   ┌──────────────────┐
│  Worker     │ ─── write Store ─│  Pipeline         │
│  thread     │                  │  download→conv→   │
│ (1 serial)  │                  │  transcribe→write │
└─────────────┘                   └──────────────────┘
```

Key points:
- The worker thread only writes Store / the event queue; the UI thread only reads and polls. They do not share mutable state directly, avoiding races.
- Transcription is GPU-heavy; single-serial thread + transcription lock avoids VRAM contention and OOM.

## 7. Mainland-China Network Optimizations

Built-in adaptations for smooth out-of-the-box use in China:
- **Model mirror**: first download goes through `hf-mirror.com` (Hugging Face China mirror), once only. Switch to the official source via `HF_ENDPOINT`.
- **SSL self-heal**: at startup, clears stale `SSL_CERT_FILE` vars (common with Anaconda) pointing to missing files to prevent handshake failures.
- **CUDA library injection**: nvidia libraries are injected via pip wheels (not system PATH); after packaging, `__init__.py`'s `sys._MEIPASS` locates them.
- **Xet disabled**: avoids git/Git LFS related network overhead.

## 8. Build & Release

### 8.1 Version Management
- **Single source of truth**: the `version` field in `pyproject.toml` (e.g. `0.1.1`).
- `scripts/release.py` auto `bump`s (default patch +1) and syncs `build/installer.iss`'s `AppVersion`.
- All three artifacts carry a version suffix, satisfying the "every release must carry a version" rule.

### 8.2 Three Artifacts
| Artifact | Command / Flow | Output |
|----------|----------------|--------|
| Wheel | `python -m build` (hatchling) | `dist/bili_transcriber-{ver}-py3-none-any.whl` |
| Portable | PyInstaller + in-process `zipfile` | `dist/bili-transcriber-portable-{ver}.zip` |
| Installer | InnoSetup (`build/installer.iss`) | `dist/bili-transcriber-setup-{ver}.exe` |

> The portable build uses Python `zipfile` for in-process compression, avoiding PowerShell `Compress-Archive` being blocked by a sandbox safe-delete hook (which would kill the whole process tree). `do_portable` is hardened: if PyInstaller fails but `dist/bili-transcriber/` already exists, it reuses the existing build to continue zipping.

### 8.3 Packaging Commands
```powershell
# one-click (bump + wheel + portable + installer)
python scripts/release.py

# wheel only
python scripts/release.py --wheel-only
```

## 9. Testing

`tests/` contains 19~20 cases:

| Module | Cases | Strategy |
|--------|-------|----------|
| store | 5 | real temp SQLite DB |
| downloader | 5 | mock yt-dlp |
| writers | 3 | compare written content |
| pipeline | 5 | `FakeDownloader` / `FakeTranscriber` mock external boundaries; `converter.to_wav16k_mono` replaced with a file-writing lambda |
| ui | 1 | NiceGUI `user_plugin` injection |
| + regression | 1 | `test_safe_delete_failure_keeps_job_done`: simulate safe-delete block, assert job still `done` |

Run:
```powershell
.venv\Scripts\pytest -q
```

## 10. Known Limitations

- Verified on Windows only; macOS/Linux need their own pywebview native-window validation.
- The app code currently does not read/display an internal version number (only the filename carries the version suffix).
- In sandboxed/restricted environments, PyInstaller re-runs may be blocked by safe-delete policy; final exe build must be done on a native machine.
