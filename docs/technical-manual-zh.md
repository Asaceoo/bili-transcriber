# 技术手册 —— bili-transcriber

> 对应版本 **v0.1.45** · 面向要改代码、要发布、要排障的人。
> 想知道"怎么用"，看 [用户手册](user-guide-zh.md)。

---

## 目录

1. [架构总览](#1-架构总览)
2. [数据流与状态机](#2-数据流与状态机)
3. [模块地图](#3-模块地图)
4. [三个转写引擎](#4-三个转写引擎)
5. [网络与 Cookie 子系统](#5-网络与-cookie-子系统)
6. [无界面层（CLI / MCP）](#6-无界面层cli--mcp)
7. [桌面界面层](#7-桌面界面层)
8. [打包与发布](#8-打包与发布)
9. [质量基线与测试](#9-质量基线与测试)
10. [已知限制与设计取舍](#10-已知限制与设计取舍)

---

## 1. 架构总览

<img src="../docs/assets/pipeline.svg" alt="数据流" width="100%" />

三层，边界很硬：

```text
┌────────────────────────────── 入口层 ──────────────────────────────┐
│  main.py (GUI 分发器)   cli.py (命令行)   mcp_server.py / mcp_http.py │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓ 都依赖同一套服务层
┌────────────────────────── 服务层 headless.py ──────────────────────┐
│  Service: submit / wait / status / list / transcript / search /   │
│           cancel / prewarm / get_settings / set_settings          │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
┌────────────────────────── 编排层 pipeline.py ──────────────────────┐
│  单工作线程串行编排 + 状态机 + 断点续跑 + 模型缓存/卸载            │
└───┬──────────┬──────────┬──────────┬──────────┬───────────────────┘
    ↓          ↓          ↓          ↓          ↓
downloader  converter  transcriber   writers    store
（yt-dlp）  （FFmpeg）  （三引擎）   （SRT/TXT/MD）（SQLite）
```

**为什么这样切**：三个入口（GUI / CLI / MCP）必须行为一致 —— 否则"桌面端能转、CLI 不能"这种
分裂会不断出现。所以入口层只做协议转换，所有业务都在 `Service` 与 `pipeline` 里。

**app/ 共 32 个 Python 文件**（28 顶层 + `ui/` 4 个）。

## 2. 数据流与状态机

### 2.1 一次任务的完整路径

```text
submit(url)
  → downloader.probe(url)             展开成 MediaInfo 列表（单 P / 多 P / 合集）
  → 每个条目 enqueue
  → worker 取出 →
      [1] downloader.download_audio   只下音频流（notes 开启时下视频流）
      [2] converter.to_wav            16 kHz 单声道 WAV
      [3] transcriber.transcribe      GPU 转写，持锁
      [4] writers.write_*             SRT / TXT / MD（+ notes）
      [5] store.upsert                写历史库
  → done
```

### 2.2 状态机

```text
queued → downloading → converting → transcribing → saving → done
                     ↘           ↘            ↘        ↘
                      failed / cancelled（任一步都可）
paused ⇄（恢复后回到 queued）
```

状态落库（`history.db`），所以进程被杀也能知道上次停在哪；`recover_paused()` 在启动时
把 `paused` 恢复成 `queued`。

### 2.3 断点续跑

- 音频与 WAV 按媒体 ID 缓存在输出目录的中间区，重跑直接复用。
- 中间产物（分片、临时 wav）处理完立即删，不占盘。
- 重跑同一条会 `UPSERT` 覆盖，不会堆重复记录（主键 = 平台内媒体 ID）。

### 2.4 并发模型

| 阶段 | 并发 | 原因 |
|---|---|---|
| 下载 / 转码 | 可并行（`workers` 1~6） | 网络与 IO 吃不满 CPU |
| 转写 | **串行** | GPU 显存只够一个模型；并发反而 OOM |
| 写盘 | 随 worker | 无冲突（每条独立文件） |

`pipeline.apply_settings()` 在空闲时重建线程池；忙时跳过，避免新旧线程池并存。
判定"忙"要同时看 `running + paused` —— 暂停的工作线程可能正阻塞在 `wait_if_paused`，
只看 running 会漏。

### 2.5 模型缓存与卸载

- 缓存键：`(model_size, device, compute_type, language, vad)`；键不变就不重载。
- 三个引擎的 `load()` 都是 double-checked locking（并发双加载已修）。
- 空闲 600 秒无任务 → `maybe_unload_model()` 释放显存；下次任务重新加载。
- 保存设置后触发 `prewarm()` 预热（幂等、忙时跳过），并做 stale 校验：加载完成时
  若 `_transcriber` 已换实例，立即 `unload()`，防止泄漏。
## 3. 模块地图

| 模块 | 行数 | 职责 | 关键点 |
|---|---|---|---|
| `main.py` | 446 | 进程入口 + 界面分发 | exe 默认 GUI，必须 `--cli` 才进 CLI；`_is_spawn_child()` 前置放行 NiceGUI 子进程 |
| `ui/pages.py` | 1006 | 三个页签 + 设置页 | 动画走 CSS class；Quasar `loading` 会吃掉按钮文案，改用 `set_text+disable` |
| `ui/helpers.py` | 402 | UI 辅助 | `_notify()` 安全包装（托盘线程无 UI 上下文时不崩） |
| `ui/state.py` | 61 | 界面状态 | 0.6 s 轮询 Store + 事件 deque |
| `pipeline.py` | 784 | 单线程编排 | 状态机 / 断点续跑 / 预加载 / 空闲卸载 / 控制位 |
| `downloader.py` | 573 | 下载编排 | 平台识别、短链、SSR、**浏览器 Cookie 桥接**、可达性缓存 |
| `converter.py` | 189 | FFmpeg 转码 | 16 kHz 单声道 WAV；优先随包静态 ffmpeg |
| `transcriber.py` | 239 | Whisper 引擎 | CUDA 下 BatchedInferencePipeline，批量失败回退逐段 |
| `qwen_transcriber.py` | 466 | Qwen3-ASR 引擎 | 分组批量；结果数不符整批回退 |
| `sensevoice_transcriber.py` | 265 | SenseVoice 引擎 | sherpa-onnx int8；token 级时间戳聚合 |
| `writers.py` | 178 | 产物写盘 | SRT / TXT / MD / notes；原子写（临时文件 + rename） |
| `store.py` | 246 | SQLite | `busy_timeout=5000`；`DEFAULT_SETTINGS` 是设置的唯一真相 |
| `headless.py` | 505 | 服务层 | 通道开关守卫、设置校验、搜索、`check_network` 所需数据 |
| `cli.py` | 393 | 命令行 | 34 个选项；非 tty 强制 UTF-8；BrokenPipe 自愈 |
| `mcp_server.py` | 640 | MCP stdio | 11 工具；`_isolate_stdout()` 防 print 污染协议帧 |
| `mcp_http.py` | 487 | MCP HTTP | 仅绑 127.0.0.1；会话 TTL 30 min；Origin 白名单；Bearer token |
| `agent_setup.py` | 390 | 8 客户端配置 | DSH 拒绝直写；Codex 只写文档化 TOML 键；JSONC 给可行动报错 |
| `netcheck.py` | 1218 | 网络与 Cookie 检测 | 元信息/值/锁三态分离；探测硬截止；`ProxyHandler({})` |
| `cookiebridge.py` | 735 | Cookie 解密桥 | 最小 profile 复制 + 无窗口实例 + 标准库 WebSocket 走 CDP |
| `keyframes.py` | 288 | 关键帧 | 讲义截图；按场景变化挑帧 |
| `kuaishou.py` | 141 | 快手解析 | 自研分享页解析（yt-dlp 无该提取器） |
| `models.py` | 257 | 模型管理 | 外置目录、完整性校验、下载（镜像/自愈） |
| `hardware.py` | 195 | 硬件探测 | CUDA 可用性、显存、设备选择 |
| `gpu_runtime.py` | 118 | 运行时注册 | 把 nvidia 包的 bin 目录加进 PATH |
| `paths.py` | 21 | 路径 | 冻结时以 exe 目录为根；源码以项目根为根 |
| `task_control.py` | 63 | 控制位 | 取消/暂停标志，提交时清位防"秒杀" |
| `tray.py` | 98 | 托盘 | on_open 唤回窗口而非开浏览器 |
| `native_dialog.py` | 110 | 原生对话框 | 文件选择（WebView 里不能用浏览器 file picker） |
| `ffmpeg_bin.py` | 74 | ffmpeg 定位 | 随包静态版优先 |
| `logging_setup.py` | 59 | 日志 | `%LOCALAPPDATA%\Bili Note\logs`，2 MB 轮转留 3 份 |

## 4. 三个转写引擎

共同契约（`pipeline` 只认这三件事）：

```python
load(key) -> None          # 幂等；double-checked locking
transcribe(wav, language, progress) -> list[Segment]
unload() -> None           # 持锁；幂等
```

| 引擎 | 批量策略 | 失败回退 |
|---|---|---|
| Whisper | CUDA 下 `BatchedInferencePipeline(_BATCH_SIZE=8)` | 批量异常 → 整段改逐段 |
| Qwen3-ASR | 按 `_BATCH_CHUNKS=4` 分组（原生支持 List[audio]） | 结果数不符/异常 → 丢弃半成品整批回退 |
| SenseVoice | 单段（模型本身轻） | 无 |

时间戳来源不同：Whisper 原生；Qwen 用 ForcedAligner，失败时按字数比例估算；SenseVoice 用 token 级
时间戳再聚合成段。

`transcribe()` 期间 `pipeline` 全程持锁 —— 这是"转写串行"的实现方式，不是靠线程池大小。

## 5. 网络与 Cookie 子系统

### 5.1 三条检测线（`netcheck`）

| 线 | 入口 | 是否解密 |
|---|---|---|
| 浏览器扫描 | `detect_browsers()` | 否，只读 `(host_key, name, expires_utc)` |
| cookies.txt 扫描 | `detect_cookie_files()` | 否（文件本来就是明文） |
| 网络探测 | `probe_network()` | —（系统代理 → 环境变量 → 常见端口 → YouTube 验证） |

`build_report()` 按「覆盖平台数」选源：数量相同优先 cookies.txt；识别不到就保留用户现值，绝不清空。

### 5.2 三态语义（关键设计）

"值读不到" ≠ "登录态读不到"。`test_configuration()` 返回四元组：

```python
(values, states, metadata, error)
```

| 情形 | metadata | 平台行状态 | report.ok |
|---|---|---|---|
| 值拿到了 | dict | present / verified | 可以是 True |
| 值没拿到、元信息在 | dict + error | **present**（附"值加密读不到"） | **False** |
| 连元信息都没有 | None | unreadable（五行共用一句短文案） | False |

> 最后一行那条 `report.ok=False` 是审查抓出来的：把"登录态在"当成"配置可用"是假阳性 ——
> 用户看到绿灯去下载，结果失败了，还找不到原因。

### 5.3 Chrome 127+ 的 v20（应用绑定加密）

**问题**：Cookie 密文前缀从 `v10` 变成 `v20`，密钥由 elevation service 包裹且绑定原始
`user-data-dir` 路径。yt-dlp 只实现 v10，遇 v20 一律 `Failed to decrypt with DPAPI`（issues/10927）。

**方案**（`cookiebridge.py`）：

```text
复制 Local State + Cookie 库到临时目录
   → 起 --headless=new 临时实例（--user-data-dir=临时候本）
   → 标准库手写 WebSocket 走 CDP Network.getAllCookies
   → 浏览器自己解密，返回明文 JSON
   → 杀进程 + 删临时目录
```

**实现细节（都踩过）**：

1. **WebSocket 帧必须带 mask** —— 不带 mask 服务端立刻断开，现象是握手 101 之后 `closed`。
   服务端帧则不带 mask，别"对称"处理。
2. **布局要还原** —— `Local State` 在 User Data 根，Cookie 库在 `<root>/<Profile>/Network/Cookies`。
   只复制两个文件但不还原这层，浏览器找不到库，读出 0 条。
3. **多 profile 必须指名** —— 原路径策略要传 `--profile-directory=<名>`，否则 Chrome 一律回
   Default，读到别人的登录态（静默错误）。
4. **锁判定用复制探测** —— 能复制 = 没被独占。比 `SingletonLock`/进程名可靠：锁文件异常退出不清理，
   进程名会把"另一个 profile 在跑"误判成"这个被占用"。
5. **被锁时不要白等** —— Chromium 运行时连共享读都不给（实测 30 s / 60 次全失败），
   把 copy 重试从 6 s 降到 0.4 s，失败路径从 6.08 s → 0.46 s。

### 5.4 下载侧必须同时修

只修"测试连接"会留下自相矛盾：测试通过、下载报"需要 Cookie"。所以
`downloader._cookie_opts()` 增加了桥接：把明文落成 `data/.cookies-from-<浏览器>.txt`
（10 分钟缓存）交给 yt-dlp 走 `cookiefile`；桥接不可用时退回 `cookiesfrombrowser`。

> 缓存文件在 `data/` 下，已被 `.gitignore` 覆盖。

### 5.5 国内网络适配

| 坑 | 处理 |
|---|---|
| HuggingFace 慢 | `hf-mirror` 镜像 |
| SSL 证书异常 | 自愈重试 |
| 系统代理劫持回环 | 所有内部 urllib 显式 `ProxyHandler({})` |
| CUDA 轮子找不到 | 发布流程注入轮子源 |
| socket timeout 是"每地址每次" | 探测统一用 `future.result(timeout=)` 硬截止 |
## 6. 无界面层（CLI / MCP）

### 6.1 通道开关

`enable_cli` / `enable_mcp` 在 `store.DEFAULT_SETTINGS`，桌面端设置页写，CLI/MCP 只读。
门禁在**构造 Service 之前**判断（不白占 SQLite 锁）：

| 通道 | 关闭后 |
|---|---|
| CLI | 退出码 3，stdout 空 |
| MCP stdio | stderr 一行说明，返回 1，stdout 保持空（不污染协议帧） |

### 6.2 stdout 保护

MCP 的 stdout 就是协议通道。`_isolate_stdout()` 把真协议句柄存局部变量、`sys.stdout` 改指 stderr，
任何遗留的 `print` 都不会破坏帧。

### 6.3 CLI 输出编码

PyInstaller 运行时**不继承** `PYTHONUTF8` / `PYTHONIOENCODING`。旧兜底"装不下才降级"永不触发
（GBK 装得下常用汉字），所以打包版 CLI 曾输出 GBK。现在 `main()` 首行 `_configure_output_encoding()`：

- 非 tty 的 stdout/stderr → `reconfigure(encoding="utf-8", errors="replace")`
- 终端保持控制台代码页（否则 `cmd.exe` 花屏）

### 6.4 MCP HTTP

| 项 | 设计 |
|---|---|
| 绑定 | 只 `127.0.0.1` |
| 会话 | TTL 30 min、上限 64 淘汰最旧；`touch()` 先 expire 再查（否则过期会话被复活） |
| 鉴权 | 可选 Bearer token，`hmac.compare_digest` |
| 跨站 | Origin 白名单；POST/DELETE **同防** |
| 超时 | Handler `timeout=60`（防慢速半开连接占线程） |
| 并发 | Service SQLite 非并发安全 → `_handle_lock` 串行化工具调用 |
| 启动 | `start()` 起守护线程跑 accept；`server_bind` 覆盖掉 `getfqdn()`（DNS 被接管时会挂几十秒） |

### 6.5 八个客户端

`agent_setup.py` 里 `ClientSpec` 描述格式（JSON / TOML）、键名、路径、是否可写：

| 客户端 | 格式 | 可写 | 特殊处理 |
|---|---|---|---|
| WorkBuddy | JSON `mcpServers` | ✅ | 保留其它条目 |
| DSH | JSON（连接表） | ❌ | 界面托管，拒绝直写并给粘贴片段 |
| Codex | TOML | ✅ | 只写文档化键 + `startup_timeout_sec` |
| Claude / Cursor / VS Code / Tare / 通用 | JSON | ✅/粘贴 | VS Code 用 `servers` 键 |

写前 `.bak` 备份；JSONC（带 `//` 注释）给可行动报错而不是裸 traceback。

## 7. 桌面界面层

- **三页签**：任务 / 历史 / 设置；@1 秒轮询 Store（`0.6 s`）驱动刷新。
- **单实例**：NiceGUI spawn 的子进程会重跑 `main()`，靠 `_is_spawn_child()` 前置放行，
  否则每次启动多开一个浏览器页。
- **原生窗口**：`_resolve_ui_mode()` 默认 native（有 WebView2 时）。`ui.run(window_size=...)`
  会强制 native，所以只有 native 模式才传它。
- **WebView2 数据目录必须固定**：pywebview 在 `private_mode` 下把 UserDataFolder 设成临时目录，
  那个临时对象被 GC 后目录被删、渲染进程崩溃、程序静默退出（"开着几分钟自己没了"）。
- **唤回而不是新开**：`_activate_existing_window()` 轮询 `FindWindowW` 最多 30 s。
- **托盘**：`on_open` 同样是唤回窗口，不是开浏览器。

## 8. 打包与发布

```powershell
.venv\Scripts\python.exe scripts\release.py            # 全量
.venv\Scripts\python.exe scripts\release.py --bin-only  # 只重建二进制（注意：仍会重建 single + GPU）
```

### 8.1 四个必炸坑

1. **沙箱钩子** —— 脚本内已设 `CODEBUDDY_SAFE_DELETE_SANDBOX=0`。绕过脚本手跑 PyInstaller 必须自己设，
   否则收尾 `os.remove` 被拦死、构建卡死退出码 1。
2. **版本三处同步** —— `pyproject.toml` / `build/installer.iss` / `app/__init__.py APP_VERSION`。
   `release.py` 自动 bump；手改必须三处一起。
   ⚠️ 脚本**一开头就 bump**，被打断后重跑会再 +1 —— 重跑前先把三处与 README/手册版本头回退到上一版。
3. **snapshot_docs 滚动快照** —— 发布时生成 `docs/*-{ver}.md` 并删旧快照。
   **常驻文档（`ai-integration.md`、`adversarial-review.md`）不在快照列表，严禁当旧快照删**。
4. **dist/build 清理** —— 旧产物移入待删目录（`dist/_trash_YYYYMMDD/`）而非 `rm`，避开 safe-delete 钩子。

### 8.2 产物与上传

| 产物 | 说明 |
|---|---|
| `setup.exe` | Inno Setup 安装包 |
| `portable.zip` | onedir 便携版 |
| `single.exe` | onefile（PyInstaller hook 先判 `sys.stderr is not None` 再写） |
| `gpu.zip` | NVIDIA 运行库 |
| `*.whl` | hatchling wheel |
| `bili-note-skills-*.zip` | 配套技能包（`git archive` 从 `~/.workbuddy/skills/` 三个目录打，过滤 `.bak`/`.tsv`） |

上传：`python scripts/github_release_upload.py <tag> <assets...> --notes <md>`（含 3 次重试）。

### 8.3 冻结版分发开关

`main.py`：**exe 默认进 GUI，必须 `--cli` 才进 CLI**；源码 `python -m app.cli` 天然是 CLI。
改 `render_cli` / `agent_setup` 片段时 frozen 分支**必须保留 `--cli`**（曾有"误修"把它删掉，
后来真机跑 `--doctor` 起了 GUI 才抓回）。回归测试双向锁定：冻结版必含 / 源码版必不含。

## 9. 质量基线与测试

- **544 条测试 / 29 个文件**，全绿约 2 分钟。
- 测试**全部离线**：Cookie 库用临时 SQLite 造，网络探测全 mock，不依赖本机装了什么。
- 组装层（CLI 入口、HTTP 服务、冻结包）必须有**子进程级 e2e** —— 曾经 517 项全绿但
  `--mcp-http` 真机一跑就 `NameError`（测试只盖了类，没盖包装入口）。

| 测试文件组 | 覆盖 |
|---|---|
| `test_pipeline / test_downloader` | 状态机、断点续跑、Cookie 桥接与缓存 |
| `test_transcriber / qwen / sensevoice` | 三引擎批量与回退 |
| `test_netcheck` | 三态语义、桥接布局、Netscape 闭环、前缀识别 |
| `test_mcp_server / test_mcp_http` | 11 工具、边界值、鉴权、跨站、真 socket e2e |
| `test_cli / test_release_docs` | 参数不冲突、文档片段与冻结版一致性 |
| `test_ui / test_ui_mode` | 界面渲染与形态分发 |

## 10. 已知限制与设计取舍

1. **浏览器开着时读不到 Cookie** —— Chromium 独占锁，属浏览器设计；文案让用户关掉浏览器而非假装重试。
2. **v20 只能在原路径解** —— ABE 密钥绑定路径，所以"复制 profile 到别处再解"对 v20 无效。
3. **转写串行** —— 不是懒，是显存决定的；并行只会 OOM。
4. **MCP HTTP 无服务端推送** —— GET /mcp 恒 405（规范允许），长任务靠轮询。
5. **会话表 64 上限** —— 超限淘汰最旧；业务状态在 DB，回收无副作用。
6. **不加新运行时依赖** —— Cookie 桥用标准库手写 WebSocket，打包体积不变。
