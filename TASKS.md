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

### 对外 MCP 工具（15 个）
- [x] `search_tools` - 搜索工具（关键词 + kind 筛选）
- [x] `list_tools` - 列出所有工具
- [x] `get_tool_info` - 查看工具详情
- [x] `launch_tool` - 返回启动命令信息；2026-10-18 起支持显式 `execute=true` 真正执行启动（默认仍只返回命令）
- [x] `get_tool_help` - 工具帮助信息：文档链接 + 能力清单（mcp 子工具 / launcher 动作）+ 帮助命令预览；2026-10-18 新增，`kind=app` 显式 `run=true` 才执行 `--help` 并捕获输出（默认零副作用）
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
      warning 标注共用同一判定结果（P1-05 起收敛为 `policy.decide_destructive`），
      避免免确认调用被误标
- [x] **P1-05 Phase 1 动作级门禁**（2026-10-17）- 破坏性判定收敛到统一入口
      `policy.decide_destructive`（判定表 + 结构化审计行 decision/rule_hit/client_declared）：
      `run_launcher_action` 支持 `destructiveActions`（glob，大小写不敏感，匹配动作
      id/name/type），命中才拦；未配置回退条目级全拦（现语义不回退）；未配置新字段时
      与旧行为逐字等价（`tests/test_policy_gate.py` 冻结旧逻辑对照锁定）；
      `data/tools/quicker.json` 不启用该字段（默认行为零变更）
- [x] **P1-05 Phase 2 部署策略**（2026-10-17）- `data/policy.json`（`AWAM_POLICY_FILE`
      可覆盖）：`tools.deny`/`tools.allow`（deny 优先、allow=null 不启用白名单）+
      `destructive.mode`（deny 时策略赢过客户端 `allowDestructive`——H1 修复；
      require_confirm 默认；allow 跳过确认）；dispatch 前置 `policy.check_call`；
      新增 errorType `permission_denied`（16→17，客户端无解需改策略）；
      策略在 import 期校验（坏策略快速失败）；判定审计追加 `<runtime>/audit.log`；
      无策略文件时零变更（`tests/test_policy_config.py` 13 条含 H1 验收）

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
- [x] **A13 动作清单自动刷新** - 此前 `list_actions` 只读盘、写盘只发生在手动 sync，
      是事实上的死列表（`lastSynced` 写了却没人读）。修复：`load_launcher_catalog`
      暴露时间戳 + `_ensure_fresh` 按 `ACTIONS_CACHE_TTL`(300s) 判定过期自动重扫；
      `refresh=true` 强制重扫；`find_action` 未命中且缓存陈旧时重扫再找（新动作首次
      调用即可命中）；刷新失败静默沿用旧缓存；对外返回带 `lastSynced`/`source`
- [x] **动作名称第二个来源：运行日志** - Quicker 每次执行动作会写
      `执行动作：<名称> id=<uuid>`（含轮转日志 .1~.5），只补空不覆盖；本机实测补全
      16 个名称（144 → 160 / 212）。另排查 `*_searchhistory.db`（仅 34 条、带二进制
      噪音，是搜索历史不是全量索引），未采用
- [x] `tests/test_launcher_client.py`（55 用例）- 启动器动作清单解析（含「必须解析 JSON
      再取字符串」的回归）、关键词搜索、调用命令构造（`-cN` / `runaction:` / `?参数` /
      stdin 隔离）、GBK 解码与「退出码为 0 但输出说失败」的判定、执行确认门禁、
      A13 自动刷新 7 例（陈旧一律改写 `lastSynced` 制造，不依赖时钟刻度）、
      uTools 适配器 26 例（U4 22 例：asar Pickle 封包现场构造与解析 / 协议 URL 编码 /
      fire-and-forget 语义 / 观察窗口封顶；QA 加固 4 例：URL 斜杠编码 / asar 对齐 pad）
- [x] `tests/test_runtime_paths.py`（10 用例）- 运行时目录解析：默认
      `%LOCALAPPDATA%\.awam-tool-manager`、`AWAM_RUNTIME_DIR` 覆盖、旧路径数据
      一次性迁移（含目标已存在时不重复搬）、迁移失败不炸启动
- [x] **A14 运行时数据迁出仓库** - `backend-tools/`、`launcher-actions/`、
      `backend_desired_state.json` 是运行时生成数据却受 git 跟踪，多实例并跑被
      反复改写。修复：`src/runtime_paths.py`（runtime dir 解析 + 旧路径迁移），
      `JsonStore` 拆双目录（`tools/` 留仓库，缓存走 runtime dir），server 启动时
      自动迁移（本机实测 4 文件平移）；conftest autouse fixture 把用例隔离到
      tmp 目录，`test_docs_sync`/`test_registry` 两处「把数据集当断言」改为
      合成数据/自包含
- [x] 合计 **168 passed**（`c40d80d` 时点签署，9 个贡献用例的文件；P0 修复后为 183，见下节）；
     真实后端子进程启停 + 调用回归通过

