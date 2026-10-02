# bili-transcriber Technical Manual (English)

> For developers and maintainers. Covers architecture, module responsibilities, concurrency model, data flow, security mechanisms, build/release process, and the test suite.
> 发布版本快照 **v0.1.33** — 由 `scripts/release.py` 自动生成,版本号取自 `pyproject.toml` 单一来源。

## 1. Overview

bili-transcriber is a **fully local multi-platform video/audio transcription desktop tool**. Core pipeline:

```
Link (Bilibili/Douyin/Xiaohongshu/Kuaishou/YouTube)
  → yt-dlp / platform-specific resolver downloads best audio (or video) stream
  → FFmpeg transcodes to 16kHz mono WAV
  → local inference engine (faster-whisper / Qwen3-ASR / SenseVoice, one of three)
  → export SRT / TXT / Markdown (optional illustrated notes.md)
```

Design principles: no data upload, resumable jobs, lock-protected GPU access, decoupled UI and compute threads.

## 2. Tech Stack

| Layer | Choice |
|-------|--------|
| Language | Python ≥ 3.11 (dev env 3.14) |
| Download | yt-dlp (Bilibili/Douyin/Xiaohongshu/YouTube) + custom Kuaishou SSR parser |
| Transcode | FFmpeg (subprocess; bundled static build resolved by `app/ffmpeg_bin.py`) |
| Engine 1 | faster-whisper + ctranslate2 (CUDA backend, default) |
| Engine 2 | qwen-asr (Qwen3-ASR 0.6B/1.7B, torch backend) |
| Engine 3 | sherpa-onnx (SenseVoiceSmall, ONNX int8) |
| Keyframes | FFmpeg scene detection (preferred) / PyAV per-frame MAD (fallback), dual backend |
| UI | NiceGUI (Quasar), browser mode by default; pywebview native window optional |
| Tray | pystray (system tray resident, silent degrade) |
| Native dialog | ctypes comdlg32 (Windows file picker) |
| Storage | SQLite (history.db index) + JSON (settings.json) |
| Build | hatchling (wheel) + PyInstaller (onedir/onefile) + InnoSetup (installer) |

## 3. Repository Layout

```
bili-transcriber/
├── app/                        # 20 modules
│   ├── main.py                 # NiceGUI 3-tab UI + events + launch mode
│   ├── pipeline.py             # orchestration: state machine + WORKERS=2 + engine locks
│   ├── downloader.py           # platform routing / short links / SSR / cookies / proxy / DNS guard
│   ├── kuaishou.py             # Kuaishou share-page SSR parser (login-free mp4 direct link)
│   ├── transcriber.py          # engine 1: faster-whisper wrapper
│   ├── qwen_transcriber.py     # engine 2: Qwen3-ASR (chunked inference + aligner)
│   ├── sensevoice_transcriber.py # engine 3: SenseVoice (token→segment timestamps)
│   ├── converter.py            # FFmpeg → 16kHz mono WAV
│   ├── keyframes.py            # keyframe extraction (ffmpeg scene / PyAV dual backend)
│   ├── writers.py              # SRT/TXT/MD/notes.md writers
│   ├── models.py               # model dir scan/detect/locate (3 engines)
│   ├── gpu_runtime.py          # external GPU pack DLL registration
│   ├── ffmpeg_bin.py           # ffmpeg binary resolution (PATH first, bundled fallback)
│   ├── hardware.py             # hardware probing (GPU model/VRAM/CUDA)
│   ├── store.py                # SQLite + JSON settings (14 keys defined)
│   ├── task_control.py         # per-task pause/cancel (thread-safe)
│   ├── native_dialog.py        # Windows native file picker (ctypes comdlg32)
│   ├── tray.py                 # system tray (pystray, silent degrade)
│   ├── logging_setup.py        # logging bootstrap
│   └── __init__.py             # APP_VERSION + frozen-mode DLL search paths
├── build/
│   ├── bili-transcriber.spec         # PyInstaller onedir config
│   ├── bili-transcriber-onefile.spec # PyInstaller onefile config
│   ├── preload_transformers.py       # packaged runtime hook (transformers/nagisa compat)
│   ├── installer.iss                 # InnoSetup script
│   └── smoke_*.py / verify_*.py      # packaging smoke/verification scripts
├── scripts/
│   ├── release.py              # one-click release: bump + wheel + portable + setup + single + gpu
│   ├── e2e_check.py            # end-to-end link check
│   └── bdelete/clean_*/move_old/sh_delete.py  # sandbox-safe deletion tools
├── tests/                      # pytest suite (21 files / 243 cases)
├── docs/                       # bilingual user guide + technical manual
├── data/                       # settings.json + history.db (runtime)
├── output/                     # transcripts (runtime)
└── pyproject.toml              # single version source
```

## 4. Module Responsibilities (highlights)

