# awam-tool-manager 任务清单

统一工具管理器 - 维护启动器工具集名单，提供统一的工具注册、搜索、启动、能力路由调用能力。

> 2026-10-25 起对外工具面改用 Provider / Capability / Runtime / Target 模型：
> 旧名 `call_backend_tool` / `list_backend_tools` / `sync_backend_tools` /
> `start_backend` / `stop_backend` / `get_backend_status` / `get_backend_logs` /
> `set_backend_log_level` / `list_launcher_actions` / `run_launcher_action` /
> `sync_launcher_actions` 已**硬切删除**（不再作为别名），调用会按 `unknown_tool` 拒绝。
> 下文历史条目保留原名作为当时记录，当前对外工具名以本节清单为准。

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

### 对外 MCP 工具（lazy 19 个；default 26 / full 35）
- [x] `search_tools` - 搜索工具（关键词 + `kind` / `status` / `publisher` 筛选）
- [x] `list_tools` - 列出所有工具（同上三个筛选维度，见「筛选维度增强」）
- [x] `get_tool_info` - 查看工具详情
- [x] `launch_tool` - 返回启动命令信息；2026-10-18 起支持显式 `execute=true` 真正执行启动（默认仍只返回命令）
- [x] `get_tool_help` - 工具帮助信息：文档链接 + 能力清单（mcp 子工具 / launcher 动作）+ 帮助命令预览；2026-10-18 新增，`kind=app` 显式 `run=true` 才执行 `--help` 并捕获输出（默认零副作用）
- [x] `get_usage_stats` - 调用使用统计：2026-10-18 新增（P3），只读 `<runtime>/usage.log` 并按窗口聚合，见「工具使用统计」小节
- [x] `discover_tools` - 本机工具发现：2026-10-18 新增（P1），按 `data/discovery.json` 声明的候选清单做只读探测，见「工具自动发现」小节
- [x] `list_packages` / `search_packages` / `launch_package` / `install_package` - 包管理面：2026-10-19 新增（P2-07），直接读安装器落盘清单（当前 `scoop`），列已装 / 搜可装**纯读盘**，启动与安装都要显式 `execute=true`（安装额外要 `allowDestructive=true`），见「包管理适配」小节
- [x] `list_capabilities` - 列出提供者能力（MCP 子工具 / 启动器动作 / 应用 launch），带完整 `inputSchema`
- [x] `call_capability` - 统一能力调用入口（嵌套 `arguments`；只读默认执行、写/破坏性默认预览）
- [x] `sync_capabilities` - 同步能力清单（MCP 启动运行时并刷新，启动器重扫，应用无清单）
- [x] `start_runtime` / `stop_runtime` / `get_runtime_status` - MCP 运行时管理
- [x] `get_runtime_logs` / `set_runtime_log_level` - 运行时日志
- [x] 7 个意图前门 + 5 个 full 补充工具 - 2026-10-20 新增（P1），三档暴露模式见
      「暴露模式与意图级工具门面」；lazy 19 个契约随能力模型重构重新基线
- [x] `check_package_updates` - 第 5 个 full 补充工具（2026-10-24），查「哪些包有可用更新」：
      默认零 spawn（scoop 已装版本 vs 本地 bucket 索引版本），`execute=true` 才问 UniGetUI，
      见「包升级查询与 UniGetUI 外部口径」小节
- [x] UniGetUI 只读查询 4 工具（2026-10-31，full）：`list_package_managers` /
      `search_uniget_packages` / `list_uniget_packages` / `get_package_details`；
      安装 / 升级 / 卸载 / 提权不在本期内，见「UniGetUI 只读查询面」小节

### 协议合规
- [x] 后端工具参数名统一为 `tool_id`（移除历史遗留的 mcp_tool_id / launcher_id）
- [x] 入参别名兼容 + 必填缺失自描述错误（2026-10-19，见「入参别名与必填自描述错误」）：
      对外 schema 认 `id`/`query`/`name` 这类直觉名，dispatch 入口折回规范名，退役名不得复活。
      **同日二次收敛**（见「别名二次收敛」）：词汇改由 `description` 承载（17/17 覆盖必填名），
      别名表砍到最小集，对外上下文净减 5338 字符
- [x] `read_resource` 参数归一化：SDK 传的是 `AnyUrl`，直接与字符串比较会全部落到未知资源
- [x] `get_prompt` 按协议返回 `GetPromptResult`（老版本 SDK 退回裸列表）
- [x] 失败返回 MCP 协议层 `isError`（此前 success:false 被协议层当成成功）
- [x] `read_resource` 返回 `Iterable[ReadResourceContents]`（此前返回 TextContent 会报错）
- [x] `requirements.txt` 锁定 `mcp<2.0.0`（2.x 移除了本项目的低层装饰器 API）

### 路由能力
- [x] BackendClient - 单个后端 MCP 客户端（懒加载连接）
- [x] BackendManager - 后端连接池管理
- [x] `call_capability` - 路由转发到 MCP 子工具 / 启动器动作 / 应用 launch
- [x] `list_capabilities` - 列出提供者能力（含参数 schema）

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
- [x] `call_capability` 的 destructive 门禁（`allowDestructive`，默认拒绝）
- [x] **A12 子工具级破坏性门禁** - 破坏性实际属于「子工具」而非「整个后端」：
      新增 `destructiveTools` 字段（schema + `docs/format.md` 自动同步），配置后只拦截
      清单内的后端子工具，未配置则回退条目级 `destructive` 语义；拦截判定与
      warning 标注共用同一判定结果（P1-05 起收敛为 `policy.decide_destructive`），
      避免免确认调用被误标
- [x] **P1-05 Phase 1 动作级门禁**（2026-10-17）- 破坏性判定收敛到统一入口
      `policy.decide_destructive`（判定表 + 结构化审计行 decision/rule_hit/client_declared）：
      `call_capability` 调启动器动作支持 `destructiveActions`（glob，大小写不敏感，匹配动作
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

> 文件数口径（2026-10-19 更新）：`tests/` 下有 **23 个** `test_*.py`（贡献 pytest
> 用例），另有 `tests/e2e/` 下的 **9 个** e2e 用例文件（9 个场景）——它们**默认不收集**
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
- **当前 9 个场景**：① 起服 + `tools/list` ≡ golden、② 只读工具真调、
  ③ 仓内假后端全链路（含无残留子进程断言）、④ resources / prompts 往返、
  ⑤ policy deny 端到端（后端零调用）、⑥ install 门禁未授权不执行、
  ⑦ `launch_tool` 的 `execute=true` 真启动（含 destructive 门禁双向）、
  ⑧ `get_tool_help` 的默认零副作用 / 显式 `run=true` 才 spawn、
  ⑨ `discover_tools` 的 `run_commands=true` 真跑命令且子进程不污染 stdio 通道。
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
- **场景⑨ 新增（2026-10-18 晚，配合真机端到端跑通）**：`discover_tools` 的
  `run_commands=true` 是**第四条**能 spawn 的路径，也是第一条曾经**把整个网关打死**的
  路径——真机上子进程默认继承 server 的 stdio 管道，把 inbound JSON-RPC 读走了
  （客户端 `anyio.BrokenResourceError`，server 日志「工具 discover_tools 执行完成，
  返回 11550 字符」紧跟「服务退出」）。根因排除过程：同会话 41.9s 后 `list_tools()`
  仍正常，所以**不是** 30s 后端空闲超时。
  - 探针 `tests/e2e/stdin_guard_probe.py` 自己报告 stdin 的下场：`EOF`（合格，
    说明拿到的是 `DEVNULL`）/ `BLOCKED` / `STOLEN=<n> bytes:<内容>`，marker 落的
    就是这个 verdict；同时打一行 `StdinGuardProbe 1.2.3`，用来证明版本号确实是从
    **子进程 stdout** 经 `version_pattern` 取回来的。
  - 四条断言：默认路径当场不 spawn、`run_commands=true` 取到 `version=1.2.3`、
    verdict 恒为 `EOF`、**调用之后 `list_tools()` 仍一张工具定义不少**（期望值取自
    `conftest.EXPECTED_TOOL_NAMES`，不写死数字；传输通道活着）。最后一条才是本场景的
    存在理由——前三条在单测层用桩就能验。
  - 红绿：摘掉 `stdin=subprocess.DEVNULL` → 场景⑨ 9.33s 内红（客户端先断），
    单测 `test_run_commands_executes_and_parses_version` 同时红；还原后全绿
- **口径**：`pytest tests/ -q` = 589 passed / **9 deselected**；
  `pytest -m e2e -q` = 9 passed / 589 deselected。说「589」时要带上
  「另有 9 个 e2e 默认不收集」，否则会被误读成总数。
  （本行随每次落地更新：NE-8 时为 314，NE-9「工具使用统计」落地后为 343，
  NE-10「工具自动发现」落地后为 411，真机端到端 + 发现层三处修复后为 425，
  Steam 假阴性纠正（`registry_value` 探针）后为 441，
  P2-07「包管理适配」落地（适配层 + handler + 门禁）后为 517，
  P0「Steam 接入」（库解析 + 启动器适配器）落地后为 557，
  「入参别名与必填自描述错误」落地（含别名属性顺序守卫）后为 579，
  「别名二次收敛」（描述承载必填名 + 6 条新守卫 + 别名表钉死）后为 589）
- **手工脚本**（不在上面 9 个里）：
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
      落地时全量口径 **303 passed**（现口径见上节：NE-8 之后 314，另有 8 个 e2e
      默认不收集）
- [x] **非等价点 NE-7**（行为变更申报）- ① `tools/list` 多出一个工具
      （15→16），golden 已重基线（纯追加，16 条 diff 只在末尾，5 字段其余条目
      逐字节不变）；② 显式 `run=true` 的调用会产生本地子进程并捕获其输出（新增
      副作用面，由参数本身与 destructive 门禁双重显式化）；③ `policy.py` 的 app
      种审计 `action` 与拒绝文案跟随执行入口——**旧数据无影响**（`launch_tool`
      路径 `sub_op` 为 `{}`，文案与审计行逐字不变），只有 help 路径新增标注

### 筛选维度增强（P1，2026-10-18）
- [x] **维度选型：`status` + `publisher`** - TASKS 里这条原本写的是「`kind` / `tags`
      之外的维度」。盘点条目已有字段后选定这两个：`tags` 已经参与关键词匹配（`search_tools`
      搜 tags 就是筛选），`version` / `website` 拿来筛没有真实诉求，
      「装了没」「谁出的」才是用户会问的两件事。**没有新增字段**，`schema.py` 零改动
- [x] **单一筛选入口 `registry._apply_filters`** - `list_all_tools` 与 `search_tools`
      共用，`kind` 保持原样（它决定的是「按类型切」，语义与 `tags` 重叠，不动）；
      `search_tools` 是「先关键词命中、再按维度收窄」，两者正交叠加。
      `status` 精确匹配、`publisher` 子串且大小写不敏感（发布者写法多样：
      `voidtools` vs `Some Corp (Beta)`，精确匹配等于要求调用方抄准原文）
- [x] **缺省口径与对外投影对齐** - 条目未写 `status` 时筛选用 `installed`，
      与 `list_tools` 投影的 `t.get("status", "installed")` 同一个值。
      不对齐就会出现「列表里显示 installed、按 installed 却筛不到」的自相矛盾。
      （`get_tool_info` 的缺省是 `unknown`，属历史遗留，见「已知边界」）
- [x] **能筛的维度必须能看见** - `list_tools` / `search_tools` 的每条结果补
      `publisher` 键（条目没写投影成空串而不是缺键）。只加筛选参数却不返回该字段，
      调用方等于要凭猜测填发布者写法——这是把功能做一半
- [x] **`status` 带 `enum`，顺带查实了 SDK 的校验行为**（新事实，值得留痕）：
      走协议入口时 **SDK 确实按 `inputSchema` 校验**——`{"status":"isntalled"}` →
      `isError` + `Input validation error: ... is not one of [...]`，
      缺 `keyword` → `Input validation error: 'keyword' is a required property`。
      旧评审文档（`docs/architecture_review_2026-10-16.md` §评审项、
      `docs/architecture_incremental_p1-04_*.md`）说的「inputSchema 声明但不校验」
      只对**直接调 `dispatch_tool`** 成立（handler 用 `.get()` 兜底）；
      经协议通道时 `required` 与 `enum` 都会被拦。enum 由此不是装饰而是
      「拼错的筛选值不会静默返回空集」的护栏，已由协议回环测试锁定
- [x] **测试：+11 条（303 → 314）** - `tests/test_registry.py` 9 条（筛选语义 6 +
      参数转发 2 + 投影形状 1，全部 tmp_path 造数据：仓库 `data/tools/` 5 条清一色
      `status: installed`，拿它筛 `not_installed` 只会得空集，红了也不知道为什么红）；
      `tests/test_protocol_roundtrip.py` 2 条（enum 拒绝 + 真实名单上筛选确实收窄）
- [x] **红绿验证（4 个变异，逐个精确打红）** - A `if want:` 退回 `if status:` →
      「纯空白筛选」1 条红；B `status` 缺省从 `installed` 改空串 → 4 条红
      （缺省口径守卫是有效的）；C handler 不转发 `status`/`publisher` → 2 条转发测试红；
      D 返回体去掉 `publisher` → 投影测试红；摘掉 `enum` → enum 协议测试红。
      **变异 A 顺手抓到一个真 bug**：原写法 `if status:` 对 `"  "` 是 truthy，
      会拿归一化后的空串去精确匹配把整张名单筛空，而同样输入的 `publisher` 因为子串
      语义反而成了 no-op——一类输入两种结果。改成「先归一化再判空」并补测试锁定