> 文件数口径（2026-10-18 更新）：`tests/` 下有 **15 个** `test_*.py`（贡献 pytest
> 用例），另有 `tests/e2e/` 下的 **7 个** e2e 用例文件——它们**默认不收集**
> （`pytest.ini` 的 `addopts = -m "not e2e"`），须 `pytest -m e2e` 显式跑。
> `tests/e2e/manual_install_gate.py`（原 `tests/test_e2e_install_gate.py`，C 期
> 改名并去掉 `test_` 前缀）是**手工脚本**，依赖本机 npm，不进 pytest。

#### 真实 e2e 用例的跑法（ARC-P3-02，2026-10-17 新增）

**发布 / CI 前必须显式跑一次**：

```
"C:\Users\Administrator\.pyenv\pyenv-win\versions\3.12.0\python.exe" -m pytest -m e2e -q
```

- **为什么默认不跑**：这一层起**真实子进程 + 真实 MCP 会话**（起服、拉后端、
  拆进程），单场景 2~6s、全量约 20~30s，且依赖本机解释器与 mcp SDK；放进默认
  收集会让日常回归从 6s 变成 30s，还会把本机环境差异带进每次提交。
  默认排除由 `pytest.ini` 的 `addopts = -m "not e2e"` 实现，命令行的 `-m e2e`
  会覆盖它。
- **当前 8 个场景**：① 起服 + `tools/list` ≡ golden、② 只读工具真调、
  ③ 仓内假后端全链路（含无残留子进程断言）、④ resources / prompts 往返、
  ⑤ policy deny 端到端（后端零调用）、⑥ install 门禁未授权不执行、
  ⑦ `launch_tool` 的 `execute=true` 真启动（含 destructive 门禁双向）、
  ⑧ `get_tool_help` 的默认零副作用 / 显式 `run=true` 才 spawn。
- **场景② 取证加固（QA #29，2026-10-18）**：原写法用「不存在的 exe 探针」验证
  `launch_tool` 只返回不执行，但违约子进程从未真正 spawn，正常与违约两条路都
  「零证据」——测试实际什么都没验。根因：子进程继承 server stdio 管道 + Windows
  mcp 客户端 Job Object `KILL_ON_JOB_CLOSE` + 本沙箱子进程慢 ~10x。改为**双轨取证**：
  快通道（宽限窗口内 marker 文件出现 = 命令真被执行）+ 慢通道（marker 没出现再
  WMI 扫进程表补刀）；探针脚本与被跑证据文件都复制进 tmp、绝不落仓库。
  自然（继承管道）与重定向两种违约形态均已验证破坏即红
- **场景⑦ 新增（2026-10-18，配合 P0「launch_tool 执行启动」）**：验显式
  `execute=true` 的新副作用面——ok 探针真跑并落 marker（退出码 0）、fail 探针
  退出码 3 → `isError` + `runtime_error`（与「命令不存在」的 `config_error`
  可区分）、destructive 条目无 `allowDestructive` → `destructive_blocked` 且
  **当场**取证 marker 不出现（门禁在 spawn 之前），显式确认后放行并真执行。
  红绿验证：拆掉 destructive 门禁 → 场景⑦ 红（`:171`）；把默认路径改成也
  spawn → 场景②与⑦同时红（默认只返回命令的硬契约由两处双重锁定）；还原后全绿
- **场景⑧ 新增（2026-10-18，配合 P1「工具帮助文档查看」）**：`get_tool_help`
  是第三条能 spawn 的路径，取证形状与⑦同款——探针脚本 `tests/e2e/help_probe.py`
  既打 stdout（证明输出真被捕获回传）又写 marker（证明进程真诞生）；默认路径当场
  断言 marker 不出现；destructive 条目拦截时断言 marker 不出现**且**拒绝文案说的是
  「执行帮助命令」；另外覆盖两种不 spawn 的分支（`kind=launcher` 只回数据层 + note、
  无 `launch`/`help` 来源 → `config_error`）。
  红绿验证：默认路径改成也执行 → 场景②⑦⑧ 同时红（3 条单测一并红）；
  「帮助参数回落 `launch.args`」的变异 **e2e ⑧ 抓不到**（该条目显式配了
  `help.args`，走的是另一分支）→ 补单测
  `test_resolve_help_command_falls_back_to_double_dash_not_launch_args` 后红→绿。
  destructive 门禁摘除的变异本轮被权限策略拦下未跑（同款摘除已在场景⑦验过，
  help 路径的门禁由单测 `test_handler_destructive_entry_blocks_run_until_allowed`
  与场景⑧的断言锁定）
- **口径**：`pytest tests/ -q` = 303 passed / **8 deselected**；
  `pytest -m e2e -q` = 8 passed / 303 deselected。说「303」时要带上
  「另有 8 个 e2e 默认不收集」，否则会被误读成总数。
- **手工脚本**（不在上面 8 个里）：
  `"<pyenv 3.12 python>" tests/e2e/manual_install_gate.py`——验「已授权时
  install.command 真的被提交给 shell」，依赖本机 npm，故保留手工。

