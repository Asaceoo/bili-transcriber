# bili-transcriber — Technical Manual (English)

> For developers and maintainers. Covers layering, the concurrency model, the state machine, module responsibilities, data contracts, security boundaries, packaging pitfalls, and the test suite.
> 发布版本快照 **v0.1.43** — 由 `scripts/release.py` 自动生成,版本号取自 `pyproject.toml` 单一来源。

---

## Table of Contents

1. [Overview](#1-overview)
2. [Layered Architecture](#2-layered-architecture)
3. [Tech Stack](#3-tech-stack)
4. [Directory Layout](#4-directory-layout)
5. [Concurrency and Threading](#5-concurrency-and-threading)
6. [State Machine and Task Recovery](#6-state-machine-and-task-recovery)
7. [Module Responsibilities](#7-module-responsibilities)
8. [Data Contracts](#8-data-contracts)
9. [Security and Robustness](#9-security-and-robustness)
10. [Packaging Pitfalls](#10-packaging-pitfalls)
11. [Build and Release](#11-build-and-release)
12. [Test Suite](#12-test-suite)
13. [Known Limitations](#13-known-limitations)

---

## 1. Overview

bili-transcriber is a **fully local multi-platform video/audio transcriber** with three drivers (GUI / CLI / MCP) sharing one service layer.

Core pipeline:

```
Input: a link (Bilibili/Douyin/Xiaohongshu/Kuaishou/YouTube) or a local media file
  → Probe & expand (platform detection / short-link resolution / single·multi·collection → N tasks)
  → Download the best audio stream (video stream instead when notes are enabled)
  → FFmpeg → 16 kHz mono WAV
  → Local inference (faster-whisper / Qwen3-ASR / SenseVoice)
  → Optional: keyframe extraction → illustrated notes
  → Write SRT / TXT / Markdown
```

**Design principles applied throughout:**

| Principle | Implementation |
|---|---|
| Nothing is uploaded | No external AI API calls; only video fetching and model downloading egress |
| Resumable | Audio / WAV / video path caching; leftover in-progress tasks resumed at startup |
| No GPU contention | Transcription holds an engine lock for its whole duration |
| UI decoupled from compute | Workers only write Store / event queue; the UI thread only polls |
| Single source of truth | Version lives only in `pyproject.toml`; status sets only in `pipeline.py` |

---

## 2. Layered Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Driver layer                                            │
│  app/main.py (GUI)   app/cli.py (CLI)   app/mcp_server.py │
└───────────────┬──────────────┬───────────────┬───────────┘
                │              │               │
                │       ┌──────┴───────┐       │
                │       │ app/headless │◀──────┘  Service layer (sole business entry)
                │       │   Service    │
                │       └──────┬───────┘
                ▼              ▼
┌──────────────────────────────────────────────────────────┐
│  Orchestration  app/pipeline.py (Task / state machine /   │
│                 worker pool)                              │
└──────────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────┐
│  Capability layer                                        │
│  downloader / kuaishou / converter / transcriber /       │
│  qwen_transcriber / sensevoice_transcriber / keyframes /  │
│  writers / models / gpu_runtime / hardware / ffmpeg_bin   │
└──────────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────┐
│  Foundation  paths / store / task_control / native_dialog /│
│              tray / logging_setup                         │
└──────────────────────────────────────────────────────────┘
```

**CLI and MCP never touch `pipeline` directly** — everything goes through `Service` in `app/headless.py`. Consequences:

- Both AI channels behave identically; only transport differs.
- Argument validation, JSON serialization, and error wrapping exist in exactly one place.
- Adding a channel (HTTP, message queue) requires no business-logic changes.

---

## 3. Tech Stack

| Layer | Choice |
|---|---|
| Language | Python ≥ 3.11 (dev on 3.14) |
| Download | yt-dlp (Bilibili / Douyin / Xiaohongshu / YouTube) + custom Kuaishou SSR parser |
| Transcode | FFmpeg via subprocess; bundled imageio-ffmpeg static build resolved by `app/ffmpeg_bin.py` |
| Engine 1 | faster-whisper + ctranslate2 (CUDA backend, default) |
| Engine 2 | qwen-asr (Qwen3-ASR 0.6B / 1.7B, torch backend) |
| Engine 3 | sherpa-onnx (SenseVoiceSmall, ONNX int8) |
| Keyframes | FFmpeg scene detection (primary) / PyAV frame-diff MAD (fallback) |
| UI | NiceGUI (Quasar components) + pywebview native window (WebView2); falls back to the browser without WebView2 |
| Tray | pystray (silent degradation on failure) |
| Native dialog | ctypes comdlg32 |
| Single instance | ctypes `CreateMutexW` |
| Storage | SQLite (`history.db`) + JSON (`settings.json`) |
| Build | hatchling (wheel) + PyInstaller (onedir / onefile) + InnoSetup |

---

## 4. Directory Layout

```
bili-transcriber/
├── app/                        # 31 modules, ~9700 lines
│   ├── __init__.py             # APP_VERSION + frozen-mode DLL search path
│   ├── paths.py                # Runtime path resolution (GUI/CLI/MCP share one data/)
│   ├── main.py                 # Entry: single instance / port / tray / UI wiring
│   ├── pipeline.py             # Orchestration: state machine / workers / model lock
│   ├── headless.py             # Service layer: sole business entry for CLI and MCP
│   ├── cli.py                  # CLI driver
│   ├── mcp_server.py           # MCP stdio server (JSON-RPC 2.0 subset)
│   ├── downloader.py           # Download orchestration: platform / short links / cookies
│   ├── netcheck.py             # Network & cookie detection (browsers / proxies / config test)
│   ├── kuaishou.py             # Kuaishou share-page SSR parser
│   ├── converter.py            # FFmpeg → 16 kHz mono WAV
│   ├── transcriber.py          # Engine 1: faster-whisper
│   ├── qwen_transcriber.py     # Engine 2: Qwen3-ASR (chunked + aligner)
│   ├── sensevoice_transcriber.py # Engine 3: SenseVoice (token→segment timestamps)
│   ├── keyframes.py            # Keyframe extraction (ffmpeg scene / PyAV)
│   ├── writers.py              # SRT / TXT / MD / notes writers (atomic)
│   ├── models.py               # Model dir scan / validate / locate
│   ├── gpu_runtime.py          # External GPU pack DLL registration
│   ├── ffmpeg_bin.py           # ffmpeg binary resolution (PATH first, bundled fallback)
│   ├── hardware.py             # Hardware probe (GPU model / VRAM / CUDA)
│   ├── store.py                # SQLite + JSON settings (14 keys)
│   ├── task_control.py         # Per-task pause / cancel (thread-safe)
│   ├── native_dialog.py        # Windows native file dialog (ctypes comdlg32)
│   ├── tray.py                 # System tray (pystray, silent fallback)
│   ├── logging_setup.py        # Logging bootstrap
│   └── ui/
│       ├── state.py            # Shared singletons: store / settings / pipeline / events
│       ├── pages.py            # Tab layout construction
│       └── helpers.py          # Action functions: open folder / task ops / upload
├── build/
│   ├── bili-transcriber.spec         # PyInstaller onedir
│   ├── bili-transcriber-onefile.spec # PyInstaller onefile
│   ├── preload_transformers.py       # Runtime hook (transformers / nagisa compat)
│   ├── installer.iss                 # InnoSetup script
│   └── smoke_*.py / verify_*.py      # Packaging smoke and verification
├── scripts/
│   ├── release.py              # One-shot release: bump + five artifacts + doc snapshots
│   ├── e2e_check.py            # End-to-end link check
│   └── bdelete/ clean_*/ move_old/ sh_delete.py  # Sandbox-safe deletion helpers
├── tests/                      # pytest (28 files / 512 cases)
├── docs/                       # Bilingual manuals + assets/ concept art
├── data/                       # settings.json + history.db (runtime; logs live in %LOCALAPPDATA%\Bili Note\logs)
├── output/                     # Transcription output (runtime)
└── pyproject.toml              # Version single source
```

---

## 5. Concurrency and Threading

### 5.1 Three tiers

```
┌──────────────┐   Store / SQLite      ┌──────────────────┐
│  UI thread   │ ─── poll every 0.6s ▶ │  SQLite + JSON   │
│  (NiceGUI)   │ ◀── event deque ───── │     (Store)      │
└──────────────┘     maxlen=200        └──────────────────┘
                                              ▲ write only
┌─────────────────────────────┐               │
│  Worker pool (2 default, 1–6)│ ──────────────┘
│  download / convert overlap  │
│  transcribe serial (lock)    │
└─────────────────────────────┘
```

### 5.2 Why downloads parallelize but transcription does not

Download and transcode are mixed I/O and CPU work — running several in parallel raises throughput meaningfully: while task A transcribes, task B downloads; when A finishes, B's audio is ready.

Transcription must be serial: several tasks pushing into one GPU exhaust VRAM and trigger `CUDA out of memory`, failing everything. Serial is better for aggregate throughput too.

### 5.3 Three locks

| Lock | Location | Protects |
|---|---|---|
| `_model_lock` | `Pipeline` | Lazy init / replacement of `_transcriber` (rebuilt on settings change) |
| engine `self._lock` | each transcriber | Serializes `transcribe()` so only one task owns the model/VRAM |
| double-checked lock | each engine's `load()` | Null-check and load happen atomically, preventing double load on cold start |

`_model_lock` is held only briefly while acquiring an engine reference; an in-flight transcription holds its own reference, so a settings change cannot interrupt a running task.

### 5.4 Queue de-duplication

```python
self._scheduled: set[str] = set()   # media_id currently in queue
```

- `_enqueue(task)` is the single enqueue point and `add`s the id.
- Workers `discard` when they pick the task up (no longer "in queue").
- All three enqueue sites (`submit_local`, `submit_prepared`, `rerun`) go through `_enqueue`.

**Real bug this fixed**: `submit_prepared` → `start()` plus `submit_prepared`'s own enqueue meant double enqueueing; two workers processed the same media_id, one deleted the wav and the other died with `FileNotFoundError`. `_scheduled` is the sole arbiter now.

### 5.5 Single-instance guard

Two processes working the same queue delete each other's intermediates. The guard:

```python
name = f"Local\\bili-transcriber-{sha1(str(DATA_DIR))[:12]}"
h = ctypes.windll.kernel32.CreateMutexW(None, False, name)
if ctypes.get_last_error() == ERROR_ALREADY_EXISTS (183):
    webbrowser.open(existing_url)   # point the user at the running window
    return                          # exit quietly
```

The name includes a hash of `DATA_DIR`, so **different data directories can run concurrently** (e.g. a stable build and a test build); the same directory is mutually exclusive.

**Spawn children must be excluded.** NiceGUI spawns a child that re-runs the module's `__mp_main__` branch; that child walks `main()` again, cannot possibly win the mutex, and falls into the "already running" branch — **opening another browser tab** (the real log gained `__mp_main__: 已有实例在运行,本进程退出` on every launch). Hence the identity check comes first:

```python
if sys.platform != "win32" or multiprocessing.current_process().name != "MainProcess":
    return True      # a child is not a second instance: no mutex, no browser
```

### 5.6 Idle model unload

`MODEL_IDLE_UNLOAD_SEC = 600`. When the queue is empty and no task has run for 10 minutes, the model is unloaded to free VRAM/RAM. The next submission reloads lazily — the only cost is one cold start.

Irrelevant for one-shot CLI processes; saves ~1.6 GB on a long-running desktop instance.

---

## 6. State Machine and Task Recovery

### 6.1 Status sets (single source of truth)

```python
# app/pipeline.py
ACTIVE_STATUSES = frozenset({
    "queued", "downloading", "converting", "transcribing",
    "extracting", "saving", "paused",
})
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
```

`app/ui/state.py` imports and re-exports them. Previously both files defined their own copies; updating one and forgetting the other silently drifted the "in progress" judgment. Now that is physically impossible.

### 6.2 Transitions

```
queued
  └→ downloading ──→ converting ──→ transcribing ──┬→ saving → done
                                     │              │
                                     └→ extracting ─┘   (notes enabled)
                     any stage ──→ failed / cancelled
                     any stage ──→ paused ──→ (resume) back to prior stage
```

### 6.3 Startup recovery

`Pipeline.start()` doesn't just spin up workers — it resumes leftovers:

```python
for job in self.store.list_jobs():
    if job.media_id in self._scheduled:      # already queued, skip
        continue
    if job.status not in ACTIVE_STATUSES:    # only in-progress ones
        continue
    job.status, job.progress, job.error, job.finished_at = "queued", 0.0, "", ""
    self.store.upsert_job(job)
    self._enqueue(Task(kind=..., url=job.url, media_id=job.media_id))
    recovered += 1
```

**Why every `ACTIVE_STATUSES` value matters, not just queued/paused**: a real test showed that after `--no-wait` the CLI process exited and the task sat at `transcribing` forever, ignored by the desktop app. Covering all in-progress states made the same scenario resume successfully.

### 6.4 Semantics of a repeated submission

When `submit_prepared` / `submit_local` sees a media_id already in flight, it does **not** re-enqueue and returns:

```python
{"media_id": ..., "title": ..., "status": "running", "stage": <current status>}
```

Callers (CLI / MCP / UI) can therefore distinguish "newly accepted" from "already running". This is also the first gate against two workers competing for one task.

---

## 7. Module Responsibilities

### 7.1 `paths.py` — path resolution

```python
def app_root() -> Path   # frozen = exe directory; source = project root
def data_dir() -> Path   # app_root()/data
```

**CLI / MCP must resolve to the same `data/` as the GUI**, otherwise AI calls read and write a second, independent database and cannot see desktop history or settings.

### 7.2 `main.py` — startup and UI wiring

`main()` order (the order is the correctness):

1. Early return for spawned children (`_is_spawn_child()` → `return`)
2. `setup_logging()`
3. Resolve port (`BILI_PORT`, default 8765)
4. `_resolve_ui_mode()` → single-instance check → on failure **raise the existing window** and `return`
5. `pipeline.start()` (including leftover recovery)
6. Tray → WebView2 storage dir → window watchdog → `ui.run()`

The single-instance check must precede `pipeline.start()` — otherwise the second process would first mark tasks as queued and then get evicted, leaving dirty state.

**Why step 1 exists**: from source, `multiprocessing` re-executes this module as `__mp_main__` via `runpy`, which also hits the entry guard. Continuing into `main()` would start a second server and fight over the port. Frozen builds are intercepted earlier by `multiprocessing.freeze_support()` (`--multiprocessing-fork`), so the early return is a no-op there.

**UI mode (`_resolve_ui_mode`)**

| Condition | Result |
|---|---|
| `BILI_FORCE_BROWSER=1` | `(native=False, show=True)` |
| `BILI_FORCE_NATIVE=1` | `(native=True, show=False)` |
| win32 with WebView2 detected | `(native=True, show=False)` — **default** |
| otherwise | `(native=False, show=True)` |

Trap: inside `nicegui.ui.run()` there is

```python
if window_size: native = True
```

Passing `window_size` **forces native mode**, silently overriding `native=False`. The old "browser by default" switch therefore never took effect — `use_native=False` was printed while the app actually ran natively. `window_size` is now passed only when native is intended.

**Second double-click** (`_activate_existing_window`)

After losing the mutex the second process no longer calls `webbrowser.open` and returns (the "a browser pops up and the app vanishes" symptom). It **polls `FindWindowW` for up to 30 s**, then `ShowWindow(SW_RESTORE)` + `SetForegroundWindow`, then exits. Polling is required: a cold start spends 10–20 s importing torch/transformers, so an immediate "no window" verdict would wrongly take the browser branch. If no window appears and native mode is active, a `MessageBoxW` explains it instead of opening a browser.

**WebView2 user data folder** (`_configure_native_window`)

With `private_mode` (the default) pywebview sets the WebView2 `UserDataFolder` to

```python
cache_dir = tempfile.TemporaryDirectory().name   # webview/platforms/winforms.py
```

The `TemporaryDirectory()` instance is **immediately collected**, its finalizer deletes the directory, and WebView2 keeps writing into it — the renderer crashes, the window disappears, and NiceGUI's `check_shutdown` thread sees a dead window process and calls `core.stop_and_exit()`. The whole app **exits silently with code 0 and nothing in the log**. That is the root cause of "it disappears after a few minutes".

Fix: before `ui.run()`, pin the storage path:

```python
nicegui_app.native.start_args["storage_path"] = str(DATA_DIR / "webview2")
```

The directory is stable, and instances under different install dirs no longer share one WebView2 profile.

**The trap we hit**: the first attempt used `native.settings["storage_path"] = …`, and the window process died with `KeyError: "Cannot add new key 'storage_path'. Only existing keys can be modified."` — `webview.settings` is an `ImmutableDict` that only allows modifying existing keys (`webview/util.py`); `start_args` is what actually reaches `webview.start(**…)`. The error came from a real run, not from reasoning.

**Window watchdog** (`_watch_native_window`): a background thread checks for the window every 0.5 s, logs the time-to-ready, and after 60 s logs an `ERROR` pointing at `BILI_FORCE_BROWSER=1`. A windowed exe has no console, so diagnostics belong in the log file.

The UI thread polls `Store` every **0.6 s** and drains an event `deque(maxlen=200)`.

### 7.3 `pipeline.py` — orchestration

| Method | Responsibility |
|---|---|
| `submit(url)` | Submit a link (probes internally, delegates to `submit_prepared`) |
| `submit_local(path)` | Submit a local file (content hash as media_id) |
| `submit_prepared(entries)` | Submit probed entries, returning per-entry acceptance |
| `_enqueue(task)` | The only enqueue point; maintains `_scheduled` |
| `rerun(media_id)` | Reset status and re-enqueue |
| `pause/resume/cancel` | Delegates to `task_control` |
| `apply_settings(settings)` | Hot-apply settings; rebuilds the pool when worker count changes |
| `prewarm()` | Load the model on a background thread |
| `maybe_unload_model()` | Idle unload |
| `stalled(media_id)` | Stuck detection (default: no progress for 60 s) |

### 7.4 `headless.py` — service layer

Public API: `submit` / `status` / `list_jobs` / `wait` / `transcript` / `cancel` / `prewarm` / `get_settings` / `set_settings` / `close`.

**Three key designs:**

**① `json_safe(value)`** — recursively converts `NaN` / `±Infinity` to `None`. Every outbound JSON passes through it with `json.dumps(..., allow_nan=False)`. The default `allow_nan=True` emits bare `NaN`, which breaks `JSON.parse` on the JS side.

**② `_validate_settings(changes)`** — whitelist checks on `engine` / `device` / `compute_type`, and a **write probe** for `output_dir` (actually creates the directory and writes a temp file). Hard errors raise `ServiceError`; a missing `cookie_file` / `model_dir` only yields a warning (they may be created later).

**③ `_has_orphan(ids)`** — inside `wait()` it detects "no live workers + empty queue + queued tasks exist" and returns `{"stalled": True, "hint": ...}` instead of blocking until timeout. This matches the real case of "the CLI submitted a task but this process isn't scheduling it".

**④ `channel_block_reason(channel)`** — reads `settings.json` for `enable_cli` / `enable_mcp` and, when the channel is off, returns a user-facing reason (including how to turn it back on). File-only: it never builds a `Service`.

**⑤ Persisted vs effective settings** — `Service` keeps `_persisted` (what's on disk) alongside `settings` (persisted + this process's temporary overrides). `get_settings()` reports the former and lists overrides separately:

```python
data = asdict(self._persisted)
if self._overrides:
    data["session_overrides"] = dict(self._overrides)
```

`transcribe(engine="sensevoice")` only affects the current process and is never written to disk. Previously `get_settings()` echoed the overridden object, so an AI would happily report "I changed your engine to sensevoice" when the file was untouched.

`set_settings()` is now a **read-modify-write**: it re-reads the file, then applies only the changed keys. Otherwise a long-lived MCP process would push its startup snapshot back over a switch the user just flipped in the Settings tab — the least tolerable desync for a field with security semantics.

### 7.5 `cli.py` — command line

**Encoding adaptation (important)**:

```python
def _dump_json(payload, indent=2):
    safe = json_safe(payload)
    text = json.dumps(safe, ensure_ascii=False, indent=indent, allow_nan=False)
    try:
        text.encode(_stream_encoding())      # can stdout represent this?
    except (UnicodeEncodeError, LookupError):
        text = json.dumps(safe, ensure_ascii=True, indent=indent, allow_nan=False)
    return text
```

`_write()` adds a `backslashreplace` safety net.

**Why this is mandatory**: a PyInstaller `console=False` exe reuses the parent's pipes, so `sys.stdout.encoding` is the system ANSI code page — **GBK** on Chinese Windows. `print('🎉')` then raises `UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f389'` and the CLI crashes at the last step.

Test with `PYTHONIOENCODING=gbk` and verify the output is still valid JSON (ASCII escapes parse fine).

**Channel gate and exit codes.** `main()` is ordered deliberately: `--version` → hand `--mcp` over → **gate** → `setup_logging()` → build `Service`. The gate runs before `Service` because a disabled channel should not open the database (otherwise you take a SQLite lock and spin up a pipeline for nothing); `test_cli_does_not_touch_database_when_disabled` asserts the factory was never called.

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | job failed / execution error (including invalid values) |
| 2 | bad arguments (missing value, conflicting flags) |
| 3 | channel disabled in Settings |

`--version` bypasses the gate — at least you can still get the version while debugging.

**Broken pipes don't raise.** `cli ... | head -1` closes stdout early; Python raises `BrokenPipeError`, the exit code becomes 1 and scripts read it as "job failed". `_write()` catches `(BrokenPipeError, OSError, ValueError)` and points `sys.stdout` at `devnull`, which also avoids a second `Exception ignored in: <_io.TextIOWrapper>` flush at interpreter shutdown.

**Piped output is forced to UTF-8.** The first line of `main()` is `_configure_output_encoding()`: if `stdout`/`stderr` are not a tty (redirected, or a pipe from a parent process), they get `reconfigure(encoding="utf-8", errors="replace")`; a real terminal keeps the console code page.

Why this matters: the frozen build embeds its own runtime and **does not inherit the caller's `PYTHONUTF8` / `PYTHONIOENCODING`**, so on Chinese Windows stdout uses the ANSI code page `cp936`. JSON then goes out as GBK — fine for a local `subprocess.run(..., text=True)` that decodes with the locale, but a client that parses UTF-8 (Node `child_process`, Python with an explicit `encoding="utf-8"`) gets mojibake or a hard `UnicodeDecodeError`, and those are exactly the consumers of the CLI channel.

The earlier implementation only fell back to `ensure_ascii=True` when the stream genuinely could not encode a character (`_dump_json`) — but **GBK can encode common Chinese, so that branch never fired**, leaving output that humans could read and machines could not. Reconfiguring the stream makes `_stream_encoding()` return `utf-8` and the two paths finally agree.

Leaving terminals alone is deliberate: writing UTF-8 to a default `cmd.exe` code page garbles the screen, and that copy is the one a human reads.

### 7.6 `mcp_server.py` — MCP server

Hand-written JSON-RPC 2.0 subset; no third-party SDK (smaller bundle, fewer dependency risks).

Methods: `initialize` / `tools/list` / `tools/call` / `notifications/initialized` / `ping`.

**Robustness measures:**

| Problem | Handling |
|---|---|
| Unreliable arg types (AI clients send `limit` as a string) | `_arg_int` / `_arg_num` / `_pick` coerce; `"10"` parses, `"abc"` yields a readable error |
| Client sends a batch (JSON array) | Run `handle()` per item and **flatten** into one response array |
| NaN in results | `_tool_result()` applies `json_safe` + `allow_nan=False` |
| Started without pipes (stdin/stdout are None) | `_reopen_null_streams()` logs and returns `False` → `main()` returns 1 |
| Client unconditionally calls `resources/list` / `prompts/list` | Return empty lists instead of `-32601`; some clients mark the whole server unusable on "method not found" |
| Stray `print` or third-party output | `_isolate_stdout()` keeps the protocol handle local and points `sys.stdout` at stderr |
| Client sends non-UTF-8 bytes | stdin uses `errors="replace"`; `serve()` catches decode/read errors and ends the session instead of raising |
| Channel disabled in Settings | `main()` refuses before building `Service`, writes the reason to stderr, keeps **stdout clean**, exits 1 |

**Why stdout must be isolated**: an MCP stdio frame is literally "one JSON per line". A single non-JSON line on stdout makes the client fail `JSON.parse` or drop the connection — and it's brutal to debug, because the culprit is one leftover `print` in your own code. Pulling the protocol channel aside is the standard LSP / MCP approach:

```python
proto = sys.stdout          # protocol only
sys.stdout = sys.stderr     # everything else (logs, library prints) goes to stderr
serve(server, sys.stdin, proto)
```

**Why bad bytes must not raise**: one malformed byte kills the process and the client only sees "server exited unexpectedly", while the real cause (encoding) hides inside a traceback. Measured: `serve()` used `iter(stdin.readline, "")` and died with `UnicodeDecodeError` on `\xff\xfe`; an explicit `readline()` + `try/except` downgrades the bad line to a `-32700 Parse error` response and the session continues.

### 7.7 `downloader.py` — download orchestration

- **Platform detection** by domain: `bilibili / youtube / douyin / xiaohongshu / kuaishou`; unknown domains treated as Bilibili-compatible. media_id prefixes: `yt_` / `dy_` / `xhs_` / `ks_`.
- **Short-link safety**: b23.tv and friends pass a **domain allowlist** plus **DNS-rebinding protection** (resolved IP re-check) before redirects are followed.
- **Cookies / proxy**: `cookiefile` (cookies.txt) takes precedence over `cookiesfrombrowser`; browser names outside `SUPPORTED_BROWSERS` are ignored to avoid YoutubeDL constructor errors.
- **Reachability cache**: `_reach_cache` is invalidated when the proxy changes.
- **WeChat Channels are rejected intentionally** — no login-free direct stream exists; the UI is told to use local upload instead of attempting a doomed download.

### 7.8 `netcheck.py` — network and cookie auto-detection

Backend for the Settings page's "Detect" and "Test connection" buttons. **Read-only**: it never writes
settings (the UI decides what to fill) and degrades every failure into a readable message.

| Capability | Entry point | Notes |
|---|---|---|
| Browser scan | `detect_browsers()` | Enumerates Chromium-family and Firefox profiles under `LOCALAPPDATA`/`APPDATA`; reads only `(host_key, name, expires_utc)` metadata — **never decrypts cookie values** |
| cookies.txt scan | `detect_cookie_files()` | Looks for `*cookie*.txt` in project dir / `data/` / Downloads / Desktop / Documents |
| Proxy discovery | `detect_proxies()` | System proxy (registry) → env vars → common local ports (7890/7897/10809…), each verified against YouTube |
| Network probe | `probe_network()` | Five platforms probed in parallel, hard deadline caps total time |
| Recommendation | `build_report()` | Picks the source with most platform coverage; ties go to cookies.txt; keeps current values when nothing is found |
| Config test | `test_configuration()` | Real network + real cookie values; Bilibili checked via `x/web-interface/nav` (`isLogin`/`uname`) |
| Export | `export_browser_cookies()` | Decrypts via yt-dlp and writes a Netscape file (contains credentials; only on explicit click) |

**Why detection does not decrypt**: "is platform X logged in?" only needs domain/name/expiry.
Metadata is cheap and needs no DPAPI work; only "Test" and "Export" need values.

**Chromium lock limitation**: while the browser runs it holds an exclusive lock on `Cookies`;
`shutil.copy2` fails with `WinError 32`. `BrowserInfo.running` is then `True` and the UI says
"close the browser and re-detect, or use cookies.txt" — a known limitation, not a crash.

**Two traps worth remembering**: (1) `urllib`'s default opener honours env proxies, so direct probes must pass
`ProxyHandler({})`; (2) socket timeouts apply per-address, so probes use `future.result(timeout=…)` as a hard
deadline, with a 2 s DNS cap that is skipped whenever a proxy is in use.

### 7.9 Three engines

| Module | Backend | Timestamp strategy | Language mapping |
|---|---|---|---|
| `transcriber.py` | faster-whisper / ctranslate2 | Native | whisper codes |
| `qwen_transcriber.py` | qwen-asr (torch) + ForcedAligner | Aligner; falls back to duration-based estimation | `_map_qwen_language()` whisper→full name, unknown → auto |
| `sensevoice_transcriber.py` | sherpa-onnx int8 | Token-level timestamps aggregated by `_tokens_to_segments()` | `_map_sensevoice_language()` |

Shared behavior:

- **Lazy load + cache key** `(model_size, device, compute_type, language, vad)`; reload only when it changes.
- **VAD self-healing**: when Silero VAD filters out all speech (music), retry once with VAD disabled.
- **Qwen chunking**: long audio split into 60 s chunks with `on_event` reporting "chunk i/n".

### 7.10 `writers.py` — output writing

```python
def _write_text_retry(path, text, attempts=6):
    tmp = path.with_name(f".{path.name}.tmp")
    for i in range(attempts):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)      # atomic replace
            return path
        except PermissionError:
            time.sleep(0.2 * (i + 1))  # linear backoff
    raise
```

Write `.tmp`, then `os.replace`, with six `PermissionError` retries. Two real problems solved: antivirus briefly locking the directory, and the user having the file open elsewhere. **You never get a truncated file.**

### 7.11 `models.py` / `gpu_runtime.py`

- `default_model_dir()` → `%LOCALAPPDATA%\Bili Note\models`; `resolve_model_dir()` honors a custom path.
- `scan_models()` / `describe_models()` validate completeness per engine's characteristic files, driving the "Check models" button.
- `gpu_runtime` locates 17 CUDA DLLs under `%LOCALAPPDATA%\Bili Note\gpu` using the `nvidia/{cublas,cudnn,cuda_runtime,cuda_nvrtc}/bin/` layout (`_dll_root()` probes for `cublas64_12.dll`, and tolerates a flat layout), then injects both `add_dll_directory` and PATH before ctranslate2 can load.
  - **Why both injections matter**: ctranslate2 is built with `CUDA_DYNAMIC_LOADING=ON`; lazy cuBLAS loading bypasses Windows safe-search mode, so `add_dll_directory` alone makes ctypes succeed while transcription still fails.
  - **Handles must stay alive**: once a handle from `add_dll_directory` is GC'd the search path vanishes, showing up as "model loaded fine, then died during transcription" — hence `_dll_handles` keeps them forever.
  - **Idempotent registration**: `_registered_dirs` stops every transcription from re-prepending PATH and growing it without bound.
- `ensure_gpu_or_cpu()` falls back to CPU silently when the pack is absent, and pre-validates `ctypes.WinDLL("cublas64_12.dll")` so failures surface at detection time, not mid-transcription.

### 7.12 `keyframes.py` — dual backend

| Backend | Detection | When |
|---|---|---|
| ffmpeg (primary) | `select='gte(scene,0)'` + `metadata=print`, threshold 0.11 | Fast, almost always works |
| PyAV (fallback) | 64×36 grayscale MAD > 15 | When ffmpeg is unavailable |

Shared policy: always capture the first frame; capture 0.4 s after a cut; minimum 5 s interval; cap at 400 frames. `KeyframeError` and user cancellation propagate instead of falling back.

### 7.13 The `ui/` trio

- `state.py` — creates singletons at first module import (store / settings / pipeline / events), matching pre-split `main.py` behavior.
- `pages.py` — four-tab layout.
- `helpers.py` — pure action functions, no layout; UI-level names like `_launch_explorer` are monkeypatchable in tests.

**Table row event gotcha**: after NiceGUI `$emit('open-path', path)`, `e.args` is a list `[path]` and must be unwrapped by `_first_arg(e)`. The explorer call `/select,"path"` needs quotes or paths with spaces get truncated.

### 7.14 `mcp_http.py` — MCP Streamable HTTP transport

For callers that **cannot spawn child processes**: DSH streamable-http connections, containers, in-browser agents. Protocol handling is fully shared with `MCPServer.handle`, so stdio and HTTP expose identical tools and error semantics.

| Capability | Detail |
|---|---|
| Routes | `POST /mcp` (JSON or SSE per Accept), `GET /mcp` always 405 (no server-push stream), `DELETE /mcp` 204 ends session, `GET /healthz` liveness |
| Sessions | created on `initialize` (`Mcp-Session-Id`), 30-min idle TTL, cap 64 (oldest evicted); business state lives in Service/DB so eviction is side-effect-free |
| Security defaults | binds `127.0.0.1` only; Origin allowlist (non-local 403, anti DNS-rebinding); zero CORS by default; optional Bearer token (`BILI_MCP_HTTP_TOKEN`) |
| Request guards | missing Content-Length → 400 (no chunked), 8 MB body cap, bad JSON `-32700`, unknown session → 404 asking to re-initialize |

**Two gotchas caught by real tool-closure tests:**

1. **`start()` must spawn its own thread running `serve_forever()`** — the stdlib constructor alone never accepts connections; clients see "requests hang while the server listens".
2. **`server_bind` calls `socket.getfqdn()` by default** — on machines whose DNS is hijacked by proxies this reverse lookup stalls startup for tens of seconds. `_QuietHTTPServer` overrides it with the literal IP.

Also: the HTTP server is threaded while the Service's SQLite connection is not concurrency-safe → `_handle_lock` serializes tool calls (matching stdio's natural serialization).

### 7.15 `agent_setup.py` — per-client AI onboarding config generator

One MCP server, eight clients, eight config formats — this module freezes the known shapes:

| Client | Format | Delivery |
|---|---|---|
| WorkBuddy | JSON `mcpServers` | `--write` to `~/.workbuddy/mcp.json` |
| DSH | `tables.connections` entry | **direct write refused** (the table is UI-managed); a paste-ready streamable-http / stdio entry is generated instead |
| Codex | TOML `[mcp_servers.*]` | `--write`; only documented keys (`startup_timeout_sec=300`) — **an invented unknown key breaks the whole config.toml** |
| Claude / Cursor / VS Code / Tare / generic | JSON | `--write` or manual paste |

Design rule: **generate, never silently mutate user config**. Writes require explicit `--write`, back up to `.bak`, and touch only the `bili-transcriber` entry; `merge_toml` replaces by section boundaries, leaving the user's provider/model config intact. `resolve_executable` distinguishes frozen (`xxx.exe --mcp`) from source (`python -m app.mcp_server`). `doctor()` aggregates local client detection and common pitfalls; paths always go through `app.paths.data_dir()` (hardcoding either flavor's path misreports on the other).

---

## 8. Data Contracts

### 8.1 Settings (16 keys)

```jsonc
{
  "output_dir": "output",         // relative to app_root, or absolute
  "model_size": "large-v3-turbo",
  "device": "auto",               // auto / cuda / cpu
  "compute_type": "auto",         // auto / float16 / int8_float16 / int8
  "language": "",                 // empty = auto
  "keep_audio": true,
  "vad": true,
  "notes": false,
  "engine": "whisper",            // whisper / qwen3-asr / sensevoice
  "model_dir": "",                // empty = default dir
  "cookies_from_browser": "",
  "cookie_file": "",
  "proxy": "",
  "workers": 2,                   // 1–6
  "enable_cli": true,             // CLI channel (desktop-only write)
  "enable_mcp": true              // MCP channel (desktop-only write)
}
```

The last two are **not** in `OVERRIDABLE_SETTINGS`: both `--set` and the MCP `update_settings` tool reject them. That isn't fussiness — if they were writable, a prompt-injected AI could call `update_settings(enable_mcp=true)` to keep itself installed, or disable the channel to fake a "broken service". The permission boundary lives on the human side.

The reader (`channel_block_reason`) parses `settings.json` directly instead of building a `Service`, because a disabled channel should not open the database or spin up a pipeline. On a corrupt file it falls back to the default (enabled): this switch narrows the external attack surface, it is not a tamper-proof boundary (anyone who can edit the file can flip it back), and a user who wrecked their config is worse off if the CLI is also locked out.

### 8.2 Job record (SQLite `jobs`)

Primary key `media_id`, UPSERTed. Fields: `media_id` / `url` / `title` / `status` / `progress` / `error` / `source_type` / `output_dir` / `created_at` / `finished_at`.

`source_type` decides whether recovery enqueues a `local` or `prepared` task — **recovering as `prepared` rather than `url`** prevents a multi-part video from being re-probed as a whole batch and dragging the single part the user wanted back into the queue.

### 8.3 Output Directory and Filename Rules

Two places own the whole naming scheme; changing either affects every artifact:

**Directory** (`Pipeline._job_dir`):

```python
folder = info.playlist_title or info.title           # collections prefer the overall title
name = _safe_name(folder) if not info.bv else f"{info.bv}_{_safe_name(folder)}"
if unique_suffix:                                     # local jobs de-dup via media_id
    name = f"{name}_{_safe_name(unique_suffix)}"
```

- `info.bv` comes from `_bilibili_id()`, which takes the part before the underscore — **the `_p2` suffix is already stripped** — so every part of a collection lands in the same directory.
- Local files have an empty `info.bv` and pass `unique_suffix = media_id` (`local_<sha1>`), so the directory is `{filename}_{hash}` and same-named files with different content never collide.

**Filename stem** (`downloader.output_stem`):

```python
stem = _safe_name(info.title) or info.media_id
if info.total_parts > 1:
    stem = f"{stem}_P{info.part:02d}"
```

`_P01` is appended only when `total_parts > 1`, because part titles inside a collection often repeat and would otherwise overwrite each other. Single-part videos keep the plain title for backward compatibility.

`_safe_name()` replaces `<>:"/\|?*` with `_`, trims trailing spaces and dots, and truncates to 80 characters.

### 8.4 Service Responses

```jsonc
// submit
{"accepted": 2, "running": 1, "skipped": 1, "jobs": [{"media_id": "...", "status": "queued"}]}

// wait
{"state": "done", "done": 3, "failed": 0, "jobs": [...], "waited_sec": 12.4}

// wait (abnormal)
{"state": "stalled", "stalled": true, "hint": "task is queued but this process isn't scheduling it..."}

// transcript
{"media_id": "...", "format": "md", "chars": 4123, "truncated": false, "text": "..."}
```

---

## 9. Security and Robustness

| Risk | Mitigation |
|---|---|
| SSRF / DNS rebinding (short links) | Domain allowlist + resolved-IP re-check before following redirects |
| Filename injection | `_safe_name()` strips illegal characters and truncates |
| Bad settings crashing startup | Whitelist validation + output-dir write probe on save |
| Truncated outputs / write failures | Atomic writes + `PermissionError` backoff |
| Breaking JS `JSON.parse` | `json_safe` + `allow_nan=False` on every outbound payload |
| Packaged-exe encoding crash | stdout encoding probe + `ensure_ascii` fallback + `backslashreplace` |
| Two processes racing one task | `CreateMutexW` single instance + `_scheduled` de-duplication |
| An AI flipping the channel switches | Both are desktop-only; `--set` / `update_settings` reject them |
| Desktop and an AI process writing together | `PRAGMA busy_timeout=5000` (WAL allows one writer; wait instead of failing) |
| Third-party `print` corrupting MCP framing | `_isolate_stdout()` points `sys.stdout` at stderr, protocol handle held separately |
| Non-UTF-8 input from a client | stdin `errors="replace"` + `serve()` catches decode/read errors → `-32700` instead of a crash |
| Zombie tasks | Startup recovery over all `ACTIVE_STATUSES` + `wait()` stalled detection |
| Cookie leakage | Cookies go only to yt-dlp during download; never logged or packaged |
| Destructive scans/deletes | `scripts/bdelete/` tools require explicit paths; no wildcard deletion |

---

## 10. Packaging Pitfalls

| Problem | Mitigation |
|---|---|
| transformers mis-detects sklearn → `GenerationMixin` import crash | Spec `excludes=["sklearn"]` + `preload_transformers.py` hook setting `_sklearn_available=False` |
| nagisa bare-imports `prepro` (PEP 366 failure) | Hook inserts the nagisa package dir into `sys.path` (importing `nagisa.prepro` causes a circular import); hiddenimports add `nagisa/dynet/_dynet` |
| Sandbox safe-delete hook blocks PyInstaller cleanup | `release.py` calls `_evacuate()` to rename old dist/build aside so `--noconfirm` has nothing to delete |
| native window misbehaves | WebView2 storage dir is pinned; `BILI_FORCE_BROWSER=1` falls back to the browser |
| CUDA DLL resolution | `__init__.py` injects `sys._MEIPASS` into the search path in frozen mode; `gpu_runtime` registers the external pack |
| **Windowed exe stdout is GBK** | CLI encoding probe + `ensure_ascii` fallback; log handler skipped when `sys.stderr is None` |
| **`console=False` leaves stdin/stdout as None** | `logging_setup` null-guards; MCP `_reopen_null_streams()` fails explicitly instead of `AttributeError` |
| NiceGUI `multiprocessing` re-imports the module | Check `multiprocessing.current_process().name` to avoid building the UI in children |

---

## 11. Build and Release

### 11.1 Version single source

`pyproject.toml`'s `version` is the only truth. `release.py` synchronizes:

1. `app/__init__.py`'s `APP_VERSION`
2. `build/installer.iss`'s `AppVersion`
3. **The four manuals' header declarations** + versioned snapshot generation
4. **The README header** (rolling only, no snapshot)

`tests/` asserts three-way version consistency, so a missed sync fails the suite.

### 11.2 Five artifacts

| Artifact | Output |
|---|---|
| Wheel | `dist/bili_transcriber-{ver}-py3-none-any.whl` |
| Portable | `dist/bili-transcriber-portable-{ver}.zip` (full onedir, ~6900 files) |
| Installer | `dist/bili-transcriber-setup-{ver}.exe` (InnoSetup) |
| Single-file | `dist/bili-transcriber-single-{ver}.exe` (onefile, CPU) |
| GPU pack | `dist/bili-transcriber-gpu-{ver}.zip` (17 CUDA DLLs) |

Artifact checks: zip `testzip()`, PE headers (`MZ` / `PE`), and `_built_fresh()` timestamps to prevent shipping stale builds.

### 11.3 Commands

```powershell
python scripts/release.py               # bump + five artifacts + doc snapshots
python scripts/release.py --wheel-only  # fast delivery
python scripts/release.py --bin-only    # portable + installer only (version already set)
python scripts/release.py --setup-only  # onedir + installer only
python scripts/release.py --single-only # rebuild the single-file build only
```

---

## 12. Test Suite

`tests/` holds **28 files / 512 cases**, green in about 2 minutes.

| Area | Files | Strategy |
|---|---|---|
| Storage | `test_store.py` | Real SQLite temp databases |
| Download | `test_downloader.py` / `test_kuaishou.py` | Mocked yt-dlp / SSR HTML fixtures |
| Network & cookies | `test_netcheck.py` / `test_native_dialog.py` | Temporary SQLite cookie DBs, probes mocked, no network |
| Engines | `test_transcriber` / `test_qwen_transcriber` / `test_sensevoice_transcriber` | FakeModel injection; **concurrent `load()` regression** (`Barrier(2)` asserts a single load) |
| Orchestration | `test_pipeline.py` | FakeDownloader / FakeTranscriber at the boundary; covers "no re-enqueue for active tasks", leftover recovery, mid-run kill recovery |
| Service layer | `test_headless.py` | `running` semantics / stalled detection / prewarm / four settings validations / status aliases / **search full-text retrieval** |
| CLI | `test_cli.py` | Argument parsing, JSON output, **GBK stdout regression**, non-integer `--set`, **--grep/--doctor/--agent-config/--mcp-http gating** |
| MCP | `test_mcp_server.py` | Handshake, **11 tools**, bad-arg tolerance, **batch flattening**, **NaN sanitizing**, timeout clamping |
| MCP HTTP | `test_mcp_http.py` | Session TTL/eviction, Origin allowlist, Bearer token, ten route states, **real-socket end-to-end** |
| Onboarding config | `test_agent_setup.py` | 8-client rendering, JSON/TOML merge preserving foreign entries, DSH write refusal, **no invented TOML keys** |
| Writers | `test_writers.py` | Atomic write retries, no truncated files |
| UI | `test_ui.py` / `test_main_helpers.py` | NiceGUI `user_plugin`; `_first_arg` / explorer quoting |
| Release | `test_release_docs.py` | Automatic manual version-header sync |
| Packaging | `test_nagisa_ascii.py` etc. | Runtime hooks and compatibility |

### 12.1 Adversarial review methodology

This project uses three-perspective iterative review:

| Perspective | Focus | Typical output |
|---|---|---|
| **Tool implementer** | Edge cases, resource cleanup, encoding, thread safety | `_scheduled` de-dup, atomic writes, encoding fallback |
| **Reviewer** | Contract consistency, error paths, duplication | `ACTIVE_STATUSES` single source, `json_safe` single exit |
| **AI consumer** | Are args easy to pass? Are errors readable? Is state decidable? | `running` semantics, stalled hints, `_arg_int` coercion |

The rule: **reproduce a real failure before fixing anything.** Every fix above corresponds to an actual failing run, not a "looks suspicious" hunch.

### 12.2 Running

```powershell
.venv\Scripts\pytest -q                       # full suite
.venv\Scripts\pytest tests/test_cli.py -v     # single file
```

---

## 13. Known Limitations

- **Windows only in practice.** macOS/Linux are untested: `native_dialog` (comdlg32), the single-instance guard (`CreateMutexW`), and `ui/helpers`' explorer calls are Windows-specific. They degrade elsewhere but are unverified.
- **GPU acceleration covers whisper only** (ctranslate2). Qwen3-ASR runs on torch (GPU helps, different optimization); SenseVoice runs on onnxruntime CPU.
- **yt-dlp cannot be upgraded inside packaged builds.** Platform API changes require a new release.
- **The Kuaishou SSR parser depends on share-page structure**; site changes require updating `app/kuaishou.py`.
- **Sandboxed environments may block PyInstaller rebuilds** via safe-delete policy; final exe builds need full permissions on the host.
- **MCP implements a JSON-RPC subset only** — no resources, prompts, or sampling.
- **Keyframe extraction caps at 400 frames**, so very long videos lose some slides.
