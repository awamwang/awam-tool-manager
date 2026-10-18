---
name: awam-tool-manager
description: 统一工具管理器，维护本机工具名单，提供统一的工具注册、搜索、启动能力。作为 MCP Server 暴露给 AI 客户端，支持搜索工具、列出工具、启动工具、路由调用后端 MCP、管理后端进程等。
---

# awam-tool-manager 技能

统一管理本机各类工具，提供统一的注册、搜索、启动与**后端路由调用**能力。

工具有三类 `kind`：MCP 工具（`kind=mcp`）、普通应用（`kind=app`）与启动器（`kind=launcher`，
如 Quicker、uTools——不是 MCP，调用为一次性外部命令），三者共用同一套 `tool_id` 体系。

## 核心能力

- **工具注册**：以 JSON 文件为单位登记工具元数据（`data/tools/`）
- **筛选查询**：`list_tools` / `search_tools` 除 `kind` 外还可按 `status`（安装状态，
  精确匹配，条目未写时按 `installed` 计）与 `publisher`（发布者，子串、大小写不敏感）
  收窄，多个维度取交集
- **MCP 服务**：作为 MCP Server 暴露给 AI 客户端（stdio 模式）
- **路由调用**：把调用转发给 `kind=mcp` 工具对应的后端 MCP Server
- **进程管理**：管理后端 MCP 子进程的启停、状态、日志
- **目录同步**：拉取后端 MCP 的子工具清单并缓存到本地
- **依赖自装**：启动命令缺失时按 `install` 元数据安装（**默认不执行，需显式授权**，见「安全约定」；服务启动时后台预热已退化为纯检测）
- **帮助查看**：汇总工具的文档链接与能力清单；`kind=app` 显式 `run=true` 才执行
  帮助命令并捕获输出（默认只读数据，不 spawn）
- **使用统计**：每次 `tools/call` 在分发出口记一行（对外工具名、目标 `tool_id`、
  成败、耗时）到 `<runtime>/usage.log`，`get_usage_stats` 按日历日窗口聚合。
  刻意**不记调用参数**；统计只覆盖经本 server 分发的调用，用户直接启动工具、
  以及其他进程的历史调用都不在计数内
- **工具发现**：`discover_tools` 按仓库内声明式候选清单（`data/discovery.json`）
  做**只读**探测（路径存在 / 注册表登记值 / 目录条目数 / 命令是否在 PATH），对照已登记工具给出
  `installed` / `not_installed` / `unknown`；默认不执行任何命令，取版本号要显式传
  `run_commands=true`。探测面是声明出来的、不是穷举，「没发现」不等于「本机没有」；
  `{ENV}` 位置在进程环境取不到时会回退查 Windows 注册表（MCP 客户端拉起 server
  时按白名单剥变量，`ProgramFiles` / `SCOOP` 这类常常不在环境里），两处都取不到
  才判 `unknown`。声明式路径穷举不出「装在非常规盘」——Steam 装 `S:\Program Files (x86)`
  时只声明 `C:\Program Files` 的清单把它判成了 `not_installed`，所以每个候选都该有
  一条**不依赖盘符**的证据（`registry_value` 读安装器自己写的登记值）
- **包管理适配**：`list_packages` / `search_packages` / `launch_package` /
  `install_package` 直接读安装器落在盘上的清单（当前后端：`scoop`），**不跑
  `scoop list`**——列已装包读 `apps/*/current/manifest.json`（`current` 断链时
  回落版本目录并如实标 `current_broken`）、搜可装包读 `buckets/*/*.json` 的文件名、
  挑启动入口看 `shortcuts` / `bin` / `shims/<包名>`。启动与安装都要显式
  `execute=true`；安装额外要 `allowDestructive=true`（`kind=package` 门禁是
  fail-closed 的，见「安全约定」）。`winget` / `npm` 没有可靠的纯读盘来源，
  属执行面，尚未接线

## 目录结构

