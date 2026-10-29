# 对抗性审查清单 —— MCP/CLI 多端接入（v0.1.43）

> 方法：三个视角分轮审查（工具实现者 → 审查者 → AI 使用方），每轮用**真实调用**
> 取真实报错作为修复依据（stdio 管道 / 真实 HTTP 请求 / CLI 子进程），
> 修复后复跑验证收敛；最后以本清单做兜底校验。

## 审查范围

- `app/mcp_http.py`（MCP Streamable HTTP 传输）
- `app/agent_setup.py`（WorkBuddy/DSH/Codex/Claude/Cursor/VS Code/Tare 配置生成）
- `app/mcp_server.py` 新增工具 `search_transcript` / `check_network`
- `app/headless.py` 新增 `Service.search`
- `app/cli.py` 新增 `--grep / --doctor / --agent-config / --agent-list / --mcp-http`

## 第一轮：工具实现者视角（真实调用闭环）

| # | 发现 | 真实报错/证据 | 修复 | 复验 |
|---|---|---|---|---|
| 1.1 | `McpHttpServer.start()` 只构造 server 对象，**没有线程跑 `serve_forever`** —— 无人 accept，客户端请求全部挂死 | 真实 POST 到 `127.0.0.1` 15s 无响应；服务端 DEBUG 日志零输出（handler 从未执行） | `start()` 起守护线程跑 accept 循环；`serve_forever()` 语义改为等待后台线程 | HTTP 十项闭环全过 |
| 1.2 | `BaseHTTPRequestHandler.server_bind` 默认调 `socket.getfqdn()` 反向解析 —— DNS 被代理接管的机器上服务启动即挂几十秒 | 首次冒烟连 `listening` 都打不出来 | `_QuietHTTPServer` 覆盖 `server_bind`，`server_name` 用字面 IP | 启动即监听 |
| 1.3 | `_Sessions.touch()` 无条件复活会话：过期会话被 touch 后又活了 | `test_sessions_expire_by_ttl` 真实失败 | `touch()` 先跑 `_expire_locked` 再查存在 | 过期即 404 |
| 1.4 | CLI `--limit` 重复定义，argparse 直接崩 | `ArgumentError: conflicting option string: --limit` | 复用既有 `--limit`，help 注明同时管 `--list/--grep` | CLI 冒烟过 |
| 1.5 | `doctor()` 把 settings.json 写死在打包版路径，源码版必误报 | `--doctor` 报"还没生成 settings.json"，但 `data/settings.json` 明明存在 | 改用 `app.paths.data_dir()`（同时覆盖两种形态） | doctor 输出正确路径 |
| 1.6 | Python `urllib` 默认走系统代理 —— 本机开着 Clash 时，连 `127.0.0.1` 的请求都被代理出去，表现为"服务挂了"（实为调用方环境问题） | e2e 第一步 initialize 超时；换 `ProxyHandler({})` 立即通 | 服务端无需改；文档标注 Python 调用方需 `ProxyHandler({})` 或 `no_proxy` | e2e 全过 |

## 第二轮：审查者视角（协议契约与边界）

| # | 发现 | 修复 |
|---|---|---|
| 2.1 | `--agent-config dsh --write` 会把 `{"mcpServers": …}` 写进 DSH 的 `tables.connections` 结构 → 客户端起不来 | `ClientSpec.writable=False`，`write_config` 对 DSH 直接拒绝并给手动添加指引；测试锁定 |
| 2.2 | Codex TOML 里写了想当然的 `tool_timeout_sec` 键 —— Codex 对未知键解析失败，**整个 config.toml 起不来，用户其它 provider 配置陪葬** | 只保留文档化的 `startup_timeout_sec = 300`；超时建议写进注释行；测试断言禁止该键出现 |
| 2.3 | `_agent-config` 的说明文字追加在 JSON 片段后面 → stdout 不再是"复制即用"，脚本解析也炸 | 说明走 stderr，stdout 保持纯净配置；`--json` 时输出结构化 `{client, config}` |
| 2.4 | `check_network mode` 未知值静默回退 quick —— AI 传错枚举拿不到任何提示 | 未知 mode 抛可读 `ServiceError`（列出合法值）；测试锁定 |
| 2.5 | HTTP 服务默认零 CORS + Origin 白名单 + 可选 Bearer token + 只绑 127.0.0.1 —— 安全默认值在审查中逐一确认 | 维持默认；`_origin_allowed` 测试覆盖 7 种 Origin |

## 第三轮：AI 使用方视角（真实怪参数管道）

用真实 stdio 管道喂 AI 常犯的参数错误，以真实返回为准：