### 4.1 `main.py` — UI & Event Loop
- Three tabs: **Tasks / History / Settings**; UI thread polls `Store` every **0.6s** and drains the event `deque(maxlen=200)`.
- Engine switch: `model_select` options follow the engine (`MODEL_OPTIONS` / `QWEN_MODEL_OPTIONS` / `SENSEVOICE_MODEL_OPTIONS`); invalid combos auto-corrected.
- Row-level events: NiceGUI `$emit('open-path', path)` → `e.args` is `[path]` list, unpacked by `_first_arg(e)`; `_open_path()` degrades file → parent dir → output dir; explorer `/select,"path"` is quoted for spaces.
- Launch mode: browser mode default (no WebView2 dependency); `BILI_FORCE_NATIVE=1` for pywebview; `BILI_PORT` overrides port (default 8765).

### 4.2 `pipeline.py` — Orchestration
- **Concurrency (v0.1.33)**: `WORKERS = 2` threads drain the queue (download/convert overlap); **transcription protected by three lock layers**:
  - `_model_lock` (pipeline): guards transcriber lazy-load/replacement on settings change.
  - Engine-level `self._lock`: serializes `transcribe()` so only one job holds model/VRAM.
  - Engine `load()` uses **double-checked locking**: null-check + load inside the lock, eliminating cold-start double-loads (fixed since v0.1.32, with concurrency regression tests for all three engines).
- **State machine**: `queued → downloading → converting → transcribing → extracting(optional) → saving → done / failed`; per-task pause/cancel via `task_control.py`.
- **Resume-safe**: `audio_path` / `wav_path` cached and reused.
- **Cleanup tolerance**: `OSError` from sandboxed deletion degrades to warning; job stays `done`.

### 4.3 `downloader.py` — Download Routing
- **Platform routing** by hostname: `bilibili / youtube / douyin / xiaohongshu / kuaishou` (unknown → bilibili compat); media_id prefixes `yt_/dy_/xhs_/ks_`.
- **Short links & security**: b23.tv expansion validates against a **domain whitelist** with **DNS-rebinding protection** (resolved-IP recheck) before following redirects; Douyin share links matched via `modal_id` / redirect regexes.
- **Cookies/proxy**: `cookiefile` (cookies.txt) takes precedence over `cookiesfrombrowser`; unsupported browser names are ignored to keep YoutubeDL construction alive; proxy format `http://127.0.0.1:7890` / `socks5://…`.
- **Reachability cache**: `_reach_cache` cleared on proxy change.

### 4.4 Three Transcription Engines
| Module | Backend | Timestamps | Language mapping |
|--------|---------|-----------|------------------|
| `transcriber.py` | faster-whisper/ctranslate2 | native | whisper codes |
| `qwen_transcriber.py` | qwen-asr(torch) + ForcedAligner | aligner; duration-based fallback when absent | `_map_qwen_language()` whisper→full name, unknown→auto |
| `sensevoice_transcriber.py` | sherpa-onnx int8 | token-level aggregation `_tokens_to_segments()` | `_map_sensevoice_language()` |

- **Lazy load + cache key**: `(model_size, device, compute_type, language, vad)`; reload only on change.
- **VAD self-healing**: zero-segment results (music-only) auto-retry with VAD off.
- **Qwen chunked inference**: long audio split into 60s chunks, `on_event` reports "chunk i/n"; CPU users of 1.7B get a slow-model warning.

### 4.5 `models.py` — Model Management
- `default_model_dir()`: `%LOCALAPPDATA%\Bili Note\models`; `resolve_model_dir()` supports custom paths.
- `scan_models()` / `describe_models()`: engine-specific integrity checks (missing files / version match / single-qwen fallback) driving the Settings "detect models" panel.
- `find_{whisper,qwen_asr,sensevoice}_model()`: locate a model inside the dir; online downloads land in the same dir via `download_root`.

### 4.6 `gpu_runtime.py` — External GPU Pack
- Detects CUDA DLLs (cudnn/cublas/cufft…, 17 files) under `%LOCALAPPDATA%\Bili Note\gpu`, registers via `add_dll_directory` + PATH injection so ctranslate2 can load them.
- `ensure_gpu_or_cpu()`: silently falls back to CPU when the pack is absent.

### 4.7 `keyframes.py` — Keyframe Extraction (dual backend)
- **ffmpeg (preferred)**: `select='gte(scene,0)'` + `metadata=print` streams per-frame scores (threshold 0.11), snapshots via `-ss`; per-frame callbacks support progress/cancel.
- **PyAV (fallback)**: 64x36 grayscale MAD > 15 marks a cut.
- Common policy: first frame always captured; 0.4s after each cut; 5s min gap; cap 400 frames; `KeyframeError` and user cancellation propagate without fallback.

### 4.8 `store.py` — Storage
- **SQLite** (`data/history.db`): history index, UPSERT keyed by `media_id`.
- **JSON** (`data/settings.json`) 14 keys: `output_dir / model_size / device / compute_type / language / keep_audio / vad / notes / engine / model_dir / cookies_from_browser / cookie_file / proxy`.