- [x] **golden 重基线（NE-8）** - 这次是**修改而非追加**：`search_tools` /
      `list_tools` 两条的 `description` + `inputSchema` 变更，diff 28+/2-，
      其余 14 条逐字节不变（`git diff` 已核对只命中这两个条目）。
      三方名字守卫（`EXPECTED_TOOL_NAMES` / SKILL.md 工具表）无需改动——名字集合没变
- [x] **文档同步** - `SKILL.md`（核心能力增「筛选查询」、工具表参数列、用例数）、
      `docs/format.md`（两条契约行 + 投影可见性说明 + enum 边界说明）、
      `docs/architecture.md`（能力表、模块表 `registry.py` 141→188 / `tool_specs.py`
      891→922、工具表参数、实现索引新增 `_apply_filters` 行）。
      顺带清一处 NE-6 留下的陈旧能力表行：「工具启动信息返回（仅返回命令，不执行）
      ⚠️ 半成品」——`execute=true` 路径 2026-10-18 已落地并由场景⑦实测，改判 ✅
- [x] **未做 e2e 场景**：纯读路径、无子进程、无副作用；协议回环层已经用真实 SDK
      会话验过校验与投影，再起一个 stdio 子进程不增加覆盖，故不新增场景⑨

### 工具使用统计（P3，2026-10-18）

- [x] **`src/usage.py`（新模块，209 行）** - append-only JSONL 落
      `<runtime>/usage.log`。选日志而不是内存计数器：上游网关周期性整池回收
      本 server 进程（架构 §进程回收实测），进程一活内存计数就归零，落在盘上才是真相源。
      形态刻意与 `policy.py` 的 `audit.log` 一致：audit 只记门禁决策，usage 记所有调用，
      两份日志互相印证
- [x] **刻意不记调用参数** - 行字段只有 `ts` / `tool` / `target`（= 参数里的
      `tool_id`）/ `ok` / `ms`（失败才带 `errorType`）。参数里可能带本机路径、
      关键词、动作正文，而聚合只需要「哪个对外工具、动的是哪个工具、成没成、快不快」
- [x] **dispatch 出口埋点（`server.py` `dispatch_tool`）** - 选 dispatch 作为唯一漏斗：
      它同时是协议入口和单测直调入口，五个出口（成功 / `unknown_tool` /
      `permission_denied` / handler 业务错 / 意外异常）各记一行，计数不会漏。
      业务错的 `errorType` 直接从异常正文取（那份 JSON 就是客户端看到的载荷，同源不另编），
      意外异常记 `internal_error` 并原样再抛。**埋点是旁路**：返回值与异常语义一字未改
      （`test_dispatch_instrumentation_does_not_change_contract` 锁定），
      落盘失败只向 stderr 告警
- [x] **新增第 17 个对外工具 `get_usage_stats`**（`tool_specs.py`，标 `readOnlyHint`）-
      参数 `days`（按日历日、含今天，缺省 7，越界收敛到 1~365）与 `tool_id`
      （与记录里的 `target` 精确匹配），均非必填；返回 `window`（自证口径）+
      `stats`（total / success / failed / byName / targets / byDay / errorTypes）+ `logPath`。
      `total=0` 时附 `note` 说明覆盖范围——空窗口不解释会被读成「这工具没人用」
- [x] **载荷嵌套修正（实现期抓到的真问题）** - 聚合视图最初平铺进载荷，
      `aggregate` 自带的 `success`（成功次数，int）把信封的 `success: true`（bool，
      `SHARED_OUTPUT_SCHEMA` 唯一必填字段）覆盖掉了：客户端按布尔读「成没成」会全错。
      改为嵌在 `stats` 下，并补协议回环用例
      `test_usage_stats_call_carries_boolean_success_over_wire` 钉住「线上顶层
      `success` 必须是 bool、`stats.success` 才是整数」
- [x] **窗口按日历日而非 timedelta** - 「最近 7 天」按 `ts` 前 10 位比对日期集合
      （含今天），避免「7 天前 00:31」这类边界歧义；`ts` 不合法的行留在窗口外但
      不带 `days` 时仍如实返回
- [x] **测试：+29 条（314 → 343）** - `tests/test_usage.py` 28 条（`record_call` 行形与
      参数隐私 / `OSError` 吞异常、`error_type_from` 四种输入、`read_rows` 缺文件与坏行、
      窗口裁剪、`aggregate` 视图形状、**5 条 dispatch 级集成**、3 条 handler 直通、
      spec 只读注解），`tests/test_protocol_roundtrip.py` +1 条线上载荷用例
- [x] **变异红绿（5 组，各打死对应用例后按 sha256 还原）** - M1 去掉成功埋点 →
      dispatch 集成用例红；M2 `aggregate` 不统计失败 → 2 条红；M3 聚合字段改回平铺 →
      4 条红（含协议回环）；M4 `record_call` 带上参数 → 2 条红；M5 未知工具出口不记
      `errorType` → dispatch 集成用例红
- [x] **golden 重基线（NE-9）** - 纯追加：16→17，diff 35+/0-，前 16 条逐字节不变
- [x] **文档同步** - `SKILL.md`（核心能力增「使用统计」、工具表新行、用例数 343）、
      `docs/format.md`（契约行 + 统计口径/嵌套说明块）、`docs/architecture.md`
      （能力表、目录树、模块表新增 `usage.py` 行与 `tool_specs.py` 922→975、
      实现索引 `dispatch_tool` / `usage.py` 三段、6.1 工具表、头部落点与
      顺带修正 `schema.py` 450→470 / `server.py` 464→525 / `policy.py` 426→429 的既有漂移）、
      `docs/mcp-conformance.md`（只读注解清单加 `get_usage_stats`）
- [x] **踩到一条文档体例约束并留痕** - `tests/test_error_type_contract.py` 的解析器取
      文档里**第一个** `` `errorType` `` 反引号 token 作为枚举锚点（体例要求它出现在
      「`errorType`（取值）：」那一行）。我在 `docs/format.md` 契约表后新写的注释里
      用了带反引号的 `errorType`，位置在枚举之前 → 守卫立刻红
      （「解析不到任何 errorType 取值」）。改为「失败行才附带同载荷里的错误类型键」
      措辞后恢复。**后续在 format.md 枚举锚点之前新增内容时，不要写带反引号的
      `errorType`**（要么改措辞，要么把注释挪到锚点之后）
- [x] **未做 e2e 场景** - 纯读 + 只写运行时目录，无子进程、无外部副作用；
      dispatch 级与协议回环级两层已覆盖埋点与载荷
- [x] **明确不做** - ①跨进程计数精度不做保证（多实例并发 append 的行序不保证，
      计数以日志行数为准）；②`usage.log` 不做轮转（与 `audit.log` 同一取舍，
      首版刻意不做，避免过度工程）；③不做「用户直接启动工具」的统计——
      那条路径根本不经过本 server

### 工具自动发现（P1，2026-10-18）

授权口径（用户选定「代码先行，真机由客户端跑」）：仓库内实现探测面，**单测全部跑
`tmp_path` 合成数据、不碰用户真机**；真机结果由客户端调一次 `discover_tools` 得到。
本轮因此没有读取过用户的 `LOCALAPPDATA` / `PATH` 内容。

- [x] **`src/discovery.py`（新模块，落地时 425 行；同日晚补 `registry_value` 探针后 653 行）+ `data/discovery.json`（9 个候选）** -
      声明式候选清单（scoop / powertoys / winget / quicker / utools / everything /
      node / python / steam）+ 三类探测（落地时三类：`path_exists`（能下结论）、`dir_count`
      （只补充证据，目录空不等于没装）、命令是否在 PATH（`shutil.which`，**不 spawn**）；
      第四类 `registry_value` 是同日晚为 Steam 假阴性加的，见「真机端到端打通」缺陷④）
- [x] **结论三态：`hit` / `miss` / `inconclusive`** - `{ENV}` 位置因为环境变量没设
      而没检查成时必须判「不知道」而不是「没装」，否则探测面自己制造假结论；
      `miss` 要求**所有**能下结论的维度都说没有、且没有维度没检查成。
      `status` / `kind` 复用工具条目的同一套词表（与 NE-8「能筛的维度都能看见」反过来成立：
      发现结果要能被 `list_tools` 的筛选参数接住）
- [x] **失败时机与 `policy.py` 刻意不同** - 两者都是「仓库里的 JSON 声明 + 环境变量覆盖」，
      但坏策略等于安全约束静默失效所以 import 期快速失败；发现清单不是安全面，
      坏文件只让 `discover_tools` 这一次调用返回 `config_error`，不拖垮整个服务
- [x] **第 18 个对外工具 `discover_tools`**（`tool_specs.py`）- 参数 `run_commands`
      （默认 false）与 `status`（带 `enum`）均非必填；返回 `manifestPath`（自证声明源）+
      `runCommands` + `summary`（整份清单计数，不随筛选裁剪）+ `returned` +
      `candidates`（逐条 `status` / `confidence` / `registered` / `evidence`）+ `note`。
      清单为空时 `note` 改口说「这是还没声明要找什么，不是本机没有工具」
- [x] **`note` 自证探测面不是穷举** - 清单没写的安装位置发现不了，所以「未命中」
      只能给中信心（`confidence=medium`）；这条限制写在返回体里而不是只写在文档里，
      否则调用方会把「没发现」读成「本机没有」
- [x] **默认零 spawn 的实现细节** - `probe_command()` 只用 `shutil.which` 判可用性
      （不起进程），`run=True` 才用 which 解析出的**绝对路径**无 shell 执行
      （Windows 的 `.cmd` 垫片必须绝对路径），超时与 `OSError` 都折成结论返回而不抛；
      载荷里的 `run` 报的是「这一维真的跑过没有」，不是回显调用方传的标志位
- [x] **实现期抓到的两个真问题** - ①`fail()` 参数顺序写反（`fail(ErrorType.CONFIG_ERROR, str(exc))`）
      会把任意异常文本发布成 `errorType` 契约值，改为 `fail(str(exc), error_type=…)`；
      ②`probe.get("limit") or DEFAULT_DIR_LIMIT` 把显式 `0` 吞成默认值，改为
      `get("limit", DEFAULT)` 并让 `limit<=0` 返回空、校验期要求 int
- [x] **测试：+68 条（343 → 411）** - `tests/test_discovery.py` 66 条（清单校验 14 条参数化、
      仓库清单静态校验 + 只声明 env token 的守卫、`expand_path` 四种失败、`run_probes`
      三态折叠、`fold_verdicts` / `status_of` 表、命令面 11 条含超时/OSError/截断、
      `discover` 编排、**5 条 dispatch 集成**含「默认一次都不 spawn」的正证与 `config_error`、
      spec 注解），`tests/test_protocol_roundtrip.py` +2 条（真实会话载荷形状 + 不 spawn、
      `status` enum 在线上就拦下拼错的值）
- [x] **变异红绿（9 组，各打死对应用例后按 sha256 还原）** - M1 环境变量没设退化成相对路径、
      M2 `inconclusive` 塌缩成 `miss`、M3 忽略 `run` 门禁、M4 命令可用性不参与结论、
      M5 不校验 `version_pattern`、M6 `status` 去掉 enum、M7 把会 spawn 的工具声明成只读、
      M8 `dir_count` 也参与命中判定、M9 清掉 `note`；9/9 击杀集合精确、无多余无漏杀
- [x] **M3 第一版杀不动，暴露两个测试缺口（本轮最有价值的一次红）** -
      ①门禁用例原先用不在 PATH 的命令，「没 spawn」平凡为真 → 换成 tmp bin 里的
      `herecli`（可用但不该跑），让「可用」与「跑过」两个维度分离；
      ②`command.run` 原先回显调用方标志位，「忽略门禁」的变异照样报 `run: false` →
      改为在真正 `subprocess.run` 之前才置真。用例与实现一起补，之后 M3 击杀 5/5
- [x] **golden 重基线（NE-10）** - 纯追加：17→18，diff 39+/1-（唯一删除是文件末尾的 `]`），
      前 17 条逐字节不变；三方名字守卫（`SERVER_TOOL_SPECS` / `conftest.EXPECTED_TOOL_NAMES` /
      SKILL.md 工具表）同步加 `discover_tools`
- [x] **顺带修一条脆弱用例** - `test_usage_stats_is_read_only_spec` 原先取 `names[-1]`，
      追加新工具就让不相干用例变红；改为 `get_spec("get_usage_stats")` 按名取
- [x] **文档同步** - `SKILL.md`（核心能力增「工具发现」、工具表新行、目录树、
      用例数 343→411、安全约定的「显式参数才 spawn」对照表加第三条）、`docs/format.md`
      （新增「发现候选清单」字段表 + 示例 + 三态表、契约行、词表闭环说明）、
      `docs/architecture.md`（能力表、目录树、4.4 新节、模块表加 `discovery.py` 行与
      `tool_specs.py` 975→1038、7.2 门禁纪律行、6.1 工具表 17→18、实现索引）、
      `docs/mcp-conformance.md`（`discover_tools` 归入 `openWorldHint` 一类并说明
      「能力面含执行就不声明只读」，顺带修正 12/16 两处陈旧工具数）
