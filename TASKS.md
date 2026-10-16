# awam-tool-manager 任务清单

统一工具管理器 - 维护启动器工具集名单，提供统一的工具注册、搜索、启动、路由调用能力。

---

## 一、项目目标

打造一个统一的"工具入口层"：
1. **注册**：统一管理各种启动器（Steam、WinGet、Everything、uTools 等）下的子工具
2. **搜索**：AI 可以按关键词搜索所有已注册的工具
3. **路由**：智能转发调用到对应的后端 MCP Server 执行
4. **启动**：返回工具启动信息，或直接执行启动

最终效果：说"我想玩黑神话" → 自动找到 Steam 里的游戏并启动；说"搜索 XX 文件" → 路由到 Everything 执行搜索。

---

## 二、已完成 ✅

### 基础框架
- [x] 两层数据格式设计（工具条目 / 后端子工具缓存）
- [x] JSON 存储层（store.py）
- [x] 工具注册中心（registry.py）：搜索、列出、详情、启动信息
- [x] MCP Server 入口（server.py）：stdio 模式
- [x] 日志系统（输出到 stderr，不污染协议通道）

### 对外 MCP 工具（12 个）
- [x] `search_tools` - 搜索工具（关键词 + kind 筛选）
- [x] `list_tools` - 列出所有工具
- [x] `get_tool_info` - 查看工具详情
- [x] `launch_tool` - 返回启动命令信息
- [x] `call_backend_tool` - 路由调用后端 MCP 的子工具
- [x] `list_backend_tools` - 列出后端子工具（含完整参数 schema）
- [x] `start_backend` / `stop_backend` / `get_backend_status` - 后端进程管理
- [x] `get_backend_logs` / `set_backend_log_level` - 后端日志
- [x] `sync_backend_tools` - 同步后端子工具目录到本地

### 协议合规
- [x] 后端工具参数名统一为 `tool_id`（移除历史遗留的 mcp_tool_id / launcher_id）
- [x] `read_resource` 参数归一化：SDK 传的是 `AnyUrl`，直接与字符串比较会全部落到未知资源
- [x] `get_prompt` 按协议返回 `GetPromptResult`（老版本 SDK 退回裸列表）
- [x] 失败返回 MCP 协议层 `isError`（此前 success:false 被协议层当成成功）
- [x] `read_resource` 返回 `Iterable[ReadResourceContents]`（此前返回 TextContent 会报错）
- [x] `requirements.txt` 锁定 `mcp<2.0.0`（2.x 移除了本项目的低层装饰器 API）

### 路由能力
- [x] BackendClient - 单个后端 MCP 客户端（懒加载连接）
- [x] BackendManager - 后端连接池管理
- [x] `call_backend_tool` - 路由转发到后端 MCP 执行
- [x] `list_backend_tools` - 列出后端 MCP 提供的工具

### 稳定性
- [x] Tool 目录同步失败时不会用空结果覆盖已有缓存
- [x] 连接清理：client / stdio 各自独立 try + 独立清理锁（串行幂等）
- [x] 后端调用统一超时（默认 30s，可用 `connection.timeout` 覆盖）
- [x] start / stop 串行化，start 前清理上次残留连接
- [x] 心跳失败与调用超时都会释放连接，不再留孤儿子进程
- [x] **依赖自装**：`install.command` 接入执行——启动命令缺失时自动安装（限时、只装一次、
      装完复检），服务启动时后台预热；安装失败不阻断启动
- [x] JSON 读容错：坏文件 / 非对象文件跳过并告警，不再拖垮整个服务
- [x] JSON 原子写：临时文件 → `fsync` → `os.replace`，崩溃不会留下半截 JSON
- [x] `get_backend_logs` 的 `limit<=0` 返回空列表（此前 `-0 == 0` 会返回全部）
- [x] `_log()` 按日志级别过滤后再入队（此前 debug 心跳会挤掉 error）
- [x] `connection` 配置热更新：后端空闲时按磁盘最新配置重建，无需重启 MCP Server
- [x] `lastSynced` 写真实 UTC 时间戳（此前是字面量 `"now"`）

### 连接生命周期与状态机（2026-10-16 批次，A7–A11）
- [x] **A7** - 5 处 `asyncio.wait_for` 改 `anyio.fail_after`（同一任务内取消，不撕裂
      作用域栈）；`ensure_npm_bin_on_path()` 在 npm 全局 bin 不在 PATH 时自动解析
      `npm prefix -g` 并注入；安装锁等待加超时（`start_backend` 不再被别人的
      `npm i -g` 无限期堵住）