| 输入 | 修复前 | 修复后 |
|---|---|---|
| `transcribe wait=true timeout_sec=1e9` | 无上限，服务可被挂到天荒地老 | 夹紧到 86400s（1 天）；单测断言 `seen["timeout"]==86400` |
| `search_transcript keyword=12345`（数字） | — | `str` 强转，正常检索 |
| `wait_jobs media_ids="单个ID"`（字符串） | — | 自动包装成 `[str]` |
| `update_settings workers="three"` | — | `_coerce_setting` 强转/回退，坏值不落盘（实测 settings.json 仍为 2） |
| `get_transcript format="yaml"` | — | 可读错误"不支持的格式" |
| `tools/call` 的 `arguments` 传字符串 | — | "arguments 必须是对象" |

## 第四轮（v0.1.43 发布后复审）：三视角再收敛

上轮修复后全量测试全绿，本轮换攻击面复审 —— 又抓出 **4 个测试全绿仍漏网的真 bug**，
全部以真实执行取证据、修复后复跑收敛：

### 实现者视角

| # | 发现 | 真实证据 | 修复 | 复验 |
|---|---|---|---|---|
| 4.1 | `serve_cli()` 用 `sys.stdout` 但模块没 import sys —— **真实 `--mcp-http` 路径一跑即 NameError**，而 517 项测试全绿（测试只盖 `McpHttpServer` 类，CLI 包装入口零覆盖） | 真机跑 `--mcp-http 8791` 即崩 `NameError: name 'sys' is not defined` | 补 `import sys`；新增子进程端到端回归（stdout 端点行 + POST initialize） | 真机全通；518→522 passed |
| 4.2 | HTTP Handler 无读写超时 —— 半开连接（只发半个头）永久占住一条线程 | 代码走查（BaseHTTPRequestHandler 默认 `timeout=None`） | `Handler.timeout = 60` | slowloris 场景线程 60s 自愈 |
| 4.3 | token 比较用 `==`，理论上有时序侧信道 | 代码走查 | `hmac.compare_digest` | 行为不变，测试全过 |

### 审查者视角

| # | 发现 | 真实证据 | 修复 | 复验 |
|---|---|---|---|---|
| 4.4 | `merge_toml` 只精确匹配 `[mcp_servers.bili-transcriber]` —— 用户手写行尾注释/空格变体即漏匹配 → 旧 section 不替换 + 新块追加 = **TOML 同名 table 两份，Codex 解析直接失败，整个 config.toml 坏**（正是 2.2 担心的"陪葬"场景） | 闭环脚本：`tomllib PARSE-FAIL: Cannot declare ('mcp_servers','bili-transcriber') twice`；连带发现 skipping 状态遇 `[other] # 注释` 不退出、会把用户下一个 section 吞掉 | section 头按 `#` 截断再比较（头行无字符串字面量，安全）；skipping 退出条件同源修复 | tomllib 解析 OK、旧/新/第三 section 内容全保真；回归测试锁定 |
| 4.5 | `do_DELETE` 无 Origin/token 校验 —— POST 设防了，DELETE 裸奔，外部网页可经浏览器清空会话 | 真实 socket：带 `token=sekrit` 的服务 DELETE 无鉴权照样 204 | DELETE 与 POST 同等校验（Origin 403 / token 401） | 无 token 401 / 外部 Origin 403 / 本机 Origin 204（真实 HTTP 三连） |

### AI 使用方视角

| # | 发现 | 真实证据 | 修复 | 复验 |
|---|---|---|---|---|
| 4.6 | ~~`render_cli(frozen=True)` 生成含非法 `--cli` 参数~~ **本条为误修,v0.1.44 真机冒烟时纠正**：`--cli` 是冻结版必需的模式开关（exe 入口是 GUI 分发器,不带 `--cli` 跑 `--doctor` 会直接启动 GUI 窗口）；源码版 `python -m app.cli` 才不带它。原实现本就正确 | 误修证据:只验证了源码模式 `python -m app.cli --cli` 报错,未做冻结版真机验证;打包后 PowerShell 真机跑 `bili-transcriber.exe --doctor` 起了 GUI 才发现 | 恢复 frozen 分支 `--cli`；回归测试改为"冻结版必含 / 源码版必不含"双向断言 | 真机 `bili-transcriber.exe --cli --doctor --json` 输出正确 doctor JSON |
| 4.7 | `--agent-config --write` 遇 JSONC 配置（Cursor/VS Code 的 mcp.json 常带 `//` 注释）→ 裸 JSONDecodeError traceback，AI 无从下手 | 闭环脚本：`JSONDecodeError: Expecting property name...`（原文件 intact） | 捕获后抛 ValueError：指明"文件含注释、原文件未动、粘贴这段片段"；片段随错误给出 | 报错含"注释"+"mcpServers"+片段；回归锁定 |
| 4.8 | 11 工具边界值轰炸（未知工具/非字符串 name/超大与负 timeout/负 limit/空 keyword/非法 mode/10MB keyword） | 真实 stdio 管道逐个喂：全部返回可读 `isError` 结构或夹紧生效，零 traceback 泄漏 | 无需修 —— 夹紧与校验层经此轮加固后已达标 | 8+3 用例期望行为逐项匹配 |

