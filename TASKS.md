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
- [x] 三层数据格式设计（启动器配置 / 工具条目 / 对外 MCP 工具）
- [x] JSON 存储层（store.py）
- [x] 工具注册中心（registry.py）：搜索、列出、详情、启动信息
- [x] MCP Server 入口（server.py）：stdio 模式
- [x] 日志系统（输出到 stderr，不污染协议通道）

### 对外 MCP 工具（6 个）
- [x] `search_tools` - 搜索工具（关键词 + 来源筛选）
- [x] `list_tools` - 列出所有工具
- [x] `get_tool_info` - 查看工具详情
- [x] `launch_tool` - 返回启动命令信息
- [x] `list_launchers` - 列出所有启动器
- [x] `get_launcher_info` - 查看启动器详情

### 路由能力
- [x] BackendClient - 单个后端 MCP 客户端（懒加载连接）
- [x] BackendManager - 后端连接池管理
- [x] `call_backend_tool` - 路由转发到后端 MCP 执行
- [x] `list_backend_tools` - 列出后端 MCP 提供的工具

### 启动器接入
- [x] Everything - 配置完成，待实际连接验证

### 其他
- [x] Git 仓库初始化，代码版本管理
- [x] .gitignore 配置（测试脚本、缓存文件排除）

---

## 三、待完成 ⏳

### P0 - 核心打通
- [ ] 验证 everything-mcp 实际连接和调用是否跑通
- [ ] 接入 Steam 启动器（steam-local-mcp）
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

## 四、协议格式规范

### 4.1 三层架构

| 层级 | 文件位置 | 说明 |
|---|---|---|
| 启动器配置 | `data/launchers/{id}.json` | 每个启动器一个文件 |
| 工具条目 | `data/tools/{id}.json` | 每个启动器一个大文件，列所有子工具 |
| 对外 MCP 工具 | `src/server.py` | 暴露给 AI 客户端的工具列表 |

### 4.2 启动器配置字段

```json
{
  "schemaVersion": "1.0",
  "id": "everything",
  "name": "Everything",
  "displayName": "Everything 文件搜索",
  "type": "utility",
  "description": "描述",
  "detected": true,
  "installed": true,
  "installPath": "C:\\...",
  "mcp": {
    "serverName": "everything-mcp",
    "installMethod": "npm",
    "installCommand": "npm i -g everything-mcp",
    "connection": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "everything-mcp"]
    },
    "capabilities": [...]
  },
  "sync": {
    "enabled": false,
    "method": "manual"
  }
}
```

### 4.3 工具条目字段

```json
{
  "schemaVersion": "1.0",
  "source": "everything",
  "tools": [
    {
      "id": "everything:main",
      "name": "Everything",
      "displayName": "Everything 文件搜索",
      "description": "描述",
      "version": "1.4.1",
      "publisher": "voidtools",
      "website": "https://...",
      "docUrl": "https://...",
      "launch": {
        "type": "executable",
        "command": "C:\\...",
        "args": []
      },
      "update": {
        "method": "winget",
        "packageId": "voidtools.Everything"
      },
      "tags": ["文件搜索", "系统工具"],
      "readOnly": false,
      "destructive": false
    }
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
├── requirements.txt            # 依赖
├── docs/
│   └── format.md               # 完整格式规范
├── data/
│   ├── launchers/              # 启动器配置
│   │   └── everything.json
│   └── tools/                 # 工具条目
│       └── everything.json
└── src/
    ├── server.py               # MCP Server 入口
    ├── store.py                # JSON 存储层
    ├── registry.py              # 工具注册中心
    ├── backend_client.py        # 后端 MCP 路由
    └── adapters/               # 启动器适配器
        ├── base.py
        └── everything.py
```