### 架构评审与 P0 修复（2026-10-16 深夜 评审 / 2026-10-17 修复）

背景：用户要求「检查项目架构以及软件原理有没有问题」。走了双线独立评审——
架构师（高见远）出 `docs/architecture_review_2026-10-16.md`，QA（严过关）出
`docs/test_health_review_2026-10-16.md`（实测 168 passed、覆盖率 83%、4 条红绿验证）。

- [x] **ARC-P0-01 修复：`install.command` 执行门禁**（用户拍板维持 P0）
      - 病根：`install.command` 写在**进版本库**的 `data/tools/*.json` 里，旧逻辑让它在
        「服务启动预热」和 `start_backend` 两条路径上被无条件
        `create_subprocess_shell` 执行——来源不可控、无任何
        `destructive`/`allowDestructive` 门禁，与「`launch_tool` 只返回命令不执行」
        的取舍正好相反（最小权限倒置 + 本地命令执行面）。
      - 三段式门禁（全收在 `BackendClient.ensure_installed()`）：
        ①**授权**：`AWAM_AUTO_INSTALL=1`（部署者显式开）**或**条目 `install.auto=true`，
        两者都缺就不执行；②**白名单**：无 shell 元字符（`&&` `;` `|` `$()`…）+
        前缀命中 `INSTALL_COMMAND_ALLOWLIST`（`npm i -g` / `winget install` /
        `pip install` 等）且前缀后跟有包名；③**失败可见**：被拦下且依赖确实缺失时
        `start()` **不 spawn**，返回带 `errorType` 的结构化失败。
      - 新增 `errorType`：`install_required` / `install_not_allowed`（SKILL.md 枚举已补全）。
      - 启动预热**退化为纯检测**：只读 PATH、不执行任何外部命令，stderr 报告
        「谁缺依赖、缺什么、怎么开授权」。
      - `schema.py` 加 `install.auto`（boolean，默认 false），`docs/format.md` 由
        生成器同步。
      - 测试 168 → **183 passed**：新增 15 条门禁用例；既有 A6/A7 用例补显式授权。
        红绿验证：拆掉授权门禁 → 6 条变红；拆掉白名单 → 2 条变红；还原后全绿。
      - **真实 stdio 端到端验证**（QA 指出的测试盲区，手工补）：
        `tests/test_e2e_install_gate.py` 用真实子进程 + 真实 MCP 会话跑两轮——
        未授权返回 `install_required` 且 stderr 无「执行安装」；授权后命令确实进 shell。
        > 2026-10-17（#27 C 期）：该脚本已按设计 §5 **拆分**——「未授权」那轮
        > 迁进自动化（`tests/e2e/test_e2e_install_gate.py`，探针写 tmp 副本），
        > 「已授权 → npm 真跑」保留手工并改名 `tests/e2e/manual_install_gate.py`
        > （去掉 `test_` 前缀，不再被 pytest import）。
- [x] 顺带清掉的评审漂移：`architecture.md` §5.2 模块表（补 `runtime_paths.py` /
      `launcher_client.py`、行数改「约」并注明易腐）、§5.4 依赖自装描述重写、
      实现索引「12 个工具」→ 15；`README.md:13` `mcp>=1.9.0` → 1.29；
      `SKILL.md` errorType 枚举补全为真实集合。

### 启动器接入（`kind=launcher`，2026-10-16 / 2026-10-18）
- [x] **新增 `kind=launcher` 工具类型** - 启动器不是 MCP，但有可列/可搜/可调用的动作：
      `launcher` 配置（适配器类型 / 命令 / 数据目录 / 超时）+ 第二层数据
      `<runtime_dir>/launcher-actions/{tool_id}.json`（与 backend-tools 同构，A14 起同迁运行时目录）
- [x] **新增 3 个对外工具**（12 → 15）：`sync_launcher_actions` / `list_launcher_actions`
      / `run_launcher_action`；执行受 `destructive` 门禁（需 `allowDestructive`），列/搜不受限
- [x] **Quicker 适配器**（`src/launcher_client.py`）- 调用走
      `QuickerStarter.exe -c<秒> "runaction:<ID或名称>?参数"`；动作 ID 来自
      `states/state_<uuid>.json` 文件名，名称来自其 state 缓存的动作列表（尽力而为，
      缓存没覆盖到的只有 ID，`source=partial`）
- [x] **uTools 适配器**（`src/launcher_client.py`，2026-10-18）- 调用走
      `cmd start "utools://<URL编码插件名>/<URL编码关键词>[?<URL编码文本>]"` 协议唤起
      （**fire-and-forget 无回传**，结果只在 uTools UI；返回体带 `fireAndForget: true`；
      `wait` 语义为「投递后观察窗口」，默认取条目 timeout、封顶 5s）；动作清单从
      `%APPDATA%/uTools/plugins/*.asar` 的 plugin.json 纯 Python 解析（Chromium Pickle
      封包，只读文件头 + 条目，无新依赖），声明完整故 `source` 恒 `auto`；本机实测
      44 个插件全解析、**218 个动作**、0 跳过；协议调用实调成功（`start` 退出码 0，
      URL 与勘察验证样例逐字一致）。限制：无结果回传 / payload 仅文本 / 依赖 uTools
      运行 / MCP key 会变 / 插件市场动态由 A13 TTL 覆盖（详见
      `docs/utools_integration_findings_2026-10-18.md` §4/§5）
