# awam-tool-manager 格式规范

参考 MCP Tool Schema、WinGet Manifest 设计。

> 历史变更：早期版本曾有「启动器配置层」（`data/launchers/`）作为第 1 层，
> 现已废弃移除。MCP 不再是独立层级，而是工具的 `kind` 取值之一。

---

## 数据层 1：工具条目（Tool Entry）

> **只有这两个"层"是数据层**：`data/tools/`（人工维护，在仓库内）与后端子工具缓存
> `backend-tools/`（自动生成，在运行时目录 `%LOCALAPPDATA%\.awam-tool-manager/`，A14 起迁出仓库）。
> 下面第 3 节列出的「对外 MCP 工具」是**对外契约**，不是存储在磁盘上的数据层，
> 早期文档把它算作第 3 层容易造成"三层架构"的误解。

**一个工具 = 一个 JSON 文件**，存放在 `data/tools/` 目录，文件名即 `tool_id`。
条目在加载时会按本节规范校验（`src/schema.py`），不合规的条目会被跳过并向 stderr 告警。

### 字段说明

> **本表由 `src/schema.py` 生成，请勿手改。**
> 改字段请改 `src/schema.py` 的 `TOOL_ENTRY_SCHEMA`，然后运行
> `python scripts/generate_format_doc.py`（`--check` 可只做漂移检查）。
> 测试 `tests/test_docs_sync.py` 会在文档与 schema 不一致时直接失败。

<!-- BEGIN:generated:tool-entry -->

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | `string` | ✅ | 工具唯一 ID，**必须与文件名一致**（须匹配 `^[A-Za-z0-9._-]+$`） |
| `name` | `string` | ✅ | 工具名称（英文/技术名） |
| `displayName` | `string` | - | 显示名称（中文/用户友好），缺省回落 `name` |
| `kind` | `string` | ✅ | 工具类型：`app` 普通应用（`launch_tool` 返回命令）、`mcp` 可建后端连接、`launcher` 启动器（非 MCP 协议，但有可列/可搜/可调用的动作清单，见 `launcher`）（取值：`app` / `mcp` / `launcher`） |
| `description` | `string` | - | 工具功能描述（参与搜索匹配） |
| `status` | `string` | - | 安装状态（取值：`installed` / `not_installed` / `unknown`） |
| `version` | `string` | - | 版本号 |
| `publisher` | `string` | - | 发布者 |
| `website` | `string` | - | 官方网站 |
| `docUrl` | `string` | - | 文档地址 |
| `icon` | `string` | - | 图标路径或 URL |
| `launch` | `object` | - | **仅 `kind=app`**：启动方式，`launch_tool` 据此返回命令 |
| `launch.type` | `string` | - | 启动方式类型（取值：`executable` / `protocol` / `command`） |
| `launch.command` | `string` | - | 启动命令或可执行文件路径 |
| `launch.args` | `array` | - | 启动参数（元素建议统一写字符串） |
| `connection` | `object` | - | **仅 `kind=mcp`**：后端 MCP 连接方式，`BackendClient` 据此拉起子进程 |
| `connection.type` | `string` | ✅ | 连接类型，目前仅支持 stdio（取值：`stdio`） |
| `connection.command` | `string` | ✅ | 拉起子进程的命令 |
| `connection.args` | `array` | - | 子进程参数 |
| `connection.env` | `object` | - | 子进程环境变量 |
| `connection.timeout` | `number` | - | 后端操作超时（秒），覆盖 initialize / list_tools / call_tool / ping（缺省 `30`；必须 > 0） |
| `update` | `object` | - | 更新方式 |
| `update.method` | `string` | - | 更新渠道（取值：`winget` / `scoop` / `npm` / `manual`） |
| `update.packageId` | `string` | - | 包管理器中的包 ID |
| `install` | `object` | - | 安装方式；`start_backend` 检测到启动命令缺失时，仅在**显式授权**后才会执行一次 `command`（默认不执行，见安全约定），与 `update` 并存 |
| `install.method` | `string` | - | 安装渠道（取值：`npm` / `winget` / `scoop` / `manual`） |
| `install.command` | `string` | - | 安装命令 |
| `install.auto` | `boolean` | - | 是否允许自动执行 `command`；默认 false。需显式设为 true，或设环境变量 AWAM_AUTO_INSTALL=1，否则 install.command 永不被自动执行 |
| `launcher` | `object` | - | 启动器适配配置；仅 `kind=launcher` 有意义，决定如何列出与调用它的动作 |
| `launcher.type` | `string` | - | 适配器类型：`quicker` 走 QuickerStarter 命令行（有 stdout 回传）；`utools` 走 utools:// 协议唤起（fire-and-forget，无回传，结果只在 uTools UI）（取值：`quicker` / `utools`） |
| `launcher.command` | `string` | - | 启动器可执行文件路径（如 QuickerStarter.exe；utools 协议调用不需要） |
| `launcher.dataDir` | `string` | - | 启动器数据目录（Quicker 的 states 所在目录；uTools 的插件 asar 所在目录，utools 适配器会展开 %APPDATA% 等环境变量） |
| `launcher.timeout` | `number` | - | 单次动作调用的最长等待（秒）（缺省 `20`；必须 > 0） |
| `tags` | `array<string>` | - | 标签分类（参与搜索匹配） |
| `actions` | `array<string>` | - | 支持的动作（如 `launch` / `search`） |
| `capabilities` | `array<string>` | - | 能力标签（如 `search` / `index`） |
| `readOnly` | `boolean` | - | 是否只读（对应 MCP 的 `readOnlyHint`） |
| `destructive` | `boolean` | - | 是否有破坏性（对应 `destructiveHint`，`call_backend_tool` 会据此拦截） |
| `destructiveTools` | `array<string>` | - | 子工具级破坏性清单：只有列在这里的后端子工具需要 `allowDestructive` 确认；未配置时回退到条目级 `destructive`（即整个后端的所有调用都要确认） |
| `destructiveActions` | `array<string>` | - | 启动器动作级破坏性清单（glob，大小写不敏感）：按动作的 id/name/type 匹配，命中才需要 `allowDestructive` 确认；未配置时回退到条目级 `destructive`（即该启动器的所有动作都要确认） |
| `metadata` | `object` | - | 扩展元数据，键值对自由 |
| `createdAt` | `string` | - | 创建时间，ISO 8601 |
| `updatedAt` | `string` | - | 更新时间，ISO 8601 |

