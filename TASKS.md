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
- [x] 失败返回 MCP 协议层 `isError`（此前 success:false 被协议层当成成功）
- [x] `read_resource` 返回 `Iterable[ReadResourceContents]`（此前返回 TextContent 会报错）
- [x] `requirements.txt` 锁定 `mcp<2.0.0`（2.x 移除了本项目的低层装饰器 API）

### 路由能力
- [x] BackendClient - 单个后端 MCP 客户端（懒加载连接）
- [x] BackendManager - 后端连接池管理
- [x] `call_backend_tool` - 路由转发到后端 MCP 执行
- [x] `list_backend_tools` - 列出后端 MCP 提供的工具

### 工具接入
- [x] Everything（`kind=app`，返回启动命令）
- [x] Everything Search MCP（`kind=mcp`，已实测连接与调用跑通）

### 其他
- [x] Git 仓库初始化，代码版本管理
- [x] .gitignore 配置（测试脚本、缓存文件排除）

---

## 三、待完成 ⏳

### P0 - 核心打通
- [ ] 接入 Steam（steam-local-mcp）
- [ ] 接入 WinGet（官方内置 MCP）
- [ ] 接入 npm（npm MCP）
- [ ] launch_tool 真正执行启动（目前只返回命令）

### P1 - 体验完善
- [ ] 工具自动同步：通过 MCP 从启动器拉取子工具列表
- [ ] 启动器自动发现：检测本机安装了哪些启动器
- [ ] 工具标签分类和筛选优化
- [ ] 工具帮助文档查看（--help 输出）

### P2 - 生态扩展
- [ ] 接入 Scoop（写 CLI 适配器）
- [ ] 接入 WeGame（腾讯平台）
- [ ] 接入 uTools（效率启动器）
- [ ] 接入 Quicker
- [ ] 接入 PowerToys
- [ ] 与 Toolport 集成（注册为 Toolport 的后端）

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
    "args": ["/c", "npx", "-y", "everything-search-mcp"]
  },
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
├── requirements.txt            # 依赖（mcp 锁 <2.0.0）
├── docs/
│   ├── format.md               # 数据格式规范
│   ├── architecture.md         # 架构说明
│   ├── mcp-conformance.md      # MCP 协议一致性对比
│   └── code-review.md          # 代码审核报告
├── data/
│   ├── tools/                  # 工具条目
│   │   ├── everything.json
│   │   └── everything-search-mcp.json
│   └── backend-tools/          # 后端子工具缓存
│       └── everything-search-mcp.json
└── src/
    ├── server.py               # MCP Server 入口 + 对外工具定义 + 分发
    ├── store.py                # JSON 存储层
    ├── registry.py             # 工具注册中心
    └── backend_client.py       # 后端 MCP 客户端与连接池
```
