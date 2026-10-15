# awam-tool-manager 架构文档

> 本文基于**当前代码实际状态**整理（截至 2026-10-15，commit `65d108c` + 本次协议合规修复）。
>
> 历史包袱说明：仓库文档早期描述的「三层架构」含 `data/launchers/`（启动器配置）与
> `src/adapters/`（启动器适配器）。这两层已在重构中移除（`3f5257a` / `bf78c2b` / `77ed097`），
> **代码中不存在**。相关文档（SKILL.md / README.md / TASKS.md / format.md）已于本次同步更新。
>
> 协议一致性专项对比见 [`mcp-conformance.md`](mcp-conformance.md)，缺陷清单见 [`code-review.md`](code-review.md)。

---

## 1. 项目定位

一个**本地工具的统一入口层**。它本身不实现具体功能，而是把散落在各个启动器 / MCP Server 下的能力收敛成一个名单，
再以 **MCP Server** 的形式暴露给 AI 客户端，让 AI 能够：

1. 搜索 / 列出 / 查看本机已注册的工具；
2. 把调用**路由转发**到真正的后端 MCP Server 执行；
3. 管理后端 MCP Server 进程（启动 / 停止 / 状态 / 日志）。

运行形态：**stdio 模式的 MCP Server**（`python src/server.py`），由 AI 客户端（如 WorkBuddy）拉起。

---

## 2. 功能清单

### 2.1 已实现

| 能力 | 实现位置 | 状态 |
|---|---|---|
| 工具条目读取（JSON 文件即数据库） | `store.py` | ✅ |
| 工具搜索（名称 / 显示名 / 描述 / 标签模糊匹配） | `registry.py` | ✅ |
| 工具列出 + `kind` 筛选 | `registry.py` | ✅ |
| 工具详情查询（app / mcp 分支） | `server.py` | ✅ |
| 工具启动信息返回（仅返回命令，不执行） | `registry.py` | ⚠️ 半成品 |
| 后端 MCP 路由调用 | `backend_client.py` | ✅ 已实测跑通 |
| 后端 MCP 子工具列表（带缓存） | `backend_client.py` | ✅ |
| 后端进程生命周期管理（启停 / 状态 / 日志） | `backend_client.py` | ✅ |
| 心跳检测（30s） | `backend_client.py` | ✅ |
| 工具目录自动同步到磁盘 | `backend_client.py` | ✅ |
| 后端依赖自动安装 + 启动预热 | `backend_client.py` | ✅ 已实测跑通 |
| MCP Resources / Prompts | `server.py` | ✅ |

### 2.2 已废弃、不应再参照的旧概念

以下均属早期版本遗留，代码中已不存在，若在某份旧文档里看到请忽略：

| 旧概念 | 去向 |
|---|---|
| `data/launchers/` 启动器配置层 | 已移除，无对应目录与 store 方法 |
| `src/adapters/` 启动器适配器 | 已移除 |
| `list_launchers` / `get_launcher_info` 工具 | 从未在当前架构中实现 |
| `mcp_tool_id` / `launcher_id` 参数 | 统一为 `tool_id` |
| `everything-mcp` 包名 | 实际为 `everything-search-mcp`（不同的 npm 包） |

---

## 3. 目录结构（实际）

```
awam-tool-manager/
├── SKILL.md                  # 技能声明（对外契约，已过期）
├── README.md                 # 项目说明（已过期）
├── TASKS.md                  # 任务清单
├── requirements.txt          # mcp>=1.30.0,<2.0.0, pytest>=7.0.0
├── docs/
│   ├── format.md             # 数据格式规范（部分过期）
│   ├── architecture.md       # 本文档
│   └── code-review.md        # 审核报告
├── data/
│   ├── tools/                # 工具条目：一个工具一个 JSON 文件
│   │   ├── everything.json            (kind=app)
│   │   └── everything-search-mcp.json (kind=mcp)
│   └── backend-tools/        # 后端 MCP 子工具缓存（自动同步写入）
│       └── everything-search-mcp.json
├── src/
│   ├── server.py             # MCP Server 入口 + 全部对外工具定义 + 分发
│   ├── store.py              # JSON 存储层（139 行，读容错 + 原子写）
│   ├── registry.py           # 工具注册中心（140 行，含条目校验过滤）
│   ├── schema.py             # 两层数据的 JSON Schema 与校验（240 行，**文档生成源**）
│   └── backend_client.py     # 后端 MCP 客户端与连接池（842 行）
├── scripts/
│   └── generate_format_doc.py  # 由 schema.py 生成 docs/format.md 的字段表（148 行）
└── tests/                    # 7 个文件，106 个用例（含真实 MCP 会话与文档漂移检测）
```

