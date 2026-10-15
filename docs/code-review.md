# awam-tool-manager 代码审核报告

> 审核日期：2026-10-15　|　基准 commit：`65d108c`
> 审核依据：[`docs/architecture.md`](architecture.md)（项目功能与架构总结）+ 源码 + 实测验证
> 审核范围：`src/`（4 个模块）、`data/`（3 个 JSON）、`tests/`、依赖声明

---

## 0. 第四次更新：P2 已修复（最新）

修复了全部 7 项 P2，并补了 `test_store.py` / `test_server_dispatch.py` 两个测试文件（测试总数 13 → 41 passed）。

| 编号 | 问题 | 处理方式 |
|---|---|---|
| D12 | 单个坏 JSON 拖垮全服务 | `load_json` 捕获 `JSONDecodeError` / `OSError` / 顶层非对象，告警后返回 `None`；`list_tools()` 跳过损坏文件 |
| D13 | 写盘非原子，崩溃留半截 JSON | `save_json` 改为「同目录临时文件 → `fsync` → `os.replace`」；写失败时删除临时文件且不破坏原内容 |
| D9 | `limit=0` 返回全部日志 | `get_logs` 显式判 `limit<=0` → 返回 `[]`（`logs[-0:]` 等价于全量） |
| D10 | `set_backend_log_level` 形同虚设 | `_log()` 按 `LOG_LEVEL_PRIORITY` 在**入队前**过滤，避免心跳 debug 挤掉 error |
| D14 | 连接配置无法热更新 | `get_client` 在客户端空闲（stopped/error）且磁盘配置变化时原地重建并继承日志；运行中禁止热替换以免留下孤儿进程；工具被删除后不再返回旧缓存 |
| D15 | `lastSynced` 硬编码 `"now"` | 写入 `datetime.now(timezone.utc)` 的 `%Y-%m-%dT%H:%M:%SZ`，与 `docs/format.md` 示例一致 |
| D19 | server 分发无自动化测试 | 新增 `tests/test_server_dispatch.py`（14 例）：12 条分发路径、错误载荷 `errorType`、资源返回 `ReadResourceContents`、工具定义无遗留参数名 |

---

## 0.0 第三次更新：P1 已修复（2026-10-15 晚）

按第二批计划修复了全部 4 项 P1，并补了首个 backend_client 单元测试文件（13 passed）。

| 编号 | 问题 | 处理方式 |
|---|---|---|
| D4 | 同步失败用空结果覆盖缓存 | `sync_tools_catalog` 遇空列表拒绝写盘，返回错误保留原缓存 |
| D5 | `_cleanup` 共用 try 泄漏子进程 | 两个 context 各自独立 try/finally；另加 `_cleanup_lock` 保证串行幂等 |
| D6 | 后端调用无超时 | `initialize` / `list_tools` / `call_tool` / `ping` 全部 `wait_for`，默认 30s 可由 `connection.timeout` 覆盖；超时后连接标记不可用并后台清理 |
| D7 | `stop()` 未加锁 | stop 与 start 共用 `_lock`；start 前先清理上次残留 context |
| D8 | 无异常兜底、错误风格不一 | 上一轮已随 `ToolError` 机制一并解决 |
| 附带 | 启动日志 join 假定参数全为字符串 | 改为 `str(a)` 拼接（原 D11） |
| 附带 | `_log` 每次import sys、魔法数字 30/300 散落 | 提到模块顶层并抽为类常量 |

验证：真实后端子进程启停回归通过（启动→同步 3 工具→停止，状态正确回到 stopped），
新增 `tests/test_backend_client.py` 覆盖上述四个修复（含"client 退出失败仍要保证 stdio 被释放"的用例）。

---

## 0.0 第二次更新：P0 已修复 + 协议一致性专项（2026-10-15 晚）

按审核结论执行了首轮修复，并在做 MCP 协议一致性对比时**新发现一个 P0 级缺陷**。

### 0.1 本轮已修复