<!-- END:generated:tool-entry -->

### 示例：`kind=app`

```json
{
  "id": "everything",
  "name": "Everything",
  "displayName": "Everything 文件搜索",
  "kind": "app",
  "status": "installed",
  "description": "Windows 极速文件搜索工具，毫秒级搜索百万文件",
  "version": "1.4.1",
  "publisher": "voidtools",
  "website": "https://www.voidtools.com/",
  "docUrl": "https://www.voidtools.com/support/everything/",
  "launch": {
    "type": "executable",
    "command": "C:\\Program Files\\Everything\\Everything.exe",
    "args": []
  },
  "update": {
    "method": "winget",
    "packageId": "voidtools.Everything"
  },
  "tags": ["文件搜索", "系统工具", "效率"],
  "readOnly": false,
  "destructive": false,
  "createdAt": "2026-10-15T16:50:00Z",
  "updatedAt": "2026-10-15T16:50:00Z"
}
```

### 示例：`kind=mcp`

```json
{
  "id": "everything-search-mcp",
  "name": "Everything Search MCP",
  "displayName": "Everything 文件搜索 MCP",
  "kind": "mcp",
  "status": "installed",
  "description": "Everything 文件搜索的 MCP 接口，提供文件搜索、文件信息查询能力",
  "version": "latest",
  "publisher": "community",
  "website": "https://www.npmjs.com/package/everything-search-mcp",
  "tags": ["文件搜索", "MCP", "系统工具"],
  "connection": {
    "type": "stdio",
    "command": "cmd.exe",
    "args": ["/c", "npx", "-y", "everything-search-mcp"]
  },
  "update": {
    "method": "npm",
    "packageId": "everything-search-mcp"
  },
  "readOnly": true,
  "destructive": false
}
```

---

## 数据层 2：后端子工具缓存（Backend Tool Catalog）

由 `sync_backend_tools` 自动生成并写入**运行时目录**的 `backend-tools/{tool_id}.json`
（`%LOCALAPPDATA%\.awam-tool-manager/`，A14 前在 `data/backend-tools/`），**不建议手工编辑**。
结构同样由 `src/schema.py` 的 `BACKEND_CATALOG_SCHEMA` 定义并生成本表。

<!-- BEGIN:generated:backend-catalog -->

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent` | `string` | ✅ | 父 MCP 工具的 ID |
| `lastSynced` | `string` | - | 上次同步时间，ISO 8601；用于判断缓存有多旧 |
| `source` | `string` | - | 来源，表示由后端自动拉取而非人工维护（取值：`auto`） |
| `tools` | `array<object>` | ✅ | 后端 MCP 提供的子工具列表 |
| `tools[].name` | `string` | ✅ | 子工具名，即 `call_backend_tool` 的 `tool_name` 参数 |
| `tools[].description` | `string` | - | 子工具描述 |
| `tools[].inputSchema` | `object` | - | 透传自后端 MCP 的 JSON Schema |

<!-- END:generated:backend-catalog -->

```json
{
  "parent": "everything-search-mcp",
  "lastSynced": "2026-10-15T18:00:00Z",
  "source": "auto",
  "tools": [
    {
      "name": "search_files",
      "description": "Search for files and folders using Everything.",
      "inputSchema": {
        "type": "object",
        "properties": { "query": { "type": "string" } },
        "required": ["query"]
      }
    }
  ]
}
```

---

## 数据层 2.5：启动器动作清单（Launcher Action Catalog）

由 `sync_launcher_actions` 自动生成并写入**运行时目录**的
`launcher-actions/{tool_id}.json`（与数据层 2 同构），**不建议手工编辑**。
结构由 `src/schema.py` 的 `LAUNCHER_ACTIONS_SCHEMA` 定义（本表为手写，
未纳入生成器，改结构时请同步更新此处与 schema）。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent` | `string` | ✅ | 父启动器工具的 ID（`kind=launcher`） |
| `lastSynced` | `string` | - | 上次同步时间，ISO 8601；缓存超过 `ACTIONS_CACHE_TTL`（300s）后 list/run 会自动重扫 |
| `source` | `string` | - | 来源（取值：`auto` / `partial`，`partial` 表示部分动作只解析出 ID） |
| `actions` | `array<object>` | ✅ | 启动器动作列表 |
| `actions[].id` | `string` | ✅ | 动作 ID（Quicker 的 UUID），`run_launcher_action` 的 `action_id` 参数 |
| `actions[].name` | `string` | - | 动作名称（尽力而为：来自 state 缓存与运行日志，可能缺失） |
| `actions[].type` | `string` | - | 动作类型（来自 state 缓存） |
| `actions[].app` | `string` | - | 动作面向的应用（来自 state 缓存） |
| `actions[].description` | `string` | - | 动作描述（来自 state 缓存） |