- [x] **uTools 提交序列**（2026-10-18）：`99ceb25`(U1 条目) → `b90f147`(U2 asar 解析)
      → `88c0b2a`(U3 协议唤起) → `0e597e7`(U4 22 用例) → `82871e5`(U5 文档 + 实调记录)
      → `383385d`(QA 遗留加固 4 例) → `000aeec`(用例口径 250→254 同步)；每步后全量回归

### `launch_tool` 真正执行启动（P0，2026-10-18）
- [x] **新增 `src/app_launcher.py`（188 行）** - `kind=app` 的启动执行底座：
      `launch_app()` 只支持 `launch.type=executable`；`create_subprocess_exec`
      **不经 shell**（`command`/`args` 是结构化字段，元字符无机可乘）、stdio 三件套
      `DEVNULL`（A8 同款，绝不继承 server 管道）、`shutil.which` 预检；失败一律走
      返回载荷（`config_error` / `runtime_error`）不抛异常，与 `registry.launch_tool`
      / `LauncherClient.run_action` 的载荷约定一致
- [x] **观察窗口语义**（`wait`，默认 5s、封顶 30s）- 窗口内退出 → 透传退出码
      （捕捉「命令存在但一跑就崩」）；窗口到时仍在运行 → `running=true` 放任继续
      （应用启动本该长跑）；`wait<=0` → 不等待、一律按运行中报告；乱传非数值 →
      回落默认值（客户端乱传不炸）
- [x] **进程保活注册表**（易漏的坑）- CPython `BaseSubprocessTransport.close()` 会
      `kill()` 仍在运行的子进程：transport 被 GC 等于杀掉刚启动的应用。故运行中进程
      由模块级 `_LAUNCHED` 强引用持有（先登记再等待，取消路径同样有人持有），
      顺手修剪 `returncode is not None` 的已退出进程防列表无界增长；不提供枚举/杀灭
      API，生命周期交还操作系统（服务退出时运行中子进程随 transport 关闭被杀——
      与后端同一条「已知边界」）
- [x] **`launch_tool` 增 `execute` / `wait` / `allowDestructive`**（`tool_specs.py`）-
      `execute` 缺省/`false` 时 handler 与改造前**逐字等价**（只返回命令、零副作用）；
      `execute=true` 才 spawn。破坏性判定收拢到同一入口 `policy.decide_destructive`
      （新增 `app` 种：条目 `destructive` → 需 `allowDestructive=true`，策略 `deny`
      模式同样赢过客户端声明 → `permission_denied`），门禁判定在 spawn **之前**
- [x] **测试** - `tests/test_app_launcher.py` 10 用例（ok/fail/sleep 三探针现场写 tmp、
      预检失败、类型不支持、`_clamp_wait` 归一化、`wait<=0`、注册表修剪）+
      `tests/test_policy_gate.py` 判定表补 `app` 4 行与冻结对照 +
      **e2e 场景⑦** `tests/e2e/test_e2e_launch_execute.py`（真实 stdio 会话验
      默认路径/成功/失败退出码/destructive 拦截与放行，红绿双向已验证）；
      全量口径 **271 passed**（+7 个 e2e 默认不收集，见上节）
- [x] **非等价点 NE-6**（行为变更申报）- ① `tools/list` 的 `launch_tool` 三字段变更：
      description 文案、inputSchema 增 3 参、annotations 由 `readOnlyHint` +
      `destructiveHint=false` 改 `openWorldHint`（该工具确实能 spawn，不再声称只读）；
      golden 已同步重基线，diff 仅此一条；② 显式 `execute=true` 的调用会产生本地
      子进程（新副作用面，由参数本身与 destructive 门禁双重显式化）

### 工具帮助文档查看（P1，2026-10-18）
- [x] **新增 `src/tool_help.py`（256 行）** - 帮助信息的**分层取数**底座，
      刻意与「执行」解耦：`entry_help()`（description / docUrl / website +
      将要执行的命令预览）、`backend_summary()`（后端子工具的名字/描述/参数名/
      必填项，**不下发整份 inputSchema**——那是 `list_backend_tools` 的职责）、
      `launcher_summary()`（动作 id/name/type，截断 50）三条都是纯函数，零副作用
- [x] **`resolve_help_command()` 的参数来源** - `help.command` > `launch.command`，
      `help.args` > `--help`（图形程序常为 `/?`），timeout 归一化（默认 5s、封顶
      30s，调用方本次传参优先于条目）。**绝不回落 `launch.args`**：那是正常启动
      参数，「想看帮助却把应用真跑了」（`video.exe 片源.mp4 --help`）正是本功能
      最容易踩的坑，两条单测分别锁定显式覆盖与缺省回落两种形态