| 编号 | 问题 | 处理方式 |
|---|---|---|
| D1 | 未实现的工具 + 裸文本兜底 | 移除文档虚假声明；未知工具改为抛 `ToolError` → MCP `isError` |
| D2 | 依赖未锁上界，新环境崩 | `requirements.txt` 锁定 `mcp>=1.9.0,<2.0.0` |
| D3 | 三层架构文档 descr | 四份文档全部改为实际的两层结构 |
| D16 | Prompt 教错参数名 | 修正为 `tool_id`，并补充 errorType 说明 |
| D20 | 文件中部 import | 合并到顶部 |
| — | 参数命名不统一 | `mcp_tool_id` / `launcher_id` 全部统一为 `tool_id` |

### 0.2 新发现：D23（P0）`read_resource` 返回值类型错误

做协议对比时读 SDK 源码发现：`mcp/server/lowlevel/server.py` 对 `Iterable` 类型的返回值执行

```python
contents_list = [create_content(item.content, item.mime_type) for item in contents]
```

而本项目返回的是 `list[TextContent]` —— `TextContent` 只有 `.text`，**没有 `.content` 属性**，
因此**任何一次 resources/read 都会 `AttributeError`**。
因为该能力从未被实际调用过，这个缺陷一直潜伏。已修复为返回 `ReadResourceContents`。

### 0.3 协议层面最重要的一条结论

**mcp 1.x 的 `call_tool` handler 返回值被 SDK 硬编码为 `isError=False`**，
handler 内部无法修改。也就是说：本项目原先所有 `{"success": false, ...}` 的返回，
**在 MCP 协议层都被标记为"调用成功"**。唯一让客户端感知失败的途径是抛异常。
已据此重构错误处理，详见 [`mcp-conformance.md`](mcp-conformance.md)。

### 0.4 第三轮修复后的剩余问题

P0 与 P1 已全部关闭。剩余有效待办为 P2 的 D9–D15、P3 的 D17/D18/D19/D21/D22，清单见下。
其中 D12/D13（JSON 容错与原子写）建议优先，它们会让单个坏文件拖垮整个服务。

---

## 1. 总体结论

| 维度 | 评价 |
|---|---|
| 架构设计 | ⭐⭐⭐⭐ 分层清晰（接口 / 领域 / 存储 / 路由），职责边界干净 |
| 契约一致性 | ⭐⭐⭐⭐ 修复后已对齐（原 ⭐⭐） |
| 健壮性 | ⭐⭐⭐⭐⭐ 超时、清理、JSON 容错、原子写均已到位（原 ⭐⭐） |
| 可维护性 | ⭐⭐⭐⭐⭐ 4 个测试文件共 41 个用例（原 ⭐⭐） |
| 协议合规 | ⭐⭐⭐⭐ 修复后主体合规，annotations / structuredContent 待补 |

**共发现 25 项问题**：P0 级 4 项、P1 级 5 项、P2 级 8 项、P3 级 4 项、后续新增 4 项。
**P0 / P1 / P2（D9–D15）已全部修复并完成回归**，P3 的 D19 已补齐测试。
剩余待办为 **D17 / D18 / D21 / D22**（语义与命名一致性）以及 §7 的四项架构议题（A1–A4）。

---

## 2. 严重级别定义

- **P0**：会导致服务完全不可用，或对外契约错误直接误导调用方；
- **P1**：会造成数据丢失、资源泄漏或进程挂起；
- **P2**：功能行为不符合预期、边界错误、可维护性缺陷；
- **P3**：风格、命名、覆盖度等非阻塞问题。

---

## 3. P0 —— 阻断级

### D1. ✅ 已修复 —— 文档承诺的两个工具完全未实现，且失败返回格式错误

**位置**：`src/server.py:386-387`；`SKILL.md:35-43`、`README.md`、`docs/format.md:170-178`

三份文档都声明了 `list_launchers` 和 `get_launcher_info`，但 `server.py` 里根本没有这两个分支。
更糟的是兜底分支的返回：

```python
else:
    result = f"未知工具: {name}"      # 裸文本，非 JSON，且 isError 未置位
```

**影响**：AI 客户端按 SKILL.md 去调 `list_launchers` 时，既拿不到正确数据，也**收不到任何错误信号**，
只会拿到一句无法解析的自然语言，极大概率导致模型"幻觉重试"或误判能力不存在。
这直接破坏了该项目作为"AI 工具入口层"的核心价值。

