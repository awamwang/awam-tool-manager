---
name: awam-tool-manager
description: 统一工具管理器，维护启动器工具集名单，提供统一的工具注册、搜索、启动能力。作为 MCP Server 暴露给 AI 客户端，支持搜索工具、列出工具、启动工具、查看启动器信息等。
---

# awam-tool-manager 技能

统一管理本机各种启动器/工具集下的工具，提供统一的注册、搜索、启动能力。

## 核心能力

- **启动器管理**：维护已接入的启动器列表（Steam、WinGet、Everything、uTools 等）
- **工具注册**：统一格式存储所有子工具的元数据
- **MCP 服务**：作为 MCP Server 暴露给 AI 客户端
- **MCP 推荐**：指定启动器时，推荐适配的 MCP Server 安装方式

## 目录结构

```
awam-tool-manager/
├── SKILL.md                    # 本文件
├── docs/format.md              # 格式规范文档
├── data/
│   ├── launchers/              # 启动器配置（每个一个 JSON）
│   └── tools/                  # 工具条目（每个启动器一个 JSON）
└── src/
    ├── server.py               # MCP Server 入口
    ├── store.py                # JSON 存储层
    ├── registry.py             # 工具注册中心
    └── adapters/              # 启动器适配器
        ├── base.py             # 适配器基类
        └── everything.py       # Everything 适配器
```

## 对外 MCP 工具

| 工具名 | 说明 |
|---|---|
| `search_tools` | 搜索工具 |
| `list_tools` | 列出工具 |
| `get_tool_info` | 查看工具详情 |
| `launch_tool` | 启动工具 |
| `list_launchers` | 列出所有启动器 |
| `get_launcher_info` | 查看启动器详情 |

## 新增启动器步骤

1. 在 `data/launchers/` 新建 `{id}.json` 配置
2. 在 `data/tools/` 新建 `{id}.json` 工具列表
3. 在 `src/adapters/` 新建适配器（可选，用于自动检测和同步）
4. 在 `registry.py` 中注册适配器

## 数据格式

详见 `docs/format.md`