- [x] **未做 e2e 场景** - 本期授权口径就是「不探测用户真机」，而 e2e 层要证的是
      真实子进程与 stdout 通道不被污染；这两点分别由「清单里只写 `{ENV}` token」的
      静态守卫 + 协议回环用例（真实会话、PATH 换成 tmp bin、`subprocess.run` 换成
      记录型桩）在更低成本上锁定。Scoop 适配器（P2）落地时再补 e2e 场景
      —— **P2-07 已落地（2026-10-19），但提交级 e2e 场景仍未补**：真机验证走的是
      e2e 基建（`stdio_session` + `call_tool`）的一次性脚本，见
      「包管理适配（P2-07，2026-10-19）」；仓内 e2e 套数照旧是 9 个场景
      - **后续（2026-10-18 晚）场景⑨ 已补**：真机跑 `run_commands=true` 时暴露出
        「子进程继承 server stdio 管道 → 偷走 inbound JSON-RPC → 整个网关死」，
        这一条**桩级用例结构上抓不到**（桩不真的创建子进程，管道就不会被读走），
        必须真实子进程 + 真实会话才能验。详见「真机端到端打通（P1 收尾，2026-10-18 晚）」
- [x] **明确不做** - ①不把发现结果自动回填 `data/tools/`（登记是人的决定）；
      ②不做清单自动生成/穷举扫描（与「声明式」定位冲突，且会扩大读取面）；
      ③不并发探测（首版候选个位数，串行够用）；④不把 `not_installed` 当事实结论用；
      ⑤不在仓库测试里跑任何真实命令面

### 真机端到端打通（P1 收尾，2026-10-18 晚）

背景：用户要求「根据未完成 Task 把本项目加进 MCP 列表，用 MCP 做端到端调用」。
这一步把 NE-10 留下的**唯一欠账**——「真机结论要等客户端调一次 `discover_tools`」——
真的还上了，并且那一次调用当场抓出三个只在真机上才会现形的问题。

- [x] **登记进 Qoder MCP 列表** - 经 App MCP 控制面 `manage_mcp add` 登记，条目名
      `awam-tool-manager`（stdio，command = pyenv 3.12.0 `python.exe`，args = `src/server.py`）；
      `query_mcp` 复核 `status=ready`、18 个工具全部列出
- [x] **两条必须留档的机制约束** -
      ① **会话的 `mcp_call` 路由是会话开始时的快照**：新登记的 server 在**当前**会话里
      不可路由（两种命名猜测都返回「No connected MCP server offers a tool named …」，
      `mcp_list` 里也没有它）。要在同一会话里「登记完立刻原生调用」做不到——只能起新会话。
      用户选定「我起新会话代跑」，因此新会话
      （`create_chat_session`，sessionId `67f6656b-4fae-4bee-9d73-5fea8bf8ed69`）负责走
      Qoder 原生通道，本仓库侧先用**真实 stdio MCP 客户端**出结论。
      ② **常驻 server 进程不会自己重载源码**：第一轮原生调用报回的仍是**修复前**的行为
      （`command.path` = 无扩展名垫片 `S:\Libs\Scoop\shims\scoop` → `WinError 193`、
      `{SCOOP}` / `{ProgramFiles(X86)}` 未展开、steam 判 `unknown`、
      summary `8 / 0 / 1`）。根因不是修复无效，而是那个 stdio 进程**起来的时间在修复之前**，
      Python 只在 import 期读源码。`manage_mcp retry` 重启同一条连接后原样重跑，逐项翻转：
      `scoop.cmd` → 0.5.3、`python.bat` → 3.12.0、scoop `dir_count` 14 包 / 4 bucket、
      steam → `not_installed`、summary `8 / 1 / 0`，且随后 `{}` 存活探针仍正常返回。
      **结论：改完 server 代码要先重启 MCP 连接再验证**，否则拿的是旧代码的结论
- [x] **真机结论（修复后，两条独立通道一致）** - 通道 A = 仓库侧真实 stdio MCP 客户端，
      通道 B = Qoder 原生 MCP 会话（`mcp__awam_tool_manager__discover_tools`，重启连接后）。
      只读调用：`8 installed / 1 not_installed / 0 unknown`，`registered 3 / unregistered 6`；
      `run_commands=true`：版本号取回 scoop `0.5.3`、winget `1.12.350`、node `22.22.2`、
      python `3.12.0`，服务全程存活。修复前同一调用是 `5 installed / 0 not_installed / 4 unknown`
      （四个 unknown 全是假结论，见下③）
      - 命中位置实测：PowerToys `C:\Program Files\PowerToys\PowerToys.exe`、
        Quicker `C:\Program Files\Quicker\QuickerStarter.exe`、
        Everything `C:\Program Files\Everything\Everything.exe`、
        Scoop `S:\Libs\Scoop\apps`、uTools `%APPDATA%\uTools\plugins`、
        node `C:\Program Files\nodejs\node.exe`
      - **对未完成 Task 的直接影响**：Scoop / PowerToys 判 `installed`（高信心）→ 两条 P2
        确实**可实测**；6 个 `unregistered` 候选就是现成的下一步接入清单。
        ~~Steam 判 `not_installed`（中信心）→ P0「接入 Steam」的本机阻塞项得到独立佐证~~
        **这条当时读错了**：Steam 本机明明装着（`S:\Program Files (x86)\Steam`），
        `not_installed` 是清单只声明 `C:\Program Files` 两处造成的**假阴性**，
        不是「本机没有」——见下「缺陷④」
- [x] **缺陷① 命令解析取到无扩展名垫片 → `[WinError 193]`** - 根因已从 CPython 源码坐实：
      `shutil.which` 在 Windows 上把候选建成 `files = [cmd] + [cmd + ext for ext in pathext]`
      并**目录外层、候选内层**循环，即**每个 PATH 目录都先试裸名**；而 Windows 的
      `os.access(X_OK)` 对任何存在的文件都成立，所以只要目录里有个无扩展名的 shell 垫片
      （`S:\Libs\Scoop\shims\scoop`、`.pyenv\pyenv-win\shims\python`）它就胜出，
      `CreateProcess` 随后报「%1 不是有效的 Win32 应用程序」。与 PATHEXT 在不在环境里无关，
      去掉 PATHEXT 实测同样返回裸名。
      修复：`resolve_executable()` 校验 which 结果的后缀是否在可执行集合内，不在就按
      **目录外层 / PATHEXT 内层**重扫 PATH（与 cmd.exe 语义一致——反过来写会先命中
      `WindowsApps\python.exe` 这个商店占位符、退出码 9009，本轮实测踩过并已由
      `test_path_order_beats_suffix_order` 钉住）（`98e3486`）。真机复跑取证：
      `command.path` = `S:\Libs\Scoop\shims\scoop.cmd`、
      `C:\Users\Administrator\.pyenv\pyenv-win\shims\python.bat`（都是带后缀的真身），
      版本号 0.5.3 / 3.12.0 正常取回
- [x] **缺陷② 子进程继承 server 的 stdio 管道 → 整个网关被打死** - `run_commands=true`
      第一次真跑时：客户端 `anyio.BrokenResourceError`，server 日志「工具 discover_tools
      执行完成，返回 11550 字符」之后紧跟「服务退出」。先怀疑 30s 后端空闲超时，
      用同会话 41.9s 后仍正常的 `list_tools()` **排除**了它，坐实是子进程读走了
      inbound JSON-RPC。修复：`stdin=subprocess.DEVNULL`——仓内
      `app_launcher.py:133` / `backend_client.py:187` / `backend_client.py:627` /
      `tool_help.py:186` 早有先例，discovery 是唯一漏网的那条 spawn 路径（`98e3486`）
- [x] **缺陷③ `{ENV}` token 只读进程环境 → 假 unknown** - host 给多少环境变量是**宿主的
      决定，不是我们的**：mcp SDK 拉起 server 时只带 `DEFAULT_INHERITED_ENV_VARS`
      白名单（`APPDATA / HOMEDRIVE / HOMEPATH / LOCALAPPDATA / PATH / PATHEXT /
      PROCESSOR_ARCHITECTURE / SYSTEMDRIVE / SYSTEMROOT / TEMP / USERNAME / USERPROFILE`，
      `mcp/client/stdio/__init__.py`），`ProgramFiles`、`ProgramFiles(x86)`、用户级
      `SCOOP` 都**不在**里面；而 Qoder 自己的宿主给了 `ProgramFiles` 却没给
      `ProgramFiles(x86)` 和 `SCOOP`（旧代码那一轮真机调用就是这样：steam 因
      `{ProgramFiles(X86)}` 没展开而判 `unknown`、scoop 的 `dir_count` 报
      「未能展开: SCOOP」）。两条真机路径各缺一块，所以「只读进程环境」在任何宿主上
      都不够。修复：`expand_path()` 增 Windows 注册表回退（`HKCU\Environment` →
      `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` →
      `ProgramFilesDir` / `ProgramFilesDir (x86)`），`REG_EXPAND_SZ` 展开后仍残留 `%`
      照样判取不到；**三态纪律一字未改**——取不到仍是 `inconclusive`，
      绝不退化成相对路径去试存在性（`731201f`）
- [x] **缺陷④ 声明式路径穷举不出「装在非常规盘」→ Steam 假阴性** - 真机复核发现自己
      写进文档的结论是错的：Steam 本机装着（`S:\Program Files (x86)\Steam`，16 个已安装
      appid、3 个 library），而清单只声明 `{ProgramFiles(X86)}\Steam\steam.exe` 与
      `{ProgramFiles}\Steam\steam.exe` 两处 C 盘位置，两个探针全 miss → 折成
      `not_installed`（中信心）。这不是「中信心就够了」的口径问题，是**结构性盲区**：
      只要安装位置不在声明模板里，探测面就无声缩小，而返回体里的 `note` 只说
      「探测面不是穷举」，读的人不会知道自己恰好撞上的是哪一例。
      修复：给发现层加第四类只读探针 `registry_value`（`hive` / `key` / `value` /
      `require_exists`），读安装器自己维护的登记值——本机权威值实测为
      `HKCU\Software\Valve\Steam\SteamExe` = `s:/program files (x86)/steam/steam.exe`
      （REG_SZ、正斜杠、盘符小写，所以探针要先归一分隔符）。读数分四档，
      **只有「键/值不存在 / 值不是非空字符串 / 登记值指向的路径已失效」判 miss**，
      权限不足、非 Windows、`REG_EXPAND_SZ` 展开后仍残留 `%` 一律 `inconclusive`，
      与三态纪律同构。清单里 steam 候选补这条探针后，本机整份清单从
      `8 installed / 1 not_installed / 0 unknown` 变为 **`9 / 0 / 0`**，
      steam 判 `installed`（高信心）且证据两行分别是「登记值命中 + 声明位置落空」。
      顺带修掉一个红绿才暴露的次生盲区：新探针类型若忘了在 `run_probes()` 接线，
      会掉进 path 流程、而 `probe.get("path", "")` 的空串经 `Path("")` 就是当前目录、
      **存在** → 假命中；现在未接线的类型一律判 `inconclusive`（`test_unwired_probe_kind_never_becomes_a_false_hit`）。
      **未接入 Steam**（用户口径「暂时不新接入了」）：这条只纠正发现层的事实结论与
      清单表达力，`data/tools/` 里没有新增 steam 条目
- [x] **测试：+16 条（425 → 441）** - `tests/test_discovery.py` 注册表探针段 12 条
      （登记值命中含正斜杠归一 / 值不存在判 miss / 僵尸登记判 miss / 权限不足判
      inconclusive / 展开残留判 inconclusive / `require_exists=false` 只看登记 /
      非 Windows 判 inconclusive / 登记值把声明路径全 miss 的候选救回 installed /
      未接线类型不假命中 / 清单校验 6 组参数化 / 仓库 steam 候选必须带不依赖盘符的
      证据），`FakeWinreg` 增 `raises` 以区分「不存在」与「读不到」。
      红绿三次：把 `FileNotFoundError` 并回通用 `OSError` → `absent 判 miss` 红；
      `require_exists` 默认翻成 `false` → 僵尸登记与 hit 证据结构两条红；
      摘掉 registry 接线 → 7 条红（含救回 installed 的回归用例，第一次变异时它
      因为假命中而没红，于是补了逐行证据断言 + 未接线守卫）
- [x] **测试：+14 条（411 → 425），e2e 场景 8 → 9** - `tests/test_discovery.py` 补命令
      解析 7 条（垫片替换 / 非 Windows 原样返回 / 命令不存在 / PATHEXT 顺序且跳过非
      可执行后缀 / 目录优先于后缀 / 无兄弟可执行文件时保留 which 结果 /
      「跑的是真目标不是裸垫片」）、注册表回退与 env 展开若干条（含
      `FakeWinreg` 桩与 Windows-only 的 `ProgramFiles` 用例）、以及
      `test_run_commands_executes_and_parses_version` 里对 `stdin=DEVNULL` 的断言；
      新增场景⑨ `tests/e2e/test_e2e_discovery_command.py` + 探针
      `tests/e2e/stdin_guard_probe.py`
- [x] **红绿验证** - 摘掉 `stdin=subprocess.DEVNULL` → 场景⑨ 9.33s 内红（客户端先断）
      且单测同时红；把 PATH 重扫改成后缀外层 → `python` 解析到商店占位符、用例红；
      去掉注册表回退 → `ProgramFiles` 类候选回到 unknown、用例红。还原后
      `425 passed / 9 deselected`、`9 passed / 425 deselected` 全绿

### 包管理适配（P2-07，2026-10-19）
能力问题先回答：「接了 Scoop 能不能装软件 / 查已装 / 打开已装」——**三个都能**，
但按副作用分级：查询纯读盘、启动与安装都要显式表态，安装额外过破坏性门禁。
形态按用户选定：**新增通用包管理工具面**（不是把 scoop 包登记成一个个 `kind=app` 条目），
当前后端只有 `scoop`（`pkgman.BACKENDS = ("scoop",)`），`winget` / `npm` 未接线
（这里的轻量包管理面仍只有 scoop；winget 在 full 模式的意图工具面单独接线，见
「暴露模式与意图级工具门面」，`npm` 决定不接）。