- [x] **A8** - 后端连接生命周期收拢进独占任务 `_connection_loop`：`stdio_client` /
      `ClientSession` 的 `async with` 进出都在同一任务内闭合（anyio `CancelScope`
      绑定进入它的任务，跨任务进出必崩）；子进程 spawn 一律 `stdin=DEVNULL`，
      不再继承 server 的 stdio 管道（npm 子进程阻塞读 stdin 是此前启动超时的真因）
- [x] **A9** - 心跳探测 `session.ping()` → `send_ping()`（mcp 1.x 唯一存在的心跳 API，
      `ping` 是 TS SDK 的名字，此前启动 30s 后必 `AttributeError` 被清理）；
      补契约测试（探测方法必须真实存在于 `mcp.ClientSession`）+ 假 session 改用
      真实 SDK 表面白名单
- [x] **A10** - 期望状态持久化 + 跨 server 世代自愈：`start_backend` 成功把意图写入
      `backend_desired_state.json`（原子写、损坏按空处理），每个新 server 世代启动时
      后台任务据意图自动拉起，`stop_backend` 清意图、工具删除清过期记录
      （网关周期性整池回收 → 进程内状态清零，是「每次都得重新初始化」的根因）
- [x] **A11** - 启动状态机的取消安全：`CancelledError` 继承 `BaseException`，
      原 `except Exception` 接不住 → 取消落在 `starting` 之后状态永久卡死无自愈；
      补 `except BaseException` 兜底（状态落地 stopped + 收尸连接/心跳，取消继续上抛不吞）
- [x] 依赖下限经实测从 `mcp>=1.30.0` 改回 `>=1.29.0`（1.29 已具备
      structuredContent / outputSchema / CallToolResult 透传，生产 pyenv 3.12 即 1.29.0，
      原 1.30 下限建立在错误前提上）

### 语义一致性
- [x] `search_tools` 匹配范围覆盖 `id`（此前搜不到 ID 里带关键词的工具）
- [x] `docs/format.md` 明确「两层数据 + 一层对外契约」，补齐 `install` / `actions` / `capabilities`
- [x] `launch_tool` 描述改准确（只返回命令，不实际执行），失败带 `errorType`
- [x] README / format.md / SKILL.md 的工具命名与数据一致（`everything-search-mcp`）

### 合规与安全
- [x] `src/schema.py` - 两层数据的 JSON Schema 校验（`jsonschema` 可选依赖，缺失时降级）
- [x] 加载时跳过不合规条目并向 stderr 告警（缺 `id` 不再让 tools/list 整体 KeyError）
- [x] **字段规范单一真相源**：`docs/format.md` 的字段表由 `scripts/generate_format_doc.py`
      从 schema 生成，手写改动会被 `tests/test_docs_sync.py` 判为漂移
- [x] `validate_backend_catalog()` - 第二层数据（后端子工具缓存）的结构校验
- [x] Tool annotations：只读工具 `readOnlyHint`，启停类 `idempotentHint`，转发类 `openWorldHint`
- [x] `call_backend_tool` 的 destructive 门禁（`allowDestructive`，默认拒绝）
- [x] **A12 子工具级破坏性门禁** - 破坏性实际属于「子工具」而非「整个后端」：
      新增 `destructiveTools` 字段（schema + `docs/format.md` 自动同步），配置后只拦截
      清单内的后端子工具，未配置则回退条目级 `destructive` 语义；拦截判定与
      warning 标注共用 `_needs_destructive_confirm`，避免免确认调用被误标

### 测试覆盖
- [x] `tests/test_registry.py` - store / registry 基础路径
- [x] `tests/test_backend_client.py`（56 用例）- 稳定性、P2 修复、BackendManager 池管理、依赖自装、
      连接生命周期、期望状态持久化、启动状态机取消安全
- [x] `tests/test_store.py`（8 用例）- 容错、原子写、时间戳
- [x] `tests/test_server_dispatch.py`（24 用例）- 12 条分发路径、错误载荷、资源/提示定义、启动预热
- [x] `tests/test_registry_policy.py`（16 用例）- 搜索语义、schema 校验、annotations、
      条目级与子工具级 destructive 门禁