- [x] **`run_help_command()` 的执行细节** - `shutil.which` 预检（缺失 →
      `config_error`，不 spawn）→ `create_subprocess_exec` **不经 shell** →
      stdout/stderr PIPE 捕获（UTF-8 严格 → GBK → replace 三级解码，中文 Windows
      控制台程序常见）；`anyio.fail_after` 超时必 kill 并回传超时前输出；输出
      > 20000 字符截断（返回体要进 MCP 消息）；**退出码非 0 但有文本仍算成功**
      （不少程序 `--help` 也返回非 0，文本才是调用方要的），非 0 且无输出才报错。
      `executed` 字段语义钉死为「是否真的 spawn 过」：预检失败 False，跑起来但
      没拿到输出/超时被杀一律 True
- [x] **新增第 16 个对外工具 `get_tool_help`**（`tool_specs.py`）- 参数
      `tool_id`(必填) / `run` / `timeout` / `allowDestructive`；**默认只返回数据、
      绝不 spawn**（与 `launch_tool` 同款硬契约），`kind=app` 才认 `run=true`，
      `kind=mcp`/`launcher` 传 `run=true` 也只回数据层 + note（缓存里已有的就是
      它的全部「帮助」，没有 `--help` 这回事）；annotations 标 `openWorldHint`
      （有执行路径就不谎报只读）
- [x] **门禁复用同一判定入口**（`policy.py`）- `run=true` 同样先过
      `decide_destructive("app", ...)`，判定在 spawn **之前**（图形程序不认
      `--help` 时会把它当普通启动参数跑起来，不是过度设防）。app 种由此有了
      **两个执行入口**，故审计 `action` 与拒绝文案都跟随实际入口：help 的判定
      记成 `launch_tool`/「才会执行启动」属于审计失真与误导调用方，已修正并由
      `test_app_gate_audit_action_follows_execution_entry` +
      `test_handler_gate_audit_row_labels_help_entry` 双处锁定
- [x] **schema 增可选 `help` 字段**（`src/schema.py`）- `help.command` /
      `help.args` / `help.timeout`（> 0，缺省 5）；`docs/format.md` 的字段表由
      生成器同步（手改生成区块会被 `test_docs_sync.py` 拦下）
- [x] **测试** - `tests/test_tool_help.py` 31 用例（模块层 21 + handler 层 10，
      探针脚本现场写 tmp：打印型/静默型/挂住型/写 marker 型，覆盖超时必杀、
      三级解码、截断、`launch.args` 不复用）+ `tests/test_policy_gate.py` 判定
      表与审计标注 + **e2e 场景⑧** `tests/e2e/test_e2e_tool_help.py`（真实 stdio
      会话验默认零副作用 / 执行并回传输出 / destructive 拦截与放行 / 非 app 不
      spawn / 无来源 `config_error`，红绿双向见上节）；golden 重基线 15→16，
      全量口径 **303 passed**（+8 个 e2e 默认不收集，见上节）
- [x] **非等价点 NE-7**（行为变更申报）- ① `tools/list` 多出一个工具
      （15→16），golden 已重基线（纯追加，16 条 diff 只在末尾，5 字段其余条目
      逐字节不变）；② 显式 `run=true` 的调用会产生本地子进程并捕获其输出（新增
      副作用面，由参数本身与 destructive 门禁双重显式化）；③ `policy.py` 的 app
      种审计 `action` 与拒绝文案跟随执行入口——**旧数据无影响**（`launch_tool`
      路径 `sub_op` 为 `{}`，文案与审计行逐字不变），只有 help 路径新增标注

### 工具接入
- [x] Everything（`kind=app`，返回启动命令）
- [x] Everything Search MCP（`kind=mcp`，已实测连接、调用、启停跑通）
- [x] **WinGet（`kind=mcp`，官方内置）** - 实测：握手（winget-mcp 1.29.380 / 协议 2025-06-18）、
      2 个子工具带完整 schema、查询与安装双路径路由验证；2026-10-16 深夜按网关真实链路
      复跑端到端（start → tools/list → `find-winget-packages` 返回真实包数据 → stop），
      查询免确认放行、安装仍被子工具级门禁拦截
- [x] **Toolport 集成** - 已注册为 Toolport 的后端（registry `source: manual`，default profile），
      daemon 真实链路实测：启停、状态、子工具列表、路由调用全通
- [x] **WorkBuddy 直连 MCP** - `~/.workbuddy/mcp.json` 新增 `awam-tool-manager` 条目
      （pyenv 3.12.0 `python.exe` + `src/server.py`）；实测 15 个工具就位、
      A10 在新实例上 4 秒内自动恢复后端、`search_files` 端到端返回真实索引结果