**修复建议**（二选一）：
1. 补上这两个工具（从 `data/tools/*.json` 聚合出启动器视图）；
2. 从 SKILL.md / README.md / format.md 删除它们，并把兜底分支改为抛异常或返回结构化错误：

```python
else:
    raise ValueError(f"未知工具: {name}")
```

---

### D2. ✅ 已修复 —— 依赖未锁上界：`mcp>=1.0.0` 在新环境下会导致服务启动即崩 ⚠️ 已实测

**位置**：`requirements.txt:1`

`src/server.py` 使用的是 **mcp 1.x** 的低层装饰器 API：

```python
@app.list_tools()      # 1.x API
@app.call_tool()       # 1.x API
@app.read_resource()   # 1.x API
@app.list_prompts()    # 1.x API
```

但 PyPI 当前最新版已是 **mcp 2.3.0**，该版本的 `mcp.server.Server` 类**已移除**这些装饰器方法。
在全新环境中 `pip install -r requirements.txt` 后实测：

```
IMPORT FAILED -> AttributeError: 'Server' object has no attribute 'list_tools'
```

同时 mcp 2.x 的 handler 签名也变了（从 `(uri)` 变为 `(ctx, params)`，返回值要求 `ReadResourceResult` 而非 `list[TextContent]`）。

**影响**：任何新机器 / CI / 重装依赖的场景，MCP Server **连 import 都过不去**。
当前仓库能跑只是因为开发环境早就装好了 1.x，pip 不会主动升级已装包——典型的"只在老环境能跑"。

**修复建议**：

```
mcp>=1.9.0,<2.0.0
pytest>=7.0.0
```

并在 `README.md` 注明迁移到 mcp 2.x 需要重写 `server.py` 的 handler 注册方式。

---

### D3. ✅ 已修复 —— 三层架构中缺失两层：启动器配置层与适配器层

**位置**：`SKILL.md:22-33`、`README.md`、`TASKS.md:86-91`、`docs/format.md:7-81`

文档描述的是三层结构：

| 层 | 路径 | 是否存在 |
|---|---|---|
| 启动器配置 | `data/launchers/{id}.json` | ❌ |
| 工具条目 | `data/tools/{id}.json` | ✅（但结构已变） |
| 适配器 | `src/adapters/base.py`、`everything.py` | ❌ |

`store.py` 里也**没有任何** launcher / adapter 相关方法。

**影响**：SKILL.md 的「新增启动器步骤」4 步里有 3 步（建 launcher 配置、写适配器、注册适配器）无法执行。
无论是人还是 AI 照着文档扩展新启动器，都会走错方向。

**修复建议**：确认架构重构已定稿后，把三份文档统一改成当前的**两层结构**（`tools/` + `backend-tools/`），
并重写「新增 MCP 工具」的标准流程（写 `data/tools/{id}.json` → `start_backend` → `sync_backend_tools`）。

---

## 4. P1 —— 数据丢失 / 资源泄漏 / 挂起

### D4. ✅ 已修复 —— 同步失败时会用空结果覆盖已有的工具目录缓存

**位置**：`src/backend_client.py:306-323`

```python
tools = await client.list_tools(force_refresh=True)   # 内部失败时被吞掉，返回 []
self.store.save_backend_tools(tool_id, tools)         # 直接覆写磁盘
```

`_refresh_tools_cache()` 内部 `except` 只记日志，失败时 `self._cached_tools` 保持 `None`，
`list_tools()` 于是返回 `[]`，接着 `save_backend_tools` 把 `[]` 写进文件——**上一次成功的缓存被清空**。

**修复建议**：

```python
tools = await client.list_tools(force_refresh=True)
if not tools:
    return {"success": False, "error": "后端返回空工具列表，保留原缓存未覆盖"}
```

### D5. ✅ 已修复 —— 资源清理共用 try，异常会导致子进程泄漏

**位置**：`src/backend_client.py:131-142`

```python
try:
    if self._client_context:
        await self._client_context.__aexit__(...)   # 若这里抛异常
    if self._stdio_context:
        await self._stdio_context.__aexit__(...)    # 这行永远不会执行
```

`stdio_context` 持有子进程，不 exit 就会留下孤儿进程。