```
awam-tool-manager/
├── SKILL.md                  # 本文件
├── docs/
│   ├── format.md             # 数据格式规范
│   ├── architecture.md       # 架构说明
│   ├── mcp-conformance.md    # MCP 协议一致性对比
│   └── code-review.md        # 代码审核报告
├── data/                     # 仓库内只有人工维护的真相源（A14 起）
│   ├── tools/                # 工具条目（每个工具一个 JSON）
│   └── discovery.json        # 发现候选清单（discover_tools 的声明源）
├── src/
│   ├── server.py             # MCP Server 入口 + 对外工具定义 + 分发
│   ├── store.py              # JSON 存储层（tools 走仓库，缓存走运行时目录）
│   ├── registry.py           # 工具注册中心（查询语义）
│   ├── backend_client.py     # 后端 MCP 客户端与连接池
│   ├── launcher_client.py    # 启动器适配器（非 MCP：Quicker / uTools）
│   ├── discovery.py          # 本机工具发现（声明清单 + 只读探测，默认零 spawn）
│   ├── pkgman.py             # 包管理器适配层（scoop：读盘列包 / 搜 bucket / 解析入口 / 构造安装命令）
│   ├── runtime_paths.py      # 运行时目录解析 + 旧路径迁移
│   └── schema.py             # 两层数据的 JSON Schema 校验 + 文档生成源
├── scripts/
│   └── generate_format_doc.py  # 由 schema.py 生成 docs/format.md 的字段表
└── tests/                    # 517 个用例（另有 9 个真实 e2e 用例默认不收集，
                              # `pytest -m e2e` 显式跑；含真实 MCP 会话与文档漂移检测）
```

运行时数据（**不在仓库**，A14 起）：`%LOCALAPPDATA%\.awam-tool-manager/`
（可用环境变量 `AWAM_RUNTIME_DIR` 覆盖；server 启动时自动从旧路径迁移）

```
%LOCALAPPDATA%/.awam-tool-manager/
├── backend-tools/        # 后端 MCP 子工具缓存（自动生成）
├── launcher-actions/     # 启动器动作清单缓存（自动生成）
└── desired/              # 后端启动意图（每工具一个文件，A10 跨世代自愈用）
```

> 早期版本曾有 `data/launchers/`（启动器配置）与 `src/adapters/`（启动器适配器），
> 已在重构中移除，**相关概念不再存在于代码**，请勿参照旧版说明扩展。

## 对外 MCP 工具

后端相关工具统一使用 **`tool_id`** 指定目标工具（不存在 `mcp_tool_id` / `launcher_id`）。