## 5. Data Flow

```
URL → downloader.probe (platform routing + short-link expansion)
      └─ for entry in entries (WORKERS=2 concurrent):
           1. download → audio_path          # skipped on cache hit
           2. to_wav16k_mono → wav_path      # skipped on cache hit
           3. transcriber.transcribe(wav)    # serialized by engine lock; GPU first
           4. (notes) keyframes → writers.write_notes_md
           5. writers.write_srt/txt/md
           6. cleanup intermediates           # failure degrades to warning
           └─ store.update(job: done)
UI thread polls Store every 0.6s + drains event deque → refresh
```

## 6. Threading Model

```
┌─────────────┐     Store/DB      ┌──────────────────┐
│  UI thread  │ ── read-only ───▶ │   SQLite + JSON   │
│ (NiceGUI)   │ ◀─ event deque ── │   (Store)        │
└─────────────┘                   └──────────────────┘
                                        ▲ write-only
┌────────────────────────┐              │
│ WORKERS=2 workers      │ ─────────────┘
│  download/convert overlap │
│  transcribe serial (engine lock) │
└────────────────────────┘
```

Workers only write Store/events; UI only reads via polling — no shared mutable state, no races. GPU-heavy transcription is serialized by the engine lock to avoid VRAM contention and OOM.

## 7. Packaging Compatibility (PyInstaller pitfalls)

| Issue | Mitigation |
|-------|------------|
| transformers misdetects sklearn → GenerationMixin import crash | spec `excludes=["sklearn"]` + runtime hook `preload_transformers.py` sets `_sklearn_available=False` |
| nagisa bare import `prepro` (PEP366) fails | hook inserts the nagisa package dir into `sys.path` (never `import nagisa.prepro` — circular import); hiddenimports add `nagisa/dynet/_dynet` |
| Sandbox safe-delete hook kills PyInstaller cleanup | `release.py` `_evacuate()` renames old dist/build dirs aside first so `--noconfirm` finds nothing to delete |
| onefile native window white screen | browser mode default; `BILI_FORCE_NATIVE=1` to opt in |
| CUDA DLL location | `__init__.py` frozen-mode `sys._MEIPASS` search paths + `gpu_runtime` external-pack registration |

## 8. Build & Release

### 8.1 Versioning
- **Single source**: `version` in `pyproject.toml`.
- `scripts/release.py` bumps and syncs **three** locations: `build/installer.iss` `AppVersion`, `app/__init__.py` `APP_VERSION` (consistency asserted in tests).

### 8.2 Five Artifacts
| Artifact | Output |
|----------|--------|
| Wheel | `dist/bili_transcriber-{ver}-py3-none-any.whl` |
| Portable | `dist/bili-transcriber-portable-{ver}.zip` (full onedir, ~6900 files) |
| Setup | `dist/bili-transcriber-setup-{ver}.exe` (InnoSetup) |
| Single-file | `dist/bili-transcriber-single-{ver}.exe` (onefile, CPU mode) |
| GPU pack | `dist/bili-transcriber-gpu-{ver}.zip` (17 CUDA DLLs → %LOCALAPPDATA%) |

In-process verification: zip `testzip`, PE header (MZ/PE), and `_built_fresh()` timestamp guard against stale artifacts.

### 8.3 Commands
```powershell
python scripts/release.py            # bump + all artifacts
python scripts/release.py --wheel-only
python scripts/release.py --single-only
python scripts/release.py --setup-only
```

## 9. Testing

`tests/` contains **21 files / 243 cases** (all green at v0.1.33):

| Area | Files (examples) | Strategy |
|------|------------------|----------|
| Storage | test_store.py | real temp SQLite |
| Download | test_downloader.py / test_kuaishou.py | mock yt-dlp / SSR HTML fixtures |
| Engines | test_transcriber / test_qwen_transcriber / test_sensevoice_transcriber | FakeModel injection; **concurrent load() regression** (Barrier(2), asserts single load) |
| Pipeline | test_pipeline.py | FakeDownloader/FakeTranscriber at external boundaries |
| UI | test_ui.py / test_main_helpers.py | NiceGUI user_plugin; `_first_arg`/explorer quoting |
| Packaging | test_nagisa_ascii.py etc. | runtime hook & compatibility |
| Notes | test_keyframes.py / test_writers.py | dual-backend threshold calibration & output content |

Run: `.venv\Scripts\pytest -q`

## 10. Known Limitations

- Windows-only verified; macOS/Linux untested (pywebview native window and comdlg32 are Windows-specific).
- GPU acceleration covers the whisper engine (ctranslate2) only; Qwen3-ASR/SenseVoice run on CPU (Qwen slower).
- In restricted sandboxes, PyInstaller re-runs may be blocked by safe-delete policies; final exe builds require a normal desktop environment.
- The Kuaishou SSR parser depends on the share-page markup; site redesigns require updating `app/kuaishou.py`.