**修复建议**：两段 `__aexit__` 各自独立 try/except。

### D6. ✅ 已修复 —— 所有后端调用无超时保护

**位置**：`backend_client.py:91`（`initialize`）、`167`（`list_tools`）、`201`（`call_tool`）

后端 MCP 子进程卡死 / 不返回时，`await` 会无限等待，整个 `awam-tool-manager` 被拖死，
上层 AI 客户端表现为"工具调用超时"，且无法通过 `stop_backend` 恢复（因为事件循环已被占住）。

**修复建议**：统一用 `asyncio.wait_for(..., timeout=20)` 包裹，并对超时执行 `stop()` + 状态置 error。

### D7. ✅ 已修复 —— `stop()` 未加锁，与 `start()` 存在并发竞态

**位置**：`backend_client.py:113-129`

`start()` 用 `self._lock` 保护，但 `stop()` 完全没有。两者并发时可能出现：
`start` 刚 `stdio_client.__aenter__()` 完、`stop` 就 `_cleanup()` 并置 `stopped`，
随后 `start` 继续把已废弃的 session 赋给 `self.session` 并置 `running` → **状态与真实连接不一致**。

另外，心跳失败进入 `error` 状态后，再次 `start()` 会直接覆盖 `self._stdio_context`，
**旧 context 未被清理** → 泄漏。

**修复建议**：
- `stop()` 也纳入 `self._lock`；
- `start()` 开头先判断 `if self._stdio_context or self._client_context: await self._cleanup()`。

### D8. ✅ 已修复 —— 工具调用 handler 无异常兜底，且错误风格不统一

**位置**：`src/server.py:243-390`

- 整个 `call_tool` 没有 try/except。任一异常（脏 JSON、`KeyError`、`TypeError`）都会冒泡给 MCP SDK；
- 成功路径返回 JSON 字符串，未知工具返回裸文本 —— 调用方无法用统一方式解析。

**修复建议**：整体包一层 try/except，统一返回 `{"success": false, "errorType": "...", "error": "..."}` 的 JSON。

---

## 5. P2 —— 行为不符合预期

### D9. ✅ 已修复 —— `get_backend_logs` 的 `limit=0` 会返回全部日志（已实测）

**位置**：`src/backend_client.py:245`

```python
return logs[-limit:]
```

Python 中 `-0 == 0`，`logs[-0:]` 等价于 `logs[0:]` → **返回全部**。
实测：`[1,2,3,4,5][-0:]` 得到 5 条，预期 0 条。

**修复建议**：`return logs[len(logs) - limit:] if limit > 0 else []`

**已修复（第四轮）**：`get_logs()` 开头显式判 `limit is None or limit <= 0 → []`；
`test_backend_client.py::test_get_logs_limit_zero_returns_empty` 固化为用例，
真实后端回归实测 `logs(limit=0)` 返回 `[]`、`limit=3` 返回 3 条。

### D10. ✅ 已修复 —— `set_backend_log_level` 形同虚设

**位置**：`src/backend_client.py:49-55`、`247-254`

`_log()` 从不读 `self.log_level`，任何级别的日志都会写入 deque 并打印到 stderr。
后果：默认 `info` 级别下，心跳的 debug 日志每 30 秒一条，约 4 小时就能把 500 条 deque 占满，
**真正的 error 日志会被挤掉**，而 `get_backend_logs` 恰恰是排查故障的主要手段。

**修复建议**：`_log()` 开头按级别过滤；`_log` 里的 `import sys` 提到模块顶层。

**已修复（第四轮）**：新增类常量 `LOG_LEVEL_PRIORITY`，`_log()` 在**入队之前**比较优先级后返回，
stderr 打印也随之过滤（`set_backend_log_level("warning")` 后心跳噪声不再刷屏）。用例：`test_log_level_filters_before_enqueue`
（设为 warning 时 debug/info 完全不入队，error 保留）。

### D11. ✅ 已修复 —— 启动日志拼接假定 `args` 全为字符串

**位置**：`src/backend_client.py:79`

```python
self._log("info", f"启动命令: {command} {' '.join(args)}")
```

