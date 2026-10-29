# bili-transcriber — User Guide (English)

> For end users. Covers installation, the three tabs, every setting, output layout, and troubleshooting.
> This guide matches **v0.1.44** — the version comes from `pyproject.toml` as the single source of truth and is bumped automatically on release.
> The versioned release copy is [user-guide-en-0.1.44.md](user-guide-en-0.1.44.md).

---

## Table of Contents

1. [What This Is](#1-what-this-is)
2. [Installation](#2-installation)
3. [First Launch](#3-first-launch)
4. [The Tasks Tab](#4-the-tasks-tab)
5. [Transcribing Local Files](#5-transcribing-local-files)
6. [The History Tab](#6-the-history-tab)
7. [The Settings Tab](#7-the-settings-tab)
8. [Output Files](#8-output-files)
9. [Choosing an Engine](#9-choosing-an-engine)
10. [CLI and AI Integration](#10-cli-and-ai-integration)
11. [FAQ](#11-faq)
12. [Troubleshooting](#12-troubleshooting)
13. [Privacy and Data Locations](#13-privacy-and-data-locations)

---

## 1. What This Is

bili-transcriber is a **fully local** video/audio transcription desktop tool.

Give it a link and it runs four steps on its own — download audio → convert → local AI transcription → export subtitles — then hands you three files: `.srt` (timestamped subtitles), `.txt` (plain text), and `.md` (Markdown with timestamps).

**How it differs from an online transcription service:**

| | bili-transcriber | Online service |
|---|---|---|
| Audio uploaded | Never | Yes, to their servers |
| Account required | No | Usually |
| Usage limits | None | Usually |
| Works offline | Yes, once models are cached | No |
| Cost | Free | Per-minute billing |

The trade-off is a **one-time model download** (Whisper `large-v3-turbo` is about 1.6 GB) and transcription speed that depends on your GPU.

---

## 2. Installation

### 2.1 Requirements

| Item | Requirement |
|---|---|
| OS | Windows 10 / 11 (the only platform tested) |
| GPU (optional) | NVIDIA + CUDA 12.x; works without one, on CPU |
| Disk | ~700 MB app + 1.6 GB models + your outputs |
| RAM | 8 GB or more recommended |
| FFmpeg | **Not required** — a static build ships with the app |

### 2.2 Pick One of Four Builds

**Installer (recommended for most users)**

Run `bili-transcriber-setup-{version}.exe` and click through the wizard. Desktop and Start Menu shortcuts are created.

**Portable**

Unzip `bili-transcriber-portable-{version}.zip` anywhere (e.g. `D:\Tools\BiliNote\`) and run `bili-transcriber.exe` inside.

Pros: no registry writes, no dependencies — copy the whole folder to a USB stick and it runs on another machine.

Caveat: **don't move the exe out of its folder**; it depends on sibling files.

**Single-file**

Run `bili-transcriber-single-{version}.exe` directly. One file, handy for a one-off.

Caveat: it unpacks itself to a temp directory on every launch (slow first start), and it is CPU-only. For GPU speed use one of the first two.

**From source (development)**

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m app.main
```

Requires Python ≥ 3.11.

### 2.3 GPU Runtime Pack (optional but strongly recommended)

The installer and portable builds **default to CPU inference**. With an NVIDIA GPU, this pack makes a large difference:

1. Download `bili-transcriber-gpu-{version}.zip` (about 1.3 GB).
2. Extract into `%LOCALAPPDATA%\Bili Note\gpu\` — paste that path into the Explorer address bar to open it.
3. **Check the nesting level**: `gpu\nvidia\cublas\bin\cublas64_12.dll` must exist. The zip preserves the `nvidia/<package>/bin/` layout — **extract it as-is, don't flatten it**. The app looks for four DLL directories (cublas / cudnn / cuda_runtime / cuda_nvrtc) in that structure.
4. Restart the app.

The app detects and registers these DLLs automatically. **No environment variables, no separate CUDA Toolkit install.**

If you extract it wrong, nothing errors — the app silently falls back to CPU, just slower. So after installing, glance at the hardware hint near "Device" in Settings.

---

## 3. First Launch

Double-clicking the app opens a **standalone application window** (not a browser tab):

1. Starts a local server on `http://127.0.0.1:8765` by default.
2. Renders the "B站音频本地转写" window with WebView2, 980×720.
3. Closing the window quits the app; use the tray if you want it to keep running.

> A cold start has to load torch/transformers, so the first window takes roughly 10–20 seconds
> (faster on an SSD). Don't double-click again during that window — the second process waits for
> the window to appear, brings it to the front and exits.

**Right-click the tray icon** for "Open main window" and "Exit". In native mode, "Open main window"
**raises the existing window** instead of opening a browser.

### Window or browser?

| Situation | Behaviour |
|---|---|
| WebView2 present (default on Win10/11) | **Standalone window** (recommended) |
| No WebView2 | Falls back to the browser, same features |
| Force the browser | Set `BILI_FORCE_BROWSER=1` |
| Force the window without detection | Set `BILI_FORCE_NATIVE=1` and install the [WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/) |
| Using the **single-file** `bili-transcriber-single-*.exe` | Still the browser: onefile unpacks to a temp dir per run and deletes it on exit, which makes a native window unstable there — the build pins browser mode |

```powershell
# Fall back to the browser if the window misbehaves (e.g. a GPU driver crashing WebView2)
$env:BILI_FORCE_BROWSER = "1"
```

The window's WebView2 profile lives in `data\webview2\`. Deleting it resets the UI cache; task
history is untouched.

### Port conflicts

If something else holds 8765, startup fails. Change the port:

```powershell
$env:BILI_PORT = "9000"
```

Then relaunch. From source this is enough; with the installer, open PowerShell, set the variable, then run the full path to the exe.

### "An instance is already running"

The app enforces a **single instance**. Double-clicking the shortcut again means the second process:

1. Polls for the existing window (up to 30 seconds, covering cold start);
2. Restores it and **brings it to the front**, then exits.

So a second double-click just brings the window back — no stray browser tab, no silent flash-and-exit.
If no window can be found (e.g. the first instance runs in browser mode), a message box tells you the
app is already running.

---

## 4. The Tasks Tab

### 4.1 Submitting a Link

Paste a link into the input box. Supported:

| Platform | Link forms | No login needed |
|---|---|---|
| Bilibili | `bilibili.com/video/BVxxx`, `b23.tv/xxx` short links, `?p=2` parts, collections | ✅ |
| Douyin | `v.douyin.com/xxx` short links, full video pages | Cookie needed for "Featured" content |
| Xiaohongshu | `xiaohongshu.com/explore/xxx` | Cookie needed for some |
| Kuaishou | `v.kuaishou.com/xxx` share links | ✅ (custom parser) |
| YouTube | `youtube.com/watch?v=xxx`, `youtu.be/xxx` | Proxy + possibly a cookie |

> WeChat Channels links are **rejected on purpose** with a hint to use local upload — Channels exposes no login-free direct stream. Download the video and transcribe the file instead.

After you click "Add", the app **probes** the link first — this is how it discovers whether it is a single part, a multi-part video, or a collection. The result expands into **one task per entry**: a 30-part video becomes 30 tasks, each with its own row.

**Which folder do they land in?** Parts of the same collection / multi-part video **share one folder** (`{BVid}_{collection title}/`) and are distinguished by `_P01`, `_P02` filename suffixes — so a whole course season stays together. A single-part video gets `{BVid}_{video title}/`.

> Probe failures are almost always network or cookie related, and the reason appears right on the task row.

### 4.2 Status Meanings

| Status | Meaning | Typical duration |
|---|---|---|
| `queued` | Enqueued, waiting for a free worker | Instant |
| `downloading` | Fetching audio (or the video stream when notes are on) | Network- and length-dependent |
| `converting` | FFmpeg → 16 kHz mono WAV | Seconds to tens of seconds |
| `transcribing` | Model inference, the slow step | See below |
| `extracting` | Keyframe extraction for notes (only with notes on) | Scales with video length |
| `saving` | Writing SRT / TXT / MD | Instant |
| `done` | Succeeded | — |
| `failed` | Failed; the row shows why | — |
| `cancelled` | You cancelled it | — |
| `paused` | You paused it | — |

**Transcription time** (10-minute Chinese video):

| Setup | Approximate |
|---|---|
| RTX 3060 + `large-v3-turbo` + `float16` | 30–60 s |
| RTX 3060 + `large-v3` | 1.5–2 min |
| CPU (i7 class) + `large-v3-turbo` + `int8` | 8–15 min |
| CPU + `sensevoice` | 2–4 min |

### 4.3 Row Actions

Three buttons per row:

- **Open containing folder** — jumps to the output directory. If the file is gone it degrades gracefully: file → parent folder → output root, never a dead-end error.
- **Re-run** — runs the task again.
- **Cancel** — stops a running task.

Re-run has an important property: **completed steps are never redone**. Downloaded audio is reused, converted WAV is reused. Therefore:

- A failure during transcription re-runs only transcription — no re-download.
- Switching models and re-running also reuses the audio and goes straight to inference.

Only if you delete the intermediate files does it download again.

### 4.4 Live Log

Below the task list is a scrolling log. It shows:

- What is happening now (which part is downloading, which chunk is transcribing)
- The actual exception, not just the word "failed"
- Model loading and GPU fallback notices

When something breaks, read this first. It beats any FAQ.

### 4.5 Concurrency

By default **2 workers** handle download and conversion (configurable 1–6 in Settings), while **transcription is strictly serial**.

Why: several tasks pushing into one GPU at once exhaust VRAM and trigger `CUDA out of memory`, failing everything. Serial is both faster and safer in aggregate.

In practice: while task A transcribes, task B downloads. When A finishes, B's audio is already ready — near-seamless handoff.

---

## 5. Transcribing Local Files

Beyond links, you can transcribe audio/video files already on your machine. Two entry points:

### 5.1 Pick a File (recommended)

Click "Pick file (recommended, transcribe immediately)…" to open the **native file dialog**; transcription starts as soon as you choose.

Properties:

- **Zero copy** — the app references the original path directly. No copy, no upload.
- Instant submit — probing starts immediately instead of waiting for a transfer.
- Best for large files. Multi-GB screen recordings are fine.

### 5.2 Browser Upload

Click "Browser upload" and the file is sent to the local server as base64.

When to use it: **when you are reaching this machine's web UI from a phone or another computer**. The native dialog only browses the local disk, so upload is the only option there.

Notes:

- Uploaded copies land in `uploads/` and are **never auto-cleaned** — prune them yourself.
- Large files are noticeably slow because base64 inflates the payload by roughly 33%. A 1 GB file taking a long time is expected.

### 5.3 Supported Formats

```
Video: mp4  mkv  mov  avi  webm  flv  wmv
Audio: mp3  m4a  wav  ogg  opus  flac  aac
```

Convert anything else with FFmpeg first.

### 5.4 Output Folder for Local Files

The folder is `{filename}_{hash}/`, where the hash covers absolute path + size + mtime, so two files with the same name but different content never collide. The filename stem keeps the original name, so local jobs stay visually distinct from platform videos.

---

## 6. The History Tab

An **index of completed tasks**, stored in local SQLite (`data/history.db`).

What you can do:

- Filter by status (failures only, completions only).
- Search by title keyword.
- Open the folder or re-run per row.

**Clearing history deletes only the index, not the output files.** Outputs live in your output directory — delete them yourself.

Records are keyed by `media_id` (Bilibili: `BVid_pN`; other platforms: `yt_` / `dy_` / `xhs_` / `ks_` prefix plus the video id). Transcribing the same video twice updates the record instead of duplicating it.

---

## 7. The Settings Tab

Everything lives in `data/settings.json`. There is a one-click "Restore defaults".

**Values are validated on save.** Invalid input is rejected with an explanation instead of being written to disk — so a bad config can never brick the next launch.

### 7.1 Engine and Model

| Setting | Options | Advice |
|---|---|---|
| Engine | `whisper` / `qwen3-asr` / `sensevoice` | whisper by default |
| Model | Depends on engine | `large-v3-turbo` for whisper |
| Device | `auto` / `cuda` / `cpu` | `auto` if you have an NVIDIA card |
| Compute type | `auto` / `float16` / `int8_float16` / `int8` | `int8` when VRAM is tight |
| Language | empty / `zh` / `en` | set `zh` for Chinese — faster and more accurate |

Switching engines **swaps the model dropdown** to that engine's list, so combinations like "engine whisper, model Qwen" cannot occur.

### 7.2 Output and Cache

| Setting | Description |
|---|---|
| Output directory | Where outputs go. Relative to the app, or absolute (e.g. `D:\transcripts`) |
| Keep audio | Off deletes the source audio after transcription to save space |
| Notes | See [7.5](#75-illustrated-notes) |
| Model directory | Defaults to `%LOCALAPPDATA%\Bili Note\models`; point elsewhere or drop manually downloaded model packs here |
| Workers | Download/convert threads, 1–6, default 2 |

### 7.3 Network and Login (one-click detection)

This section has three inputs and a three-step flow. **The recommended path is one sentence:
click "Detect network & cookies" → let it fill things in → click "② Test connection" → click "③ Save settings".**

| Setting | Description |
|---|---|
| Browser cookies | Chrome / Edge / Firefox / Brave / Chromium / Opera / Vivaldi. The app reads cookies from that browser's logged-in profile. **Log in to the target site in that browser first** |
| cookies.txt | Path to a Netscape-format cookie file. **Takes precedence over browser cookies** and is unaffected by whether the browser is open |
| Proxy | e.g. `http://127.0.0.1:7890` or `socks5://127.0.0.1:1080`. Needed for YouTube. The "Probe" button finds it automatically |

#### What detection does

One click (5–8 seconds) runs three things in parallel:

1. **Browser scan** — enumerates installed Chrome / Edge / Brave / Vivaldi / Chromium / Opera / Firefox and
   reports which platforms (Bilibili, Douyin, Xiaohongshu, Kuaishou, YouTube) you are logged in to.
   It reads only cookie **domain, name and expiry** — never the value.
2. **cookies.txt search** — app directory, `data/`, Downloads, Desktop, Documents.
3. **Network probe** — per-platform latency plus system proxy / env-var proxy / common local ports
   (7890, 7897, 10809, 1080…), each verified against YouTube.

It then picks the source with the widest coverage and **fills the three inputs for you**, explaining why:

```
Platforms   ✅ Bilibili  ✅ Douyin  ⚪ Xiaohongshu  ⚪ Kuaishou  ⚪ YouTube
Network     ✅ Bilibili 383ms  ✅ Douyin 277ms  …  ❌ YouTube direct blocked
Proxy       ✅ http://127.0.0.1:7897 works
· Chrome/Edge are running, so their cookie DB is locked: close them and re-detect, or use cookies.txt
```

> **Auto-fill has boundaries**: it only overwrites when it finds a strictly better source and never
> clears a working configuration. If nothing is detected it keeps your values and says why.

#### Detection vs. Test connection

| | Detect | ② Test connection |
|---|---|---|
| Network | Probes reachability | Probes again, **through the proxy you configured** |
| Cookies | Metadata only (name/expiry) | Real cookie values |
| Answers | "What should I fill in?" | "Does this configuration work now?" |
| Output | Three auto-filled values | One row per platform: latency + login state + detail |

For Bilibili the test calls the official `nav` endpoint and **shows your account name**; Douyin /
Xiaohongshu / Kuaishou require request signing, so they are reported as "logged in" (cookie present)
rather than "verified".

#### When cookies or a proxy are actually needed

| Platform | Cookie? | Proxy? | Notes |
|---|---|---|---|
| Bilibili | Mostly no | No | 1080P+ / premium / some series need login |
| Douyin | **Recommended** | No | Anonymous requests are often rate-limited |
| Xiaohongshu | **Recommended** | No | Full video URLs require login |
| Kuaishou | Rarely | No | Share-page parsing, no login |
| YouTube | Sometimes | **Required** | Age/member-gated videos; blocked in mainland China |

#### Choosing between the three sources

1. **Browser cookies (easiest)** — reads the login state of your browser. Downside: while the browser
   **is running**, its cookie DB is exclusively locked, so detection fails and cookie-dependent downloads fail too.
2. **cookies.txt (most stable)** — takes precedence and is immune to the lock. Expand
   "Three cookie sources" in Settings and use "Export cookies.txt from browser" (close the browser first);
   it writes `data/cookies.txt` and fills the path for you.
3. **Neither** — public content only; enough for ordinary Bilibili videos.

> The exported cookies.txt contains login credentials and lives only in the local `data/` directory.
> Do not share it or commit it.

### 7.4 Model Management

The "Check models" button scans the model directory and reports:

- Which models are ready (files complete)
- Which are missing files (interrupted download)
- Roughly how large each one is

If something is reported missing, the simplest fix is to **delete that model folder** and let the app re-download.

### 7.5 Illustrated Notes

With this on, the app tries to detect **slide changes** in the video and produce notes with screenshots.

Output is `{title}.notes.md` plus a `{title}_frames/` folder. Each page looks like:

```
## [00:03:21] Part Three: Architecture

![slide](title_frames/slide_003.jpg)

(subtitles spoken between 00:03:21 and 00:04:05)
```

Key points:

- **Bilibili tasks switch to downloading the video stream** (not audio-only), so files get much larger. That is required to grab frames.
- Local uploads and Kuaishou tasks use the original file directly when it is already a video.
- **Audio-only files skip notes automatically** without error.
- **A notes failure never affects transcription.** Subtitles are still written; you just don't get notes.

Worth enabling for: tech talks, open courses, lectures — anything with text on screen. Pointless for pure talk, podcasts, interviews.

### 7.6 CLI and MCP Channel Switches

Further down the Settings tab is an **AI Calls and CLI** section with two switches, **both on by default**:

| Switch | When on | When off |
|---|---|---|
| Allow CLI calls | scripts / terminals can run `--cli` | refuses with an explanatory error, exit code **3** |
| Allow MCP calls | AI apps can run `--mcp` | server refuses to start, exit code **1**, reason on stderr |

When to turn them off:

- you only use the desktop UI and don't want any external program touching transcription;
- you're debugging and want to rule out an AI client quietly running jobs;
- you're lending the machine to someone.

Behaviour you must know:

- **New processes only.** The switch governs CLI/MCP processes started *after* the change; an already-connected AI client must reconnect. The desktop app itself is always available.
- **Desktop-only.** `--set enable_cli=false` and the MCP `update_settings` tool both reject these two keys ("modify from the desktop settings page"). That is deliberate: a prompt-injected AI must not be able to open its own door.
- **Save it.** Like every other setting, tick then press **Save settings**.
- **Locked out?** Edit `data/settings.json` and set `enable_cli` / `enable_mcp` back to `true` — it is plain JSON.

The same section has an **MCP client config** panel: expand it, press *Copy config*, and paste the `mcpServers` snippet into your AI client. The command path is filled in automatically (`xxx.exe --mcp` for packaged builds, `python -m app.mcp_server` from source).

### 7.7 Restore Defaults

Resets every setting to factory values, including the output directory back to `output/` and both channel switches back to on.

It does **not** delete your models, history, or output files.

---

## 8. Output Files

### 8.1 Layout

One folder per video (per task):

```
{output dir}/{platform id}_{title}/
├── {title}.srt             # timestamped subtitles
├── {title}.txt             # plain transcript
├── {title}.md              # Markdown with timestamps
├── {title}.m4a             # source audio (can be disabled)
├── {title}.notes.md        # illustrated notes (only when enabled)
└── {title}_frames/         # notes keyframes
    ├── slide_001.jpg
    └── ...
```

The platform id is `BV1xx411c7mD` for Bilibili (a collection uses the collection title) and `yt_xxx` / `dy_xxx` / `xhs_xxx` / `ks_xxx` elsewhere. The prefix prevents id collisions across platforms.

**Multi-part filename suffixes**: when a video has several parts, the filename stem gains `_P01`, `_P02`, … (zero-padded). This is required — part titles inside a Bilibili collection often repeat, and without the suffix the outputs would overwrite each other.

Titles are sanitized for the filesystem (illegal characters `\ / : * ? " < > |` removed, overlong names truncated).

### 8.2 Which Subtitle Format

**`.srt`** — the standard format almost every player and NLE understands.

```
1
00:00:03,120 --> 00:00:06,480
Hi everyone, today we're talking about this architecture

2
00:00:06,480 --> 00:00:09,200
Let me start with the conclusion: it has three layers
```

Use for: subtitle tracks in Premiere / DaVinci / CapCut, platform uploads, polishing in Aegisub.

**`.txt`** — plain text, one sentence per line, no timestamps.

Use for: reading straight through, feeding an LLM for summarization, importing into note apps.

**`.md`** — Markdown with `[hh:mm:ss]` prefixes.

```markdown
## Hi everyone, today we're talking about this architecture

[00:00:03] Hi everyone, today we're talking about this architecture
[00:00:06] Let me start with the conclusion: it has three layers
```

Use for: reading in Obsidian / Typora, jumping back to a point in the video.

### 8.3 Atomic Writes

Every output is written to a temp file and then **atomically replaced**, with retries on permission conflicts.

Practical meaning: if antivirus happens to be scanning the folder, or you have the file open elsewhere, the write retries instead of failing — and you **never get a truncated file**.

---

## 9. Choosing an Engine

| | Whisper | Qwen3-ASR | SenseVoice |
|---|---|---|---|
| Models | `large-v3-turbo` / `large-v3` / `medium` / `small` / `base` / `tiny` | `Qwen3-ASR-0.6B` / `1.7B` | `SenseVoiceSmall` |
| Chinese accuracy | Very high | Very high (better on casual speech) | Moderate |
| English accuracy | Very high | Moderate | Moderate |
| Punctuation | Good | Good | Fair |
| GPU acceleration | ✅ CUDA (ctranslate2) | ✅ faster with GPU | ❌ mostly CPU |
| CPU speed | Slow | Slow (1.7B much slower) | Fast |
| Model size | 1.6 GB (turbo) | 0.6B ≈ 1.2 GB / 1.7B ≈ 3.4 GB | ~250 MB |

**How to choose:**

- **Just use Whisper `large-v3-turbo`.** The best all-round default, and fast enough with a GPU.
- **Accuracy first, time no object** → Whisper `large-v3` (slightly better than turbo, noticeably slower).
- **Casual Chinese speech, interviews, heavy accents** → Qwen3-ASR `1.7B`. It tolerates non-standard Mandarin better.
- **Old machine / CPU only / quick rough pass** → SenseVoice. A few hundred MB, runs on CPU; you lose some punctuation and proper nouns.
- **Tight VRAM (4 GB)** → Whisper `medium` + `int8`, or Qwen3-ASR `0.6B`.

**About VAD**: on by default, it detects speech regions and skips silence. The side effect is that music or singing may be judged "no speech" and produce output. The app **automatically retries once with VAD off** in that case. If it is still empty after the retry, the video genuinely has no speech.

---

## 10. CLI and AI Integration

Beyond the UI there are two headless paths.

### 10.1 CLI

After installing from source, `Scripts\` contains `bili-transcriber-cli`. Packaged builds have no separate CLI binary — use the dispatch prefix `bili-transcriber.exe --cli <args>` (intercepted before the GUI starts, so no window opens):

```powershell
# Submit and wait, JSON out
bili-transcriber-cli "https://www.bilibili.com/video/BV1xx411c7mD" --json

# Submit without waiting
bili-transcriber-cli "https://..." --no-wait

# Query
bili-transcriber-cli --list --json --limit 10
bili-transcriber-cli --status BV1xx411c7mD_p1 --json

# Read output text
bili-transcriber-cli --result BV1xx411c7mD_p1 --format md --max-chars 20000

# Change settings
bili-transcriber-cli --set engine=sensevoice --set device=cpu

# Preload the model (blocks until ready)
bili-transcriber-cli --prewarm --json

# Full-text search across transcripts
bili-transcriber-cli --grep "attention" --limit 5

# AI onboarding trio
bili-transcriber-cli --doctor                          # self-check: which clients, what's missing
bili-transcriber-cli --agent-config workbuddy --write  # generate & write WorkBuddy config
bili-transcriber-cli --mcp-http                        # start the MCP HTTP server (for DSH etc.)
```

See the "Command-Line Reference" section of the README for the full table.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | job failed / execution error (including invalid values) |
| 2 | bad arguments (missing value, conflicting flags) |
| 3 | channel disabled in Settings |

`--json` output is always valid JSON (NaN/Infinity become `null`), and an early pipe close (`… | head -1`) no longer prints a traceback.

**Encoding**: when output is captured by a pipe or redirect it is always UTF-8, so AI clients that parse UTF-8 never see mojibake; on a real terminal the console code page is kept so `cmd.exe` does not garble the screen. Both the human-readable and the machine-readable copy come out correct.

Tasks submitted with `--no-wait` are not lost: **the next desktop launch resumes every leftover in-progress task**, including ones killed mid-run.

### 10.2 MCP (for AI clients)

**Easiest onboarding** (v0.1.43+):

```text
bili-transcriber-cli --doctor                          # self-check first
bili-transcriber-cli --agent-config workbuddy --write  # writes ~/.workbuddy/mcp.json for you
```

WorkBuddy / DSH / Codex / Claude / Cursor / VS Code / Tare and a generic preset are supported;
your existing entries are preserved and a `.bak` backup is written first. DSH's connection
table is managed by its own UI, so a paste-ready entry is generated instead.

Manual config (equivalent) — add to your client's MCP config:

```jsonc
{"mcpServers": {"bili-transcriber": {
  "command": "C:\\Program Files\\Bili Note\\bili-transcriber.exe",
  "args": ["--mcp"]}}}
```

Then just say "transcribe this link" and the client drives the tools itself.

Eleven tools are exposed: `transcribe`, `wait_jobs`, `job_status`, `list_jobs`, `get_transcript`, `search_transcript` (full-text search over transcripts), `check_network` (read-only network & cookie audit), `cancel_job`, `prewarm_model`, `get_settings`, `update_settings`.

**HTTP transport** (for clients that cannot spawn child processes, e.g. DSH's streamable-http):

```text
bili-transcriber-cli --mcp-http        # endpoint http://127.0.0.1:8765/mcp, auto port fallback
```

Loopback-only, Origin-checked, optional Bearer token; identical tool set as stdio.

When the MCP switch is off, neither stdio nor HTTP starts; the reason goes to stderr — the client reports "server failed to start", and that stderr line tells you why.

Two interop details: `resources/list` and `prompts/list` return empty lists instead of `-32601` (some clients otherwise mark the whole server unusable), and every non-protocol write — including stray `print` calls from third-party libraries — is redirected to stderr so JSON-RPC framing stays intact.

Details in [ai-integration.md](ai-integration.md).

---

## 11. FAQ

**Q1: Download or parsing fails?**

Platform APIs change constantly. First step is always upgrading the downloader:

```powershell
.venv\Scripts\pip install -U yt-dlp
```

Packaged builds cannot upgrade yt-dlp independently — wait for a new release.

**Q2: Douyin / YouTube asks for login or denies access?**

Set "Browser cookies" in Settings (after logging in to the site in that browser), or export a `cookies.txt` and point at it. YouTube also needs a proxy.

**Q3: Transcription is slow?**

Three causes, in order:

1. No GPU runtime pack → it is actually running on CPU. Check for DLLs in `%LOCALAPPDATA%\Bili Note\gpu\`.
2. First run is downloading the model (1.6 GB). The log shows progress; it never downloads again.
3. Model too large. Qwen 1.7B takes about 3× the CPU time of 0.6B.

**Q4: `CUDA out of memory`?**

Set compute type to `int8` (halves VRAM), or step down to `medium` / `Qwen3-ASR-0.6B`.

**Q5: A singing or instrumental video yields nothing?**

VAD detects speech only. The app retries once with VAD disabled; if it is still empty, there really is no speech.

**Q6: "Check models" reports missing files?**

The model pack is incomplete (an interrupted download). Delete that model folder and let the app re-download.

**Q7: Port 8765 is taken?**

Set `BILI_PORT=9000`.

**Q8: Do tasks keep running after I close the window?**

Yes. The app stays in the system tray and keeps transcribing. Right-click the tray icon → "Open main window".

**Q9: Can I run multiple tasks at once? Do they interfere?**

Yes. Downloads and conversions run concurrently (2 by default); transcription is serialized. They don't interfere — transcription is lock-protected, so only one task touches the GPU at a time.

**Q10: Does re-running re-download?**

No. Audio and WAV caches are reused unless you deleted the intermediate files.

**Q11: What does "running" mean in the task list?**

It means the task is **already queued or in flight**, so the app did not enqueue it again. Repeat submissions of the same video via CLI or MCP return `running` instead of inserting a duplicate, which prevents two workers from fighting over the same intermediate files.

**Q12: Can the output directory be somewhere outside the app, like `D:\transcripts`?**

Yes — enter an absolute path. On save the app runs a **write probe** (it actually creates the directory and writes a temp file), so a bad path fails immediately instead of after a long transcription.

---

## 12. Troubleshooting

### 12.1 Check These Three Places, In Order

1. **The log under the Tasks tab** — the most recent error is there.
2. **`%LOCALAPPDATA%\Bili Note\logs\app.log`** — full logs with stack traces (2 MB rotation, 3 backups).
3. **The error text on the task row**.

### 12.2 Symptom Table

| Symptom | Likely cause | Fix |
|---|---|---|
| Double-click does nothing | An instance is already running | Check the tray icon |
| Browser opens blank | Port taken / server didn't start | Change the port, read the console |
| Download stuck at 0% | Network / needs cookies / needs proxy | Configure cookies or proxy |
| `transcribing` never advances | Model loading (first run) or VRAM exhausted | Wait, or lower precision and re-run |
| Task stays `queued` | All workers are busy | Wait; or relaunch to trigger resume |
| Model download fails | Mirror unavailable | Set `HF_ENDPOINT=https://huggingface.co` |
| SSL certificate errors | Broken system cert chain | Self-healing runs at startup; usually nothing to do |
| GPU pack installed but unused | Wrong extraction depth (flattened or one level too deep) | Verify `gpu\nvidia\cublas\bin\cublas64_12.dll` exists |
| Outputs can't be written | Permissions / antivirus | Change the output dir; atomic writes retry, but long antivirus locks still fail |
| CLI exits at once, says the channel is disabled in desktop Settings | CLI switch is off | Tick it in Settings and save; or set `enable_cli` to `true` |
| AI client shows the MCP server as "failed to connect" | MCP switch is off, or wrong command | Read the stderr line; the config must be `xxx.exe --mcp` |
| `update_settings` reports an unsupported setting | You tried `enable_cli` / `enable_mcp` | Desktop-only by design |
| `database is locked` when desktop and AI run together | Old build without lock wait | Fixed in 0.1.40 (5 s `busy_timeout`); upgrade |

### 12.3 Full Reset

If state is scrambled and you want a clean slate:

1. Quit the app (tray → Exit).
2. Delete `data/history.db` (index) and/or `data/settings.json` (settings).
3. Relaunch.

**Never delete anything outside `data/`** — `output/` holds your transcripts.

Models live in `%LOCALAPPDATA%\Bili Note\models`; deleting them costs a 1.6 GB re-download, so normally leave them alone.

---

## 13. Privacy and Data Locations

### 13.1 What Touches the Network

Exactly two things:

1. **First-time model download** — via the `hf-mirror.com` mirror.
2. **Fetching the video itself** — the same thing your browser does when you watch it.

Transcription calls **no external AI API**. Audio and text never leave your machine.

### 13.2 Where Data Lives

| Content | Location |
|---|---|
| Settings | `data/settings.json` |
| History index | `data/history.db` |
| Logs | `%LOCALAPPDATA%\Bili Note\logs\app.log` (rotated, 3 backups) |
| Uploaded temp copies | `uploads/` (never auto-cleaned) |
| Models | `%LOCALAPPDATA%\Bili Note\models` |
| GPU runtime | `%LOCALAPPDATA%\Bili Note\gpu` |
| Outputs | Your configured output directory |

### 13.3 How Cookies Are Used

If you configure browser cookies or a cookies.txt, the app hands them to yt-dlp **only while downloading**, to prove to the platform that the request comes from a logged-in user.

Cookies are **never** written to logs, never packaged, and never sent anywhere except the target platform.

You can skip them entirely — only login-gated content (Douyin Featured, some YouTube videos) will fail.
