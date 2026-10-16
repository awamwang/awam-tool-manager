---
name: awam-tool-manager
description: 统一工具管理器，维护本机工具名单，提供统一的工具注册、搜索、启动能力。作为 MCP Server 暴露给 AI 客户端，支持搜索工具、列出工具、启动工具、路由调用后端 MCP、管理后端进程等。
---

# awam-tool-manager 技能

统一管理本机各类工具，提供统一的注册、搜索、启动与**后端路由调用**能力。

MCP 只是工具的一种类型（`kind=mcp`），普通应用是另一种（`kind=app`），
两者共用同一套 `tool_id` 体系，不再区分"启动器"与"子工具"。

## 核心能力

- **工具注册**：以 JSON 文件为单位登记工具元数据（`data/tools/`）
- **MCP 服务**：作为 MCP Server 暴露给 AI 客户端（stdio 模式）
- **路由调用**：把调用转发给 `kind=mcp` 工具对应的后端 MCP Server
- **进程管理**：管理后端 MCP 子进程的启停、状态、日志
- **目录同步**：拉取后端 MCP 的子工具清单并缓存到本地
- **依赖自装**：启动命令缺失时按 `install` 元数据自动安装（服务启动时后台预热，只装不启）

## 目录结构

```
awam-tool-manager/
├── SKILL.md                  # 本文件
├── docs/
│   ├── format.md             # 数据格式规范
│   ├── architecture.md       # 架构说明
│   ├── mcp-conformance.md    # MCP 协议一致性对比
│   └── code-review.md        # 代码审核报告
├── data/
│   ├── tools/                # 工具条目（每个工具一个 JSON）
│   └── backend-tools/        # 后端 MCP 子工具缓存（自动生成）
├── src/
│   ├── server.py             # MCP Server 入口 + 对外工具定义 + 分发
│   ├── store.py              # JSON 存储层
│   ├── registry.py           # 工具注册中心（查询语义）
│   ├── backend_client.py     # 后端 MCP 客户端与连接池
│   └── schema.py             # 两层数据的 JSON Schema 校验 + 文档生成源
├── scripts/
│   └── generate_format_doc.py  # 由 schema.py 生成 docs/format.md 的字段表
└── tests/                    # 106 个用例（含真实 MCP 会话与文档漂移检测）
```

> 早期版本曾有 `data/launchers/`（启动器配置）与 `src/adapters/`（启动器适配器），
> 已在重构中移除，**相关概念不再存在于代码**，请勿参照旧版说明扩展。

## 对外 MCP 工具

后端相关工具统一使用 **`tool_id`** 指定目标工具（不存在 `mcp_tool_id` / `launcher_id`）。

| 工具名 | 参数 | 说明 |
|---|---|---|
| `search_tools` | `keyword`(必填)、`kind` | 按关键词搜索工具 |
| `list_tools` | `kind` | 列出所有工具 |
| `get_tool_info` | `tool_id`(必填) | 查看工具详情 |
| `launch_tool` | `tool_id`(必填) | 返回工具的启动命令（**目前不实际执行**） |
| `call_backend_tool` | `tool_id`、`tool_name`(必填)、`arguments`、`allowDestructive` | 路由调用后端 MCP 的子工具；命中破坏性判定时需显式确认（A12：条目配了 `destructiveTools` 就只拦清单内子工具，未配则整个后端都拦） |
| `list_backend_tools` | `tool_id`(必填)、`force_refresh` | 列出后端子工具（含参数 schema） |
| `start_backend` | `tool_id`(必填) | 启动后端并自动同步工具目录；启动命令缺失时会先按 `install` 自动安装 |
| `stop_backend` | `tool_id`(必填) | 停止后端 |
| `get_backend_status` | `tool_id` | 查看单个或全部后端状态 |
| `get_backend_logs` | `tool_id`(必填)、`level`、`limit` | 查看后端日志（`limit<=0` 返回空） |
| `set_backend_log_level` | `tool_id`、`level`(必填) | 设置后端日志级别（低于该级别的日志不再入队/打印） |
| `sync_backend_tools` | `tool_id`(必填) | 手动同步子工具目录 |
| `list_launcher_actions` | `tool_id`(必填)、`keyword`、`limit`、`refresh` | 列出/搜索启动器（Quicker）的动作；返回带 `lastSynced`/`source` |
| `run_launcher_action` | `tool_id`(必填)、`action_id`、`action_name`、`param`、`wait`、`allowDestructive` | 执行启动器动作；条目标了 `destructive` 时需 `allowDestructive=true` |
| `sync_launcher_actions` | `tool_id`(必填) | 手动同步启动器动作清单 |