若 JSON 配置里 `args` 含数字或布尔（很常见，如 `["--port", 8080]`），
`' '.join(args)` 直接抛 `TypeError`，导致**原本可以正常启动的后端启动失败**。

**修复建议**：`' '.join(str(a) for a in args)`

### D12. ✅ 已修复 —— 单个 JSON 文件损坏会拖垮整个服务

**位置**：`src/store.py:16-21`、`42-51`

`load_json` 无 try/except；`list_tools()` 遍历整个目录，任一文件非法 JSON 就会让
`list_all_tools()` 抛异常 —— 而这个方法在**服务启动日志**和几乎每个查询接口里都被调用。

**修复建议**：`load_json` 捕获 `json.JSONDecodeError` 并记录文件名后返回 `None`，
`list_tools()` 跳过损坏文件而不是整体崩溃。

**已修复（第四轮）**：`load_json` 分别捕获 `json.JSONDecodeError` 与 `OSError`，
并额外校验顶层必须是 `dict`（防止下游 `tool.get()` 报 `AttributeError`），
失败时向 stderr 打印含文件名的告警后返回 `None`。新增 `tests/test_store.py`：
坏文件被跳过、其余工具照常返回、顶层为数组也跳过。

### D13. ✅ 已修复 —— 写盘非原子，崩溃会留下半截 JSON

**位置**：`src/store.py:23-27`

直接 `open(path, "w")` 覆写。写入过程中进程被杀（MCP Server 由客户端托管，被杀很常见）
会留下非法 JSON，与 D12 叠加形成"一次崩溃，永久不可用"。

**修复建议**：先写 `path.with_suffix(".tmp")`，再 `os.replace(tmp, path)` 原子替换。

**已修复（第四轮）**：`save_json` 用 `tempfile.mkstemp` 在同目录建前缀为 `.` 的临时文件
（必须是同分区，`os.replace` 才是原子操作），写完 `flush + fsync` 后 `os.replace` 替换目标；
任何异常都在移除临时文件后原样抛出，绝不留下半成品。用例：
`test_save_json_is_atomic_and_leaves_no_temp`（无残留）、
`test_failed_write_keeps_old_content`（写入失败时旧内容完好）。

### D14. ✅ 已修复 —— 连接配置无法热更新

**位置**：`src/backend_client.py:264-283`

`BackendClient` 在首次 `get_client()` 时把 `connection` 配置快照进实例并永久缓存。
修改 `data/tools/*.json` 的 `connection` 后，**必须重启 MCP Server** 才生效——
而 MCP Server 的重启受客户端控制，用户体验上等于"改了没反应"。

**修复建议**：`stop_backend()` 时从 `self.clients` 移除该条目，下次重新读取配置。

**已修复（第四轮）**：改为「以磁盘配置为真相源」——`get_client()` 每次都以
`store.load_tool()` 的最新内容为准；运行/启动中的连接直接复用（热替换会产生孤儿进程
且让调用方持有失效引用），而**空闲状态（stopped/error）下若 `connection` 已变化就原地重建**，
并继承旧对象的 `logs` 与 `log_level`，排障时的历史日志不会断。另外工具被删除或不再配置
`connection` 时不再返回旧缓存对象。用例：`test_idle_client_is_rebuilt_when_config_changed`、
`test_running_client_is_not_replaced`、`test_missing_tool_yields_no_client`。

### D15. ✅ 已修复 —— `lastSynced` 硬编码为字符串 `"now"`

**位置**：`src/store.py:67-72`

```python
"lastSynced": "now",
```

当前 `data/backend-tools/everything-search-mcp.json` 里存的就是 `"now"`。
缓存新鲜度无从判断，也让"超过 N 天自动重新同步"这类逻辑无法实现。

**修复建议**：`datetime.now(timezone.utc).isoformat()`

**已修复（第四轮）**：写入 `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`，
与 `docs/format.md` 里 `lastSynced` 的示例格式完全一致（便于直接 `strptime` 解析）。
真实后端回归后 `data/backend-tools/everything-search-mcp.json` 已由 `"now"`
更新为 `"2026-10-15T12:04:56Z"`。用例：`test_last_synced_is_real_timestamp`。

### D16. ✅ 已修复 —— Prompt 模板教的参数名是错的