### 本轮方法论教训

- **测试全绿 ≠ 真机可用**：4.1/4.4 两个 bug 都在"测试覆盖不到的组装层"（CLI 入口、用户手写的配置变体）。CLI 包装入口必须至少一条子进程级 e2e。
- **真机验证必须覆盖交付形态**：4.6 误修的根源是只验证了源码形态。`render_cli` 的消费方是打包版 exe —— 打包后必须用**冻结包本体**冒烟（本次就是发布前真机跑 `--doctor` 起了 GUI 才抓回）。
- **配对操作要同防**：4.5 —— 给 POST 上锁时问一句"DELETE/GET 呢"。
- **用户手写配置是脏数据**：4.4/4.7 —— 合并逻辑必须容忍注释、空格、JSONC，容忍不了就明确拒绝并给出路。

## 第五轮（第四轮后再收敛）：换攻击面复审

攻击面转向 `Service.search / set_settings / _validate_settings / _toml_str / normalize_source`
（前三轮未深查的服务层），又收敛 3 项：

| # | 视角 | 发现 | 真实证据 | 修复 | 复验 |
|---|---|---|---|---|---|
| 5.1 | 实现者 | `search()` 第一步用**区分大小写**的 `in` 做快速排除,第二步才用 `lower().find()` —— 意图是大小写不敏感,实际被第一步短路,**完全失效** | 真实库写入含 "Transformer" 的稿,search "transformer" 命中 0(同大小写命中 1) | 快速排除改用 `kw_lower not in text.lower()`,与查找同一比较基准 | 三种大小写全命中;真机 `--grep "the"` 命中真实转写稿 |
| 5.2 | 审查者 | `_validate_settings` 对 `output_dir` 只 `expanduser` 不 `resolve` —— 相对路径原样落盘,长驻 MCP 进程与 CLI 的 CWD 不同,产物散到别处 | `_validate_settings({"output_dir": "some_relative_dir"})` 落盘 `'some_relative_dir'` | `target.resolve()` 后再落盘(顺带规范化 `..`) | 落盘值必为绝对路径;回归锁定 |
| 5.3 | AI 使用方 | `update_settings engine=""`(AI 表达"重置默认"的常见写法)→ `_coerce_setting` 空串透传落盘,`engine=''` 使下次转写选择行为未定义;`--set key=` 同路 | 真实 Service:`_coerce_setting("engine","") == ""` | `set_settings` 统一空串守卫:报错含键名与指引("空串不会重置默认,要改给合法值,不改别传"),与 output_dir 的既有哲学一致 | 真机 `--set engine=` 拒绝;正常值路径不受影响;既有 output_dir 空串测试同步更新 |

排除项(闭环取证后确认无害):`_toml_str` 混合引号/中文路径 tomllib 往返一致;
`normalize_source` 收带空格 BV 号看似不补全,但 `submit()` 入口已 strip,真实路径安全。


## 第六轮（v0.1.45）：Cookie 桥接与新文档的对抗审查

攻击面：新增的 `app/cookiebridge.py`（浏览器本体解密）、`netcheck` 的三态语义与 `report.ok`、下载链路的 Cookie 传递、MCP `check_network`，以及重写后的 5 份文档。

方法未变：三视角分轮，**每轮以真实调用取真实报错**（真实 Chrome profile / 真实 stdio 管道 / 真实 yt-dlp 构造 / 真实 GUI 起服），修复后复跑收敛。

### 实现者视角