| 工具名 | 参数 | 说明 |
|---|---|---|
| `search_tools` | `keyword`(必填)、`kind`、`status`、`publisher` | 按关键词搜索工具，可按类型/安装状态/发布者收窄 |
| `list_tools` | `kind`、`status`、`publisher` | 列出所有工具，可按类型/安装状态/发布者筛 |
| `get_tool_info` | `tool_id`(必填) | 查看工具详情 |
| `launch_tool` | `tool_id`(必填)、`execute`、`wait`、`allowDestructive` | 返回工具的启动命令；**默认只返回不执行**，显式 `execute=true` 才真正启动进程（条目 `destructive` 时需 `allowDestructive=true`，见「安全约定」） |
| `call_backend_tool` | `tool_id`、`tool_name`(必填)、`arguments`、`allowDestructive` | 路由调用后端 MCP 的子工具；命中破坏性判定时需显式确认（A12：条目配了 `destructiveTools` 就只拦清单内子工具，未配则整个后端都拦） |
| `list_backend_tools` | `tool_id`(必填)、`force_refresh` | 列出后端子工具（含参数 schema） |
| `start_backend` | `tool_id`(必填) | 启动后端并自动同步工具目录；启动命令缺失时**仅在显式授权后**才按 `install` 安装（未授权返回 `install_required`，见「安全约定」） |
| `stop_backend` | `tool_id`(必填) | 停止后端 |
| `get_backend_status` | `tool_id` | 查看单个或全部后端状态 |
| `get_backend_logs` | `tool_id`(必填)、`level`、`limit` | 查看后端日志（`limit<=0` 返回空） |
| `set_backend_log_level` | `tool_id`、`level`(必填) | 设置后端日志级别（低于该级别的日志不再入队/打印） |
| `sync_backend_tools` | `tool_id`(必填) | 手动同步子工具目录 |
| `list_launcher_actions` | `tool_id`(必填)、`keyword`、`limit`、`refresh` | 列出/搜索启动器（Quicker / uTools）的动作；返回带 `lastSynced`/`source` |
| `run_launcher_action` | `tool_id`(必填)、`action_id`、`action_name`、`param`、`wait`、`allowDestructive` | 执行启动器动作；Quicker 走命令行（有 stdout 回传），uTools 走 `utools://` 协议唤起（fire-and-forget 无回传）；条目标了 `destructive` 时需 `allowDestructive=true`（配了 `destructiveActions` 时按动作 id/name/type 的 glob 判定，仅命中的动作需确认） |
| `sync_launcher_actions` | `tool_id`(必填) | 手动同步启动器动作清单 |
| `get_tool_help` | `tool_id`(必填)、`run`、`timeout`、`allowDestructive` | 取工具帮助信息：文档链接 + 能力清单（mcp 子工具 / launcher 动作）+ 帮助命令预览；**默认只读数据、不 spawn**，`kind=app` 显式 `run=true` 才执行帮助命令（`--help` / 条目 `help.args`）并捕获输出 |
| `get_usage_stats` | `days`、`tool_id` | 查询调用使用统计（只读 `<runtime>/usage.log`，不碰任何工具）：按天 / 按对外工具 / 按目标 `tool_id` 聚合调用量、成败与耗时；`days` 按日历日含今天、默认 7、越界收敛到 1~365，`tool_id` 按记录里的 `target` 精确匹配 |
| `discover_tools` | `run_commands`、`status` | 按 `data/discovery.json` 声明的候选清单发现本机工具：`path_exists` / `registry_value` / `dir_count` / 命令是否在 PATH 四类**只读**探测，对照已登记工具输出 `status`（installed / not_installed / unknown）+ `confidence` + `registered`；**默认不 spawn**，`run_commands=true` 才执行清单里的版本命令取版本号（Windows 上按可执行后缀解析出真目标再无 shell 执行，子进程 `stdin=DEVNULL` 以保证不污染服务通道）；`status` 只裁剪返回的候选，`summary` 仍是整份清单的计数 |
| `list_packages` | `manager`、`state` | 列出包管理器（当前支持 `scoop`）已装的包：**纯读盘**，不跑 `scoop list`——`current` 断链时回落版本目录、bucket 取自 `install.json`、`manifest.bin` 缺失时用 `shims/<包名>` 兜底，三种情况如实标成 `state`（ok / current_broken / no_manifest）并给 `launchable`；`count`/`states` 恒为整份清单口径，`state` 只裁剪 `packages` |
| `search_packages` | `keyword`(必填)、`manager`、`limit` | 在已添加 bucket 的清单里搜可安装的包：按 manifest 文件名筛（本机 4 个 bucket / 4287 份清单），命中才读 JSON，**纯读盘、不 spawn**；排序为完全同名 → 前缀 → 包含，`limit` 默认 20、越界收敛到 1~100；无命中要说「已知 bucket 里没有」而不是「装不了」 |
| `launch_package` | `manager`、`package`(必填)、`entry`、`execute`、`wait` | 启动已安装的包：入口按 **快捷方式 → bin → shim** 取，只挑「文件在盘上且扩展名可直接 spawn」的候选（真机实测 `.ps1` 抛 WinError 193，不可直接起）；**默认只预览**（回 `path` / `kind` / 全部候选及其 `exists`/`runnable`），显式 `execute=true` 才与 `launch_tool` 走同一条 spawn 路径；不改动已装清单，故不要求 `allowDestructive`（但留门禁审计行） |
| `install_package` | `manager`、`package`(必填)、`execute`、`allowDestructive`、`timeout` | 安装一个包：默认只回将要执行的 `argv` 与 `alreadyInstalled`；真的装要**同时**传 `execute=true`（意图）+ `allowDestructive=true`（授权）——门禁 `kind=package` 是 fail-closed 的（不看条目标记，名单外操作一律要确认；策略 `destructive.mode=deny` 时客户端无法自解）；命令跑完再读回 `apps/<包>`，退出码 0 却读不到时明示「不要当成安装成功」 |

