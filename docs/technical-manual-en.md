# Technical Manual — bili-transcriber

> matches **v0.1.45** · For people who change the code, ship releases, or debug incidents.
> For usage, see the [User Guide](user-guide-en.md).

---

## Contents

1. [Architecture](#1-architecture)
2. [Data flow and state machine](#2-data-flow-and-state-machine)
3. [Module map](#3-module-map)
4. [The three engines](#4-the-three-engines)
5. [Network and cookie subsystem](#5-network-and-cookie-subsystem)
6. [Headless layer (CLI / MCP)](#6-headless-layer-cli--mcp)
7. [Desktop UI layer](#7-desktop-ui-layer)
8. [Packaging and release](#8-packaging-and-release)
9. [Quality baseline and tests](#9-quality-baseline-and-tests)
10. [Known limits and trade-offs](#10-known-limits-and-trade-offs)

---

## 1. Architecture

<img src="../docs/assets/pipeline.svg" alt="data flow" width="100%" />

Three layers with hard boundaries:

```text
┌──────────────────────────── Entry layer ─────────────────────────────┐
│  main.py (GUI dispatcher)  cli.py (CLI)  mcp_server.py / mcp_http.py  │
└───────────────────────────────┬──────────────────────────────────────┘
                                ↓ all depend on one service layer
┌──────────────────── Service layer headless.py ───────────────────────┐
│  Service: submit / wait / status / list / transcript / search /      │
│           cancel / prewarm / get_settings / set_settings             │
└───────────────────────────────┬──────────────────────────────────────┘
                                ↓
┌──────────────────── Orchestration pipeline.py ───────────────────────┐
│  single worker thread, state machine, resume, model cache / unload   │
└───┬──────────┬──────────┬──────────┬──────────┬─────────────────────┘
    ↓          ↓          ↓          ↓          ↓
downloader  converter  transcriber   writers    store
(yt-dlp)    (FFmpeg)   (3 engines)  (SRT/TXT/MD)(SQLite)
```

**Why split this way**: three entry points (GUI / CLI / MCP) must behave identically. Otherwise
"works in the desktop app, fails in the CLI" becomes a permanent bug source. Entry points only convert
protocols; all logic lives in `Service` and `pipeline`.

**app/ holds 32 Python files** (28 top level + 4 under `ui/`).

## 2. Data flow and state machine

### 2.1 Path of one job

```text
submit(url)
  → downloader.probe(url)        expand into MediaInfo items (single / multi-part / collection)
  → enqueue each item
  → worker picks one up →
      [1] downloader.download_audio   audio stream only (video stream when notes are enabled)
      [2] converter.to_wav            16 kHz mono WAV
      [3] transcriber.transcribe      GPU, holding the lock
      [4] writers.write_*             SRT / TXT / MD (+ notes)
      [5] store.upsert                write history row
  → done
```

### 2.2 State machine

```text
queued → downloading → converting → transcribing → saving → done
                     ↘           ↘            ↘        ↘
                      failed / cancelled (from any step)
paused ⇄ (resumes back to queued)
```

States are persisted in `history.db`, so a killed process still knows where it stopped;
`recover_paused()` restores `paused` rows to `queued` at startup.

### 2.3 Resume behaviour

- Audio and WAV are cached by media ID in the output directory's staging area and reused on retry.
- Intermediates (fragments, temp WAV) are deleted as soon as the step finishes.
- Re-running the same video performs an `UPSERT` on the same primary key — no duplicate rows.

### 2.4 Concurrency model

| Stage | Concurrency | Why |
|---|---|---|
| Download / convert | Parallel (`workers` 1–6) | Network and IO rarely saturate the CPU |
| Transcribe | **Serial** | One model fits in VRAM; concurrency causes OOM |
| Writing | Per worker | No conflicts, each job owns its files |

`pipeline.apply_settings()` rebuilds the pool only when idle. "Busy" must consider
`running + paused` — a paused worker may be blocked in `wait_if_paused`, and checking only
`running` would let old and new pools coexist.

### 2.5 Model cache and unloading

- Cache key: `(model_size, device, compute_type, language, vad)`. Same key means no reload.
- All three engines use double-checked locking in `load()` (concurrent double-load was a real bug).
- 600 s idle → `maybe_unload_model()` frees VRAM; the next job reloads.
- Saving settings triggers `prewarm()` (idempotent, skipped while busy) with a stale check: if the
  engine instance changed while loading, the fresh instance is unloaded immediately to avoid a leak.
## 3. Module map

| Module | Lines | Responsibility | Notes |
|---|---|---|---|
| `main.py` | 446 | Process entry + UI dispatch | exe defaults to GUI, needs `--cli`; `_is_spawn_child()` lets NiceGUI's spawn through |
| `ui/pages.py` | 1006 | Three tabs + settings | CSS-class animations; Quasar `loading` eats button text, use `set_text+disable` |
| `ui/helpers.py` | 402 | UI helpers | `_notify()` safe wrapper (tray thread has no UI context) |
| `ui/state.py` | 61 | UI state | 0.6 s Store polling plus an event deque |
| `pipeline.py` | 784 | Single-thread orchestration | State machine, resume, prewarm, idle unload, control flags |
| `downloader.py` | 573 | Download orchestration | Platform detection, short links, SSR, **cookie bridging**, reachability cache |
| `converter.py` | 189 | FFmpeg conversion | 16 kHz mono WAV, prefers bundled static ffmpeg |
| `transcriber.py` | 239 | Whisper | BatchedInferencePipeline on CUDA, falls back to per-segment |
| `qwen_transcriber.py` | 466 | Qwen3-ASR | Grouped batches, whole-batch fallback on count mismatch |
| `sensevoice_transcriber.py` | 265 | SenseVoice | sherpa-onnx int8, token-level timestamps aggregated |
| `writers.py` | 178 | Output writers | SRT / TXT / MD / notes, atomic writes (temp + rename) |
| `store.py` | 246 | SQLite | `busy_timeout=5000`; `DEFAULT_SETTINGS` is the single source of truth |
| `headless.py` | 505 | Service layer | Channel gate, settings validation, search |
| `cli.py` | 393 | CLI | 34 options, forced UTF-8 off-TTY, BrokenPipe recovery |
| `mcp_server.py` | 640 | MCP stdio | 11 tools; `_isolate_stdout()` keeps prints out of protocol frames |
| `mcp_http.py` | 487 | MCP HTTP | Loopback only, 30 min session TTL, Origin allowlist, Bearer token |
| `agent_setup.py` | 390 | 8 client configs | DSH refuses direct writes; Codex gets documented TOML keys only; JSONC yields actionable errors |
| `netcheck.py` | 1218 | Network/cookie detection | Metadata vs value vs lock kept separate; hard probe deadlines; `ProxyHandler({})` |
| `cookiebridge.py` | 735 | Cookie decryption bridge | Minimal profile copy, headless instance, stdlib WebSocket over CDP |
| `keyframes.py` | 288 | Keyframes | Slide screenshots, scene-change aware |
| `kuaishou.py` | 141 | Kuaishou | Hand-written share-page parser (yt-dlp has no extractor) |
| `models.py` | 257 | Model management | External directory, integrity checks, mirror download |
| `hardware.py` | 195 | Hardware probing | CUDA availability, VRAM, device choice |
| `gpu_runtime.py` | 118 | Runtime registration | Adds NVIDIA package bin dirs to PATH |
| `paths.py` | 21 | Paths | Frozen: exe directory; source: project root |
| `task_control.py` | 63 | Control flags | Cancel/pause bits, cleared on submit |
| `tray.py` | 98 | Tray | `on_open` raises the window instead of opening a browser |
| `native_dialog.py` | 110 | Native dialogs | File pickers (browser pickers unusable inside WebView) |
| `ffmpeg_bin.py` | 74 | ffmpeg location | Bundled static build first |
| `logging_setup.py` | 59 | Logging | `%LOCALAPPDATA%\Bili Note\logs`, 2 MB rotation keeping 3 |

## 4. The three engines

Shared contract — `pipeline` relies on exactly three methods:

```python
load(key) -> None          # idempotent, double-checked locking
transcribe(wav, language, progress) -> list[Segment]
unload() -> None           # locked, idempotent
```

| Engine | Batching | Fallback |
|---|---|---|
| Whisper | `BatchedInferencePipeline(_BATCH_SIZE=8)` on CUDA | batch error → per-segment |
| Qwen3-ASR | groups of `_BATCH_CHUNKS=4` (native List[audio]) | count mismatch/error → discard the batch and retry per chunk |
| SenseVoice | single segment (model is light) | none |

Timestamps come from different places: Whisper natively; Qwen via ForcedAligner with a
duration-based estimate as fallback; SenseVoice from token-level timestamps aggregated into segments.

`pipeline` holds the lock for the whole `transcribe()` call — that is how "transcription is
serial" is enforced, not by pool sizing.

## 5. Network and cookie subsystem

### 5.1 Three detection lines (`netcheck`)

| Line | Entry point | Decrypts values? |
|---|---|---|
| Browser scan | `detect_browsers()` | No — reads `(host_key, name, expires_utc)` only |
| cookies.txt scan | `detect_cookie_files()` | No (the file is already plaintext) |
| Network probe | `probe_network()` | — (system proxy → env vars → common ports → YouTube verification) |

`build_report()` picks the source with the widest platform coverage; ties prefer cookies.txt;
when nothing is found it keeps the user's current values instead of blanking them.

### 5.2 Three-outcome semantics (the important design)

"Value unreadable" is not "login state unreadable". `test_configuration()` returns four values:

```python
(values, states, metadata, error)
```

| Situation | metadata | Per-platform state | report.ok |
|---|---|---|---|
| Value obtained | dict | present / verified | may be True |
| No value, metadata available | dict + error | **present** (with "value encrypted") | **False** |
| Not even metadata | None | unreadable (one shared short line) | False |

> The last row's `report.ok=False` came out of review: treating "login exists" as "usable
> configuration" is a false positive — the user sees green, tries to download, fails, and has no clue why.

### 5.3 Chrome 127+ v20 (App-Bound Encryption)

**Problem**: cookie ciphertext moved from `v10` to `v20`, and the key is wrapped by the
elevation service and bound to the original `user-data-dir` path. yt-dlp implements v10 only and
reports `Failed to decrypt with DPAPI` (issues/10927) for v20.

**Solution** (`cookiebridge.py`):

```text
copy Local State + cookie DB into a temp directory
   → start a --headless=new throwaway instance (--user-data-dir=temp copy)
   → hand-written stdlib WebSocket over CDP Network.getAllCookies
   → the browser decrypts for us, returns plaintext JSON
   → kill the process, delete the directory
```

**Implementation details (all learned the hard way)**:

1. **Client frames must be masked** — unmasked frames make the server drop the connection right after the
   `101` handshake. Server frames are unmasked; do not mirror the masking.
2. **Preserve the layout** — `Local State` sits at the User Data root while the cookie DB lives at
   `<root>/<Profile>/Network/Cookies`. Copying the two files without recreating that nesting makes
   the browser find no database and return zero cookies.
3. **Multiple profiles need an explicit name** — the in-place strategy must pass
   `--profile-directory=<name>`, otherwise Chrome silently falls back to `Default` and reads
   someone else's login state.
4. **Lock detection by copy probe** — if a copy succeeds, the DB is not exclusively held. This beats both
   `SingletonLock` (left behind after crashes) and process-name checks (which misread "another
   profile is open" as "this profile is locked").
5. **Do not retry into the void** — Chromium grants no shared read at all while running (measured: 60
   attempts in 30 s, all failing). Copy retry dropped from 6 s to 0.4 s, taking the failure path from
   **6.08 s to 0.46 s**.

### 5.4 The download side must be fixed too

Fixing only "Test connection" leaves a contradiction: the test passes, the download says it needs cookies.
So `downloader._cookie_opts()` bridges as well: plaintext is written to
`data/.cookies-from-<browser>.txt` (10-minute cache) and handed to yt-dlp as `cookiefile`;
when bridging is impossible it falls back to `cookiesfrombrowser`.

> The cache lives under `data/`, which is git-ignored.

### 5.5 Mainland-China adaptations

| Trap | Handling |
|---|---|
| Slow HuggingFace | `hf-mirror` |
| SSL certificate failures | self-healing retry |
| System proxy hijacking loopback | every internal urllib call passes `ProxyHandler({})` |
| CUDA wheels not found | release flow injects the wheel index |
| socket timeout is per-address-per-operation | probes use `future.result(timeout=)` as a hard deadline |
## 6. Headless layer (CLI / MCP)

### 6.1 Channel gates

`enable_cli` / `enable_mcp` live in `store.DEFAULT_SETTINGS`, written by the desktop
settings page and read-only for CLI/MCP. The gate is evaluated **before** constructing `Service` so no
SQLite lock is taken for a request that will be refused.

| Channel | When disabled |
|---|---|
| CLI | exit code 3, empty stdout |
| MCP stdio | one stderr line, return 1, stdout stays empty (no protocol corruption) |

### 6.2 stdout protection

MCP's stdout is the protocol channel. `_isolate_stdout()` keeps the real handle in a local variable
and points `sys.stdout` at stderr, so any stray `print` cannot break a frame.

### 6.3 CLI output encoding

PyInstaller's runtime does **not** inherit `PYTHONUTF8` / `PYTHONIOENCODING`. The old
"downgrade only when unencodable" fallback never fired (GBK encodes common Chinese), so packaged CLI
emitted GBK. Now `main()` starts with `_configure_output_encoding()`:

- non-TTY stdout/stderr → `reconfigure(encoding="utf-8", errors="replace")`
- a real terminal keeps the console code page (otherwise `cmd.exe` garbles Chinese)

### 6.4 MCP HTTP

| Concern | Design |
|---|---|
| Binding | `127.0.0.1` only |
| Sessions | 30 min TTL, cap 64 evicting oldest; `touch()` expires before lookup (else dead sessions revive) |
| Auth | optional Bearer token compared with `hmac.compare_digest` |
| Cross-site | Origin allowlist; POST and DELETE guarded **identically** |
| Timeouts | handler `timeout=60` (no slow half-open connection pinning a thread) |
| Concurrency | Service SQLite is not concurrency-safe → `_handle_lock` serializes tool calls |
| Startup | `start()` runs the accept loop in a daemon thread; `server_bind` is overridden to avoid `getfqdn()` (hangs for tens of seconds behind a hijacked DNS) |

### 6.5 Eight clients

`agent_setup.py` describes each client with a `ClientSpec` (format, keys, path, writability):

| Client | Format | Writable | Special handling |
|---|---|---|---|
| WorkBuddy | JSON `mcpServers` | ✅ | other entries preserved |
| DSH | JSON connection table | ❌ | UI-managed; refuses to write, provides a paste-ready snippet |
| Codex | TOML | ✅ | documented keys only plus `startup_timeout_sec` |
| Claude / Cursor / VS Code / Tare / generic | JSON | ✅/paste | VS Code uses a `servers` key |

A `.bak` backup is written first. JSONC input (with `//` comments) produces an actionable error
instead of a raw traceback.

## 7. Desktop UI layer

- **Three tabs**: Tasks / History / Settings, refreshed by a 0.6 s Store poll.
- **Single instance**: NiceGUI's spawned child re-runs `main()`; `_is_spawn_child()` lets it
  through early, otherwise every launch opened an extra browser page.
- **Native window by default**: `_resolve_ui_mode()` prefers native when WebView2 exists.
  `ui.run(window_size=...)` forces native, so only native mode passes it.
- **Fixed WebView2 data directory**: under `private_mode` pywebview points UserDataFolder at a temp
  directory; when that temporary object is garbage collected the directory is deleted, the renderer
  crashes, and the app exits silently ("it quits by itself after a few minutes").
- **Raise, don't reopen**: `_activate_existing_window()` polls `FindWindowW` for up to 30 s.
- **Tray**: `on_open` raises the window as well.

## 8. Packaging and release

```powershell
.venv\Scripts\python.exe scripts\release.py              # everything
.venv\Scripts\python.exe scripts\release.py --bin-only    # binaries only (note: still rebuilds single + GPU)
```

### 8.1 Four traps that always bite

1. **Sandbox hook** — the script sets `CODEBUDDY_SAFE_DELETE_SANDBOX=0`. Running PyInstaller manually must
   do the same, or the final `os.remove` is blocked and the build hangs with exit code 1.
2. **Version in three places** — `pyproject.toml` / `build/installer.iss` / `app/__init__.py APP_VERSION`.
   `release.py` bumps them; manual edits must touch all three.
   ⚠️ The script bumps **at the very start**, so an interrupted run bumps again on retry — roll the three
   values (and the README/manual version headers) back before rerunning.
3. **snapshot_docs rolling snapshots** — the release writes `docs/*-{ver}.md` and deletes old
   snapshots. The permanent documents (`ai-integration.md`, `adversarial-review.md`) are not in
   that list and must never be deleted as "old snapshots".
4. **dist/build cleanup** — move old artefacts into a trash directory (`dist/_trash_YYYYMMDD/`) instead of
   `rm`, to stay clear of the safe-delete hook.

### 8.2 Artefacts and upload

| Artefact | Notes |
|---|---|
| `setup.exe` | Inno Setup installer |
| `portable.zip` | onedir build |
| `single.exe` | onefile; the PyInstaller hook checks `sys.stderr is not None` before writing |
| `gpu.zip` | NVIDIA runtime libraries |
| `*.whl` | hatchling wheel |
| `bili-note-skills-*.zip` | companion skills (built with `git archive` from three directories under `~/.workbuddy/skills/`, filtering `.bak`/`.tsv`) |

Upload with `python scripts/github_release_upload.py <tag> <assets...> --notes <md>` (3 retries).

### 8.3 Frozen entry switch

`main.py`: **the exe defaults to the GUI and needs `--cli` for CLI mode**; source
`python -m app.cli` is a CLI by construction. When editing `render_cli` /
`agent_setup` snippets, the frozen branch **must keep `--cli`** — a "fix" once removed it and
only a real run of the packaged `--doctor` (which opened the GUI) caught it. Regression tests now assert
both directions: frozen must contain it, source must not.

## 9. Quality baseline and tests

- **544 tests across 29 files**, green in about two minutes.
- Tests are **fully offline**: cookie databases are built as temporary SQLite files, network probing is
  mocked, and nothing depends on which browsers a machine happens to have.
- The assembly layer (CLI entry, HTTP server, frozen package) needs **subprocess-level e2e**: once 517 tests
  were green while `--mcp-http` crashed on the first real run with `NameError` (tests covered the
  class but not the wrapper entry point).

| Test group | Coverage |
|---|---|
| `test_pipeline / test_downloader` | state machine, resume, cookie bridging and caching |
| `test_transcriber / qwen / sensevoice` | batching and fallback per engine |
| `test_netcheck` | three-outcome semantics, bridge layout, Netscape round trip, prefix detection |
| `test_mcp_server / test_mcp_http` | 11 tools, boundary values, auth, cross-site, real socket e2e |
| `test_cli / test_release_docs` | option conflicts, doc snippets vs frozen build |
| `test_ui / test_ui_mode` | rendering and window-mode dispatch |

## 10. Known limits and trade-offs

1. **Cookies are unreadable while the browser runs** — Chromium holds an exclusive lock. Messages tell the
   user to close the browser instead of pretending to retry.
2. **v20 can only be decrypted in place** — the ABE key is path-bound, so copying a profile elsewhere does
   not help for v20.
3. **Transcription is serial** — that is VRAM, not laziness; parallelism only causes OOM.
4. **No server push over MCP HTTP** — GET /mcp always returns 405 (spec-permitted); long jobs poll.
5. **Session cap of 64** — oldest evicted; business state lives in the DB, so eviction is harmless.
6. **No new runtime dependency** — the cookie bridge hand-writes WebSocket with the standard library, so
   the package size does not grow.