| # | 发现 | 真实证据 | 修复 | 复验 |
|---|---|---|---|---|
| 6.1 | **下载链路没跟着修**：只修了「测试连接」，下载仍走 `cookiesfrombrowser`，而 Chrome 运行时库被独占，yt-dlp 连复制都做不到 | 真实 `yt-dlp + cookiesfrombrowser=chrome`：`Could not copy Chrome cookie database (issues/7271)`；同一份 Cookie 走 `cookiefile` 则被正常接受（jar 内 1 条） | `downloader._cookie_opts()` 增加桥接：明文落 `data/.cookies-from-<browser>.txt`（10 分钟缓存）→ `cookiefile`；桥接失败才回退旧行为 | 4 条 downloader 回归；实测缓存命中 0.001 s |
| 6.2 | 库被锁时白等 6 秒（`copy_min_profile(wait=6.0)` 默认值） | 实测失败路径 **6.08 s** | 确认被锁就传 `copy_wait=0.4`，不再假装努力 | 同场景 **0.46 s** |
| 6.3 | 桥接异常路径是否泄漏临时目录 / 残留进程 | 连续制造 3 次失败后：临时目录 before=0 after=0，chrome 进程 29→29 | 无需修（`finally` 已覆盖） | 复测一致 |
### 审查者视角

| # | 发现 | 真实证据 | 修复 | 复验 |
|---|---|---|---|---|
| 6.4 | `read_cookies_in_place` 用 `--user-data-dir=<root>` 却**不传 `--profile-directory`** —— 多 profile 用户被静默切到 Default，读到别人的登录态 | 代码走查 + `profile_root()` 返回 `rel="Default"`，而 `_launch` 的 argv 里没有该开关 | `_launch/_drive` 增加 `profile_dir`；`rel != "."` 时传入 | 参数已进 argv（Opera 根即 profile 时不传） |
| 6.5 | **`report.ok` 假阳性**：把「登录态在」当「配置可用」。值取不到时 yt-dlp 拿到空串，下载必失败 | 构造 metadata 有值 + value_error：修复前 `ok=True`、`summary="配置可用"` | `ok = net_ok and cookie_ok and not value_error`；新增 summary「识别到登录态但值取不到」 | `ok=False` + 两条回归（假阳性 / 对照组） |
| 6.6 | 「值读不到」与「登录态读不到」共用一个分支，元信息可用时仍把五平台标红 | 真机：metadata 有的平台被标 `unreadable` | 三态：`metadata is not None` 才算读到；未读到才 `unreadable`，五行共用一句短文案 | 3 条回归覆盖三态 |
| 6.7 | 锁判定用 `SingletonLock` / 进程名，会把「另一个 profile 在跑」误判成「这个被占用」 | 隔离 profile（无锁）被进程名判成 running=True，in-place 路径被误拦 | 改为**复制探测**（能复制 = 没被独占） | 独占句柄实验：占用时 True、释放后 False |

### AI 使用方视角

| # | 输入 | 修复前 | 修复后 |
|---|---|---|---|
| 6.8 | `check_network {"mode":"test"}` | 真实 stdio 管道：`source='未配置 Cookie(仅能下载公开内容)'`、五平台全 `missing` —— 与「按当前配置实测」自相矛盾 | 读 `get_settings()` 并传 browser/cookie_file/proxy；回归断言四个参数逐一对上 |
| 6.9 | `{"mode":" test "}` | `ServiceError: mode 只支持…`（而 `TEST` 能过） | `strip().lower()` 同做；`test` / `TEST` / `" Test "` 全等价（4 种写法回归） |
| 6.10 | `timeout` = 1e9 / -5 / "abc" | 夹紧到 15 s / 1 s；非数字给可读错误 | 无缺陷（既有夹紧层有效） | 逐项匹配 |
| 6.11 | 浏览器名 `"Chrome"` / `"CHROME"` / `"edge "` | 路径发现返回 None，**静默失败**（上层仍显示「Chrome 浏览器 Cookie」） | `normalize_browser()` 收口（含 `MS Edge` / `google-chrome` / `brave-browser` 别名） | 12 种写法逐个验证 |
| 6.12 | `browser="safari"` / `"netscape"` | 「没找到 safari 的 Cookie 库」—— AI 分不清「不支持」与「没安装」 | `require_supported()` 抛「不支持 X；支持：chrome、edge…」 | 三个未知名均给支持列表 |
| 6.13 | 超大 Cookie 值（1 MB） | 写入 1,000,148 字节且回读成功 | 无需修（Netscape 无长度上限） | 复测一致 |
### 本轮方法论教训

- **修一个入口不算修**：6.1 —— 「测试连接」和「下载」是两条独立路径，只修前者会留下「测试通过、下载报需要 Cookie」的自相矛盾。改涉及凭据/配置的读取时，必须顺着**所有消费方**走一遍。
- **「能跑」和「结论正确」是两件事**：6.5/6.6 —— 代码不报错、界面全绿，但结论是错的（假阳性）。这类问题只能靠「故意构造中间态」取证：值有/无 × 元信息有/无，四种组合逐一验。
- **静默失败最难查**：6.4/6.11 —— profile 读错人、浏览器名大小写不匹配，都不报错。约定：凡「找不到就用默认值」的分支，必须能证明默认值是对的，否则宁可报错。
- **契约变更要同步测试**：6.8 的三元组→四元组变更让旧测试桩直接 `ValueError`，这正是回归测试的价值 —— 它逼着把新契约写清楚。