> `kind=launcher`（启动器，如 Quicker / uTools）不是 MCP：没有长连接，调用是一次性的。
> - **Quicker**：`QuickerStarter.exe -c<秒> "runaction:<ID或名称>?参数"`，stdout 回传动作
>   结果（失败时退出码仍是 0，按输出文本判定）。`source=partial` 表示部分动作只拿到 ID
>   （`quicker.db` 是加密的，名称只能从 state 缓存 + 运行日志尽力解析）。
> - **uTools**：`cmd start "utools://<URL编码插件名>/<URL编码关键词>[?<URL编码文本>]"`
>   协议唤起，**fire-and-forget 无结果回传**（结果只在 uTools UI，返回体带
>   `fireAndForget: true` 明示此事；payload 仅文本，文件类功能经 URL 不可达）。
>   动作清单从 `plugins/*.asar` 的 plugin.json 解析（纯 Python 读 Pickle 封包，
>   只读文件头 + 条目；声明完整，`source` 恒 `auto`）；`wait` 语义改为「投递后
>   观察窗口」（等 renderer 起来，默认取条目 timeout、封顶 5s）。
>   面板在投递后约 1s 才真正呈现（呈现后失焦不自动隐藏），所以「调用后看一眼屏幕
>   说没反应」不成立——要判断呈现必须连续采样，`success` 也只代表协议被系统接收。
>
> 动作清单缓存在运行时目录的 `launcher-actions/{tool_id}.json`。
>
> **这份清单是活的**：缓存超过 5 分钟（`ACTIONS_CACHE_TTL`）时，`list`/`run` 会
> 自动重扫一次再回答，`refresh=true` 可强制重扫，所以启动器里新增的动作无需手动
> 同步就能搜到、调到。刷新失败（如数据目录不可用）会静默沿用旧缓存，
> 不会让查询失败——看返回里的 `lastSynced` 就知道数据有多旧。

另有 3 个 Resource：`awam://tools`、`awam://mcp-servers`、`awam://backends/status`。

## 错误处理约定

调用失败时走 **MCP 协议层错误**（`isError=true`），文本内容是可解析的 JSON：

```json
{ "success": false, "errorType": "not_found", "error": "未找到工具: xxx" }
```

`errorType` 取值：`not_found` / `unknown_tool` / `unknown_resource` / `unknown_prompt`
/ `invalid_params` / `connection_error` / `runtime_error` / `config_error`
/ `destructive_blocked` / `permission_denied` / `backend_busy` / `backend_installing`
/ `internal_error` / `launcher_error` / `install_required` / `install_not_allowed`
/ `unknown_error`。

- **`invalid_params`**：参数缺失 / 类型错 / 取值非法。当前发射点：`set_backend_log_level`
  的 `level` 非法值（参数校验议题上线后将扩大启用面）。
- **`backend_busy`**：同一后端正在处理另一个调用（单后端串行，并发调用会被直接拒绝，不排队）。
  调用方应稍后重试，不要无限重试打满。
- **`backend_installing`**：后端依赖正在自动安装中——分钟级忙态，不是错误（与 `backend_busy`
  的秒级并发冲突分工不同，故单列）。**可重试**：轮询 `get_backend_status` 或稍后重试
  `start_backend`；不要并发重试。
- **`permission_denied`**：被部署策略拒绝（`data/policy.json` 的 `tools.deny` 名单、
  白名单未收录、或 `destructive.mode=deny` 命中破坏性判定）。**客户端无法自行解除**
  （与 `destructive_blocked` 区分：后者传 `allowDestructive=true` 即可），需管理员调整策略。
- **`install_required`**：后端依赖缺失，且**未授权**自动执行 `install.command`（默认即此状态）。
  见「安全约定」——开授权后重试 `start_backend` 即可。
- **`install_not_allowed`**：已授权，但 `install.command` 不在受支持的白名单内
  （含 shell 元字符或前缀不合法），已拒绝执行。改条目里的命令后重试。