**位置**：`src/server.py:520-543`

`getting_started` / `how_to_call_backend` 里写的是：

```
call_backend_tool(launcher_id="everything", tool_name="everything_search", ...)
```

而实际的 `call_backend_tool` schema 要求的是 `mcp_tool_id`（`server.py:109`），`launcher_id` 并不存在。

**影响**：这是最典型的"AI 自我误导"——模型读了 prompt 后按 `launcher_id` 调用，
必然得到参数缺失错误。而 prompt 的存在本意恰恰是教 AI 正确调用。

**修复建议**：把模板里的 `launcher_id` 全部改为 `mcp_tool_id`，工具名示例改为真实值
（`search_files` / `get_file_info` / `check_status`）。

---

## 6. P3 —— 规范与覆盖度

### D17. 参数名、字段名与文档不一致

| 项 | 文档（format.md） | 实现（server.py） |
|---|---|---|
| 筛选参数 | `source`（按启动器） | `kind`（按类型 app/mcp） |
| 详情参数 | `id` | `tool_id` |
| 工具条目结构 | `{schemaVersion, source, tools:[...]}` | 扁平单条，无 `schemaVersion`/`source` |

两者语义还不一样：`source` 是"来自哪个启动器"，`kind` 是"这是什么类型的工具"。
按当前数据模型，**已无法按启动器筛选**（数据里没有 `source` 字段）。

**修复建议**：二选一 —— 要么给工具条目补 `source` 字段并支持 `source` 筛选，
要么把文档改成 `kind` 并明确"按启动器筛选暂不支持"。

### D18. 搜索未覆盖 `id` 字段，语义描述有歧义

**位置**：`src/registry.py:29-38`、`src/server.py:83`

`search_tools` 只匹配 `name` / `displayName` / `description` / `tags`，**不含 `id`**。
而 `get_tool_info` 的参数说明把 `tool_id` 举例如 `everything`、`everything-search-mcp`
（这其实是"工具 ID"，但命名和历史上的"启动器 ID"混在一起），容易误用。

### D19. ✅ 已修复 —— 测试覆盖薄弱

- `tests/test_registry.py` 覆盖 store + registry 的 happy path；
- `tests/test_backend_client.py`（14 个用例）覆盖 P1/P2 的稳定性修复：
  清理顺序（client 退出失败仍要保证 stdio 被释放）、清理幂等、stop/start 串行、
  残留连接清理、空结果不覆盖缓存、超时标记、心跳失败清理、**日志 limit 边界**、
  **日志级别过滤**、**配置热更新三种情形**；
- `tests/test_store.py`（8 个用例，第四轮新增）：坏 JSON 跳过 / 顶层非对象跳过 /
  原子写无残留 / 写失败保原内容 / `lastSynced` 真实时间戳；
- `tests/test_server_dispatch.py`（14 个用例，第四轮新增）：12 条分发路径、
  错误载荷的 `errorType`、`internal_error` 兜底、工具定义不含遗留参数名、
  三个资源均返回可读的 `ReadResourceContents`；
- 根目录 8 个 `test_*.py` 调试脚本已被 `.gitignore` 排除。

> 全部异步用例用 `asyncio.run` 驱动，**不引入 pytest-asyncio**，运行测试只需 `mcp` + `pytest`。

### D20. ✅ 已修复 —— 代码组织细节

- `server.py:395` 在文件中部 import（`from mcp.types import Resource, Prompt, ...`），应移到顶部；
- 被暴露的工具名 `list_tools` 与 handler 函数名 `list_tools` 同名，阅读时容易混淆；
- 12 个工具的 schema 全部内联在 `list_tools()` 里，函数长达 200 行，建议抽成常量表。

### D21. 命名混乱（README 与数据对不上）

`README.md:35` 和 `docs/format.md:106` 写的是 `everything-mcp`，
而 `data/tools/everything-search-mcp.json` 用的是 `everything-search-mcp` —— 这是**两个不同的 npm 包**。
`SKILL.md:108` 的安装命令仍是 `npm i -g everything-mcp`。

### D22. `launch_tool` 名不副实

