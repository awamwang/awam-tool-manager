# awam-tool-manager

统一工具管理器 —— 维护本机工具名单，提供统一的注册、搜索、启动与能力路由调用能力。

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

> `requirements.txt` 锁定 `mcp>=1.29.0,<2.0.0`。
> mcp 2.x 移除了本项目使用的低层装饰器 API（`@app.list_tools()` 等），升级会导致无法启动。

### 运行 MCP Server

```bash
cd src
python server.py
```

### 在 MCP 客户端中配置

```json
{
  "mcpServers": {
    "awam-tool-manager": {
      "command": "python",
      "args": ["G:\\Projects\\19AI\\skills\\awam-tool-manager\\src\\server.py"]
    }
  }
}
```

## 已登记工具

| tool_id | kind | 说明 |
|---|---|---|
| `everything` | app | Everything 文件搜索（返回启动命令） |
| `everything-search-mcp` | mcp | Everything 的 MCP 接口，可路由调用 `search_files` 等 |
| `winget-mcp` | mcp | WinGet 官方 MCP：查询/安装升级包（安装受破坏性门禁） |
| `quicker` | launcher | Quicker 启动器：列出/搜索/执行动作 |
| `utools` | launcher | uTools 启动器：解析插件 asar 列出功能，`utools://` 协议唤起（fire-and-forget） |

后续可接入：Steam、npm、Scoop、WeGame、PowerToys 等，
按 [新增工具步骤](SKILL.md#新增工具步骤) 添加 JSON 即可。

## 目录结构

```
data/                  # 仓库内只有人工维护的真相源（A14 起）
└── tools/             # 工具条目，每个工具一个 JSON
src/
├── server.py          # MCP Server 入口
├── store.py           # JSON 存储层（tools 走仓库，缓存走运行时目录）
├── registry.py        # 工具注册中心
├── backend_client.py  # MCP 运行时客户端与连接池
├── capabilities.py    # Provider / Capability / Runtime 路由层
├── launcher_client.py # 启动器适配器（Quicker / uTools）
└── runtime_paths.py   # 运行时目录解析 + 旧路径迁移
```

> 运行时数据（MCP 能力缓存、启动器动作清单、运行时启动意图）不在仓库，
> 位于 `%LOCALAPPDATA%\.awam-tool-manager/`（`AWAM_RUNTIME_DIR` 可覆盖）。
>
> `data/launchers/` 与 `src/adapters/` 属于早期版本遗留，已在重构中移除，代码中不存在。

## 文档

- [数据格式规范](docs/format.md)
- [架构说明](docs/architecture.md)
- [MCP 协议一致性对比](docs/mcp-conformance.md)
- [代码审核报告](docs/code-review.md)
