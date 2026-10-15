# awam-tool-manager 格式规范

参考 MCP Tool Schema、WinGet Manifest 设计。

> 历史变更：早期版本曾有「启动器配置层」（`data/launchers/`）作为第 1 层，
> 现已废弃移除。MCP 不再是独立层级，而是工具的 `kind` 取值之一。

---

## 第 1 层：工具条目（Tool Entry）

**一个工具 = 一个 JSON 文件**，存放在 `data/tools/` 目录，文件名即 `tool_id`。

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 工具唯一 ID，**必须与文件名一致** |
| `name` | string | ✅ | 工具名称（英文/技术名） |
| `displayName` | string | - | 显示名称（中文/用户友好），缺省回落 `name` |
| `kind` | string | ✅ | `app`（普通应用）/ `mcp`（MCP 工具） |
| `description` | string | - | 工具功能描述（参与搜索匹配） |
| `status` | string | - | `installed` / `not_installed` / `unknown` |
| `version` | string | - | 版本号 |
| `publisher` | string | - | 发布者 |
| `website` | string | - | 官方网站 |
| `docUrl` | string | - | 文档地址 |
| `icon` | string | - | 图标路径或 URL |
| `launch` | object | - | **仅 `kind=app`**：启动方式 |
| `launch.type` | string | - | `executable` / `protocol` / `command` |
| `launch.command` | string | - | 启动命令/路径 |
| `launch.args` | array | - | 启动参数（元素建议统一写字符串） |
| `connection` | object | - | **仅 `kind=mcp`**：后端 MCP 连接方式 |
| `connection.type` | string | - | 目前仅支持 `stdio` |
| `connection.command` | string | - | 拉起子进程的命令 |
| `connection.args` | array | - | 子进程参数 |
| `connection.env` | object | - | 子进程环境变量 |
| `update` | object | - | 更新方式 |
| `update.method` | string | - | `winget` / `scoop` / `npm` / `manual` |
| `update.packageId` | string | - | 包管理器中的包 ID |
| `tags` | array | - | 标签分类（参与搜索匹配） |
| `readOnly` | boolean | - | 是否只读（对应 MCP 的 readOnlyHint） |
| `destructive` | boolean | - | 是否有破坏性（对应 MCP 的 destructiveHint） |
| `metadata` | object | - | 扩展元数据 |
| `createdAt` | string | - | 创建时间 ISO 8601 |
| `updatedAt` | string | - | 更新时间 ISO 8601 |

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

## 第 2 层：后端子工具缓存（Backend Tool Catalog）

由 `sync_backend_tools` 自动生成并写入 `data/backend-tools/{tool_id}.json`，**不建议手工编辑**。

| 字段 | 说明 |
|---|---|
| `parent` | 父 MCP 工具的 ID |
| `lastSynced` | 上次同步时间（ISO 8601） |
| `source` | 固定 `auto`，表示由后端自动拉取 |
| `tools[].name` | 后端 MCP 提供的子工具名（`call_backend_tool` 的 `tool_name` 参数） |
| `tools[].description` | 子工具描述 |
| `tools[].inputSchema` | 透传自后端的 JSON Schema |

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

## 第 3 层：对外 MCP 工具

本技能作为 MCP Server 暴露给 AI 客户端的工具列表。
后端相关工具统一使用 **`tool_id`** 指定目标工具。

| 工具名 | 参数 | 说明 |
|---|---|---|
| `search_tools` | `keyword`(必填)、`kind` | 按关键词搜索工具 |
| `list_tools` | `kind` | 列出所有工具 |
| `get_tool_info` | `tool_id`(必填) | 查看工具详情 |
| `launch_tool` | `tool_id`(必填) | 返回启动命令（当前不实际执行） |
| `call_backend_tool` | `tool_id`、`tool_name`(必填)、`arguments` | 路由调用后端子工具 |
| `list_backend_tools` | `tool_id`(必填)、`force_refresh` | 列出后端子工具 |
| `start_backend` | `tool_id`(必填) | 启动后端并同步目录 |
| `stop_backend` | `tool_id`(必填) | 停止后端 |
| `get_backend_status` | `tool_id` | 查看后端状态 |
| `get_backend_logs` | `tool_id`(必填)、`level`、`limit` | 查看后端日志 |
| `set_backend_log_level` | `tool_id`、`level`(必填) | 设置日志级别 |
| `sync_backend_tools` | `tool_id`(必填) | 同步子工具目录 |

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

`errorType`：`not_found` / `unknown_tool` / `unknown_resource` / `invalid_params`
/ `connection_error` / `runtime_error` / `config_error` / `internal_error`

---

## 设计原则

- 符合 MCP 官方 Tool Schema 规范，`inputSchema` 必须是合法 JSON Schema
- 元工具模式：只暴露少量高层工具，内部路由到具体实现
- `readOnly` / `destructive` 与 MCP Tool annotations 语义对齐（当前尚未映射到 Tool 定义）