### 声明式注册表重构（P1-04，2026-10-17）
- [x] **对外工具层收敛为单一声明源**：新增 `src/tool_specs.py`（15 条 `ServerToolSpec`：
      name / description / inputSchema / annotations / handler 同条记录，`build_mcp_tools()`
      统一构造，`validate_specs()` import 期自检）与 `src/errors.py`（`ToolError` / `fail` /
      `ensure_success` 原样搬出，避免 server → tool_specs 循环 import，server 命名空间
      re-export）；`server.py` 只保留协议装配 + 查表薄分发 + Resources/Prompts + 生命周期
      （1020 → 464 行）。新增一个对外工具的标准动作从「9 个维护点、4 个手写副本」变为
      「1 个文件、2 个相邻代码块」（1 条 spec + 1 个 handler）
- [x] **行为等价判据**：15 个工具的 name / description / inputSchema / annotations /
      outputSchema 五字段与重构前 HEAD 逐位一致。M00 在未改一行代码的 HEAD 上采集
      `tests/golden/tools_list.json`（先于一切源码改动提交，`5f06756`）；由 golden 五字段
      比对 + 三方名字守卫（spec 表 == `EXPECTED_TOOL_NAMES` == `list_tools()`）+ spec 完整性
      （handler 可调用 / schema 结构合法 / 注解键合法）+ unknown_tool 与 15 名可达性守卫 +
      SKILL.md 工具表守卫固化；「故意破坏一条 spec 确认守卫变红」一次性验证通过
- [x] **提交序列**（M00–M07 每步一 commit、步骤后全量回归）：M00 `5f06756` → M01 `ee252eb`
      → M02 `d2b4cd2` → M03 `019713d` → M04 `c3b2b08` → M05 `738b843`；M06（本文档 +
      `docs/architecture.md` 同步）与 M07（SKILL.md 工具表守卫 + 全量回归 + 真实 stdio 冒烟
      + P0 install 门禁 e2e 复跑）为收尾两步，哈希见 `git log`
- [x] **非等价点（显式标注 NE-1~NE-7，U6 要求对用户可见）**：NE-1 `json.dumps` 统一带
      `default=str`（5 → 15 个工具；可达输入上的 no-op，单向放宽，不启用 `strict_dumps`
      回退）；NE-2 未知工具失败时机改为查表未命中即失败（错误消息与 errorType 逐字相同，
      完全等价）；NE-3 `validate_specs()` import 期自检（对合法输入零差异，spec 表损坏时
      快速失败）；NE-4 `server.py` 行数下降致旧文档行号引用失效（M06 已同步
      `docs/architecture.md`）；NE-5 `search_tools`/`list_tools` 入参文案里的 `driver`
      举例漂移已于 2026-10-17 漂移清理批次修正为 `launcher`（§3.1 #20，行为变更：tools/list
      两处 description 文案变更，golden 同步重新基线）；NE-6 `launch_tool` 增
      `execute` / `wait` / `allowDestructive`（2026-10-18 P0 落地，详见「`launch_tool`
      真正执行启动」小节：tools/list 三字段变更 + 显式调用产生本地子进程）；
      NE-7 新增第 16 个对外工具 `get_tool_help`（2026-10-18 P1 落地，详见「工具
      帮助文档查看」小节：golden 纯追加重基线 15→16 + 显式 `run=true` 产生本地
      子进程；`policy.py` 的 app 种审计 action/文案跟随执行入口，`launch_tool`
      路径逐字不变）

### 其他
- [x] Git 仓库初始化，代码版本管理
- [x] .gitignore 配置（测试脚本、缓存文件排除）

### 已知边界（非缺陷，记录备查）
- **uTools 协议投递成功但 UI 不呈现**：本机 uTools 只剩托盘、零可见顶层窗口时，
  `utools://` 投递（`start` 退出码 0）成功但界面不渲染；用原始协议直接唤起重现，
  同现象——判为会话状态问题而非集成缺陷。待办：在可见桌面会话里单跑一次
  `run_launcher_action` 做 UI 级实调确认（勘察报告 §5 已留痕）
- 真正的「后端永不重启」需要外部 supervisor（后端生命周期移出网关控制的进程），
  当前只能做到「跨网关回收自愈」（A10），列为架构演进
- 硬杀 server 会孤儿化后端子进程（下一代自动恢复会另起一个，stdio 无端口冲突可共存）
- **`launch_tool` 的 `execute=true` 启动的应用随 server 退出而被杀**：运行中进程由
  `app_launcher._LAUNCHED` 强引用保活（防 GC 杀进程），但 server 正常退出时
  transport 关闭仍会 kill 掉仍在运行的子进程——与后端子进程同一条边界。要「启动后
  独立于 server 存活」需外部 supervisor 或 `CREATE_NEW_PROCESS_GROUP`+detach 一类的
  进程分离方案（未做，属架构演进）
- **`--help` 不是通用约定**：`get_tool_help` 的 `run=true` 只是「按条目配置跑一条
   命令并把输出交回」。命令行程序普遍成立；GUI 程序多数不认 `--help`（会把参数当
   普通输入甚至直接启动自己），所以要靠条目 `help.args` 显式写对（`/?` 之类），
   且这条路径同样吃 destructive 门禁。缺省输出为空即报 `runtime_error`——不猜、
   不粉饰成「这个工具没有帮助」