## 兜底校验清单（发布前逐项打勾）

- [x] stdio：initialize → tools/list（11 工具）→ tools/call 全链路真实管道通过
- [x] HTTP：initialize(返 session)/tools/list/SSE/GET 405/坏 JSON 400/跨站 403/未知 session 404/DELETE 204/healthz 200 —— 十项真实 socket 闭环
- [x] Bearer token 开启后：无 token 401 / 错 token 401 / 对 token 200；DELETE 同样受 Origin+token 管控（4.5）
- [x] `--doctor` 真机输出正确路径与客户端表
- [x] `--agent-config` 8 种客户端 × stdio/http 渲染正确；`--write` 有 `.bak` 备份；DSH 拒绝直写；JSONC 配置给可行动报错（4.7）；TOML 合并容忍行尾注释（4.4）
- [x] `--mcp-http` 真机起服（curl healthz 200 + POST ping 有响应 + stdout 打印端点行）—— 含子进程级 e2e 回归（4.1）
- [x] MCP 通道门禁：设置页关闭时 `--mcp` 与 `--mcp-http` 同样被拦（退出码 3）
- [x] 全量 pytest 525 通过（第五轮新增回归 3:search 大小写 / 空串守卫 / output_dir resolve）
- [x] `--grep` 真机命中真实转写稿;`--set engine=` 真机拒绝并给可读指引
- [x] 服务层换面复审(search/set_settings/validate_settings/toml_str/normalize_source)完成,tomllib 往返与 submit strip 链路取证排除
- [x] 全量 pytest **544** 通过（第六轮新增回归 19：桥接布局/Netscape 闭环/三态语义/ok 非假阳性/下载桥接 4 条/设置透传/mode 规范化）
- [x] **Cookie 桥接**：真实 Chrome profile 的 v20 密文在临时副本上解出明文（1.06 s）；导出的 Netscape 文件被项目自身 `parse_cookie_file()` 读回登录态（闭环）
- [x] **下载链路**：桥接落的 `cookiefile` 被真实 yt-dlp 接受（jar 内 1 条）；缓存命中 0.001 s；桥接失败回退 `cookiesfrombrowser`
- [x] **三态语义**：值有/无 × 元信息有/无 四种组合逐一构造验证；`report.ok` 不把「有登录态」当「可用」
- [x] **MCP `check_network(test)`**：真实 stdio 管道验证设置四参数透传，`source` 由「未配置 Cookie」变为实际浏览器
- [x] `mode` 四种写法（`test` / `TEST` / ` Test ` / 双空格）等价；不支持浏览器给支持列表；浏览器名 12 种写法规范化
- [x] **资源与异常**：连续 3 次桥接失败后临时目录 0 泄漏、chrome 进程数不变；1 MB Cookie 值写入回读正常
- [x] 真机 GUI：起服 8811，页面含「网络与 Cookie / 一键检测 / 测试连接 / 保存设置」，日志 0 ERROR
- [x] 冻结包冒烟：`bili-transcriber.exe --cli --version` → 0.1.45；`--cli --doctor` 正常
- [x] 文档：README（概念图 4 张、20 实例、34 参数）+ 四份手册重写；`docs_lint` 0 error；相对链接与图片全存在
- [x] 文档版本号自动递增：README + 四份手册头部可被 `release.py` 识别并同步，同时生成 `docs/*-0.1.45.md` 快照

## 遗留与已知限制（不阻塞发布）

1. **`tool_timeout_sec` 未写入 Codex 配置**：Codex 若某版本不支持该键，写进去反而炸配置；先靠客户端侧放宽。
2. **HTTP 无断连续传/服务端推送**：GET /mcp 恒 405（规范允许）；长任务靠轮询 `wait_jobs`/`job_status`。
3. **会话表上限 64、TTL 30 分钟**：超限淘汰最旧；业务状态在 Service/DB，回收无副作用。
4. **`detect_clients` 读整份 `~/.claude.json` 判断注册态**：该文件个别用户可达数十 MB，一次性读取可接受。
5. **Python 调用方需注意系统代理**：`urllib` 会把 127.0.0.1 也代理出去（1.6）；Node/Go/curl 默认不受影响。