- [x] **适配层 `src/pkgman.py`（644 行）** - 一律**直读安装器落盘清单**，不跑
      `scoop list`（那条命令要拉 PowerShell，冷启 3~6s，且它读的东西盘上本来就写着）。
      真机（`S:\Libs\Scoop`）勘察钉出六个坑，每个都对应一处实现：
      ① `apps/<名>/current` 是 junction，本机 14 个包里 **6 个已断链**成空目录 →
      回落读残留版本目录，状态报 `current_broken` 而不是假装 `ok`；
      ② 有 2 个包**根本没有 manifest**（`scoop` 自己就是）→ `no_manifest` 是合法态；
      ③ 桶与包的对应关系在 `install.json` 的 `bucket`，不在 manifest；
      ④ manifest 的 `bin` 写成字符串时按字符迭代产出垃圾路径（真机表现为「bw.exe 有 6 个 bin」）
      → `_as_entries` 归一化；
      ⑤ manifest 不声明 `bin` 但 `shims/<包名>.exe` 能跑（真机 `bayes`）→ shim 候选；
      ⑥ **扩展名决定能不能直接 spawn**（`.exe` / `.cmd` / `.bat` 可，`.ps1` 抛
      `OSError [WinError 193]`，`.vbs` 不可）→ 候选带 `runnable`，`launchable` 与
      `launch_target` 由同一批候选算出，避免一行数据两种答案
- [x] **4 个对外工具（18 → 22）** - `list_packages` / `search_packages` 纯读盘、
      零 spawn；`launch_package` 默认只回「会起哪个入口、为什么是它」（候选带
      `exists` / `runnable`），显式 `execute=true` 才起，起法复用 `app_launcher.launch_app`
      （保活注册表键用 `scoop/<包名>`，与人工登记条目不同命名空间）；
      `install_package` 默认只回 argv，真的装要 `execute=true` **且** `allowDestructive=true`。
      `docs/architecture.md` §4.5 记的是**别人写在盘上的东西**，所以没有 schema、没有校验器，
      只有实测结论
- [x] **门禁：`policy` 增 `package` 分支，fail-closed** -
      `PACKAGE_CONFIRMED_OPS = frozenset({"launch_package"})` 是**免授权白名单**，
      不是黑名单：`install_package` 以及任何将来新增的包操作，只要没登记进去就默认要
      `allowDestructive`。方向选反一次，以后加操作就是默认放行。
      `destructive.mode=deny` 时报 `permission_denied`（客户端无法自行解除），
      文案也不再提示「传 allowDestructive=true」
- [x] **整数边界口径** - 刻意不写 `int(value or DEFAULT)`：那会把显式 `0` 当「没传」
      悄悄放大成默认值。改成 `_clamp_int`——只有 `None` / 空串才是缺省，传了就越界夹取，
      非整数抛 `invalid_params`；夹取只在 `pkgman` 一处做，handler 不再二次换算（两处口径迟早分家）
- [x] **真机端到端（2026-10-19，真实 stdio MCP 会话）** - 复用 `tests/e2e/helpers.py`
      的 `stdio_session` + `call_tool` 对 `src/server.py` 起真会话（这一步是必要的绕路：
      上一轮 `manage_mcp retry` 后 `query_mcp list_tools` 已报 22 个，但**本会话的工具路由
      快照仍钉在会话开始时的 18 个**，`mcp_call` 报「No connected MCP server offers a tool
      named …」——那是客户端快照，不是服务端缺陷）。实测 9 次调用：
      `tools/list` = **22 个**，4 个包管理工具全部可见；
      `list_packages` = 14 个包 / `ok 6` / `current_broken 6` / `no_manifest 2` / `launchable 12`，
      `rootSource` = 「进程环境 SCOOP」；`state=current_broken` 筛出 6 条而 `count` 仍是 14；
      `search_packages keyword=yt limit=5` = 扫 4 个 bucket / **4287 份清单**、返回 5 条；
      `launch_package 7zip` 预览挑中 `kind=shortcut` 的 `7zFM.exe`（`executed=false`）；
      `launch_package scoop entry=scoop.ps1` → `code=not_spawnable` + 候选里
      `scoop.cmd runnable=true` / `scoop.ps1 runnable=false`（坑⑥ 在真机上的正面兑现）；
      `install_package yt-dlp` 预览回 argv 且 `alreadyInstalled=false`；
      `install_package yt-dlp execute=true`（不给授权）→ `errorType=destructive_blocked`，
      stderr 同步落审计行 `{"action":"install_package","kind":"package","sub_op":"yt-dlp",
      "tool":"scoop:yt-dlp","decision":"block","rule_hit":"package_destructive",
      "client_declared":false,"policy_mode":"require_confirm"}`——**没有下载、没有改本机清单**
      （审计的 `sub_op` 记包名，与 mcp 分支记子工具名同位不同义，读日志时按 `kind` 分派）
- [x] **两条真机才看得出的边界事实** - `search_packages limit=0` 与
      `list_packages manager=winget` 都在**进 handler 之前**被 SDK 按 inputSchema 拦下，
      返回的是裸文本 `Input validation error: 0 is less than the minimum of 1` /
      `'winget' is not one of ['scoop']`（不是本项目的 JSON 错误载荷）。
      含义有两层：schema 声明的 `minimum` / `enum` 是第一道真边界，`_clamp_int`
      与 `resolve_root` 的 `invalid_params` 是第二道（直调 handler、以及从 `call_backend_tool`
      之类不经 schema 的路径进来时才生效）；同时说明**协议级校验失败的载荷形态不受我们控制**，
      文档不再宣称「所有错误都是结构化 JSON」
- [x] **未跑的路径（明确留白）** - `launch_package execute=true` 真起进程这唯一一条没在真机跑：
      它会拉起 7zFM 这样的 GUI，属于对用户机器的可见副作用，需要用户在场确认。
      该路径由单测钉住参数传递（`seen["tool_id"] == "scoop/goodpkg"`），
      底层 `launch_app` 本身早在「`launch_tool` 真正执行启动」一节真机验证过。
      本次探针脚本曾在 `.scratch/p2-probe/`（gitignore，本轮收尾时已清理，结论逐条记在本节），
      **没有升格成提交级 e2e 场景**：
      它读的是真机 scoop 布局，数字不可复现；可复现的部分（三态、入口挑选、门禁、
      错误码）已经全部在 `test_pkgman*` 的合成树里钉住
- [x] **测试 425 → 517** - 新增 `tests/test_pkgman.py`（51 条，全部 `tmp_path` 合成 scoop 树，
      **不碰真机 scoop**）、`tests/test_pkgman_handlers.py`（22 条：默认零 spawn /
      门禁位置 / 错误分类不靠猜文案——把执行器 patch 成「一调就炸」的协程来钉「默认不 spawn」）；
      `tests/test_policy_gate.py` 决策表补 5 行 package + 新增第 6 节 fail-closed 与审计标签；
      golden `tools_list.json` 重基线为 22 个（`git diff --stat` = 190 insertions / 0 deletions，
      证明 18 条既有声明逐字节未变）。守卫全部兑现：漏改 `EXPECTED_TOOL_NAMES` / SKILL.md
      工具表 / 文档计数任一处都会红。口径 `517 passed / 9 deselected`，`-m e2e` = `9 passed / 517 deselected`
- [x] **顺带纠一个会腐烂的字面量** - e2e 场景⑨ 里 `assert tools_after == 18` 改成
      `== len(EXPECTED_TOOL_NAMES)`：写死数字的用例每次加工具都要跟着改，等于没在断言任何东西

### Steam 接入（P0，2026-10-19）
- [x] **形态选定并落地** - 用户口径「自研读本地文件 + 登记 app 条目」。落地时把「登记条目」
      收敛成 **`kind=launcher` + `launcher.type=steam`**，不是 `kind=app`：游戏清单是动态的、
      每个 appid 一条 `data/tools/*.json` 会立刻腐烂，而启动器面已经有
      `sync_launcher_actions` / `list_launcher_actions` / `run_launcher_action` 三个工具在管
      「一个启动器下面的 N 个可执行项」这件事。**因此对外工具数没有从 22 变成 23**——
      能力全部走现成三件套，`tool_specs.py` 零改动。`kind=app` 那条路同时留着：条目里写了
      `launch`，所以 `launch_tool` 依然能启动 Steam 客户端本体（查得到、预览得出命令）。
      也不接第三方 `steam-local-mcp`：那是个 MCP 后端，等于把「读本地清单」这件事
      交给一个不受我们控制、且要长连接和门禁配 `destructiveTools` 的子进程
- [x] **本机实测布局（全部来自只读勘察，不是从文档推）** - 权威位置是注册表
      `HKCU\Software\Valve\Steam` 的 `SteamPath` = `s:/program files (x86)/steam`
      （**正斜杠**，且不在 `C:\Program Files`）；`libraryfolders.vdf` 声明 **3 个库 / 27 个 appid**，
      盘上只有 **16 份 `appmanifest_*.acf`**（`S:\Program Files (x86)\Steam` 5 +
      `D:\03Games\SteamLibrary` 11），`E:\Games\SteamLibrary` 目录当前已不存在。
      「vdf 声明了 ≠ 装着」是本节的核心事实：那 11 个只在 vdf 里出现的 appid 是历史残留，
      认了就会造出 11 个点不开的游戏
- [x] **`src/steam_library.py`（282 行，纯只读）** - `resolve_root()`：条目 `dataDir` →
      注册表 `SteamPath` → 穷举常见位置（兜底命中时来源说明写明「未经登记值确认」），
      正斜杠归一化；`parse_vdf()`：自写 tokenizer（制表符缩进、成对 `\\` 转义、
      字符串化的数字保持原样），**截断的块和「键后没有值」抛 `SteamError` 快速失败**，
      而显式写全的 `"key" {}` 判合法——半份清单比没有清单更坏，但作者手写的空对象不是错误；
      `installed_games()`：games 只来自 acf，vdf 声明量汇成 `declaredAppTotal`，
      失踪库进 `missingLibraries` + note，逐库带 `exists` / `manifests` / `count`
- [x] **适配器接线** - `launcher_client.py`（628 → 761 行）：`SUPPORTED_ADAPTERS =
      ("quicker", "utools", "steam")` 与 `schema` 的 `launcher.type` enum 逐字一致（有守卫测试
      钉住，两边漏改任一侧即红）；`sync_actions` 先判 steam 分支（`dataDir` 可空、回退注册表），
      其余分支保持原样；`_scan_steam()` 把 acf 折成 `{id: "app:<appid>", name, type: "game",
      description: "库 <libname> / <MB>", appid, installdir, library, sizeOnDisk, lastPlayed}`，
      有失踪库时 `source` 降为 `partial`；`_run_steam()` **在 spawn 之前**校验 ID
      （`STEAM_ACTION_RE`，非数字直接拒），命令缺失回退 `SteamExe`，执行委托给
      `app_launcher.launch_app`（不新开 spawn 路径），返回体标 `deliveryOnly: true`
      ——客户端单实例、命令只做投递，退出码 0 不代表游戏起来了
- [x] **门禁按同型启动器口径配** - `data/tools/steam.json`：`destructive: true` 且
      **故意不配 `destructiveActions`**（配了就是把未命中的动作由拦转放，等于放宽安全属性），
      `run_launcher_action` 没有 dry-run、唯一的意图旋钮是 `allowDestructive`，
      所以启动游戏必须显式传 `allowDestructive=true`；`version` 写 `latest` 而不是猜一个号
      （版本要从 `version.dll` / `appinfo.vdf` 之类内部文件取，超出「只读解析本地库清单」的约定范围，
      勘察时也被权限分类器拦下，未重试）
- [x] **真机 stdio 会话七次调用**（探针脚本 `.scratch/p2-probe/live_steam_tools.py`，走
      `tests/e2e/helpers.stdio_session`，非本会话的客户端快照；该目录已在收尾时清理，
      逐条结果与审计行原样记在下面）：
      ①`search_tools {keyword:"Steam"}` → `count: 1`；
      ②`sync_launcher_actions {tool_id:"steam"}` → `count: 16, source: "partial"`，
      行形如 `{"id":"app:1509960","name":"PICO PARK","description":"库 SteamLibrary / 76 MB",
      "library":"D:\\03Games\\SteamLibrary"}`，服务端日志
      「Steam 根=s:\program files (x86)\steam（来源：注册表 SteamPath），库 3 个 / 清单 16 份 /
      已装 16 项 / vdf 声明 27 项」；③`list_launcher_actions`（不带 keyword）→ 16 条；
      ④`list_launcher_actions {keyword:"PICO"}` → 1 条；⑤`get_tool_info {tool_id:"steam"}` → OK；
      ⑥`launch_tool {tool_id:"steam"}` → 预览
      `{"command":"S:\\Program Files (x86)\\Steam\\steam.exe","args":[]}`，**未 spawn**；
      ⑦`run_launcher_action {tool_id:"steam", action_id:"app:400040"}`（不传授权）→
      `errorType: destructive_blocked`，`audit.log` 同步落一行
      `{"event":"destructive_gate","action":"run_launcher_action","tool":"steam",
      "kind":"launcher","sub_op":"app:400040","decision":"block","rule_hit":"entry_level",
      "client_declared":false,"policy_mode":"require_confirm"}`