> 根目录下的 `test_*.py`、`test_output.txt` 是调试期临时脚本，已被 `.gitignore` 排除。

---

## 4. 数据模型

### 4.1 工具条目 `data/tools/{tool_id}.json`

**一个工具 = 一个文件**，文件名即工具 ID。`store.list_tools()` 把每个文件整体当作一个工具对象返回。

```json
{
  "id": "everything",            // 必填，与文件名一致，全局唯一
  "name": "Everything",          // 技术名
  "displayName": "Everything 文件搜索",
  "kind": "app",                 // app | mcp（决定 get_tool_info 走哪个分支）
  "status": "installed",
  "description": "...",
  "version": "1.4.1",
  "publisher": "voidtools",
  "website": "...", "docUrl": "...", "icon": "",
  "launch": {                    // kind=app 时有效
    "type": "executable",        // executable | protocol | command | mcp
    "command": "C:\\...\\Everything.exe",
    "args": []
  },
  "connection": {                // kind=mcp 时有效，BackendClient 读取
    "type": "stdio",
    "command": "cmd.exe",
    "args": ["/c", "everything-search-mcp"]
  },
  "install": { "method": "npm", "command": "npm i -g ..." },  // 启动命令缺失时自动执行一次
  "update": { "method": "winget", "packageId": "voidtools.Everything" },
  "tags": ["文件搜索", "系统工具", "效率"],
  "actions": ["launch", "search"],
  "capabilities": ["search", "index"],
  "readOnly": false,
  "destructive": false,
  "metadata": { "portable": false, "free": true, "supportsRegex": true },
  "createdAt": "...", "updatedAt": "..."
}
```

`kind` 的语义作用：

- `kind == "app"` → `get_tool_info` 返回 `version` / `tags` / `launch`；`launch_tool` 可返回启动命令。
- `kind == "mcp"` → `get_tool_info` 额外返回 `backend`（运行状态）与 `backendTools`（子工具目录）；`BackendManager` 才会为其创建连接。

### 4.2 后端子工具缓存 `data/backend-tools/{parent_id}.json`

由 `sync_tools_catalog` 自动写入，文件名 = 父 MCP 工具 ID。

```json
{
  "parent": "everything-search-mcp",
  "lastSynced": "2026-10-15T12:04:56Z",
  "source": "auto",
  "tools": [
    { "name": "search_files", "description": "...", "inputSchema": { ... } }
  ]
}
```

---

## 5. 模块架构

### 5.1 分层

```
┌──────────────────────────────────────────────────────────┐
│  AI 客户端（WorkBuddy 等）                                │
└───────────────────────┬──────────────────────────────────┘
                        │  MCP / stdio (JSON-RPC)
┌───────────────────────▼──────────────────────────────────┐
│  接口层  src/server.py                                   │
│   · 12 个 Tool 定义 + 分发                                │
│   · 3 个 Resource、3 个 Prompt                           │
│   · 全部返回值统一 JSON 序列化                            │
└───────┬───────────────────────────────┬──────────────────┘
        │ 查询类                         │ 路由/管理类
┌───────▼───────────────┐   ┌───────────▼──────────────────┐
│ 领域层 registry.py      │   │ 路由层 backend_client.py     │
│  · list_all_tools      │   │  · BackendManager（连接池）   │
│  · search_tools        │   │  · BackendClient（单连接）    │
│  · get_tool            │   │    启动/停止/状态/日志/心跳    │
│  · launch_tool         │   │    工具列表缓存（5 分钟）     │
└───────┬───────────────┘   └───────────┬──────────────────┘
        │                               │
┌───────▼───────────────────────────────▼──────────────────┐
│ 存储层  store.py（JsonStore）                           │
│  · data/tools/          读取工具条目                     │
│  · data/backend-tools/  读写后端子工具缓存                │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│  后端 MCP Server（子进程，如 everything-search-mcp）      │
└──────────────────────────────────────────────────────────┘
```