- [x] `tests/test_protocol_roundtrip.py`（9 用例）- **真实 MCP 会话**：isError 映射、
  resources/read、prompts/get、annotations 下发、structuredContent / outputSchema（不 mock SDK）
- [x] `tests/test_backend_client.py` 补 BackendManager：错误归类、`close_all`、缓存 TTL
- [x] `tests/test_server_dispatch.py` 补 `AnyUrl` 参数、resources 与 prompts 定义回归
- [x] `tests/test_docs_sync.py`（12 用例，`data/` 文件数变化时会增减）- 文档与 schema 一致、
      示例过校验、`data/` 真实文件全部过校验且 `id` 与文件名一致
- [x] **A3 并发限流（方案 2：单后端串行 + 超限报 busy）** - `BackendClient._call_lock` +
      `BackendBusyError` → `errorType=backend_busy`（不排队）；并发两个 `call_tool`
      一个成功、一个 busy 的回归已钉住
- [x] **A5 structuredContent / outputSchema**（依赖下限经 A9 实测后为 `mcp>=1.29`） - `call_tool` 返回带
      `structuredContent` 的 `CallToolResult`，`list_tools` 给每个工具挂统一 `outputSchema`
      （`SHARED_OUTPUT_SCHEMA`）；`test_protocol_roundtrip.py` 验证客户端真实收到
      structuredContent 且 outputSchema 校验通过（无 `Output validation error`）
- [x] **A6 后端依赖自动安装 + 启动预热** - `install` 元数据从死数据变为真能力：
      `BackendClient.ensure_installed()`（壳包裹命令剥壳解包 → `shutil.which` 检测 →
      限时 `INSTALL_TIMEOUT` 安装一次 → 复检，幂等）+ `start()` spawn 前前置调用 +
      服务启动时 `BackendManager.ensure_all_installed()` 后台预热（只装不启、异常全吞）；
      安装失败不阻断启动（spawn 原生错误照旧）；已用临时探针工具实测全链路
- [x] **A11 回归** - `test_start_cancelled_does_not_strand_starting_status`：先在旧代码复现
      FAILED、修复后 passed（红绿双向验证）
- [x] 修掉 flaky：`test_tools_cache_respects_ttl_and_force_refresh` 依赖 `time.time()`
      相邻两次返回不同值（Windows 刻度约 15ms，可能同值 → `cache_age=0.0` 误判命中），
      改为显式回拨 `_tools_cache_time`，不再依赖时钟精度（10 连跑确定性通过）
- [x] `tests/test_launcher_client.py`（20 用例）- 启动器动作清单解析（含「必须解析 JSON
      再取字符串」的回归）、关键词搜索、调用命令构造（`-cN` / `runaction:` / `?参数` /
      stdin 隔离）、GBK 解码与「退出码为 0 但输出说失败」的判定、执行确认门禁
- [x] 合计 **151 passed**（8 个文件）；真实后端子进程启停 + 调用回归通过

### 启动器接入（`kind=launcher`，2026-10-16）
- [x] **新增 `kind=launcher` 工具类型** - 启动器不是 MCP，但有可列/可搜/可调用的动作：
      `launcher` 配置（适配器类型 / 命令 / 数据目录 / 超时）+ 第二层数据
      `data/launcher-actions/{tool_id}.json`（与 backend-tools 同构）
- [x] **新增 3 个对外工具**（12 → 15）：`sync_launcher_actions` / `list_launcher_actions`
      / `run_launcher_action`；执行受 `destructive` 门禁（需 `allowDestructive`），列/搜不受限
- [x] **Quicker 适配器**（`src/launcher_client.py`）- 调用走
      `QuickerStarter.exe -c<秒> "runaction:<ID或名称>?参数"`；动作 ID 来自
      `states/state_<uuid>.json` 文件名，名称来自其 state 缓存的动作列表（尽力而为，
      缓存没覆盖到的只有 ID，`source=partial`）

### 工具接入
- [x] Everything（`kind=app`，返回启动命令）
- [x] Everything Search MCP（`kind=mcp`，已实测连接、调用、启停跑通）
- [x] **WinGet（`kind=mcp`，官方内置）** - 实测：握手（winget-mcp 1.29.380 / 协议 2025-06-18）、
      2 个子工具带完整 schema、查询与安装双路径路由验证
- [x] **Toolport 集成** - 已注册为 Toolport 的后端（registry `source: manual`，default profile），
      daemon 真实链路实测：启停、状态、子工具列表、路由调用全通
