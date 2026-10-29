# User Guide — bili-transcriber

> matches **v0.1.45** · Written for people who want to get work done, not for reading source code.
> For internals, see the [Technical Manual](technical-manual-en.md).

---

## Contents

1. [What this is](#1-what-this-is)
2. [Installation](#2-installation)
3. [Your first transcription](#3-your-first-transcription)
4. [Tasks page](#4-tasks-page)
5. [History page](#5-history-page)
6. [Settings, item by item](#6-settings-item-by-item)
7. [Network and login (cookies / proxy)](#7-network-and-login-cookies--proxy)
8. [Reading the output](#8-reading-the-output)
9. [Command line](#9-command-line)
10. [Letting an AI use it](#10-letting-an-ai-use-it)
11. [Troubleshooting](#11-troubleshooting)
12. [Data and privacy](#12-data-and-privacy)

---

## 1. What this is

Turns videos from Bilibili (and Douyin / Xiaohongshu / Kuaishou / YouTube) into
**timestamped text**.

Each job does five things: parse the link → download the audio → convert to 16 kHz WAV →
transcribe locally → write SRT / TXT / Markdown.

Three things matter:

- **Fully local.** Audio never leaves the machine; transcription runs on your own GPU or CPU.
- **Headless is supported.** Both a CLI and an MCP server exist, so AI clients can drive it.
- **Output is plain files**, not rows locked in a database — use them, edit them, feed them to other tools.

## 2. Installation

### 2.1 Pick a form

| You want | Choose |
|---|---|
| Normal install with a Start Menu entry | installer (`setup.exe`) |
| No install, unzip and run, maybe on a USB stick | portable (`portable.zip`) |
| Use it once | single-file (`single.exe`) |
| Install into your own Python environment | wheel |

### 2.2 Requirements

- Windows 10 / 11 (x64)
- 8 GB RAM or more
- GPU optional: an NVIDIA card is much faster, but CPU works

### 2.3 First launch is slow

Expect **10–20 seconds** before the window appears — torch and transformers are being imported.
Do not double-click again meanwhile (the second launch is caught by single-instance protection and
just raises the existing window).

### 2.4 Window vs browser

The default is a standalone window (WebView2). To fall back to the browser UI:

```powershell
$env:BILI_FORCE_BROWSER = "1"
bili-transcriber.exe
```

Related switches: `BILI_FORCE_NATIVE=1` (force the window), `BILI_NO_BROWSER=1` (never auto-open a browser).

## 3. Your first transcription

1. Open the app and go to **Tasks**.
2. Paste a Bilibili link (single part, multi-part, collections and short links all work).
3. Click **Start**.
4. Watch the state machine: `downloading → converting → transcribing → saving → done`.

The first run downloads a model (~1.5 GB through a China mirror); afterwards it works offline.

Output goes to the **output directory** in Settings. The default is `output/` next to the app;
point it at your own drive (e.g. `D:\transcripts`) and you will not have to hunt for files later.

## 4. Tasks page

### 4.1 Three kinds of input

| Input | Example |
|---|---|
| Bilibili link | `https://www.bilibili.com/video/BV1xx411c7mD` |
| Multi-part / collection | `.../video/BV1xx411c7mD?p=3`, collection URL |
| Local file | `D:\recordings\standup.m4a` (drag and drop works) |

### 4.2 How the queue runs

- Download and conversion may run in parallel (**Concurrent jobs**, 1–6).
- **Transcription is always serial** — one job holds the GPU at a time, which is also more stable.
- Each row shows stage, progress, elapsed time, and the real error when it fails.

### 4.3 Pause / resume / cancel

- **Pause** stops the current job, the queue is kept.
- **Resume** continues from the breakpoint (already-downloaded audio is reused).
- **Cancel** drops the job; files already written are kept.
- Paused jobs are restored automatically after a restart.

### 4.4 Run log

Scrolls live at the bottom of the page. This is the first place to look when something fails:
it names the stage and the real error.

## 5. History page

- Every job goes into SQLite (`data/history.db`) keyed by the platform media ID. **Re-running the
  same video overwrites the record** instead of piling up duplicates.
- Filter by title, uploader, platform.
- Open a row to read the output, open its directory, or see the parameters used.
- The search box searches **titles**. To search transcript *text*, use `--grep` or the AI's
  `search_transcript` tool.
## 6. Settings, item by item

Sorted by how much each setting affects the result.

### 6.1 Model choice (accuracy and speed)

| Setting | Values | How to choose |
|---|---|---|
| Engine | whisper / qwen3-asr / sensevoice | whisper by default; qwen3-asr for long Chinese talks; sensevoice on weak machines |
| Whisper model | tiny / base / small / medium / large-v3 / large-v3-turbo | Default large-v3-turbo is the accuracy/speed sweet spot |
| Device | auto / cuda / cpu | auto prefers CUDA and falls back to CPU |
| Compute type | auto / float16 / int8_float16 / int8 | Drop a step when you hit out-of-memory |
| Language | auto / zh / en | Stating the language is faster and more accurate than auto |
| VAD | on / off | On saves a lot of time on long videos; turn it off for music-heavy content |

### 6.2 Output

| Setting | Meaning |
|---|---|
| Output directory | Where files land. Relative paths are resolved to absolute so different processes cannot scatter output |
| Keep audio | Keeps the 16 kHz WAV; otherwise it is deleted right after transcription |
| Notes (slide deck) | Downloads the video stream for keyframes, producing `*.notes.md` plus `*_frames\` |
| Model directory | Defaults to `%LOCALAPPDATA%\Bili Note\models`; models placed there are not re-downloaded |

### 6.3 Network and external access

| Setting | Meaning |
|---|---|
| Browser cookies | Which browser to read the login state from |
| cookies.txt | File path; **takes precedence** over browser cookies |
| Proxy | e.g. `http://127.0.0.1:7890` or `socks5://127.0.0.1:1080` |
| Concurrent jobs | Parallelism for download/conversion, 1–6, default 2 |

### 6.4 External channels

| Setting | When turned off |
|---|---|
| Allow CLI | `bili-transcriber-cli` and `--cli` exit immediately with code 3 |
| Allow MCP | `--mcp` / `--mcp-http` writes one stderr line and returns 1; stdout stays empty |

> These two switches can only be changed **here**. The CLI and the MCP server cannot change them —
> that keeps a prompt-injected AI from opening or closing its own channel.

## 7. Network and login (cookies / proxy)

### 7.1 The three-step flow

**Detect → Test connection → Save.** That is the whole recommended workflow.

### 7.2 What "Detect" does

In roughly 5–8 seconds it does three things in parallel:

1. **Scan browsers** — enumerates Chromium-family and Firefox profiles and reports which platforms you are
   logged into. This step reads only cookie **domain, name, expiry** — never the values.
2. **Find cookies.txt** — searches the app directory, `data/`, Downloads, Desktop and Documents for
   `.txt` files whose names contain "cookie".
3. **Probe the network** — direct latency per platform, plus system proxy, environment proxies and common
   local ports (7890 / 7897 / 10809 / 1080…), verifying which proxy actually reaches YouTube.

It then fills the three fields from the source with the widest platform coverage. When nothing is found it
keeps your current values — it never blanks your configuration.

### 7.3 Detect vs Test connection

| | Detect | Test connection |
|---|---|---|
| Network | Probes reachability | Same, but **through the proxy you configured** |
| Cookies | Metadata only (name / expiry) | Actually reads the cookie **values** |
| Answers | "what should I fill in?" | "does this configuration work right now?" |

Bilibili is verified against its real login endpoint, so it can **show your account name**. Douyin /
Xiaohongshu / Kuaishou require request signing, so they only confirm that a login cookie exists.

### 7.4 Chrome 127+ / Edge cookies are app-bound encrypted

Since Chrome 127, cookies are protected by **App-Bound Encryption (v20)**: the key is wrapped by the
browser's elevation service and can only be unwrapped **by the browser itself, at its original profile
path**. Third-party decryptors (yt-dlp and friends) only understand the old v10 format.

This app lets the browser do the decryption: it copies a minimal profile to a temp directory, starts a
**headless throwaway instance** (killed and deleted right after) and reads the plaintext back through the
debugging protocol.

| What you see | Meaning | What to do |
|---|---|---|
| Green "logged in" + "value encrypted, unreadable" | Login state exists but the value cannot be extracted | Close the browser and test again, or click "Export" |
| Lock icon "unreadable" | Cookie DB is locked; not even names are readable | Close the browser and retest, or switch to cookies.txt |
| "value encrypted" yet downloads work | A temporary cookies.txt was produced (10-minute cache) | Nothing |

> Measured: with Chrome running the cookie DB on Windows is **completely uncopyable**
> (`PermissionError 32`, 60 attempts in 30 s all failing). That is Chrome's security design, not a hang.
> Closing the browser makes it work on the first try.

### 7.5 Choosing between the three sources

1. **Browser cookies (easiest)** — reads your login state directly. Downside: while the browser runs, the
   database is exclusively locked.
2. **cookies.txt (most stable)** — highest precedence, unaffected by the browser being open; one click
   exports it.
3. **Neither** — public content only, which is enough for ordinary Bilibili videos.

> The exported cookies.txt contains credentials and lives only under `data/`. Do not share or commit it.

### 7.6 When cookies / proxies are actually needed

| Platform | Cookies? | Proxy? | Notes |
|---|---|---|---|
| Bilibili | Rarely | No | 1080p+, premium, some series need login |
| Douyin | **Recommended** | No | Not logged in often trips risk control |
| Xiaohongshu | **Recommended** | No | Full URL only after login |
| Kuaishou | Usually not | No | Resolved from the share page |
| YouTube | Sometimes | **Required** | Age/member gates need login; blocked in mainland China |
## 8. Reading the output

A job produces up to four files plus an image directory:

| File | Purpose |
|---|---|
| `<title>.md` | Metadata (platform / uploader / duration / source / engine / timestamp) plus per-segment text — best for reading and citing |
| `<title>.txt` | Plain text; cheapest to feed an AI |
| `<title>.srt` | Standard subtitles for any player |
| `<title>.notes.md` | Only with "Notes" enabled: paragraphs plus slide screenshots |
| `<title>_frames\` | Slide images (`slide_001.jpg` …) |

Text is segmented, one timestamp per segment:

```markdown
[00:12] Starting with two-phase commit…
[00:31] It solves atomic commit under partial failure
```

Collections share one output directory; individual parts are distinguished by `_P01` / `_P02` suffixes.

## 9. Command line

### 9.1 The three you will actually use

```powershell
bili-transcriber-cli "https://www.bilibili.com/video/BV1xx411c7mD"     # transcribe one
bili-transcriber-cli --grep "keyword" --context 400                    # search every transcript
bili-transcriber-cli --result BV1xx411c7mD --format md --max-chars 0    # read existing output
```

From source the equivalent is `.venv\Scripts\python -m app.cli ...`.

### 9.2 Watch the exe switch

The packaged `bili-transcriber.exe` starts the **GUI** by default. For CLI mode you must pass `--cli`:

```powershell
bili-transcriber.exe --cli --doctor
bili-transcriber.exe --cli "BV1xx411c7mD" --json
```

(From source, `python -m app.cli` is a CLI by construction and does not need the switch.)

### 9.3 All options

See the [CLI reference in the README](../README.md#命令行参考) for all 34 options, or run `--help`.

### 9.4 Output encoding

When stdout is a pipe or a file it is always UTF-8 (AI clients parse it correctly).
On a real terminal it keeps the console code page so `cmd.exe` does not garble Chinese.

## 10. Letting an AI use it

### 10.1 Health check first

```powershell
bili-transcriber-cli --doctor
```

It reports which AI clients are installed, where their config lives, and whether the MCP entry point works.

### 10.2 Write a client config with one command

```powershell
bili-transcriber-cli --agent-config workbuddy --write
bili-transcriber-cli --agent-config codex --write
bili-transcriber-cli --agent-config claude --write
```

A `.bak` backup is written first, and only the `bili-transcriber` entry is touched.
**DSH is the exception**: its connection table is managed by the UI, so the tool refuses to write and
gives you a snippet to paste instead.

### 10.3 Clients that can only connect to a URL

```powershell
bili-transcriber-cli --mcp-http 8765
```

Point the client at `http://127.0.0.1:8765/mcp`. For authentication set `BILI_MCP_HTTP_TOKEN` and
send `Authorization: Bearer <token>`.

### 10.4 What the AI can do

Eleven tools: submit, wait, status, list, read output, **full-text search**, **network/cookie check**,
cancel, prewarm, and read/write settings.

```text
You: can my cookies and proxy be used right now?
AI: calls check_network mode=test → per-platform latency and login state

You: transcribe this video and find where it covers consistency
AI: transcribe → wait_jobs → search_transcript → returns timestamps and text
```

> The `test` mode of `check_network` reads your current settings, so it agrees with the
> "Test connection" button in the GUI.

## 11. Troubleshooting

**No window / nothing happens**
Wait 10–20 seconds. If it still does not appear, read `%LOCALAPPDATA%\Bili Note\logs\app.log`.

**It exits by itself after a few minutes**
A known issue in 0.1.40 and older (pywebview temp directory reclaimed). Fixed in 0.1.41 — upgrade.

**Bilibili only offers 480p**
That needs a login: configure browser cookies or a cookies.txt.

**Transcription is slow**
Verify the GPU is really used (Device = `cuda`, check with `--doctor`). On CPU, large-v3-turbo is
3–10× slower; switch to `small` or SenseVoice.

**Download says it needs cookies although Test connection passed**
Check for `data\.cookies-from-<browser>.txt` (valid for 10 minutes). Closing and reopening the browser
refreshes it; otherwise export cookies.txt and point the setting at it.

**One part of a multi-part job fails — does everything restart?**
No. Each part has its own state; completed parts are not downloaded again.

**Can I skip the GUI?**
Yes. Install the wheel and use `bili-transcriber-cli`, or drive it from an AI over MCP.

**Port already in use**
Set `BILI_PORT=9000`. Double-clicking twice only raises the existing window — that is single-instance
protection working as designed.

## 12. Data and privacy

| Data | Location |
|---|---|
| Settings | `data/settings.json` |
| History | `data/history.db` |
| Models | `%LOCALAPPDATA%\Bili Note\models` |
| Logs | `%LOCALAPPDATA%\Bili Note\logs\app.log` (2 MB rotation, 3 files kept) |
| Output | the configured output directory |
| Exported cookies.txt | `data/cookies.txt` (**contains credentials**) |

Only two kinds of outbound request exist: downloading media and downloading models. Logs, history and
output stay on the machine.

> Cookies are handed to yt-dlp only at download time — never logged, never packaged, never sent to a third party.