### 5.2 模块职责

| 模块 | 行数 | 职责 | 依赖 |
|---|---|---|---|
| `store.py` | 139 | JSON 文件的读写与目录遍历，读有容错、写走原子替换，无缓存 | 标准库 |
| `schema.py` | 240 | 两层数据的 JSON Schema 与校验（`jsonschema` 可选，缺失时降级）；**同时是 `docs/format.md` 字段表的唯一生成源** | `jsonschema`(可选) |
| `registry.py` | 140 | 工具查询语义（列表 / 搜索含 `id` / 详情 / 启动信息），加载时过滤不合规条目 | `store`、`schema` |
| `backend_client.py` | 842 | 后端 MCP 子进程的连接管理、调用转发、缓存、心跳、日志、依赖自装 | `mcp`、`store` |
| `server.py` | 796 | MCP 协议入口，定义并分发全部对外能力（含 annotations 与 destructive 门禁），启动时后台预热后端依赖 | `mcp`、`store`、`registry`、`backend_client`、`schema` |
| `scripts/generate_format_doc.py` | 148 | 读 `schema.py` 渲染字段表并就地替换 `docs/format.md` 的生成区块；`--check` 做漂移检查 | 标准库、`schema` |

### 5.3 后端路由调用链

```
call_backend_tool(tool_id, tool_name, arguments)
  └─ BackendManager.get_client(tool_id)          # 首次调用时读取 data/tools/{id}.json
       └─ 校验 kind == "mcp" 且存在 connection  →  构造 BackendClient（懒加载）
  └─ BackendClient.call_tool(tool_name, arguments)
       ├─ 状态检查（非 running 抛 ConnectionError）
       ├─ session.call_tool(...)                 # 转发到后端子进程
       ├─ 检查 result.isError                    # 后端报错 → RuntimeError
       └─ 拼接 content[*].text 返回
  └─ 统一包装：{success, result} 或 {success, error, errorType}
```

### 5.4 后端连接生命周期

```
                    start()
        stopped ─────────────► starting ──────────► running
           ▲                      │                    │
           │                      │ 失败                │ 心跳失败 / stop()
           └────── stop() ────────┴──── error ◄─────────┘
```

- `start()` 成功后会**预热工具列表**并启动心跳任务（间隔 30s，探测超时 10s）；
- `start()` 在 spawn 子进程前会先走一遍 `ensure_installed()`：启动命令的真实目标不在 PATH
  且配了 `install` 时自动安装（限时 300s、只装一次），装完复检再继续启动；
- `BackendManager.start_backend()` 在启动成功后**自动执行一次 `sync_tools_catalog`**；
- 心跳失败会把状态置为 `error` 并**主动清理连接**（不再留僵尸子进程）；
- 调用超时同样会把连接标记为 `error` 并后台清理，让后续请求快速失败而不是继续卡住。

**并发与清理约束**：

- `start()` 与 `stop()` 共用 `_lock`，避免状态与真实连接不一致；
- `start()` 开头会清理上次残留的 context（例如心跳失败后遗留的）；
- 所有清理统一走 `_cleanup()`，用独立的 `_cleanup_lock` 保证**串行且幂等**。

---

## 6. 对外 MCP 契约（实际实现）

### 6.1 Tools（12 个）

后端相关工具统一使用 **`tool_id`** 指定目标工具（`mcp_tool_id` / `launcher_id` 已废弃）。