- [x] **WorkBuddy 直连 MCP** - `~/.workbuddy/mcp.json` 新增 `awam-tool-manager` 条目
      （pyenv 3.12.0 `python.exe` + `src/server.py`）；实测 12 个工具就位、
      A10 在新实例上 4 秒内自动恢复后端、`search_files` 端到端返回真实索引结果

### 其他
- [x] Git 仓库初始化，代码版本管理
- [x] .gitignore 配置（测试脚本、缓存文件排除）

### 已知边界（非缺陷，记录备查）
- 真正的「后端永不重启」需要外部 supervisor（后端生命周期移出网关控制的进程），
  当前只能做到「跨网关回收自愈」（A10），列为架构演进
- 硬杀 server 会孤儿化后端子进程（下一代自动恢复会另起一个，stdio 无端口冲突可共存）
- `data/backend-tools/*.json` 是自动生成的缓存却受 git 跟踪，多实例在跑时 `lastSynced`
  会被反复改写（**待定**：要不要 gitignore 需权衡——`tests/test_docs_sync.py` 对该目录
  `glob("*.json")` 做参数化，目录为空时用例会静默消失，等于丢失这条真实数据校验）
- 上游 `everything-search-mcp` 返回体 `path` 为空、`isFile`/`isFolder` 恒 false
  （全路径塞在 `name` 里）——属上游包自身的数据整形，转发层原样透传；消费方以 `fullPath` 为准
- **Quicker 动作名是尽力而为的**：动作正文在加密的 `quicker.db`（非 SQLite 魔数），
  名称只能从 state 缓存里解析，缓存没覆盖的动作只剩 ID（`source=partial`，本机实测
  212 个动作里 144 个有名称）。只有 ID 也能调用，但搜索会漏——想补名称只能人工登记
- **Quicker 调用的成功路径尚未真实执行验证**：只用不存在的动作 ID 验证了错误路径
  （证明命令构造、GBK 解码、失败判定都正确），真实执行会跑用户动作，需授权后再冒烟

---

## 三、待完成 ⏳

### P0 - 核心打通
- [ ] 接入 Steam（steam-local-mcp）— ⚠️ 本机未安装 Steam，无法端到端实测
- [ ] 接入 npm（npm MCP）— ⚠️ 需求待澄清：npm 官方无 MCP server，需明确接哪个包
- [ ] launch_tool 真正执行启动（目前只返回命令）

### P1 - 体验完善
- [ ] 工具自动发现：检测本机已安装哪些可作为 MCP 后端接入的工具
- [ ] 工具标签分类和筛选优化（`kind` / `tags` 之外的维度）
- [ ] 工具帮助文档查看（--help 输出）

### P2 - 生态扩展
- [ ] 接入 Scoop（写 CLI 适配器）— 本机已装（`S:\Libs\Scoop`），可实测
- [ ] 接入 WeGame（腾讯平台）
- [ ] 接入 uTools（效率启动器）— ⚠️ 本机未安装
- [x] 接入 Quicker — ✅ 已完成（见「启动器接入」）：实测同步 212 个动作、144 个有名称，
      关键词搜索命中（"搜索"→5 条 / "剪贴"→2 条），调用链路通（错误路径已验证）
- [ ] 接入 PowerToys — 本机已装，可实测

### P3 - 高级功能
- [ ] 智能路由：根据用户意图自动选择从哪个安装器安装
- [ ] 版本检测和更新提醒
- [ ] 工具使用统计
- [ ] 权限控制（哪些工具 AI 可以直接执行）

---

## 四、数据格式规范

### 4.1 两层结构

| 层级 | 文件位置 | 说明 |
|---|---|---|
| 工具条目 | `data/tools/{tool_id}.json` | 每个工具一个文件，文件名即 tool_id |
| 后端子工具缓存 | `data/backend-tools/{tool_id}.json` | 由 `sync_backend_tools` 自动生成 |
| 对外 MCP 工具 | `src/server.py` | 暴露给 AI 客户端的工具列表 |

> 早期设计中的「启动器配置层」`data/launchers/` 与「适配器层」`src/adapters/` 已废弃移除。
> MCP 现在是**工具的一种类型**（`kind=mcp`），而非独立层级，
> 所有对外工具统一用 `tool_id` 指定目标。

### 4.2 工具条目（`data/tools/{tool_id}.json`）