> `kind=launcher`（启动器，如 Quicker）不是 MCP：没有长连接，调用是一次性外部命令
> （`QuickerStarter.exe -c<秒> "runaction:<ID或名称>?参数"`）。动作清单缓存在
> `data/launcher-actions/{tool_id}.json`，`source=partial` 表示部分动作只拿到 ID
> （Quicker 的 `quicker.db` 是加密的，名称只能从 state 缓存 + 运行日志尽力解析）。
>
> **这份清单是活的**：缓存超过 5 分钟（`ACTIONS_CACHE_TTL`）时，`list`/`run` 会
> 自动重扫一次再回答，`refresh=true` 可强制重扫，所以 Quicker 里新增的动作无需手动
> 同步就能搜到、调到。刷新失败（如 Quicker 数据目录不可用）会静默沿用旧缓存，
> 不会让查询失败——看返回里的 `lastSynced` 就知道数据有多旧。

另有 3 个 Resource：`awam://tools`、`awam://mcp-servers`、`awam://backends/status`。

## 错误处理约定

调用失败时走 **MCP 协议层错误**（`isError=true`），文本内容是可解析的 JSON：

```json
{ "success": false, "errorType": "not_found", "error": "未找到工具: xxx" }
```

`errorType` 取值：`not_found` / `unknown_tool` / `unknown_resource` / `invalid_params`
/ `connection_error` / `runtime_error` / `config_error` / `destructive_blocked`
/ `backend_busy` / `internal_error`。

- **`backend_busy`**：同一后端正在处理另一个调用（单后端串行，并发调用会被直接拒绝，不排队）。
  调用方应稍后重试，不要无限重试打满。

成功结果除文本通道（JSON 文本）外，还附带 **`structuredContent`**（dict），客户端无需
再解析文本即可按结构消费；每个工具的 `outputSchema` 统一声明输出含顶层 `success`（bool）。
依赖下限为 `mcp>=1.29`（A9 实测 1.29 已有 structuredContent / outputSchema /
CallToolResult 透传；生产环境 pyenv 3.12 即 1.29.0）。

## 安全约定

- 工具条目里的 `readOnly` / `destructive` 会映射到 MCP Tool annotations
- `call_backend_tool` 转发前会检查目标工具的 `destructive`：
  为 `true` 时必须显式传 `allowDestructive=true`，否则返回 `destructive_blocked`
- 不合规的 `data/tools/*.json` 会被跳过并向 stderr 告警，不会让整个服务不可用

## 新增工具步骤

1. 在 `data/tools/` 下新建 `{tool_id}.json`（字段见 `docs/format.md`）
2. 普通应用写 `kind=app` + `launch`；MCP 工具写 `kind=mcp` + `connection`
3. MCP 工具调用 `start_backend(tool_id=...)` 建立连接并自动同步子工具目录；
   启动命令不在 PATH 时会按 `install.command` 自动安装一次（服务启动时也会后台预热）

> 修改已有工具的 `connection` 时，若后端当前是停止/错误状态，**下次调用会用新配置重建连接**，
> 无需重启 MCP Server；运行中则不受影响（避免留下孤儿进程），需先 `stop_backend` 再 `start_backend`。
> 坏掉的 JSON 会被跳过并打印 stderr 告警，不会导致整个服务不可用。

> **后端状态跨进程自愈（A10）**：`start_backend` 成功会把启动意图落盘（`backend_desired_state.json`）。
> 上游网关（toolport）会周期性整池回收它拉起的 MCP server 进程，回收后新进程内存状态清零——
> 有了意图落盘，每个新进程启动时会**自动恢复**上次启动过的后端，用户任何时刻查状态都是 running；
> `stop_backend` 会清除意图。若后端业务依赖本机应用（如 everything-search-mcp 依赖 Everything
> 桌面程序的 IPC），业务成败取决于该应用是否运行，与连接管理无关。

> **新增/修改字段时**：只改 `src/schema.py`，再跑 `python scripts/generate_format_doc.py`
> 更新 `docs/format.md` 的字段表。直接手改文档会被 `tests/test_docs_sync.py` 判为漂移。

## 依赖约束

`requirements.txt` 锁定 `mcp>=1.29.0,<2.0.0`（A9 实测 1.29 已支持 structuredContent /
outputSchema 与 CallToolResult 透传；心跳探测用 `send_ping()`，1.x 全版本可用）。
**mcp 2.x 移除了本项目依赖的低层装饰器 API，不可直接升级**，迁移需重写 `server.py`。

## 数据格式

详见 `docs/format.md`