| 工具名 | 参数 | 说明 |
|---|---|---|
| `search_tools` | `keyword`(必填)、`kind` | 关键词搜索 |
| `list_tools` | `kind` | 列出工具 |
| `get_tool_info` | `tool_id`(必填) | 工具详情（按 kind 返回不同字段） |
| `launch_tool` | `tool_id`(必填) | 返回启动命令（不执行） |
| `call_backend_tool` | `tool_id`、`tool_name`(必填)、`arguments` | 路由调用 |
| `list_backend_tools` | `tool_id`(必填)、`force_refresh` | 后端子工具（含 schema） |
| `start_backend` | `tool_id`(必填) | 启动后端 + 自动同步 |
| `stop_backend` | `tool_id`(必填) | 停止后端 |
| `get_backend_status` | `tool_id` | 单个或全部状态 |
| `get_backend_logs` | `tool_id`(必填)、`level`、`limit` | 日志查询 |
| `set_backend_log_level` | `tool_id`、`level`(必填) | 设置日志级别 |
| `sync_backend_tools` | `tool_id`(必填) | 手动同步子工具目录 |

**失败语义**：一律抛出 `ToolError`，由 MCP SDK 转成 `CallToolResult(isError=True)`，
正文是可解析的 JSON（`success` / `errorType` / `error`）。
详见 [`mcp-conformance.md`](mcp-conformance.md#21-严重问题iserror-从未置位-已修复)。

### 6.2 Resources（3 个）

| URI | 内容 |
|---|---|
| `awam://mcp-servers` | 所有 `kind=mcp` 的工具 |
| `awam://tools` | 所有已注册工具 |
| `awam://backends/status` | 所有后端运行状态 |

### 6.3 Prompts（3 个）

`getting_started`、`how_to_search`、`how_to_call_backend`
（示例参数已于本轮修正为 `tool_id`，见审核报告 D16）

---

## 7. 运行时行为

### 7.1 启动流程

```
python src/server.py
  → 计算 DATA_DIR = <项目根>/data
  → 构造 JsonStore / ToolRegistry / BackendManager（同步，无 IO 校验）
  → 注册 MCP handlers
  → 打印已加载工具数量到 stderr
  → 创建后台任务 _prewarm_backends()：为各后端装好缺失的依赖（只装不启，异常全吞）
  → stdio_server() → app.run() 事件循环
  → finally: 取消预热任务 → backend_manager.close_all()
```

### 7.2 关键设计点

| 设计 | 评价 |
|---|---|
| 日志统一输出 stderr，不污染 stdout 协议通道 | ✅ 正确且关键 |
| 所有工具返回统一 JSON 字符串 | ✅ 便于 AI 解析 |
| 后端连接懒加载 + 连接池缓存 | ✅ 合理 |
| 工具列表缓存（`TOOLS_CACHE_TTL` = 300s）+ `force_refresh` | ✅ 合理 |
| 子进程目录同步到磁盘 | ✅ 便于离线查询；空结果不覆盖已有缓存 |
| 后端调用统一超时（默认 30s，`connection.timeout` 可覆盖） | ✅ 避免子进程卡死拖垮服务 |
| `start` / `stop` 共用锁 + 独立清理锁 | ✅ 状态与真实连接一致，清理幂等 |
| 后端依赖自装 + 启动预热（`ensure_installed` / `ensure_all_installed`） | ✅ `install` 元数据从死数据变为真能力：缺失才装、只装一次、装不坏不阻断启动 |
| `store` 每次请求全量重读 JSON | ⚠️ 小规模可接受，无缓存层 |

---

## 8. 已同步项与遗留缺口

### 8.1 第一轮已修复 / 已同步 ✅（P0）

| 项 | 处理 |
|---|---|
| SKILL.md / README.md / TASKS.md / format.md 里的 launcher、adapters 描述 | ✅ 全部改为当前两层结构 |
| `list_launchers` / `get_launcher_info` 的虚假声明 | ✅ 从文档移除；未知工具改为返回协议层错误 |
| 参数名混乱（`mcp_tool_id` / `launcher_id`） | ✅ 统一为 `tool_id` |
| `requirements.txt` 无上界导致新环境装 2.x 后崩溃 | ✅ 锁定 `mcp>=1.30.0,<2.0.0`（D2 先锁 `<2.0.0`，A5 再把下限提到 1.30 以启用 structuredContent / outputSchema） |
| 业务失败不被 MCP 客户端感知（`isError` 恒为 False） | ✅ 改为抛 `ToolError` |
| `read_resource` 返回 `TextContent`（缺 `.content` 属性必报错） | ✅ 改为 `ReadResourceContents` |
| Prompt 模板教 AI 用错误参数名 | ✅ 修正示例并补充错误类型说明 |

### 8.2 第二轮已修复 ✅（P1）

| 项 | 处理 |
|---|---|
| 同步失败时用空结果覆盖 backend-tools 缓存 | ✅ 空列表拒绝写盘，保留原缓存 |
| `_cleanup` 共用 try 导致子进程泄漏 | ✅ 两个 context 各自独立 try/finally |
| 清理可能并发（心跳失败 vs `stop`） | ✅ 引入 `_cleanup_lock`，清理串行且幂等 |
| 后端调用无任何超时保护 | ✅ `initialize`/`list_tools`/`call_tool`/`ping` 全部限时，超时标记连接不可用 |
| `stop()` 未加锁与 `start()` 竞态 | ✅ 共用 `_lock`；start 前先清残留 |
| 启动日志拼接假定 `args` 全为字符串 | ✅ 改为 `str(a)` |
| backend_client 零测试 | ✅ 新增 `tests/test_backend_client.py`（8 用例） |

### 8.3 第三轮已修复 ✅（P2）

| 项 | 处理 |
|---|---|
| JSON 读容错（D12） | ✅ `load_json` 捕获 `JSONDecodeError` / `OSError` + 校验顶层为 dict，告警后返回 `None`，坏文件不影响其他工具 |
| 原子写（D13） | ✅ 同目录 `mkstemp` → `fsync` → `os.replace`；写失败清理临时文件且保留原内容 |
| `get_backend_logs` 的 `limit=0` 边界（D9） | ✅ 显式判 `limit<=0 → []`（`-0 == 0` 陷阱） |
| 日志级别未真正过滤（D10） | ✅ `_log()` 按 `LOG_LEVEL_PRIORITY` 在入队前过滤 |
| 连接配置无法热更新（D14） | ✅ 空闲时按磁盘最新 `connection` 重建客户端并继承日志；运行中禁止热替换 |
| `lastSynced` 字面量 `"now"`（D15） | ✅ 写入 UTC `%Y-%m-%dT%H:%M:%SZ` |
| server 分发零测试（D19） | ✅ 新增 `tests/test_store.py`（8）+ `tests/test_server_dispatch.py`（14），合计 41 passed |

### 8.4 第四轮已修复 ✅（语义一致性与合规）

| 项 | 处理 |
|---|---|
| 搜索不含 `id`（D18） | ✅ `search_tools` 匹配范围加入 `id` |
| 文档与数据字段漂移（D17） | ✅ `format.md` 明确「两层数据 + 一层对外契约」，补齐 `install` / `actions` / `capabilities` |
| `launch_tool` 名不副实（D22） | ✅ 描述改为「只返回命令，不实际执行」并标 `readOnlyHint`（未改名，避免破坏已接入客户端） |
| 无 schema 校验（A1） | ✅ 新增 `src/schema.py`（`jsonschema` 可选依赖），`registry` 跳过不合规条目并告警 |
| 安全元数据形同装饰（A2） | ✅ 只读工具下发 `readOnlyHint`；`call_backend_tool` 新增 destructive 门禁（`allowDestructive`） |
| 测试 | ✅ 新增 `test_registry_policy.py`（13 例），合计 54 passed |

### 8.5 第五轮已修复 ✅（协议层回环测试发现）

| 项 | 处理 |
|---|---|
| `read_resource` 用 `AnyUrl` 与字符串比较（D25） | ✅ 入口 `str(uri)` 归一化；此前三个 Resource **全部**读不出来 |
| `get_prompt` 返回类型不合规（D26） | ✅ 按版本返回 `GetPromptResult`（老 SDK 退回裸列表）；此前 prompts/get 全部失败 |
| 协议层契约无回归保护 | ✅ 新增 `tests/test_protocol_roundtrip.py`（7 例，真实 MCP 会话，不 mock SDK） |
| 其他必要测试 | ✅ BackendManager 错误分类 / `close_all` / 缓存 TTL、resources 与 prompts 定义、子工具查找，合计 **72 passed** |

### 8.6 第六轮已修复 ✅（A4：文档由 schema 反向生成）

| 项 | 处理 |
|---|---|
| 双真相源（A4） | ✅ `src/schema.py` 成为唯一真相源，`docs/format.md` 的两张字段表由 `scripts/generate_format_doc.py` 生成 |
| 文档手写漂移 | ✅ `tests/test_docs_sync.py` 四条防线：生成块逐字节一致 / 文档示例过校验 / `data/` 真实文件过校验且 `id` 与文件名一致 / schema 字段不得缺 `description` |
| `update` / `install` 子字段只写在文档里 | ✅ 已补进 schema 并纳入枚举校验（`update.method` 等） |
| 第二层数据无校验 | ✅ 新增 `BACKEND_CATALOG_SCHEMA` 与 `validate_backend_catalog()` |

**单向依赖方向**：`src/schema.py` →（生成）→ `docs/format.md` →（被测试校验）→
与 `data/*.json` 三方一致。改字段只改 schema，文档与测试会强制跟随。

### 8.7 后续项（部分已收口）

| 项 | 说明 |
|---|---|
| `structuredContent` | ✅ 已启用（mcp>=1.30）：`call_tool` 返回带 `structuredContent` 的 `CallToolResult`，`list_tools` 给每个工具挂统一 `outputSchema`（见 `SHARED_OUTPUT_SCHEMA`） |
| 依赖自装 + 启动预热（A6 ✅ 已收口） | `install` 元数据接入执行：`ensure_installed()`（`_resolved_command` 剥壳解包 → `shutil.which` 检测 → 限时装一次 → 复检）+ `start()` 前置调用 + 服务启动时 `ensure_all_installed()` 后台预热（只装不启）。已用临时探针工具实测全链路 |
| 工具变更通知 | `notifications/tools/list_changed` 未发送（顶层工具集静态，影响小） |
| 标准 logging capability | 目前用自定义 Tool 查日志 |
| 并发限流（A3 ✅ 已收口） | 单后端串行（`_call_lock`），并发调用报 `backend_busy`；要升级信号量 + 排队只需调 `MAX_CONCURRENT_CALLS` |

> 完整清单见 [`code-review.md`](code-review.md) 与 [`mcp-conformance.md`](mcp-conformance.md)。

---

### 9.2 关键实现索引

| 位置 | 关键实现 |
|---|---|
| `server.py` 顶部 | mcp 依赖的兼容导入（`ReadResourceContents` 缺失时降级） |
| `server.py` `ToolError` / `fail` / `ensure_success` | 业务错误 → MCP `isError` 的转换层 |
| `server.py` `call_tool` | `tools/call` 入口，含意外异常兜底 |
| `server.py` `dispatch_tool` | 12 个工具的分发（if/elif 链） |
| `server.py` `read_resource` | 返回 `Iterable[ReadResourceContents]` |
| `server.py` `main` | stdio 事件循环 + 启动后台预热任务 `_prewarm_backends()`，`finally` 取消预热并关闭全部后端 |
| `store.py` `load_json` / `save_json` | JSON 读写：读有容错（坏文件跳过），写走 `mkstemp` + `os.replace` 原子替换 |
| `registry.py` `search_tools` | 关键词匹配 name / displayName / description / tags（不含 id） |
| `backend_client.py` `BackendClient.start` | 子进程启动、连接、预热工具列表、起心跳；spawn 前先 `ensure_installed()` 自装缺失依赖 |
| `backend_client.py` `BackendClient.ensure_installed` | 依赖自装：剥壳解包启动命令 → `shutil.which` 检测 → 限时装一次（`INSTALL_TIMEOUT`）→ 复检；幂等，装不坏不阻断 |
| `backend_client.py` `BackendManager.ensure_all_installed` | 启动预热：遍历所有 MCP 工具只装不启，单个失败不影响整体 |
| backend_client.py 新增的 `_cleanup_lock` 保证清理串行且幂等（第一轮修复前两个 context 共用 try） |
| `backend_client.py` `BackendClient.call_tool` | 转发调用，`wait_for` 限时，超时标记连接不可用 |
| `backend_client.py` `BackendManager.get_client` | 连接池；以磁盘配置为真相源，空闲时按新 `connection` / `install` 重建 |