---

## 对外契约：MCP 工具

本技能作为 MCP Server 暴露给 AI 客户端的工具列表（**不是磁盘上的数据层**）。
后端相关工具统一使用 **`tool_id`** 指定目标工具。
可静态判定的工具都带 annotations（`readOnlyHint` / `idempotentHint` / `openWorldHint`）。

| 工具名 | 参数 | 说明 |
|---|---|---|
| `search_tools` | `keyword`(必填)、`kind` | 按关键词搜索工具（匹配 `id` / 名称 / 描述 / 标签） |
| `list_tools` | `kind` | 列出所有工具 |
| `get_tool_info` | `tool_id`(必填) | 查看工具详情 |
| `launch_tool` | `tool_id`(必填) | 返回启动命令（**当前不实际执行**） |
| `call_backend_tool` | `tool_id`、`tool_name`(必填)、`arguments`、`allowDestructive` | 路由调用后端子工具（目标标记 `destructive` 时需 `allowDestructive=true`） |
| `list_backend_tools` | `tool_id`(必填)、`force_refresh` | 列出后端子工具 |
| `start_backend` | `tool_id`(必填) | 启动后端并同步目录 |
| `stop_backend` | `tool_id`(必填) | 停止后端 |
| `get_backend_status` | `tool_id` | 查看后端状态 |
| `get_backend_logs` | `tool_id`(必填)、`level`、`limit` | 查看后端日志 |
| `set_backend_log_level` | `tool_id`、`level`(必填) | 设置日志级别 |
| `sync_backend_tools` | `tool_id`(必填) | 同步子工具目录 |
| `sync_launcher_actions` | `tool_id`(必填) | 手动同步启动器动作清单 |
| `list_launcher_actions` | `tool_id`(必填)、`keyword`、`limit`、`refresh` | 列出/搜索启动器动作（缓存超过 5 分钟自动重扫） |
| `run_launcher_action` | `tool_id`(必填)、`action_id`/`action_name`(至少一个)、`param`、`wait`、`allowDestructive` | 执行启动器动作（条目或目标标记 `destructive` 时需 `allowDestructive=true`；配 `destructiveActions` 时按动作 id/name/type 的 glob 判定，仅命中的动作需确认） |

### Resources

| URI | 内容 | mimeType |
|---|---|---|
| `awam://tools` | 所有已注册工具 | application/json |
| `awam://mcp-servers` | 所有 `kind=mcp` 的工具 | application/json |
| `awam://backends/status` | 所有后端运行状态 | application/json |

### 错误返回约定

失败时返回 MCP 协议层错误（`isError=true`），正文为 JSON：

```json
{
  "success": false,
  "errorType": "connection_error",
  "error": "无法连接后端 MCP: 后端未运行，当前状态: stopped"
}
```

`errorType`：`not_found` / `unknown_tool` / `unknown_resource` / `unknown_prompt`
/ `invalid_params` / `connection_error` / `runtime_error` / `config_error`
/ `destructive_blocked` / `permission_denied` / `backend_busy` / `backend_installing`
/ `internal_error` / `launcher_error` / `install_required` / `install_not_allowed`
/ `unknown_error`

---

## 设计原则

- 符合 MCP 官方 Tool Schema 规范，`inputSchema` 必须是合法 JSON Schema
- 元工具模式：只暴露少量高层工具，内部路由到具体实现
- `readOnly` / `destructive` 与 MCP Tool annotations 语义对齐：
  只读工具已下发 `readOnlyHint`，`destructive` 已在 `call_backend_tool` 转发前设门禁
- **单一真相源**：`src/schema.py` 既是运行期校验依据，也是上面两张字段表的生成源。
  改字段只改 schema，再跑 `python scripts/generate_format_doc.py`；
  本文件其余部分（示例、对外契约、错误约定）仍手写，
  但示例会被 `tests/test_docs_sync.py` 拿去跑校验，不会悄悄腐烂