- **`unknown_error`**：兜底值——后端抛出了既非 `BackendBusyError` / `ConnectionError` /
  `RuntimeError` 的未分类异常。出现它说明后端抛了未预期类型：看 `get_backend_logs`；可重试一次。

成功结果除文本通道（JSON 文本）外，还附带 **`structuredContent`**（dict），客户端无需
再解析文本即可按结构消费；每个工具的 `outputSchema` 统一声明输出含顶层 `success`（bool）。
依赖下限为 `mcp>=1.29`（A9 实测 1.29 已有 structuredContent / outputSchema /
CallToolResult 透传；生产环境 pyenv 3.12 即 1.29.0）。

## 安全约定

- 工具条目里的 `readOnly` / `destructive` 会映射到 MCP Tool annotations
- **破坏性门禁**（`call_backend_tool` 转发前 / `run_launcher_action` 执行前 /
  `launch_tool` 的 `execute=true` 启动前 / `get_tool_help` 的 `run=true` 执行帮助
  命令前 / `install_package` 的 `execute=true` 装包前）：
  条目未标 `destructive` → 不拦；标了之后命中判定才需显式传
  `allowDestructive=true`，否则返回 `destructive_blocked`。
  - mcp：配 `destructiveTools` 时只拦清单内的子工具（精确匹配），未配则整个后端都拦；
  - launcher：配 `destructiveActions`（新增可选字段，glob、大小写不敏感，匹配动作
    `id`/`name`/`type` 任一）时只拦命中的动作，未配则所有动作都拦。默认不启用——
    启用该字段等于放宽安全属性（未命中的动作由拦转放），须部署者按实际动作清单
    核对后显式配置。
  - package（`install_package` / `launch_package`）：**没有条目可标**，破坏性由操作
    本身定且方向是 fail-closed——只有一张「不需要授权」白名单
    `policy.PACKAGE_CONFIRMED_OPS`（当前仅 `launch_package`：起的是用户早就装好的
    程序，与 `launch_tool` 同级），名单外（`install_package` 及一切未登记的操作）
    一律要 `allowDestructive=true`。将来新增卸载 / 更新工具时忘了登记，代价是多一次
    确认，而不是静默免检。
- **部署策略（`data/policy.json`，ARC-P1-05 Phase 2）**：部署者的显式决定，随仓库分发、
  可评审；`AWAM_POLICY_FILE` 环境变量可覆盖。`tools.deny` 拒绝任意工具的调用、`tools.allow`
  非 null 时进入白名单模式（deny 优先）；`destructive.mode` 三态——`require_confirm`（默认，
  现行为）、`deny`（命中破坏性判定直接拒绝，**客户端传 `allowDestructive` 也不放行**）、
  `allow`（本机信任，跳过确认，须显式写）。被策略拒绝返回 `permission_denied`（客户端无解，
  需管理员改策略）。策略文件在服务启动时校验，坏策略让启动失败而非静默失效；无该文件时
  全部走现行为（零配置零变更）。破坏性判定审计（含策略判决）追加到运行时目录的 `audit.log`。
- 不合规的 `data/tools/*.json` 会被跳过并向 stderr 告警，不会让整个服务不可用
- **服务内任何 spawn 都必须 `stdin=DEVNULL`**：本服务通过 stdio 管道与上游网关通信，
  子进程若继承这条 stdin（Windows 上 `.cmd` / `.bat` 垫片经 cmd.exe 尤其如此），
  会**从协议通道里读走字节**，等价于吞掉入站 JSON-RPC——真机实测整个 server 随即退出。
  `backend_client.py` / `app_launcher.py` / `tool_help.py` / `discovery.py` /
  `pkgman.py` 五条 spawn 路径都按这条纪律走，新增第六条时不要漏
- **改完服务端代码要用 MCP 客户端验证前，先重启那条连接**：stdio server 是常驻进程，
  Python 只在 import 期读源码，不重启则拿到的仍是旧行为的结论