工具名与描述是"启动工具 / `launch_tool`"，实现却只是返回启动命令（`registry.py`），
**不实际执行**。`TASKS.md` P0 已记录此项，现已在 SKILL.md / README.md 的工具说明中明确标注"目前不实际执行"。

---

### D23. ✅ 已修复 —— `read_resource` 返回值类型错误（协议专项中新发现）

**位置**：`src/server.py` `read_resource`

协议要求返回 `str | bytes | Iterable[ReadResourceContents]`，但代码返回 `list[TextContent]`。
SDK 对 Iterable 执行 `[create_content(item.content, item.mime_type) for item in contents]`，
而 `TextContent` 没有 `.content` 属性 → **任何一次 resources/read 都会 AttributeError**。

**修复**：返回 `[ReadResourceContents(content=text, mime_type="application/json")]`，
并对 mcp <1.9（无该类）降级为 deprecated 的 str 形式。实测 `awam://tools` 读取正常。

### D24. ✅ 已修复 —— 业务失败未映射为 MCP 协议的 `isError`

详见 [`mcp-conformance.md §2.1`](mcp-conformance.md#21-严重问题iserror-从未置位-已修复)。
核心结论：SDK 恒把 handler 的返回值标为 `isError=False`，只有抛异常才能置 `True`。
已引入 `ToolError` / `fail()` / `ensure_success()` 完成转换。

---

## 7. 架构层面的不合理

### A1. 无任何 schema 校验

`docs/format.md` 定义了详尽的字段规范，但代码里**从未使用**（`requirements.txt` 也没有 `jsonschema`）。
字段拼错、类型写错、必填缺失都不会被发现，只会表现为运行期行为异常。
建议加载时用 `jsonschema` 校验一次，失败则跳过该文件并记日志。

### A2. `readOnly` / `destructive` 字段形同装饰

数据里记录了这两个安全元数据，但 `launch_tool` / `call_backend_tool` 都**没有读取**它们。
`destructive: true` 的工具同样会被无差别转发执行。TASKS.md 把它放在 P3「权限控制」，
考虑到这是"让 AI 操作系统工具"的入口层，建议优先级上调。

### A3. 全局单例 + 无并发限流

`BackendManager` 是模块级单例，`clients` 字典无锁保护（除 `BackendClient` 内部启动锁）。
并发 `call_backend_tool` 打到同一个 session 时，正确性完全依赖后端 MCP 的实现，缺少限流与排队。

### A4. 数据与文档双真相源

`data/*.json` 是事实标准，`docs/format.md` 是纸面标准，二者已经分叉（D17）。
长期看应当**由代码生成文档**或**由 schema 反向校验数据**，避免再次漂移。

---

## 8. 建议的修复顺序

**第一批（已完成 ✅）**

1. ~~**D2** —— requirements 加 `,<2.0.0` 上界~~ → 已锁 `mcp>=1.9.0,<2.0.0`
2. ~~**D1** —— 补全或删除虚设工具 + 兜底改协议错误~~ → 已删 + 抛 `ToolError`
3. ~~**D16** —— 修正 prompt 模板参数名~~ → 已改为 `tool_id`
4. ~~**D3** —— 文档与两层架构对齐~~ → 四份文档已重写
5. ~~**D20** —— 文件中部 import~~ → 已合并至顶部
6. ~~**D23 / D24** —— 协议合规（resources 返回值、isError 映射）~~ → 已修复并实测

**第二批（已完成 ✅）**

7. ~~**D4 / D5 / D7** —— 缓存覆盖保护、清理逻辑拆分、stop 加锁~~
   另加 `_cleanup_lock` 保证清理串行幂等（心跳失败与 stop 并发的场景）
8. ~~**D6** —— 后端调用统一加超时~~ 默认 30s，可用 `connection.timeout` 覆盖
9. ~~**D11** —— `args` 拼接改 `str(a)`~~
10. ~~**D8** —— 随上一轮的 `ToolError` 机制一并解决~~

**第三批（已完成 ✅）**

11. ~~**D12 / D13** —— JSON 读容错 + 原子写~~ → 坏文件跳过，`mkstemp` + `os.replace` 原子写盘
12. ~~**D9 / D10 / D14 / D15** —— 边界与配置问题~~
    - D9：`limit<=0` 返回 `[]`（`-0 == 0` 的陷阱）
    - D10：`_log()` 按级别在入队前过滤
    - D14：空闲时按磁盘最新 `connection` 重建客户端，运行中禁止热替换
    - D15：`lastSynced` 写真实 UTC 时间戳
13. ~~**D19** —— 补 `test_store.py`（8 例）与 `test_server_dispatch.py`（14 例）~~ → 合计 41 passed

**第四批（待做，一致性与合规）**

14. **D17 / D18 / D21** —— `source`/`kind` 语义分叉、搜索不含 `id`、README 与数据命名不一致；
15. **D22** —— `launch_tool` 只返回命令不实际执行（已在文档中标注，待定是否需要改名）；
16. **A1 / A2** —— 引入 `jsonschema` 校验；把 `readOnly` / `destructive` 映射为 MCP Tool annotations；
17. **A3** —— `BackendManager` 并发限流与排队。

> 原先 §8 里有一个重复编号的「第三批（迭代，一致性）」小节，已合并到上面的第四批。

---

## 9. 验证方法（可在本机复现）

```bash
# 1) 依赖约束 —— 必须是 1.x
pip install "mcp>=1.9.0,<2.0.0" pytest

# 2) 静态检查
python -m py_compile src/*.py tests/*.py

# 3) 单元测试（41 项：store/registry + backend_client 稳定性 + store 容错/原子写 + server 分发）
python -m pytest tests/ -q

# 4) import 验证（mcp 2.x 下会 AttributeError）
python -c "import sys; sys.path.insert(0,'src'); import server; print('OK')"

# 5) 关键路径实跑：失败应抛 ToolError（协议层 isError），成功应返回 JSON
python - <<'PY'
import sys, asyncio, json
sys.path.insert(0, 'src'); import server
async def main():
    print(json.loads(await server.dispatch_tool("search_tools", {"keyword": "文件"}))["count"])
    for name, args in [("no_such_tool", {}), ("get_tool_info", {"tool_id": "nope"}),
                       ("launch_tool", {"tool_id": "everything-search-mcp"})]:
        try:
            await server.dispatch_tool(name, args)
        except server.ToolError as e:
            print(name, "-> isError:", e)
    print(type(await server.read_resource("awam://tools")).__name__)
asyncio.run(main())
PY

# 6) 真实子进程启停回归（必须跑通：启动 -> 同步 -> 停止 -> 状态回到 stopped）
python - <<'PY'
import sys, asyncio, json
sys.path.insert(0, 'src')
from store import JsonStore
from backend_client import BackendManager
async def main():
    m = BackendManager(JsonStore("data"))
    print(await asyncio.wait_for(m.start_backend("everything-search-mcp"), timeout=90))
    print(m.get_backend_status("everything-search-mcp")["status"]["status"])
    print((await m.stop_backend("everything-search-mcp")))
asyncio.run(main())
PY

# 7) D9 边界复现 —— logs[-0:] 确实是全量，这正是 limit=0 返回全部的原因
python -c "logs=[{'i':i} for i in range(5)]; print(len(logs[-0:]))"   # 输出 5，预期 0

# 8) D13 原子写验证：写入成功后数据目录不应有 . 开头的临时文件残留
python -c "
import sys, json; sys.path.insert(0,'src')
from pathlib import Path
from store import JsonStore
s = JsonStore('data'); s.save_backend_tools('_selftest', [{'name':'x'}])
p = Path('data/backend-tools')
print('残留:', [f.name for f in p.iterdir() if f.name.startswith('.')])
(p/'_selftest.json').unlink()
"
```

本次审核中 **D2、D9、D23、D24** 均通过上述方式实测确认；
D4 / D5 / D6 / D7 的修复由 `tests/test_backend_client.py` 覆盖，
D9 / D10 / D12 / D13 / D14 / D15 由新增的 `tests/test_store.py`、`test_backend_client.py`
与 `tests/test_server_dispatch.py` 覆盖（`41 passed`），
并已用真实后端子进程做过完整启停 + 调用回归：启动成功 → 自动同步 3 个工具 →
`lastSynced` 写入真实时间戳 → 调用 `search_files` 返回结果 → 停止后状态回到 `stopped`。