- [x] **测试 517 → 557** - 新增 `tests/test_steam_library.py`（20 条：嵌套块、`\\` 解转义、
      三处 `SteamError` 拒绝、空 `{}` 接受、`resolve_root` 优先级与兜底标注、
      失踪库不变成 games、逐库 `exists`、坏清单跳过并给原因、无 vdf 只当主库、
      「vdf 在但一个库都没解析出来」要抛）与 `tests/test_steam_launcher.py`（19 条：
      动作 id 顺序与 `source` 两态、关键词搜名称/库、注册表定位（无 `dataDir`）、
      定位不到根目录的报错、argv 逐字 `["-applaunch","400040"]`、`tool_id` 透传、
      `deliveryOnly`、非数字 ID **在 spawn 前**被拒、`ignoredParam`、`wait` 透传、
      退出码 1 折成失败载荷、enum↔`SUPPORTED_ADAPTERS` 守卫、仓库 steam 条目形状、
      以及走真实 `server.dispatch_tool` 的未授权拦截）。
      `launcher_client.launch_app` 全程被打桩，**新测试一次真 spawn 都没有**。
      口径核对：摘掉这两个新文件后基线是 **518**，即此前已有 1 条用例没写进「517」这个数
      （518 + 39 = 557），本次按实测改口径而不是把差值摊平
- [x] **未跑的路径（明确留白）** - `run_launcher_action(..., allowDestructive=true)` 真启动
      游戏没有执行：会拉起全屏客户端，属对用户机器的可见副作用，需用户在场。
      同样只留白不猜的是 Steam 版本号（见上条 `version: "latest"`）
- [x] **顺带** - 提交级 e2e 场景仍是 9 个，Steam 这轮的复现证据同样只落在探针脚本里
      （`.scratch/` 走本地 exclude，收尾时已整体清理）：合成树能钉的行为已全在 `test_steam_*`，
      真机数字（3 库 / 27 声明 / 16 份 acf）不可复现，不升格成用例

### 入参别名与必填自描述错误（P1，2026-10-19）

> 本节记录**首次**落地的机制与当时的真机复核。别名表的范围已被同日的
> 「别名二次收敛」小节改掉：`toolId` / `actionId` / `actionName` / `q` / `text` / `pkg` /
> `log_level` / `tool_id ← name` **均已不再接受**，`acceptedParams` 的清单也相应变短。
> 当前对外契约以「别名二次收敛」与 `SKILL.md` 的别名表为准。

- [x] **起因是真实会话里的两次空转**（用户截图）：同一个意图先
      `search_tools {"query":"everything"}` → `params must have required property 'keyword'`，
      再 `get_tool_info {"id":"everything"}` → `params must have required property 'tool_id'`，
      两次都到不了 handler；Agent 于是放弃 MCP 通道，自己起 shell 跑 `which es.exe` 探路径。
      宿主那句文案是通用模板，既没列可接受的名字、也没说这工具干什么，调用方只能再猜一轮
- [x] **根因定在「两道入站校验都在我们代码之前」**：① 宿主按下发的 `inputSchema` 预校验，
      ② mcp SDK 在 `call_tool` 路由里按同一份 schema 再校验一次（「筛选维度增强」一节已实测
      SDK 会拦 `required` 与 `enum`）。所以 handler 里 `.get()` 兜底救不了这种请求，
      唯一能动的层是**导出的 schema** + **dispatch 入口的归一化**——方案 ① 就落在这两层
- [x] **① 别名兼容：声明表不动，导出时展开** - `ServerToolSpec` 新增 `arg_aliases`（按 spec
      覆盖默认表，空元组=退出），默认表按规范名共享：`tool_id ← id/toolId/name`、
      `keyword ← query/q/text`、`package ← name/id/pkg`、`level ← log_level`。
      `build_mcp_tools()` 改用它算出的 `export_input_schema()`：补别名属性 + 把必填规范名展开成
      **任选组**（单组 `anyOf`、多组 `allOf`）；声明表里的 `required` 仍只写规范名，
      SSOT 不变（`test_export_does_not_mutate_declaration_table` 钉住）。
      口径（实测自 `aliases_for` / `export_input_schema`）：22 个工具里 **19 个带别名**，
      没有的 3 个是 `list_tools` / `discover_tools` / `list_packages`（本来就没有必填参数）；
      **17 份导出 schema 带 `anyOf`/`allOf`**——`call_backend_tool`、`set_backend_log_level`
      有两个必填，用 `allOf` 包两组；`get_backend_status`、`get_usage_stats` 的 `tool_id`
      是选填，别名只在归一化里生效、不成任选组
- [x] **`name` / `id` 的歧义按工具裁决，不搞一刀切** - `call_backend_tool` 的 `name` 指
      **后端子工具**、`run_launcher_action` 的 `id` 指**动作**，与默认的「name=工具 ID」不是
      一个语义，所以这两条用 spec 级 `arg_aliases` 覆盖（`call_backend_tool` 只给
      `tool_id ← toolId`、`tool_name ← name/backend_tool_name/sub_tool_name`；
      `run_launcher_action` 给 `action_id ← id/actionId`、`action_name ← name/actionName`）
- [x] **JSON Schema 的 `required` 是 AND，不是 OR**（实现时踩到，值得留痕）：把四个候选名塞进
      同一个 `required` 等于要求全传。首版这样导出后 `test_protocol_roundtrip` 3 条当场红
      （`Input validation error: {'keyword': '文件'} is not valid under any of the given schemas`），
      改成「一个候选名一条 `{"required": [名]}`、组间用 `allOf` 相乘」才通。
      这次红同时第二次证明了 SDK 侧确实按 schema 拦入站请求
- [x] **归一化排在 handler 之前，三条规则** - `server.dispatch_tool` 进 handler 前调
      `normalize_arguments()`，**handler 一律只认规范名、不感知别名**：
      ①只给别名 → 搬到规范名并移除别名键；②规范名与别名同时给且**取值相同** → 按规范名走不报错，
      **取值不同** → `invalid_params`（两个目标 ID 不猜意图）；③空白算没给
      （`{"id": ""}` / 纯空格不是有效目标，Agent 传空串比不传更常见）。
      埋点顺序也是有意的：`_record` 闭包读的是重绑后的 `arguments`，所以 `usage.log` 的
      `target`（`usage.py:43` 取 `args.get("tool_id")`）按规范名计，别名调用不会漏进
      `get_usage_stats` 的 `targets` 维度
- [x] **② 必填缺失自描述** - 折完别名仍缺必填时，`invalid_params` 的文案一次给全：
      「缺少必填参数 tool_id。可接受参数名：`tool_id` / `id` / `toolId` / `name`（任选其一，
      规范名是 tool_id）。示例：`{"tool_id": "everything"}`。本工具用途：…」，
      并把同样内容结构化进 `param` / `acceptedParams` / `example` 三个字段（示例取值来自
      `_EXAMPLE_VALUES`，Agent 照抄即可，不用再猜第二轮）
- [x] **退役名不得借别名复活** - `RETIRED_PARAM_NAMES = {mcp_tool_id, launcher_id}`：
      `validate_specs` 在 import 期拒绝「别名=退役名」「别名撞已声明参数」「同一别名归属两个
      规范名」「`arg_aliases` 键拼错（覆盖永不生效）」四类坏 spec，测试再从导出 schema 全文
      反向扫一遍——两条路此前已在「协议合规」里统一掉，这里防止被当成兼容性别名加回来
- [x] **`{}` 的边界：谁拦的要说清** - 一个键都不给时先被**入站校验**拦下
      （真机 stdio 会话实测：SDK 文案 `Input validation error: {} is not valid under any of the
      given schemas`），拿不到我们的自描述文案。保留 `required` 语义是刻意取舍：不写别名的
      客户端仍应知道这个参数必填；真实失败形态是**猜错参数名**（已接管），不是什么都没传。
      用例 `test_empty_arguments_is_caught_upstream_of_our_handler` 只钉「谁在拦」这个边界不漂移
- [x] **顺带抓出并修掉一个自己造的稳定性缺陷** - `_declared_params` 原本返回 `set`，
      而 `aliases_for` 按它逐个追写别名属性：**字符串 set 的迭代序随进程哈希随机**，
      同一份代码两次运行导出的 `properties` 顺序不同，golden 逐字节比对就成了掷骰子
      （变异实测：`PYTHONHASHSEED=0/1/42` 下守卫红、`7` 下绿）。改成按声明顺序返回 `list` +
      `test_exported_alias_properties_follow_declaration_order` 钉住顺序
- [x] **golden 重采工具化** - 新增 `scripts/regenerate_tools_golden.py`（不带参数重写、
      带 `--check` 只比对）：序列化直接 `import` 测试里的 `_fields()`，基线与守卫同源，
      不另抄一份规则；落盘用 `write_bytes` 而不是 `write_text`（Windows 上后者把 `\n`
      翻成 `\r\n`，基线整篇重排）。基线重采后仍是 22 个工具，差异全部是别名属性与任选组
- [x] **测试 557 → 579** - 新增 `tests/test_arg_aliases.py` 22 条，分五面：别名经**真实内存协议
      会话**（5 条参数化）与规范名结果逐字等价 / 冲突与空白的自描述载荷 / 使用统计按规范名计 /
      退役名不复活 / 别名表与声明表自洽（含导出为纯函数、schema 承认的别名归一化都认、
      `validate_specs` 两类坏 spec 快速失败、22 个工具数量与顺序不变）。
      红绿：把 `dispatch_tool` 里归一结果丢弃 → 6 条红（5 条别名等价 + 1 条统计）；
      `_declared_params` 退回 `set` → 顺序守卫在 4 个 seed 里 3 个红。口径
      `579 passed / 9 deselected`，`-m e2e` = `9 passed / 579 deselected`
- [x] **真机（子进程会话，非本会话快照）** - 用 `tests/e2e/helpers.stdio_session` 起真实
      `src/server.py` 会话跑一遍：22 个工具；`get_tool_info` 导出
      `anyOf: [{"required":["tool_id"]},{"required":["id"]},{"required":["toolId"]},{"required":["name"]}]`；
      `search_tools` 属性列 `keyword/kind/status/publisher/query/q/text`；`{"id":"everything"}` 与
      `{"query":"文件"}` 均 isError=false（结果与规范名一致）；`{"id":""}` → `invalid_params` +
      `acceptedParams=[tool_id,id,toolId,name]` + `example={"tool_id":"everything"}`；
      `{"tool_id":"everything"}` 零回归；`{}` 由 SDK 先拦。探针走 heredoc 内联，未在仓库留文件
- [x] **宿主通道复核（同日补齐，方案 ① 的最后一块）** - 上一条留白等的是「新快照」，实际不用新会话：
      server 重注册 + 宿主重新同步 MCP 之后，**同一会话**里 `mcp_get` 就下发了新 schema。
      逐项原文记录：
      ①`mcp_get search_tools` → `properties` 含 `query/q/text`、`anyOf` 四条候选；
      `mcp_get call_backend_tool` / `set_backend_log_level` → `allOf` 包两组 `anyOf`，
      说明 ajv 这套宿主认得我们导出的形状；
      ②别名调用全通：`search_tools {"query":"everything"}` → `count: 2`；
      `get_tool_info` 的 `{"id"|"toolId"|"name": "everything"}` 三次返回体逐字相同；
      `list_launcher_actions` 混用两组别名（`{"tool_id":..,"q":"剪贴"}` 与
      `{"toolId":..,"keyword":"剪贴"}`）结果同样一致；
      ③边界照实：`{}` 与退役名 `{"mcp_tool_id": "everything"}` 都由**宿主**先拦，但文案已
      顺带自描述——`params must have required property 'tool_id', ... 'id', ... 'toolId',
      ... 'name', params must match a schema in anyOf`，把可接受的名字直接列给 Agent；
      ④穿透到我们载荷的两类：`{"id":""}` → `invalid_params` + `acceptedParams` +
      `example={"tool_id":"everything"}`；`{"tool_id":"everything","id":"quicker"}` →
      「取值不同…只保留一个即可」；
      ⑤`allOf` 真调用：`call_backend_tool {"toolId":"__no_such_backend__","name":"search_files"}`
      过得了宿主校验、进到 handler 回 `config_error`（**未路由、未 spawn、未改任何后端状态**）；
      ⑥统计口径在宿主通道同样成立：`get_usage_stats {"days":1,"id":"everything"}` →
      `targets: [{"toolId":"everything","count":5,"failed":1}]`、
      `errorTypes: [{"errorType":"invalid_params","count":1}]`——别名调用按规范名入账，
      冲突那次失败也进了台账
- [x] **顺带纠正一条口径（写进项目记忆）** - 「会话快照连 `inputSchema` 一起冻」不是永久状态：
      它会在宿主重新同步 MCP（server 重注册 / 工具集变动）时刷新。所以判定顺序是
      **先 `mcp_get` 核对 schema 是不是新版**，是就直接走宿主通道，仍旧再退到 stdio 子进程会话，
      不要一上来就绕道

### 别名二次收敛：词汇改由 description 承载（P1，2026-10-19 同日）
- [x] **上一条的宿主复核顺带否证了「别名是主修复」** - 复核里那些成功调用是**我按新 schema
      手写**的；回看失败原图，Agent 两次猜名之前并没有拉过 `inputSchema`（宿主延迟加载只把
      **工具名**放进上下文，`description` 才是稳定可见的那段）。也就是说：给它别名属性它也不看，
      而它真正会读的那段文本里，一个参数名都没提
- [x] **勘察口径（实测，非推断）** - `build_mcp_tools()` 改造前：22 个工具、**17 个带必填参数，
      这 17 份描述里必填名出现 0 次**；别名属性 65 个，`inputSchema` 相对无别名基线净增
      **8744 字符**（6240 → 14984）。别名是花在最不被阅读的那段文本上的钱
