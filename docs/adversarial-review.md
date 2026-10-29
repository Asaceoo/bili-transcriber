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
| 4.6 | `render_cli(frozen=True)` 生成的示例命令带**非法 `--cli` 参数** —— AI 复制粘贴即 `unrecognized arguments` | 真机 `bili-transcriber.exe --cli ...` 报错；片段渲染复现 | 片段去掉 `--cli`（CLI 无需该参数） | frozen/源码两形态片段均无 `--cli`；回归锁定 |
| 4.7 | `--agent-config --write` 遇 JSONC 配置（Cursor/VS Code 的 mcp.json 常带 `//` 注释）→ 裸 JSONDecodeError traceback，AI 无从下手 | 闭环脚本：`JSONDecodeError: Expecting property name...`（原文件 intact） | 捕获后抛 ValueError：指明"文件含注释、原文件未动、粘贴这段片段"；片段随错误给出 | 报错含"注释"+"mcpServers"+片段；回归锁定 |
| 4.8 | 11 工具边界值轰炸（未知工具/非字符串 name/超大与负 timeout/负 limit/空 keyword/非法 mode/10MB keyword） | 真实 stdio 管道逐个喂：全部返回可读 `isError` 结构或夹紧生效，零 traceback 泄漏 | 无需修 —— 夹紧与校验层经此轮加固后已达标 | 8+3 用例期望行为逐项匹配 |

### 本轮方法论教训

- **测试全绿 ≠ 真机可用**：4.1/4.4/4.6 三个 bug 都在"测试覆盖不到的组装层"（CLI 入口、用户手写的配置变体、文档片段）。CLI 包装入口必须至少一条子进程级 e2e。
- **配对操作要同防**：4.5 —— 给 POST 上锁时问一句"DELETE/GET 呢"。
- **用户手写配置是脏数据**：4.4/4.7 —— 合并逻辑必须容忍注释、空格、JSONC，容忍不了就明确拒绝并给出路。

## 兜底校验清单（发布前逐项打勾）

- [x] stdio：initialize → tools/list（11 工具）→ tools/call 全链路真实管道通过
- [x] HTTP：initialize(返 session)/tools/list/SSE/GET 405/坏 JSON 400/跨站 403/未知 session 404/DELETE 204/healthz 200 —— 十项真实 socket 闭环
- [x] Bearer token 开启后：无 token 401 / 错 token 401 / 对 token 200；DELETE 同样受 Origin+token 管控（4.5）
- [x] `--doctor` 真机输出正确路径与客户端表
- [x] `--agent-config` 8 种客户端 × stdio/http 渲染正确；`--write` 有 `.bak` 备份；DSH 拒绝直写；JSONC 配置给可行动报错（4.7）；TOML 合并容忍行尾注释（4.4）
- [x] `--mcp-http` 真机起服（curl healthz 200 + POST ping 有响应 + stdout 打印端点行）—— 含子进程级 e2e 回归（4.1）
- [x] MCP 通道门禁：设置页关闭时 `--mcp` 与 `--mcp-http` 同样被拦（退出码 3）
- [x] 全量 pytest 522 通过（第四轮新增回归 4：serve_cli e2e / merge_toml 注释 / JSONC 报错 / DELETE 鉴权）

## 遗留与已知限制（不阻塞发布）

1. **`tool_timeout_sec` 未写入 Codex 配置**：Codex 若某版本不支持该键，写进去反而炸配置；先靠客户端侧放宽。
2. **HTTP 无断连续传/服务端推送**：GET /mcp 恒 405（规范允许）；长任务靠轮询 `wait_jobs`/`job_status`。
3. **会话表上限 64、TTL 30 分钟**：超限淘汰最旧；业务状态在 Service/DB，回收无副作用。
4. **`detect_clients` 读整份 `~/.claude.json` 判断注册态**：该文件个别用户可达数十 MB，一次性读取可接受。
5. **Python 调用方需注意系统代理**：`urllib` 会把 127.0.0.1 也代理出去（1.6）；Node/Go/curl 默认不受影响。