- **`install.command` 默认不被自动执行**（P0 修复）：它写在进版本库的数据文件里、
  且会经 shell 执行，旧逻辑让它在「服务启动预热」和 `start_backend` 两条路径上
  无条件运行——病根是「没有调用方意图」的执行，与本技能「副作用须由显式调用触发」
  的取舍正好相反（对照：`launch_tool` 默认只返回命令、`get_tool_help` 默认只取数、
  `discover_tools` 默认只读文件系统，只有显式 `execute=true` / `run=true` /
  `run_commands=true` 才真正 spawn 进程）。
  现在必须显式授权才执行，授权有两个来源（任一即可）：
  1. 环境变量 `AWAM_AUTO_INSTALL=1` —— 部署者在 MCP 客户端配置里显式打开；
  2. 条目 `install.auto: true` —— 逐工具 opt-in。
  授权之后还要过一道命令白名单：不含 shell 元字符（`&&` `;` `|` `$()` 等），
  且前缀属于 `npm i -g` / `winget install` / `pip install` 等受支持形式，
  否则返回 `install_not_allowed`。未授权时 `start_backend` 返回
  `install_required`（**不 spawn 子进程**；payload 的 `status` 为状态机值
  `error`，错误分类只看 `errorType`，二者分离），服务启动预热则退化为纯检测，
  只在 stderr 报告「谁缺依赖、缺什么」，不执行任何外部命令。

> 想恢复「启动即自动装」的旧行为：在 MCP 客户端配置的 `env` 里加
> `"AWAM_AUTO_INSTALL": "1"`。这是**部署者的显式决定**，不应由程序默认代劳。

## 新增工具步骤

1. 在 `data/tools/` 下新建 `{tool_id}.json`（字段见 `docs/format.md`）
2. 普通应用写 `kind=app` + `launch`；MCP 工具写 `kind=mcp` + `connection`。
   帮助命令与默认 `--help` 不同的应用（GUI 程序常为 `/?`）再补可选 `help`
   （`command` / `args` / `timeout`），`get_tool_help` 会优先按它取
3. 启动器写 `kind=launcher` + `launcher` 配置（`type=quicker`/`utools`、数据目录、
   超时；uTools 的 `dataDir` 可写 `%APPDATA%` 等环境变量，由适配器展开），
   动作清单由 `sync_launcher_actions` 从启动器本地数据自动生成
4. MCP 工具调用 `start_backend(tool_id=...)` 建立连接并自动同步子工具目录；
   启动命令不在 PATH 时**默认不安装**，返回 `install_required`——需要自动安装
   就设 `AWAM_AUTO_INSTALL=1` 或给条目配 `install.auto: true`（见「安全约定」）

> 修改已有工具的 `connection` 时，若后端当前是停止/错误状态，**下次调用会用新配置重建连接**，
> 无需重启 MCP Server；运行中则不受影响（避免留下孤儿进程），需先 `stop_backend` 再 `start_backend`。
> 坏掉的 JSON 会被跳过并打印 stderr 告警，不会导致整个服务不可用。

> **后端状态跨进程自愈（A10）**：`start_backend` 成功会把启动意图落盘（运行时目录的
> `desired/{tool_id}.json`，每工具一个文件）。上游网关（toolport）会周期性整池回收它拉起的 MCP server 进程，回收后新进程内存状态清零——
> 有了意图落盘，每个新进程启动时会**自动恢复**上次启动过的后端，用户任何时刻查状态都是 running；
> `stop_backend` 会清除意图。若后端业务依赖本机应用（如 everything-search-mcp 依赖 Everything
> 桌面程序的 IPC），业务成败取决于该应用是否运行，与连接管理无关。

> **新增/修改字段时**：只改 `src/schema.py`，再跑 `python scripts/generate_format_doc.py`
> 更新 `docs/format.md` 的字段表。直接手改文档会被 `tests/test_docs_sync.py` 判为漂移。

## 依赖约束

`requirements.txt` 锁定 `mcp>=1.29.0,<2.0.0`（A9 实测 1.29 已支持 structuredContent /
outputSchema 与 CallToolResult 透传；心跳探测用 `send_ping()`，1.x 全版本可用）。
**mcp 2.x 移除了本项目依赖的低层装饰器 API，不可直接升级**，迁移需重写 `server.py`。

## 数据格式

详见 `docs/format.md`
