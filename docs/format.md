# awam-tool-manager 格式规范

基于 MCP 协议规范、WinGet Manifest、SPDX 标准综合设计。

---

## 第 1 层：启动器配置（Launcher）

每个启动器一个 JSON 文件，存放在 `data/launchers/` 目录。

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schemaVersion` | string | ✅ | 格式版本，当前 `1.0` |
| `id` | string | ✅ | 启动器唯一标识，小写英文，如 `steam`、`winget` |
| `name` | string | ✅ | 启动器名称（英文/技术名） |
| `displayName` | string | ✅ | 显示名称（中文/用户友好） |
| `type` | string | ✅ | 类型：`game_platform` / `package_manager` / `utility` / `launcher` / `dev_tool` |
| `description` | string | - | 启动器描述 |
| `detected` | boolean | - | 是否已在本机检测到（默认 false） |
| `installed` | boolean | - | 是否已安装（默认 false） |
| `installPath` | string | - | 安装路径 |
| `mcp` | object | - | 适配的 MCP Server 信息 |
| `mcp.serverName` | string | - | MCP Server 名称 |
| `mcp.installMethod` | string | - | 安装方式：`npm` / `pip` / `manual` / `builtin` |
| `mcp.installCommand` | string | - | 安装命令 |
| `mcp.packageName` | string | - | 包名 |
| `mcp.capabilities` | array | - | MCP 提供的能力列表 |
| `mcp.capabilities[].name` | string | - | 能力名称 |
| `mcp.capabilities[].description` | string | - | 能力描述 |
| `mcp.capabilities[].inputSchema` | object | - | 能力输入参数（JSON Schema） |
| `sync` | object | - | 工具同步配置 |
| `sync.enabled` | boolean | - | 是否自动同步子工具 |
| `sync.method` | string | - | 同步方式：`mcp` / `cli` / `manual` |
| `sync.tool` | string | - | 通过 MCP 调用的工具名 |
| `sync.mapFields` | object | - | 返回字段 → 工具条目字段映射 |
| `createdAt` | string | - | 创建时间 ISO 8601 |
| `updatedAt` | string | - | 更新时间 ISO 8601 |

### 示例

```json
{
  "schemaVersion": "1.0",
  "id": "everything",
  "name": "Everything",
  "displayName": "Everything 文件搜索",
  "type": "utility",
  "description": "Windows 极速文件搜索工具",
  "detected": true,
  "installed": true,
  "installPath": "C:\\Program Files\\Everything\\Everything.exe",
  "mcp": {
    "serverName": "everything-mcp",
    "installMethod": "npm",
    "installCommand": "npm i -g everything-mcp",
    "packageName": "everything-mcp",
    "capabilities": [
      {
        "name": "search",
        "description": "搜索文件",
        "inputSchema": {
          "type": "object",
          "properties": {
            "keyword": { "type": "string", "description": "搜索关键词" },
            "limit": { "type": "number", "description": "返回数量上限" }
          }
        }
      }
    ]
  },
  "sync": {
    "enabled": false,
    "method": "manual",
    "tool": "",
    "mapFields": {}
  },
  "createdAt": "2026-10-15T13:00:00Z",
  "updatedAt": "2026-10-15T13:00:00Z"
}
```

---

## 第 2 层：工具条目（Tool Entry）

每个启动器一个大文件，存放在 `data/tools/` 目录，文件名与启动器 id 对应。

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schemaVersion` | string | ✅ | 格式版本，当前 `1.0` |
| `source` | string | ✅ | 来源启动器 id |
| `tools` | array | ✅ | 工具列表 |
| `tools[].id` | string | ✅ | 全局唯一 ID，格式 `{source}:{name}` |
| `tools[].name` | string | ✅ | 工具名称（英文/技术名） |
| `tools[].displayName` | string | ✅ | 显示名称（中文/用户友好） |
| `tools[].description` | string | - | 工具功能描述 |
| `tools[].version` | string | - | 版本号 |
| `tools[].publisher` | string | - | 发布者 |
| `tools[].website` | string | - | 官方网站 |
| `tools[].docUrl` | string | - | 文档地址 |
| `tools[].icon` | string | - | 图标路径或 URL |
| `tools[].launch` | object | - | 启动方式 |
| `tools[].launch.type` | string | - | 类型：`executable` / `protocol` / `command` / `mcp` |
| `tools[].launch.command` | string | - | 启动命令/路径 |
| `tools[].launch.args` | array | - | 启动参数 |
| `tools[].update` | object | - | 更新方式 |
| `tools[].update.method` | string | - | 更新方式：`winget` / `scoop` / `npm` / `manual` |
| `tools[].update.packageId` | string | - | 包管理器中的包 ID |
| `tools[].tags` | array | - | 标签分类 |
| `tools[].kind` | string | - | 工具类型：`app`（应用程序）/ `file`（文件/文件夹）/ `command`（命令行）/ `mcp_tool`（MCP 工具） |
| `tools[].actions` | array | - | 支持的操作：`launch`（启动）/ `open`（打开）/ `search`（搜索）/ `read`（读取） |
| `tools[].capabilities` | array | - | 能力标签（参考 MCP annotations） |
| `tools[].readOnly` | boolean | - | 是否只读（参考 MCP readOnlyHint） |
| `tools[].destructive` | boolean | - | 是否有破坏性（参考 MCP destructiveHint） |
| `tools[].metadata` | object | - | 扩展元数据 |

### 示例

```json
{
  "schemaVersion": "1.0",
  "source": "everything",
  "tools": [
    {
      "id": "everything:main",
      "name": "Everything",
      "displayName": "Everything 文件搜索",
      "description": "极速文件搜索工具，毫秒级搜索百万文件",
      "version": "1.4.1",
      "publisher": "voidtools",
      "website": "https://www.voidtools.com/",
      "docUrl": "https://www.voidtools.com/support/everything/",
      "icon": "",
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
      "kind": "app",
      "actions": ["launch", "search"],
      "capabilities": ["search", "index"],
      "readOnly": false,
      "destructive": false,
      "metadata": {
        "portable": false,
        "free": true
      }
    }
  ]
}
```

---

## 第 3 层：对外 MCP 工具

本技能作为 MCP Server 暴露给 AI 客户端的工具列表。

### 工具列表

| 工具名 | 说明 | 输入参数 |
|---|---|---|
| `search_tools` | 搜索工具 | `keyword`: string（搜索关键词）<br>`source`: string（可选，按启动器筛选） |
| `list_tools` | 列出工具 | `source`: string（可选，按启动器筛选） |
| `get_tool_info` | 查看工具详情 | `id`: string（工具 ID） |
| `launch_tool` | 启动/执行工具 | `id`: string（工具 ID） |
| `list_launchers` | 列出所有启动器 | - |
| `get_launcher_info` | 查看启动器详情 | `id`: string（启动器 ID） |
| `detect_launchers` | 发现本机已安装的启动器（预留） | - |

### 设计原则

- 符合 MCP 官方 Tool Schema 规范
- 使用 `readOnlyHint` / `destructiveHint` 等 annotations
- 元工具模式：只暴露少量高层工具，内部路由到具体实现