- **帮助输出封顶**：单次回传最多 20000 字符（`truncated=true` 明示被截），超时上限
   30s（`anyio.fail_after` 到点必 kill，不留孤儿）——返回体要进 MCP 消息，不能塞
   进整本手册；`kind=mcp`/`launcher` 没有可执行帮助命令，只回数据层 + `note`
- ~~`data/backend-tools/*.json` 是自动生成的缓存却受 git 跟踪，多实例在跑时
  `lastSynced` 会被反复改写~~ → 已解决（A14）：运行时缓存整体迁到
  `%LOCALAPPDATA%\.awam-tool-manager`，仓库里只剩人工维护的 `data/tools/`
- 上游 `everything-search-mcp` 返回体 `path` 为空、`isFile`/`isFolder` 恒 false
  （全路径塞在 `name` 里）——属上游包自身的数据整形，转发层原样透传；消费方以 `fullPath` 为准
- **Quicker 动作名是尽力而为的**：动作正文在加密的 `quicker.db`（非 SQLite 魔数），
  名称靠两个来源拼：state 缓存的列表（带 type/app/description）+ 运行日志
  （只有名称，只补空）。本机实测 212 个动作里 **160 个有名称**，剩余 52 个只有 ID
  （`source=partial`）。只有 ID 也能调用，但搜索会漏——想补名称只能人工登记
- ~~**Quicker 调用的成功路径尚未真实执行验证**~~ → 已验证（2026-10-16 22:11:06，
  用户授权执行动作「识别二维码」`9f530d6e-…`）：`QuickerStarter` 返回 `OK`、
  exitCode 0，Quicker 日志确认 `执行动作：识别二维码` 真的跑了

---

## 三、待完成 ⏳

### P0 - 核心打通
- [ ] 接入 Steam（steam-local-mcp）— ⚠️ 本机未安装 Steam，无法端到端实测
- [ ] 接入 npm（npm MCP）— ⚠️ 需求待澄清：npm 官方无 MCP server，需明确接哪个包

### P1 - 体验完善
- [ ] 工具自动发现：检测本机已安装哪些可作为 MCP 后端接入的工具
- [ ] 工具标签分类和筛选优化（`kind` / `tags` 之外的维度）
      （「工具帮助文档查看」已于 2026-10-18 完成，见上节 P1）

### P2 - 生态扩展
- [ ] 接入 Scoop（写 CLI 适配器）— 本机已装（`S:\Libs\Scoop`），可实测
- [ ] 接入 WeGame（腾讯平台）
- [x] 接入 Quicker — ✅ 已完成（见「启动器接入」）：实测同步 212 个动作、160 个有名称，
      关键词搜索命中（"搜索"→5 条 / "剪贴"→2 条 / "识别二维码"→1 条），
      调用全链路通（含成功路径：真实执行动作并返回 OK）
- [x] 接入 uTools — ✅ 已完成（2026-10-18，见「启动器接入」）：实测同步 218 个动作、
      source=auto，协议调用成功（`start` 退出码 0）；fire-and-forget 无回传、
      payload 仅文本等限制见勘察报告 §5
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
| 工具条目 | `data/tools/{tool_id}.json`（仓库内） | 每个工具一个文件，文件名即 tool_id |
| 后端子工具缓存 | `<runtime_dir>/backend-tools/{tool_id}.json` | 由 `sync_backend_tools` 自动生成（A14 起在 `%LOCALAPPDATA%\.awam-tool-manager`） |
| 启动器动作清单 | `<runtime_dir>/launcher-actions/{tool_id}.json` | 由 `sync_launcher_actions` 自动生成 |
| 对外 MCP 工具 | `src/tool_specs.py`（P1-04 起为单一声明源；`server.py` 只做协议装配与分发） | 暴露给 AI 客户端的工具列表（**非磁盘数据层**） |

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

- `kind`：`app`（普通应用）/ `mcp`（MCP 工具）/ `launcher`（启动器，非 MCP），决定是否可建立后端连接、决定 `get_tool_info` 的返回形状
- `launch`：仅 `kind=app` 有意义，`launch_tool` 据此返回启动命令；显式 `execute=true` 时按此 spawn 进程
- `help`：可选，`get_tool_help` 的帮助命令来源（`help.command` 缺省回落
  `launch.command`，`help.args` 缺省 `--help`、**绝不回落 `launch.args`**，
  `help.timeout` 封顶 30s）；只在显式 `run=true` 时执行
- `connection`：仅 `kind=mcp` 有意义，`BackendClient` 据此拉起子进程；目前只支持 `stdio`
- `install`：仅 `kind=mcp` 有意义，声明「启动命令缺失时怎么装」；**默认不执行**
  （P0 门禁）——需 `AWAM_AUTO_INSTALL=1` 或条目 `install.auto: true` 授权，且命令要
  过白名单，否则 `start_backend` 只返回 `install_required`，服务预热退化为纯检测