- [x] **① description 承载必填名** - 新增 `export_description()`：对**没提过**的必填名追加
      `。必填：<名>（可写 <别名>）`（别名只带第一个），`build_mcp_tools()` 的 `description`
      改用它。落地后 17/17 覆盖，净增 **334 字符**（1055 → 1389，平均 15/工具，
      最大 `call_backend_tool` +37）。技能 `description`（frontmatter）同步补上
      `tool_id`/`keyword`/`package` 三个名字与各自写法
- [x] **② 保留 `anyOf`，不退回单一 `required`** - 它的拒绝文案**自带可接受名字清单**
      （宿主那句 `params must have required property 'tool_id', ... 'id', ... params must
      match a schema in anyOf`），等于白拿一次自描述；砍掉任选组会把这个免费能力也丢掉
- [x] **③ 别名表砍到最小集：65 → 22 个属性** - `DEFAULT_ARG_ALIASES` 只留
      `tool_id ← id`、`keyword ← query`、`package ← name`（三条都是「业界公认 + 真实会话里
      被写过」的交集）；spec 覆盖同步收窄（`call_backend_tool` 留 `tool_name ← name`，
      `run_launcher_action` 留 `action_id ← id` / `action_name ← name` 并把 `tool_id` 的
      别名置空——它的 `required` 只有 `tool_id`，把 `id` 同时给两个规范名会在
      `validate_specs` 里以「同一参数取值不同」误伤）。
      砍掉并**连归一化口子一起关**：camelCase `toolId`/`actionId`/`actionName`（与全表
      snake_case 自相矛盾）、`q`/`text`/`pkg`（生态里没这条惯例）、`log_level`（标准写法是
      `level`，方向本来反了）、`tool_id ← name`（协议自己的 `tools/call.name` 指「调哪个
      工具」，留着把错层写法收成假命中）
- [x] **净账（对照上一版 65 别名，全部实测自 golden 差异）** - 去掉 43 个别名属性
      （平均 **111 字符/个**，`properties` 段 -4772），`inputSchema` 全表 14984 → 9312，
      描述 +334，**对外上下文合计 -5338 字符**（16039 → 10701）；与此同时必填名的可读覆盖
      从 0/17 变成 17/17。相对无别名基线的代价从 +8744 降到 **+3406**
- [x] **纠正两处自己写错的数字** - 源码注释与 `SKILL.md` 此前写「别名属性 88 字符/个」，
      实测平均 111（43 个去掉 4772 字符），按实测改回；描述成本从「约 +15/工具」补全为
      「+334 合计、最大 +37」
- [x] **测试 579 → 589（`test_arg_aliases.py` 22 → 32 条）** - 新增第六面：每个必填名必须出现在
      导出描述里 / 从**真正下发的 `Tool` 对象**读（`build_mcp_tools()` 若绕过两个导出函数即红）/
      描述只做追加且单工具增量 ≤40、声明表不被改写 / `DEFAULT_ARG_ALIASES` 钉死为最小集 /
      别名属性总数钉死 22 / 被砍写法（`toolId`、`get_tool_info` 的 `name`、`q`、`text`、
      `actionId`）在**协议层**就被拒而不是悄悄折回。
      红绿：`build_mcp_tools` 退回 `spec.description` → 上线描述守卫 1 条红；内存里把
      `tool_id` 加宽回 `("id","toolId")` → 预算 36≠22、`toolId` 又能折回，两类守卫同时感知。
      口径 `589 passed / 9 deselected`，`-m e2e` = `9 passed / 589 deselected`
- [x] **golden 重采并复核稳定** - `scripts/regenerate_tools_golden.py` 重写后 `--check` 在
      `PYTHONHASHSEED=0/1/7/42/123` 下五次一致通过，落盘 0 个 `\r\n`；新基线里
      16 份 schema 带任选组（`run_launcher_action` 的 `tool_id` 别名置空后退回普通
      `required`），`mcp_tool_id`/`launcher_id` 仍全文不出现

### 暴露模式与意图级工具门面（P1，2026-10-20）
- [x] **三种模式是同一张声明表的不同切片** - `ServerToolSpec` 新增 `exposure` 元组：
      `lazy` 22（旧契约原样）、`default` 29（+7 意图前门）、`full` 33（再 +4 补充工具）。
      `build_mcp_tools(mode)` 与 `dispatch_tool` 同源过滤，schema / 注解 / handler 不复制第二份
- [x] **模式解析与 instructions** - `EXPOSURE_MODE` > `data/exposure.json` 的 `mode` >
      `lazy`；进程启动解析一次（客户端重连才看到新 `tools/list`），非法取值快速失败。
      `initialize.instructions` 由 `exposure._FAMILIES` 同一张表按模式生成；lazy 的规则文本
      不点名任何不可见工具（有守卫测试逐名检查）
- [x] **7 个 default 意图工具** - `search_files` / `open_path` / `open_app` / `search_games` /
      `open_game` / `search_actions` / `run_action`。只做「解析 → 门禁 → 调用既有能力」：
      Everything / Steam / Quicker / uTools 都复用原 manager，必要时自动拉起所需后端
- [x] **4 个 full 补充工具** - `get_file_info` / `get_everything_status` /
      `search_winget_packages` / `install_winget_package`；winget 走 full 的意图面，
      `pkgman` 轻量包管理面仍只有 scoop
- [x] **副作用纪律** - `open_*` / `run_action` / `install_winget_package` 默认只回预览，
      `execute=true` 才执行；破坏性另需 `allowDestructive=true`；多候选一律返回列表或要求
      精确名称，不自动选
- [x] **文件路径执行面 `src/file_ops.py`** - 预览只读 `Path`/`stat`；执行走 `os.startfile`
      （ShellExecute）或 `explorer.exe /select,<path>` 的 argv 形式，不拼 shell 字符串；
      spawn 的 stdio 全 DEVNULL。`policy` 新增 `path` 种：按后缀判可执行
      （`.exe/.bat/.ps1/.lnk/...`），`reveal=true` 视为非破坏
- [x] **未暴露工具在协议面等同 `unknown_tool`** - 与未知工具同文案、同 usage 记账，
      不泄露存在性；意图工具解析出的真实目标经 `_usageTarget` 记进 usage.log 后再剥离，
      不进对外载荷
- [x] **测试 589 → 635，e2e 9 → 11** - 新增 `test_exposure.py`（模式优先级 / 切片 /
      instructions 可见性 / dispatch 兜底 / 自动拉起）与 `test_facade_tools.py`
      （解析 / 门禁 / 路由；外部进程全部 mock）；golden 三份基线由
      `regenerate_tools_golden.py` 一次全量 `--check` 通过（22 / 29 / 33）
- [x] **真机 full 只读复核（临时 stdio 会话 + 临时 runtime）** - instructions 出现
      `当前暴露模式：full。`、33 工具；`search_files` 返回 3 条；`get_everything_status`
      可用（`es.exe` 1.1.0.36）；`search_games` 返回 3 个 Steam 游戏；`open_path` /
      `open_app` / `open_game`（3DMark）均 `executed:false`，预览命令
      `steam.exe -applaunch 223850`；未起 GUI / 游戏

### 包升级查询与 UniGetUI 外部口径（P1，2026-10-24）
- [x] **定位先划清**：UniGetUI 在本项目里**不是包管理后端**（它是瘦客户端，数据要连一个
      活的 UniGetUI 会话），只作为「谁有可用更新」的**可选外部口径**。scoop 仍是唯一
      读盘后端，`npm` 不接（2026-10-19 决定）、`winget` 属执行面（走 `winget-mcp`）不变
- [x] **读盘层 `pkgman.read_disk_updates`** - 已装版本 vs `buckets/<bucket>/bucket/<包名>.json`
      的版本差集，零 spawn、零联网；比不了的包进 `unknown` 并给成因（读不到 install.json /
      直链安装只记 url / 索引里没这个包 / 已装版本缺失），不猜成「已最新」。
      版本比较键 `version_key` 从 `_version_dirs` 内部提到模块级，顺手修了空段 bug
      （`version_key(None)` 原返回 `[(1, '')]`，现在正确是 `[]`）
- [x] **UniGetUI 适配层** - `locate_uniget_cli()`（`AWAM_UNIGET_CLI` > `%LOCALAPPDATA%\Programs\UniGetUI`
      > `Program Files\UniGetUI`，**不走 PATH**，取不到就枚举试过的路径）、
      `uniget_status_argv` / `uniget_updates_argv`；`manager` 只认 17 个稳定 id 的白名单，
      `--elevated` 这类选项串按 `invalid_params` 拒（参数位一开就是走私面）
- [x] **`run_argv` 契约扩展：`max_output` + `stdoutTruncated`** - 原 4000 字符**留尾**对装包
      日志是对的，对 JSON 是拦腰砍。这条是**真机第一跑打出来的**：本机整份
      `uniget package updates` 57652 字符 / 238 条，按默认口径切完解析必失败，
      于是 JSON 类调用方自己抬到 `UNIGET_OUTPUT_MAX_CHARS=200000`，
      仍被截时如实报「输出被截断，用 manager 分开查」而不是给 0 条
- [x] **数组键按命令不同，读错就是假阴性** - `package updates` 给 `updates`、
      `package installed` 才给 `packages`（本机各取一份真实应答核对）。
      解析器原先读 `packages`，改成 `updates` 并**要求它是数组**：`status=success`
      但缺数组时抛错，不许退化成空清单（空清单会被下游读成「都没有更新」）
- [x] **两条口径、只有一条是默认值** - `check_package_updates`（第 5 个 full 工具）默认读盘；
      `execute=true` 才 spawn，且**先发 `status` 探一次**（唯一不需要 token 的命令），
      `running=false` 就绝不发第二条——第二条会把参数转给 `UniGetUI.exe` 冷启动 GUI。
      外部口径可用时连 `resolve_root` 都不碰；不可用则退回读盘并把原因带在 `uniget.reason`；
      `manager` 是非 scoop 而外部口径又不在 → `install_required`，不给空清单
- [x] **测试 708 → 713** - 新增 5 条：`run_argv` 上限与截断标志、`updates` 数组键三条
      （空数组=都最新 / 缺数组=报错 / 夹具改真实形状）、探测按命令抬不同上限、
      截断载荷降级为读盘且理由带自救步骤
- [x] **真机复核（临时 stdio 会话 + `EXPOSURE_MODE=full` + 临时 runtime，本机 UniGetUI 2026.3.2 在跑）**
      - `tools/list` 34 个工具，`check_package_updates` 的 schema 三个可选参数、
        `readOnlyHint=true` / `destructiveHint=false`
      - 默认口径：`engine=read_disk`、root `S:\Libs\Scoop`（进程环境 SCOOP）、
        `scanned=15` `count=4` `unknown=5` `truncated=false`、`cli=null`（没探过入口，确实零 spawn）
      - `execute=true`：`engine=uniget` `count=100` `truncated=true`（238 条按上限截），
        `manager=winget` → 62 条不截断
      - **两口径在 scoop 上给出同样的 4 条**（age 1.3.1→1.3.2 / clink 1.6.3→1.9.5 /
        innounp 0.50→2.66.1 / scrcpy 2.3.1→3.3.3），名字与两侧版本逐字段一致
      - 拒绝面：`manager=--elevated` → `invalid_params`；`manager=winget` 不带 `execute` →
        `install_required`；会话结束后 stderr 无真异常（spawn 没吃协议通道）
- [x] **刻意没测的一格**：「UniGetUI 会话未运行」这条降级只在单测里覆盖——
      本机 UniGetUI 常驻，为了测试去关掉用户的 GUI 不在授权范围内。
      真机第一跑意外验证过**另一条**降级（解析失败 → `engine=read_disk` + 理由原样带回）

### UniGetUI 只读查询面（full，2026-10-31）
- [x] **范围先划死**：只做只读查询——`status` / `manager list` / `source list` /
      `package search|installed|details|versions|updates`。安装 / 升级 / 卸载 /
      操作等待 / 提权一律不做；UniGetUI 仍是瘦客户端，不是包管理后端，
      scoop 读盘口径的默认值不变
- [x] **4 个意图工具**（全部 `exposure=("full",)`）：
      `list_package_managers`（可选 `include_sources`）、`search_uniget_packages`
      （`manager`+`keyword` 必填）、`list_uniget_packages`（`manager` 必填，默认 50 条）、
      `get_package_details`（`manager`+`package` 必填，默认带版本历史）。
      注解统一 `openWorldHint=true` + `destructiveHint=false`，**不声明
      `readOnlyHint`**——查询会 spawn 底层安装器并联网，声称只读是不诚实的
- [x] **一条通路 `run_uniget_read`**：定位 → `status` 探会话 → 数据命令 → 解析。
      `running=false` 绝不发第二条（第二条会冷启动 GUI）；`timeout` 同时传给探针与
      数据命令；JSON 输出抬到 `UNIGET_OUTPUT_MAX_CHARS`，仍被截时报「被截断」
      并给缩小范围的下一步
- [x] **失败相位可行动**：`locate` → `install_required`、`status` → `connection_error`、
      `query`/`parse` → `runtime_error`；解析器识别 `No package matching` 归 `not_found`。
      UniGetUI 的失败应答**多数**是退出码 0 的 `status=error` JSON，但 `package details`
      的 not-found 实测退出码 2、正文形状不变——失败分支因此先尝试解析正文再归类，
      不能拿退出码当判据（真机验收发现并修复，原实现把它误判成 `runtime_error`）