```json
{
  "id": "everything-search-mcp",
  "name": "Everything Search MCP",
  "displayName": "Everything 文件搜索 MCP",
  "kind": "mcp",
  "status": "installed",
  "description": "Everything 文件搜索的 MCP 接口",
  "version": "latest",
  "publisher": "community",
  "tags": ["文件搜索", "MCP", "系统工具"],
  "launch": {
    "type": "executable",
    "command": "C:\\...\\Everything.exe",
    "args": []
  },
  "connection": {
    "type": "stdio",
    "command": "cmd.exe",
    "args": ["/c", "everything-search-mcp"]
  },
  "install": { "method": "npm", "command": "npm i -g everything-search-mcp" },
  "update": { "method": "winget", "packageId": "voidtools.Everything" },
  "readOnly": false,
  "destructive": false,
  "createdAt": "2026-10-15T16:50:00Z",
  "updatedAt": "2026-10-15T16:50:00Z"
}
```

字段要点：

- `kind`：`app`（普通应用）/ `mcp`（MCP 工具），决定是否可建立后端连接、决定 `get_tool_info` 的返回形状
- `launch`：仅 `kind=app` 有意义，`launch_tool` 据此返回启动命令
- `connection`：仅 `kind=mcp` 有意义，`BackendClient` 据此拉起子进程；目前只支持 `stdio`
- `install`：仅 `kind=mcp` 有意义，`start_backend` 检测到启动命令缺失时会自动执行一次 `install.command`

### 4.3 后端子工具缓存（`data/backend-tools/{tool_id}.json`）

由 `sync_backend_tools` 写入，`tools[]` 直接透传后端 MCP 的 `inputSchema`：

```json
{
  "parent": "everything-search-mcp",
  "lastSynced": "2026-10-15T18:00:00Z",
  "source": "auto",
  "tools": [
    { "name": "search_files", "description": "...", "inputSchema": { "...": "JSON Schema" } }
  ]
}
```

> 完整规范详见：[docs/format.md](docs/format.md)

---

## 五、目录结构

```
awam-tool-manager/
├── TASKS.md                    # 本文件（任务清单）
├── README.md                   # 项目说明
├── SKILL.md                    # 技能说明
├── requirements.txt            # 依赖（mcp>=1.29.0,<2.0.0；A9 实测后从 1.30 改回 1.29）
├── backend_desired_state.json  # 运行时生成：后端启动意图（A10 跨世代自愈用，已 gitignore）
├── docs/
│   ├── format.md               # 数据格式规范
│   ├── architecture.md         # 架构说明
│   ├── mcp-conformance.md      # MCP 协议一致性对比
│   └── code-review.md          # 代码审核报告
├── data/
│   ├── tools/                  # 工具条目
│   │   ├── everything.json
│   │   ├── everything-search-mcp.json
│   │   ├── winget-mcp.json
│   │   └── quicker.json        # kind=launcher
│   ├── backend-tools/          # 后端 MCP 子工具缓存（自动生成）
│   │   ├── everything-search-mcp.json
│   │   └── winget-mcp.json
│   └── launcher-actions/       # 启动器动作清单缓存（自动生成）
│       └── quicker.json
├── src/
│   ├── server.py               # MCP Server 入口 + 对外工具定义 + 分发
│   ├── store.py                # JSON 存储层
│   ├── registry.py             # 工具注册中心
│   ├── backend_client.py       # 后端 MCP 客户端与连接池（stdio）
│   ├── launcher_client.py      # 启动器适配器（非 MCP：Quicker）
│   └── schema.py               # 两层数据的 JSON Schema 校验 + **文档生成源**
├── scripts/
│   └── generate_format_doc.py  # 由 schema.py 生成 docs/format.md 的字段表
└── tests/
    ├── test_store.py
    ├── test_registry.py
    ├── test_registry_policy.py
    ├── test_server_dispatch.py
    ├── test_backend_client.py
    ├── test_launcher_client.py
    ├── test_protocol_roundtrip.py   # 真实 MCP 会话，不 mock SDK
    └── test_docs_sync.py            # 文档/示例/data 与 schema 的一致性
```

运行：`python -m pytest -q`（未装 pytest-asyncio，异步用例统一用 `asyncio.run`）

**改字段的流程**：只改 `src/schema.py` → `python scripts/generate_format_doc.py`
→ 提交。手写改 `docs/format.md` 的字段表会被测试判失败。