### 4.3 后端子工具缓存（`<runtime_dir>/backend-tools/{tool_id}.json`）

由 `sync_backend_tools` 写入运行时目录（A14 前在 `data/backend-tools/`），
`tools[]` 直接透传后端 MCP 的 `inputSchema`：

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
├── docs/
│   ├── format.md               # 数据格式规范
│   ├── architecture.md         # 架构说明
│   ├── mcp-conformance.md      # MCP 协议一致性对比
│   └── code-review.md          # 代码审核报告
├── data/                       # 仓库内只有人工维护的真相源（A14 起）
│   ├── tools/                  # 工具条目
│   │   ├── everything.json
│   │   ├── everything-search-mcp.json
│   │   ├── winget-mcp.json
│   │   ├── quicker.json        # kind=launcher
│   │   └── utools.json         # kind=launcher
├── src/
│   ├── server.py               # MCP Server 入口：协议装配 + 薄分发 + 生命周期
│   ├── tool_specs.py           # 对外 MCP 工具声明式注册表（SSOT：16 条 spec + handlers）
│   ├── policy.py               # 破坏性门禁统一判定 + 部署策略（data/policy.json）
│   ├── errors.py               # 业务错误出口（ToolError / fail / ensure_success）
│   ├── store.py                # JSON 存储层（tools 走仓库，缓存走 runtime dir）
│   ├── registry.py             # 工具注册中心
│   ├── backend_client.py       # 后端 MCP 客户端与连接池（stdio）
│   ├── launcher_client.py      # 启动器适配器（非 MCP：Quicker / uTools）
│   ├── app_launcher.py         # kind=app 启动执行（launch_tool 的 execute=true 路径）
│   ├── tool_help.py            # 帮助信息分层取数 + 帮助命令执行（get_tool_help 底座）
│   ├── runtime_paths.py        # 运行时目录解析 + 旧路径迁移（A14）
│   └── schema.py               # 两层数据的 JSON Schema 校验 + **文档生成源**
├── scripts/
│   └── generate_format_doc.py  # 由 schema.py 生成 docs/format.md 的字段表
└── tests/                      # 16 个 test_*.py（303 passed）+ 8 个 e2e 场景（默认不收集）
    ├── test_store.py
    ├── test_registry.py
    ├── test_registry_policy.py
    ├── test_server_dispatch.py
    ├── test_dispatch_equivalence.py  # 分发等价（golden 之外）
    ├── test_tool_specs.py            # spec 表与 tools/list golden
    ├── test_error_type_contract.py   # errorType 台账（覆盖/文案契约）
    ├── test_policy_gate.py           # destructive 判定表 + 旧逻辑冻结对照
    ├── test_policy_config.py         # 部署策略（deny/白名单/H1/审计）
    ├── test_backend_client.py
    ├── test_launcher_client.py
    ├── test_app_launcher.py          # kind=app 启动执行（execute 路径底座）
    ├── test_tool_help.py             # 帮助取数分层 + help 命令执行（run 才 spawn）
    ├── test_runtime_paths.py     # 运行时目录解析与迁移（A14）
    ├── test_protocol_roundtrip.py   # 真实 MCP 会话，不 mock SDK
    ├── test_docs_sync.py            # 文档/示例/data 与 schema 的一致性
    ├── golden/tools_list.json       # tools/list 五字段基线（M00 采集）
    └── e2e/                        # 真实子进程 + 真实会话，须 `pytest -m e2e`
        ├── test_e2e_startup.py / test_e2e_readonly.py /
        │   test_e2e_backend_lifecycle.py / test_e2e_resources_prompts.py /
        │   test_e2e_policy_deny.py / test_e2e_install_gate.py（场景①–⑥）
        ├── test_e2e_launch_execute.py   # 场景⑦：execute=true 真启动 + 门禁双向
        ├── test_e2e_tool_help.py        # 场景⑧：get_tool_help 默认零副作用 / run 才 spawn
        └── manual_install_gate.py       # 手工脚本（依赖本机 npm，不进 pytest）
```

运行时数据（**不在仓库**，A14）：`%LOCALAPPDATA%\.awam-tool-manager/`
（环境变量 `AWAM_RUNTIME_DIR` 可覆盖，测试借此隔离）

```
%LOCALAPPDATA%/.awam-tool-manager/
├── backend-tools/          # 后端 MCP 子工具缓存（自动生成，随启动同步）
│   ├── everything-search-mcp.json
│   └── winget-mcp.json
├── launcher-actions/       # 启动器动作清单缓存（自动生成）
│   ├── quicker.json
│   └── utools.json
└── backend_desired_state.json  # 后端启动意图（A10 跨世代自愈用）
```

运行：`python -m pytest -q`（未装 pytest-asyncio，异步用例统一用 `asyncio.run`）

**改字段的流程**：只改 `src/schema.py` → `python scripts/generate_format_doc.py`
→ 提交。手写改 `docs/format.md` 的字段表会被测试判失败。