- [x] **参数面**：`manager` 只认 17 个稳定 id；`package` / `keyword` 是自由文本但仍挡
      开头 `-`（选项走私）、控制字符与超长；全部走 argv 直传，不过 shell。
      `validate_name` 顺带补了开头 `-` 的拒绝（原字符集允许短横开头）
- [x] **测试基线**：full 31 → 35；golden 重采（仅 full 变化）；别名属性 21 → 23
      （`search_uniget_packages` 的 `keyword ← query`、`get_package_details` 的
      `package ← name`）；新增 pkgman 解析/构造/`run_uniget_read` 与 4 个 handler 用例
- [x] **真机只读验收（临时 stdio 会话 + `EXPOSURE_MODE=full` + 临时 runtime，
      本机 UniGetUI 2026.3.2 在跑，全程未关闭用户 GUI）**：
      `tools/list` 35 个；四个新工具 schema 必填项与注解符合约定
      （`openWorldHint=true`、无 `readOnlyHint`）；
      `list_package_managers` 返回 **11** 个安装器（其中 6 个 ready）与
      `include_sources=true` 的软件源清单；
      `search_uniget_packages`（manager=scoop, keyword=7zip）返回 5 条；
      `list_uniget_packages`（scoop）返回 13 条；`get_package_details`（scoop/7zip）
      返回真实详情与空版本历史（scoop 适配层不提供，非失败）；
      拒绝面 `manager=--elevated` → `invalid_params`、未知包 id → `not_found`。
      延迟实测：details 约 9s、versions 约 3s，默认带版本历史因此约 12s——
      MCP 侧默认 120s 足够，客户端若把单次调用限制在 10s 内需显式
      `include_versions=false`

### 工具接入
- [x] Everything（`kind=app`，返回启动命令）
- [x] Everything Search MCP（`kind=mcp`，已实测连接、调用、启停跑通）
- [x] **WinGet（`kind=mcp`，官方内置）** - 实测：握手（winget-mcp 1.29.380 / 协议 2025-06-18）、
      2 个子工具带完整 schema、查询与安装双路径路由验证；2026-10-16 深夜按网关真实链路
      复跑端到端（start → tools/list → `find-winget-packages` 返回真实包数据 → stop），
      查询免确认放行、安装仍被子工具级门禁拦截
- [x] **Steam（`kind=launcher` + `launcher.type=steam`）** - 2026-10-19 完成，见「Steam 接入
      （P0，2026-10-19）」：自研只读解析 `libraryfolders.vdf` + `appmanifest_*.acf`，
      真机 `sync_launcher_actions` 得 16 个 `app:<appid>` 动作（`source=partial`，
      声明 3 库/27 appid、盘上 16 份 acf），关键词搜索命中（"PICO"→1 条），
      启动走 `steam.exe -applaunch`，未授权时被破坏性门禁拦下（真机验过 `destructive_blocked`）
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
- [x] **非等价点（显式标注 NE-1~NE-9，U6 要求对用户可见）**：NE-1 `json.dumps` 统一带
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
      路径逐字不变）；
      NE-8 `search_tools` / `list_tools` 增 `status` / `publisher` 筛选维度
      （2026-10-18 P1 落地，详见「筛选维度增强」小节：**两处既有条目的 description +
      inputSchema 变更**（golden 是修改而非追加，diff 28+/2-），返回体每条新增
      `publisher` 键；无新工具、无新副作用）；
      NE-9 新增第 17 个对外工具 `get_usage_stats` + dispatch 全出口埋点
      （2026-10-18 P3 落地，详见「工具使用统计」小节：golden 纯追加重基线 16→17，
      diff 35+/0-；**新增一项本地写副作用**——每次 `tools/call` 往
      `<runtime>/usage.log` append 一行（不含参数），客户端可见的载荷字段无变化；
      既有 16 条 spec 逐字节不变）；
      NE-10 新增第 18 个对外工具 `discover_tools`（2026-10-18 P1 落地，详见
      「工具自动发现」小节：golden 纯追加重基线 17→18，diff 39+/1-（唯一删除是文件
      末尾的 `]`），既有 17 条 spec 逐字节不变；**新增一项本机读取面**——按
      `data/discovery.json` 声明的路径做文件系统存在性/目录列举读取，
      默认不起任何子进程，`run_commands=true` 才执行清单里的版本命令；
      annotations 标 `openWorldHint` 且**不声明** `readOnlyHint`）

### 其他
- [x] Git 仓库初始化，代码版本管理
- [x] .gitignore 配置（测试脚本、缓存文件排除）

### 已知边界（非缺陷，记录备查）
- **uTools 协议投递的「UI 不呈现」已排除（2026-10-18 晚 UI 级实调确认，无需改代码）**：
  经对外通道单跑 `run_launcher_action(tool_id=utools, action_id="JSON 编辑器/Json",
  allowDestructive=true)` 返回 `exitCode=0`，同时用 Win32 连续采样抓到主面板
  （hwnd 标题 `uTools`、类 `Chrome_WidgetWin_1`、`WS_EX_TOOLWINDOW`）从
  `WS_VISIBLE=False` 翻到 `True` 且拿到前台，之后 25s 观察窗内保持可见——
  失焦不自动隐藏，只有手动关闭/ESC 才隐藏。受控实测（同进程内先起采样再投递
  同一条 `cmd /c start "" utools://…`）给出**投递→呈现 ≈1.1s**。
  旧结论「投递成功但界面不渲染」的两条成因：① **取证方式**——单次快照落在 1.1s
  呈现延迟之前、或落在面板已被关掉之后，就得到 `visible=False`；② 当时 uTools
  只剩托盘、零可见窗口的会话状态。教训是**判断呈现必须连续采样，不能单跑快照**
  （脚本曾在 `.scratch/utools-ui-2026-10-18/`，走本地 exclude、不进版本库，已清理）。
  仍然成立的边界：这是 fire-and-forget，返回体的 `success` 只代表「系统接收了协议」，
  不代表 UI 呈现、更不代表功能执行结果——要结果只有 MCP 通道或 HTTP 传输两条路
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
- **`kind` 没有 `enum`、`status` 有**（NE-8 留下的刻意不一致）：新维度带了 enum，
   拼错的值在 SDK 校验层就 `isError`；`kind` 传错值仍是空列表。给 `kind` 补 enum
   同样要改下发 schema，还会把「外部客户端一直这么传」的调用直接判死，属独立改动，
   本期有意不做，不是遗漏
- **`status` 缺省口径两处不同**：列表/搜索投影与本期筛选都按「条目未写 = `installed`」
   （`tool_specs.py` 的 `t.get("status", "installed")`），`get_tool_info` 却按
   「条目未写 = `unknown`」（`tool.get("status", "unknown")`）。改任一侧都会动既有条目
   之外的对外返回体，本期只做筛选、不动它；`data/tools/` 现有 5 条都显式写了 status，
   实机无差异。要对齐时应当**一起对齐**并申报独立 NE
- **使用统计只覆盖「经本 server 分发的调用」**（NE-9 的刻意边界）：用户直接在
   Quicker / uTools / 开始菜单里启动工具、以及任何其他进程的历史调用都不在
   `usage.log` 里，因此 `get_usage_stats` 的 `total=0` 不等于「没人用」。
   `total=0` 时返回体自带 `note` 说明口径，聚合数只作趋势参考、不作事实结论
- **多实例并发写不保证行序，也不做日志轮转**：`usage.log` 与 `audit.log` 同款
   append-only、无锁、无轮转（首版刻意不做，避免过度工程）。网关可能同时拉多个
   server 进程，行与行之间会交错——计数以行数为准（每行自带 `tool`/`ts`，交错不影响
   聚合正确性），但「第 N 行紧跟第 N+1 行」这类顺序假设不成立
- **发现面是声明出来的，不是穷举**（NE-10 的刻意边界）：`discover_tools` 只看
   `data/discovery.json` 里写了的位置和命令，清单没写的安装路径（如自定义 scoop 根、
   绿色软件目录）发现不了。所以 `not_installed` 只是**中信心**结论，不能当「本机确实
   没装」的事实用；`unknown` 更进一步——连一个位置都没检查成（`{ENV}` 在进程环境
   与注册表回退两处都取不到；mcp 客户端拉起 server 时会剥掉 `ProgramFiles` 等
   非白名单变量，所以注册表回退是必需的，见 `731201f`）。
   这条限制由返回体的 `note` 每次自证，不靠调用方读文档。
   **它已被真机兑现成一次真实的假阴性**（2026-10-18 晚）：Steam 装在 `S:\Program Files
   (x86)\Steam`，清单只声明 C 盘两处 → 判 `not_installed`，而文档当时把它当成了
   「本机没装」的证据。修法是给清单加**不依赖盘符的证据**（`registry_value` 读安装器
   自己写的登记值），并立一条维护规矩：**每个候选都该有一条不依赖盘符的证据**，
   否则「没发现」迟早会被再读成「没有」
- ~~**本期没在用户真机上跑过探测**（授权口径：代码先行）：仓库内单测与协议回环用例
   全部用 `tmp_path` 合成的假目录、假 PATH、假清单，只有两条用例**静态**读仓库清单
   （校验它合规、且路径只用 `{ENV}` token）。真实结论要等客户端调一次 `discover_tools`~~
   → 已解决（2026-10-18 晚）：server 已登记进 Qoder MCP 列表，真机 `discover_tools`
   跑过（只读 + `run_commands=true` 两次），结论为 `8 installed / 1 not_installed /
   0 unknown`、版本号 scoop 0.5.3 / winget 1.12.350 / node 22.22.2 / python 3.12.0；
   这一跑同时抓出并修掉了三个只在真机上现形的缺陷（命令解析取到无扩展名垫片、
   子进程继承 stdio 管道打死网关、`{ENV}` 只读进程环境导致四个假 unknown），
   详见「真机端到端打通（P1 收尾，2026-10-18 晚）」。
   **其中「1 not_installed = Steam 没装」这个结论本身也是错的**：同日晚复核发现
   Steam 装在 S 盘，属第四个缺陷（发现面假阴性），修法是加 `registry_value` 探针；
   修完同一台机器的整份清单是 **`9 installed / 0 not_installed / 0 unknown`**
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
- **Steam 的「声明」不等于「装着」**：`libraryfolders.vdf` 里 `apps{appid: size}` 记的是
  历史安装意图，本机声明 27 个 appid、盘上只有 16 份 `appmanifest_*.acf`，且其中一个库
  （`E:\Games\SteamLibrary`）目录已经不存在。适配层只认 acf，所以清单比 vdf 少 11 项——
  这是刻意的：认了就会造出 11 个点不开的游戏。代价是**卸载残留不会出现在任何一面**，
  只看 `declaredAppTotal` 与 `count` 的差值和 `missingLibraries` 才知道有没有掉东西
- **Steam 启动只保证投递**：客户端单实例，`steam.exe -applaunch <appid>` 的退出码 0 只代表
  steam.exe 收到了请求，不代表游戏起来了（返回体 `deliveryOnly: true`）。同理 `version`
  填 `latest` 不猜号——真版本号在 `appinfo.vdf` / `version.dll` 这类客户端内部文件里，
  超出本项目「只读解析库清单」的约定范围

### 能力模型重构：Provider / Capability / Runtime / Target（2026-10-25）

把「Backend」从对外词汇里拿掉：`kind` 即提供者类型（`mcp` / `launcher` / `app`），
统一用 `tool_id` 找提供者、`capability_id` 找能力，MCP 才有运行时（Runtime）概念。

- [x] **通用工具合并（11 → 8，硬切无兼容别名）**：
      `list_backend_tools` + `list_launcher_actions` → `list_capabilities`；
      `call_backend_tool` + `run_launcher_action` → `call_capability`；
      `sync_backend_tools` + `sync_launcher_actions` → `sync_capabilities`；
      `start_backend` / `stop_backend` / `get_backend_status` / `get_backend_logs` /
      `set_backend_log_level` → `start_runtime` / `stop_runtime` / `get_runtime_status` /
      `get_runtime_logs` / `set_runtime_log_level`。旧名在 dispatch 侧按 `unknown_tool` 拒绝
- [x] **统一能力描述符**（`src/capabilities.py`）：`capabilityId` / `name` / `description` /
      `domain` / `targetKind` / `risk`（read/write/destructive）/ `execution` /
      `deliveryOnly` / `source` / `inputSchema` / `exposedAs` / `available` / `metadata`
- [x] **调用语义**：`call_capability` 用嵌套 `arguments`（缺省 `{}`）；只读能力默认执行，
      写/破坏性能力默认只预览，`execute=true` 才产生副作用，destructive 另需
      `allowDestructive=true`；MCP 运行时未启动会自动拉起
- [x] **错误自描述**：能力不存在 / 参数不合法时，载荷带 `toolId` / `capabilityId` / `inputSchema`
- [x] **暴露模式重新基线**：31 个工具，lazy 19 / default 26 / full 31
      （`check_package_updates` 保留在 full，因此不是原计划的 30）
- [x] **统计口径**：usage 新增 `capabilities` 维度，按 `(toolId, capabilityId)` 聚合
- [x] **测试与文档**：单测/e2e 全量迁移、golden 三档重采集、`SKILL.md` /
      `docs/format.md` 同步

---

## 三、待完成 ⏳

### P0 - 核心打通
- [x] 接入 Steam — ✅ **已完成（2026-10-19，见「Steam 接入（P0，2026-10-19）」）**：
      形态定为**自研只读解析本地文件**，不接第三方 steam-local-mcp；条目登记为
      `kind=launcher` + `launcher.type=steam`（不是每个游戏一条 `kind=app`），
      因此**没有新增对外 MCP 工具**，能力走现成的 `sync_capabilities` /
      `list_capabilities` / `call_capability` 三件套。
      本机实测复核：位置 `S:\Program Files (x86)\Steam`（权威登记值
      `HKCU\Software\Valve\Steam` 的 `SteamPath` / `SteamExe`，值是正斜杠），
      `libraryfolders.vdf` 声明 3 个库 / 27 个 appid（`S:` 主库 5 份 acf、
      `D:\03Games\SteamLibrary` 11 份、`E:\Games\SteamLibrary` 目录已不存在），
      盘上合计 16 个已安装 appid → 同步得 16 条动作、`source=partial`。
      早先那句「真机 `discover_tools` 判 `not_installed` / 中信心，佐证本机没有 Steam」
      是**发现清单的假阴性**——它只声明了 `C:\Program Files` 两处位置，装在 S 盘就全落空；
      已随第四类探针 `registry_value` 修掉，现在判 `installed` / 高信心。
- [x] ~~接入 npm（npm MCP）~~ → **决定不接（2026-10-19，用户口径「把 NPM 标记不接」）**：
      npm 官方没有 MCP server，「接 npm」实际是「接某个第三方包」，需求始终没能澄清到
      可以选型的程度；而且 npm 的已装清单没有可靠的纯读盘来源（不像 scoop 那样有
      `apps/*/current/manifest.json`），要列包就得 `spawn`，与本项目「默认零 spawn」的
      姿态相反。同类需求已由包管理面（P2-07）承载：`list_packages` / `search_packages` /
      `launch_package` / `install_package` 的后端是 `BACKENDS` 里登记过的安装器，
      将来若要接 npm，正确形态是给 `src/pkgman.py` 加一个 `npm` 后端（全局包读
      `<prefix>/node_modules/*/package.json`），而不是新增一条 P0「MCP 接入」

### P1 - 体验完善
- [x] 工具自动发现 — ✅ 已完成（2026-10-18，见「工具自动发现（P1）」小节）：
      `data/discovery.json` 声明候选清单 + `src/discovery.py` 只读探测
      （路径存在 / 注册表登记值 / 目录条目数 / 命令是否在 PATH），第 18 个对外工具
      `discover_tools` 给出 `installed` / `not_installed` / `unknown` 三态结论；
      取版本号要显式 `run_commands=true`。**本期口径**：代码先行，仓库内单测全部
      跑 `tmp_path` 合成数据、不探测用户真机，真机结论由客户端调一次 `discover_tools` 得到
      —— 该口径已于同日晚兑现：server 登记进 Qoder MCP 列表并真跑过只读 +
      `run_commands=true` 两次，结论与那一次跑抓出的三个缺陷见
      「真机端到端打通（P1 收尾，2026-10-18 晚）」
      （「工具帮助文档查看」「筛选维度增强」已于 2026-10-18 完成，见上节 P1）

### P2 - 生态扩展
- [x] 接入 Scoop — ✅ 已完成（2026-10-19，见「包管理适配（P2-07，2026-10-19）」）：
      形态是**通用包管理工具面**（`list_packages` / `search_packages` / `launch_package` /
      `install_package`），不是把每个包登记成一条工具条目；直读安装器落盘清单，
      真机 `S:\Libs\Scoop` 实测 14 个包 / 4 个 bucket / 4287 份清单，
      查询零 spawn、启动与安装要显式 `execute=true`（安装再要 `allowDestructive=true`）
- [x] 接入 WeGame（腾讯平台）— ⛔ **本机确实没装主程序，无法实测**（2026-10-19 只读勘察，
      结论与本轮 Steam 那次相反，这次是真的没有）：
      ① 全盘限深扫描 `wegame*.exe` 只命中 1 个残留
      `C:\Users\Administrator\AppData\Local\WeGame\WeGameMiniLoader(2001918)\WeGameMiniLoader.exe`
      （5.8 MB，是迷你加载器不是客户端）；
      ② `HKLM\SOFTWARE\Tencent\WeGame`（32 位视图）键**存在但零值**，仅一个空子键 `Apps`；
      ③ HKLM/HKCU × 32/64 四路卸载表里 WeGame、WeGame1 全无条目（只有 腾讯应用宝 / 腾讯会议）；
      ④ `C:\Program Files\Tencent` = Androws/QQNT/WeMeet/WeType，`C:\Program Files (x86)\Tencent`
      = Qzone/QzoneMusic/UpdateSvr，均无 WeGame 目录。
      **决定不登记、也不写进 `data/discovery.json`**：残留 MiniLoader 恰好是
      「有残留 ≠ 装着」的反向陷阱，拿它当命中会造出一个新的假阳性，与缺陷④ 同源不同向。
      要接得先装客户端；届时登记形态按 `kind=app` + `launch` 走，动作面（启动游戏）不在本项目范围内
- [x] 接入 Quicker — ✅ 已完成（见「启动器接入」）：实测同步 212 个动作、160 个有名称，
      关键词搜索命中（"搜索"→5 条 / "剪贴"→2 条 / "识别二维码"→1 条），
      调用全链路通（含成功路径：真实执行动作并返回 OK）
- [x] 接入 uTools — ✅ 已完成（2026-10-18，见「启动器接入」）：实测同步 218 个动作、
      source=auto，协议调用成功（`start` 退出码 0）；fire-and-forget 无回传、
      payload 仅文本等限制见勘察报告 §5
- [x] 接入 PowerToys — ✅ 已登记（2026-10-19，`data/tools/powertoys.json`，`kind=app`）：
      主程序 `C:\Program Files\PowerToys\PowerToys.exe`（发现层早已判 `installed` / 高信心），
      版本与模块数取自本机 `settings.json`（`powertoys_version` v0.97.1、`enabled` 段 32 个模块
      实测 9 个启用），更新渠道 `winget` / `Microsoft.PowerToys`。
      条目里写死了一条边界：PowerToys 以托盘常驻方式运行，`launch` 只确保进程起来，
      单模块动作（FancyZones 编辑、PowerToys Run 唤起）由全局快捷键触发、**不经本条目**，
      所以它是「可列 / 可搜 / 可启动」而不是「可操作」

### P3 - 高级功能
- [ ] 智能路由：根据用户意图自动选择从哪个安装器安装
- [ ] 版本检测和更新提醒
      （2026-10-18 真机已验通「取本机版本」这一半：`discover_tools` 的
      `run_commands=true` 拿到 scoop 0.5.3 / winget 1.12.350 / node 22.22.2 /
      python 3.12.0。）「最新版本」这一半在 2026-10-24 落地成 `check_package_updates`
      （见「包升级查询与 UniGetUI 外部口径」）：读盘差集 + UniGetUI 外部口径。
      仍缺的是**提醒**那一层——工具只能被问，没有把结果写回 `data/tools/` 条目、
      也没有定时/主动通知的口径
- [x] 工具使用统计 — ✅ 已完成（2026-10-18，见「工具使用统计（P3）」小节）：
      dispatch 五个出口各记一行 JSONL 到 `<runtime>/usage.log`（不含参数），
      第 17 个对外工具 `get_usage_stats` 按窗口聚合；刻意不做跨进程计数精度保证、
      不做日志轮转，也无法统计「用户直接启动工具」那条不经过本 server 的路径
- [ ] 权限控制（哪些工具 AI 可以直接执行）— **剩余仅方案 B**（2026-10-19 澄清口径）：
      落地设计见 `docs/architecture_permission_model_design_2026-10-17.md`（ARC-P1-05，
      推荐方案 D = A + C，B 为远期结构载体）。A 与 C **已落地**：Phase 1 把破坏性判定
      下沉到子操作级并收敛为统一入口 `policy.decide_destructive`（见「合规与安全」小节），
      Phase 2 用 `data/policy.json` 把「谁能被调用 / 破坏性走什么模式」变成客户端不可覆盖的
      部署策略（`destructive.mode=deny` 时传 `allowDestructive` 也不放行，见 SKILL.md「安全约定」）。
      未做的只有 **B**（设计文档 §2.2 与 §3.3：spec 级 `policy` 注解 + dispatch 统一拦截、
      按会话临时授权），文档只列了进入条件、刻意不排期：①出现第二个并发客户端/网关实例需要
      按实例区分权限；②需要按会话临时授权；③执行类工具从 2 个涨到 5+ 个、散落判定再次失控。
      条件满足时 A 的判定内核直接迁进 spec 字段，dispatch 拦截点不动

---

## 四、数据格式规范

### 4.1 两层结构

| 层级 | 文件位置 | 说明 |
|---|---|---|
| 工具条目 | `data/tools/{tool_id}.json`（仓库内） | 每个工具一个文件，文件名即 tool_id |
| 后端子工具缓存 | `<runtime_dir>/backend-tools/{tool_id}.json` | 由 `sync_capabilities` 自动生成（A14 起在 `%LOCALAPPDATA%\.awam-tool-manager`） |
| 启动器动作清单 | `<runtime_dir>/launcher-actions/{tool_id}.json` | 由 `sync_capabilities` 自动生成 |
| 对外 MCP 工具 | `src/tool_specs.py`（P1-04 起为单一声明源；`server.py` 只做协议装配与分发） | 暴露给 AI 客户端的工具列表（**非磁盘数据层**） |

> 早期设计中的「启动器配置层」`data/launchers/` 与「适配器层」`src/adapters/` 已废弃移除。
> MCP 现在是**工具的一种类型**（`kind=mcp`），而非独立层级，
> 所有对外工具统一用 `tool_id` 指定目标。
>
> `data/policy.json`（部署策略）与 `data/discovery.json`（发现候选清单）同样是
> 仓库内人工维护的真相源，但**都不是数据层**：前者是「允许怎么调」的策略，
> 后者是「该去找什么」的声明，都不描述已登记的工具，也不由 `schema.py` 生成字段表
> （策略走 `schema.POLICY_SCHEMA`，清单走 `discovery.validate_manifest`）。

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
  过白名单，否则 `start_runtime` 只返回 `install_required`，服务预热退化为纯检测

### 4.3 后端子工具缓存（`<runtime_dir>/backend-tools/{tool_id}.json`）

由 `sync_capabilities` 写入运行时目录（A14 前在 `data/backend-tools/`），
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
│   │   ├── powertoys.json      # kind=app
│   │   ├── quicker.json        # kind=launcher
│   │   └── utools.json         # kind=launcher
│   ├── policy.json             # 部署策略（ARC-P1-05 Phase 2，AWAM_POLICY_FILE 可覆盖）
│   └── discovery.json          # 发现候选清单（discover_tools 的声明源，NE-10）
├── src/
│   ├── server.py               # MCP Server 入口：协议装配 + 薄分发 + 生命周期
│   ├── tool_specs.py           # 对外 MCP 工具声明式注册表（SSOT：35 条 spec + handlers）
│   ├── capabilities.py         # Provider / Capability / Runtime 路由层
│   ├── policy.py               # 破坏性门禁统一判定 + 部署策略（data/policy.json）
│   ├── errors.py               # 业务错误出口（ToolError / fail / ensure_success）
│   ├── store.py                # JSON 存储层（tools 走仓库，缓存走 runtime dir）
│   ├── registry.py             # 工具注册中心
│   ├── backend_client.py       # 后端 MCP 客户端与连接池（stdio）
│   ├── launcher_client.py      # 启动器适配器（非 MCP：Quicker / uTools / Steam）
│   ├── app_launcher.py         # kind=app 启动执行（launch_tool 的 execute=true 路径）
│   ├── tool_help.py            # 帮助信息分层取数 + 帮助命令执行（get_tool_help 底座）
│   ├── discovery.py            # 本机工具发现（声明清单 + 只读探测，默认零 spawn）
│   ├── pkgman.py               # 包管理器适配层（scoop：读盘列包 / 搜 bucket / 解析入口 / 构造安装命令）
│   ├── steam_library.py          # Steam 库只读解析（libraryfolders.vdf + appmanifest_*.acf，以 acf 为准）
│   ├── usage.py                # 调用使用统计：dispatch 出口记 usage.log + 按窗口聚合
│   ├── runtime_paths.py        # 运行时目录解析 + 旧路径迁移（A14）
│   └── schema.py               # 两层数据的 JSON Schema 校验 + **文档生成源**
├── scripts/
│   ├── generate_format_doc.py  # 由 schema.py 生成 docs/format.md 的字段表
│   └── regenerate_tools_golden.py  # 重采对外 tools/list 基线（`--check` 只比对，不落盘）
└── tests/                      # 23 个 test_*.py（589 passed）+ 9 个 e2e 场景（默认不收集）
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
    ├── test_discovery.py             # 发现清单校验 + 三态折叠 + 默认零 spawn
    ├── test_pkgman.py                # 包管理适配层：合成 scoop 树 + 三态 + 入口挑选 + 只构造命令 + 升级差集与 UniGetUI 载荷解析
    ├── test_pkgman_handlers.py       # 包管理五个工具：默认零 spawn + 门禁位置 + 错误分类 + 两条升级口径的降级理由
    ├── test_steam_library.py         # vdf/acf 只读解析：嵌套块/转义/快速失败/逐库存在性
    ├── test_steam_launcher.py        # steam 适配器：动作形状 + 注册表定位 + argv + 未授权拦截
    ├── test_arg_aliases.py           # 入参别名与规范名等价 + 必填自描述 + schema 与归一化自洽
    ├── test_usage.py                 # 使用统计：日志形状 + 日历日窗口 + dispatch 出口埋点
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
        ├── test_e2e_discovery_command.py  # 场景⑨：discover_tools 取版本号不吃网关 stdio 输入
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
