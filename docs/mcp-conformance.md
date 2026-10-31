# MCP 协议一致性对比

对比对象：**本项目的实现**（`src/server.py` 等）vs **MCP 规范 + mcp Python SDK 1.x 的实际行为**。

校验基准：
- MCP 规范确的能力清单（Tools / Resources / Prompts / Logging / Completion）
- 实测 SDK 版本：开发 venv `mcp 1.30.0` / 生产 pyenv 3.12 `mcp 1.29.0`
  （`requirements.txt` 锁定 `>=1.29.0,<2.0.0`；下限曾在 A5 提到 1.30，A9 实测
  1.29 已具备全部所需能力后改回 1.29——本文涉 1.30 的表述指**实测时的观察环境**，
  非依赖下限）
- 所有"SDK 实际行为"结论均来自阅读 SDK 源码 `mcp/server/lowlevel/server.py` + 本地实跑

---

## 0. 结论速览

| 对比维度 | 状态 | 说明 |
|---|---|---|
| 提供的方法（能力覆盖面） | ⚠️ 基本覆盖，缺 2 项 | 缺工具变更通知、未用标准 logging 能力 |
| 返回的方式 | ❌→✅ 已修复 | 失败曾不置 `isError`；`read_resource` 返回值类型错误 |
| 存储数据的结构 | ✅ 符合 | 内部存储自由，inputSchema 均为合法 JSON Schema |
| 命名与参数约定 | ⚠️→✅ 已统一 | 参数名统一为 `tool_id` |

---

## 1. 提供的方法

### 1.1 Server 能力覆盖

| MCP 方法 | 规范 | 本项目 | 评价 |
|---|---|---|---|
| `tools/list` | 必需（声明 tools 能力时） | ✅ `@app.list_tools()` | 一致 |
| `tools/call` | 必需 | ✅ `@app.call_tool()` | 一致 |
| `resources/list` | 声明 resources 能力时 | ✅ 3 个 Resource | 一致 |
| `resources/read` | 同上 | ✅ | 返回值类型**曾不合规**，已修复（见 2.3） |
| `resources/templates/list` | 声明 templates 能力时 | ❌ 未声明 | 无模板资源，合法 |
| `resources/subscribe` | 可选 | ❌ 未实现 | 静态资源无需订阅，可接受 |
| `prompts/list` / `prompts/get` | 声明 prompts 能力时 | ✅ 3 个 Prompt | 一致 |
| `logging/setLevel` | 可选（内置日志能力） | ❌ 未使用 | **建议改进**，见 1.3 |
| `completion/complete` | 可选 | ❌ 未实现 | Prompt 无动态参数，可接受 |
| `ping` | 传输层保活 | ✅ SDK 内置 | 一致 |

### 1.2 缺失：工具变更通知 ⚠️

规范提供了 `notifications/tools/list_changed`，Server 在工具集合变化时应主动通知客户端刷新。

本项目中以下操作**都会改变可见工具**，但均未发通知：

- `start_runtime` / `sync_capabilities` → 提供者能力目录变化
- 手工往 `data/tools/` 增删 JSON

后果：客户端缓存的 `tools/list` 结果过期。虽然本 Server 的**顶层工具集按暴露模式固定**
（lazy 19 / default 26 / full 35），能力目录又是透过 `list_capabilities` 动态查询的，因此实际影响有限，
但严格来说属于未履行的协议约定。

建议：若未来顶层工具集变为动态，务必补 `app.request_context.session.send_notifications...`。

### 1.3 日志能力未走标准通道 ⚠️

MCP 规范定义了标准的 `logging` 能力（`logging/setLevel` + `notifications/message`），
让 Server 把日志结构化上报给客户端。

本项目改为两条自定义 Tool：`get_runtime_logs` / `set_runtime_log_level`，
外加 `print(..., file=sys.stderr)`。

| 做法 | 评价 |
|---|---|
| stderr 输出 | ✅ **必须**且正确 —— stdio 传输下 stdout 只能是 JSON-RPC |
| 自定义 Tool 查日志 | ⚠️ 合法但笨重 —— AI 要额外消耗一次 tool call 才能看到日志 |

建议：保留 Tool 作为主动查询，同时考虑接入标准 logging capability 做被动推送。

### 1.4 分页 ⚠️

`tools/list`、`resources/list`、`prompts/list` 都支持 `cursor` / `nextCursor` 游标分页。
本项目 handler 签名不含 cursor，SDK 会返回不带 `nextCursor` 的结果 —— 当前 22 个工具规模下完全够用，
属于**有意不为**，不是缺陷。

---

## 2. 返回的方式

### 2.1 严重问题：`isError` 从未置位 ❌（已修复）

mcp SDK 1.x 的 `call_tool` handler 包装逻辑（源码实证）：

```python
async def handler(req):
    try:
        results = await func(req.params.name, req.params.arguments or {})
        return ServerResult(CallToolResult(content=list(results), isError=False))   # ← 恒为 False
    except Exception as e:
        return self._make_error_result(str(e))                                       # ← 恒为 True
```

**结论：正常返回值的 `isError` 被硬编码为 `False`，handler 内部无法修改。**

而本项目所有失败场景（工具未找到、后端未运行、连接超时……）都是返回一个文本内容为
`{"success": false, "error": "..."}` 的普通结果。

后果：

- MCP 客户端 / 调试面板显示为**"调用成功"**
- 依赖 `isError` 做重试、告警、统计的客户端逻辑完全失效
- AI 必须自己读懂文本里的 `success: false` 才知道失败了 —— 脆弱且浪费 token

**唯一正解是抛异常**，让 SDK 转成 `isError=True`。已按此修复：

```python
class ToolError(Exception):
    """业务级失败，由 SDK 转成 CallToolResult(isError=True)"""

def fail(error: str, error_type: str = "invalid_params", **extra) -> None:
    payload = {"success": False, "errorType": error_type, "error": error}
    payload.update(extra)
    raise ToolError(json.dumps(payload, ensure_ascii=False))
```

业务错误仍保留 JSON 正文，所以客户端**既**能感知失败，**也**能读到可读原因。
实测输出：

```
未知工具      -> {"success": false, "errorType": "unknown_tool", "error": "未知工具: no_such_tool", ...}
未找到工具    -> {"success": false, "errorType": "not_found", "error": "未找到工具: nope", ...}
launch 无配置 -> {"success": false, "errorType": "config_error", "error": "Tool has no launch config: ..."}
```

### 2.2 可选优化：`structuredContent` ✅（已启用）

当前所有结果仍由 `dispatch_tool` 手动 `json.dumps` 成字符串，但 `call_tool` 在其上
**额外返回结构化结果**，客户端无需再抓取文本：

mcp 1.30 起 handler 返回 `CallToolResult` 会被原样透传，因此 `call_tool` 直接构造
`CallToolResult(content=[TextContent(...)], structuredContent=<dict>)`——文本通道保留
（向后兼容），`structuredContent` 一并下发：

```python
structured = _result_to_structured(result)   # json.loads(result)，解析失败兜底为 None
return CallToolResult(
    content=[TextContent(type="text", text=result)],
    structuredContent=structured,
    isError=False,
)
```

同时 `list_tools` 给每个工具挂统一 `outputSchema`（`SHARED_OUTPUT_SCHEMA`：顶层 `success`
为 bool，`additionalProperties=true`），启用 MCP 2025-06-18 推荐的 `outputSchema` +
`structuredContent` 组合，便于客户端按 schema 消费。

**依赖下限**：`requirements.txt` 已从 `>=1.9.0` 提到 `>=1.30.0`，**A9 实测后改回
`>=1.29.0`**（仍锁 `<2.0.0`，2.x 会移除 1.x 低层装饰器）——实测 1.29 已具备
`structuredContent` / `outputSchema` / `CallToolResult` 透传，且生产环境即 1.29.0，
原 1.30 下限建立在错误前提上。协议回环测试已验证：挂了 `outputSchema` 的成功调用
确实返回 `structuredContent`，且未被 SDK 的 outputSchema 校验拒掉（`Output validation error`）。

### 2.3 `resources/read` 返回值类型错误 ❌（已修复）

SDK 对 `read_resource` 的处理：

```python
case Iterable() as contents:
    contents_list = [create_content(item.content, item.mime_type) for item in contents]
```

它读取的是每个条目的 **`.content` 和 `.mime_type`**，而本项目原先返回的是 `list[TextContent]`。
`TextContent` 只有 `.text`、没有 `.content` → 读取资源时必然 `AttributeError`。
（因为没人实际调用过 resources，这个 bug 一直没暴露。）

已改为返回 SDK 要求的 `Iterable[ReadResourceContents]`，并带上正确的 `mimeType`：

```python
return [ReadResourceContents(content=text, mime_type="application/json")]
```

实测：`awam://tools` 返回 `list[ReadResourceContents]`，`mime_type = application/json`。

**⚠️ 同类问题 D25（后由协议回环测试发现）**：返回值修对了，参数却没修 ——
`req.params.uri` 是 `pydantic.AnyUrl`，`AnyUrl("awam://tools") == "awam://tools"` 恒为 `False`，
三个资源仍全部落到"未知资源"分支。现在入口先 `str(uri)` 归一化。
教训：**参数类型与返回值类型都要在真实会话里验一遍**。

### 2.3.1 `prompts/get` 返回类型（D26，协议回环测试发现）

mcp 1.30 的实现是 `return types.ServerResult(await func(req.params.name, ...))`，
即 handler 的返回值**直接**作为 `GetPromptResult` 参与 pydantic 校验；
返回 `list[PromptMessage]` 会被拒（报 `Input should be a valid dictionary or instance of
GetPromptResult`）。协议在 2025-06-18 之后就是这个要求，早期版本才接受裸列表。

现按版本兼容：`GetPromptResult` 可导入时返回它，否则退回 `list[PromptMessage]`。

### 2.4 Tool annotations ✅

MCP Tool 支持 `annotations`（`title` / `readOnlyHint` / `destructiveHint` / `idempotentHint`
/ `openWorldHint`），客户端据此在 UI 上给出"只读/危险"提示。

数据文件里已经有 `readOnly` / `destructive` 字段，但此前**没有映射到 Tool 定义**，纯装饰。

**已修复**：annotations 逐个附加（P1-04 起声明在 `src/tool_specs.py` 的 spec 表里，
早期实现是 `server.py` 的 `TOOL_ANNOTATION_HINTS` 常量）：

| 类别 | 工具 | hints |
|---|---|---|
| 只读 | `search_tools` / `list_tools` / `get_tool_info` / `list_capabilities` / `get_runtime_status` / `get_runtime_logs` / `get_usage_stats` / `list_packages` / `search_packages` | `readOnlyHint=true`、`destructiveHint=false` |
| 幂等 | `start_runtime` / `stop_runtime` / `sync_capabilities` | `idempotentHint=true` |
| 副作用静态不可知 | `call_capability` / `launch_tool` / `get_tool_help` / `discover_tools` / `launch_package` | `openWorldHint=true`（**不谎报 readOnly**） |
| 副作用静态可知且破坏性 | `install_package` | `openWorldHint=true`、`destructiveHint=true` |

`call_capability` 只标 `openWorldHint` 是刻意的：它转发到任意能力，
副作用取决于运行时的 `capability_id`，静态断言只读会误导客户端。真正的防护落在
**转发前的 destructive 门禁**上：目标能力 `risk=destructive` 时默认拒绝
（`errorType: destructive_blocked`），需显式传 `allowDestructive=true`。

`launch_tool` 走同一条判定：默认只返回命令（零副作用），显式 `execute=true` 才
spawn 进程，条目 `destructive` 时同样需 `allowDestructive=true`；正因为多了这条
执行路径，它的 `readOnlyHint` 已撤下、改标 `openWorldHint`。

`get_tool_help` 是同一个形状的第三条执行路径：默认只组装数据（文档链接 / 后端
能力清单 / 动作清单 / 命令预览，零 spawn），`kind=app` 显式 `run=true` 才执行
帮助命令——图形程序不认 `--help` 时会把它当普通启动参数跑起来，所以这条路径也
接进了 destructive 门禁（审计与拒绝文案按 `get_tool_help` 入口标注）。

`discover_tools` 是第四条：默认路径确实只读系统状态（路径存在 / 注册表登记值 /
目录条目数 / `shutil.which` 查 PATH，注册表那一维只 `QueryValueEx` 不写），但清单里的
`command.argv` 在 `run_commands=true` 时会被
真的执行去取版本号。**能力面含执行就不声明 `readOnlyHint`**——哪怕默认路径一次都不
spawn，声明只读等于把「它可以跑命令」对客户端藏起来。返回体的 `run` 报的是
「这一维真的跑过没有」，不是调用方传了什么标志位。

`list_packages` / `search_packages` 是**真的只读**（读安装器落在盘上的
`apps/*/manifest.json` 与 `buckets/*/*.json`，一次 `scoop list` 都不跑），所以照常
声明 `readOnlyHint=true`。`launch_package` / `install_package` 各有一条执行路径，
与 `launch_tool` 同款标 `openWorldHint`；两者的差别写在注解里而非仅写在文案里：
`install_package` 会下载并执行 bucket 里的安装脚本、改本机已装清单，因此静态声明
`destructiveHint=true`，运行时还要过 `policy` 的 `kind=package` 门禁（fail-closed：
不在 `PACKAGE_CONFIRMED_OPS` 白名单里的包操作一律要 `allowDestructive=true`；
`launch_package` 因「起的是用户早就装好的程序」在白名单内，与 `launch_tool` 同级）。

### 2.5 异常兜底

SDK 已自带 `except Exception`，所以单个工具抛异常不会让 Server 崩溃。
本项目额外加了一层：非 `ToolError` 的意外异常转成 JSON 载荷的内部错误，
保证客户端拿到的仍是结构化内容而非裸异常文本。

但这句话的适用范围要划清，它是 2026-10-19 真机会话实测出来的边界：
**入参校验失败根本进不了 handler**。SDK 先按 `inputSchema` 校验，越界就回一段
SDK 自己的文本，本项目无法把它转成 JSON——`search_packages limit=0` 得到
`Input validation error: 0 is less than the minimum of 1`，
`list_packages manager=winget` 得到 `'winget' is not one of ['scoop']`。
所以「结构化错误载荷」指的是 **handler 层**的约定，不是整条调用链的不变式。
两条边界因此是叠加而不是重复：schema 的 `minimum` / `enum` 是第一道（拦在门外，
形态不受控），`pkgman._clamp_int` 与 `resolve_root` 的 `invalid_params` 是第二道
（只有绕过 schema 的路径——直调 handler、或经 `call_capability` 转发——才会走到）。

---

## 3. 存储数据的结构

### 3.1 协议未约束的部分 ✅

MCP **不规定** Server 内部如何持久化数据，因此 `data/tools/`（仓库内，人工维护）
+ 运行时目录 `backend-tools/`（`%LOCALAPPDATA%\.awam-tool-manager/`，自动生成）
的两层 JSON 设计完全自由，不构成一致性问题。

### 3.2 `inputSchema` 合法性 ✅

协议要求每个 Tool 的 `inputSchema` 是合法 JSON Schema（Draft 2020-12 子集）。
本项目 22 个工具全部为：

```json
{ "type": "object", "properties": { ... }, "required": [...] }
```

符合要求（`required` 是可选项：`discover_tools` 的两个参数都可选，声明里就不带它）。
且从 1.30 起 SDK 会**自动用 jsonschema 校验入参**（`validate_input=True` 默认开启），
参数错误会在进入 handler 前被拦截。

### 3.3 后端子工具 schema 透传 ⚠️

`data/backend-tools/*.json`（A14 起为运行时目录下的 `backend-tools/*.json`）里的
`tools[]` 是从后端透传的，只保留了 `name` / `description` / `inputSchema`。

MCP Tool 还可携带 `title` / `annotations` / `outputSchema`，这些在同步时丢失了。
建议在 `_refresh_tools_cache` 里一并保存，供未来结构化输出与安全提示使用。

### 3.4 `lastSynced` 字段 ✅

writer 曾硬编码 `"lastSynced": "now"`（历史数据里存的就是这个值），无法判断缓存新鲜度。
现已写入 `datetime.now(timezone.utc)` 的 `%Y-%m-%dT%H:%M:%SZ`，与 `docs/format.md` 的示例
格式一致；真实后端回归后运行时目录的 `backend-tools/everything-search-mcp.json` 已更新为
`"2026-10-15T12:04:56Z"`。

---

## 4. 依赖版本：最大的协议一致性风险

`mcp` 主版本升级会**直接摧毁**本 Server：（已实测）

| 版本 | `Server` 的装饰器 API | 本项目 |
|---|---|---|
| **1.x** | `list_tools()` / `call_tool()` / `read_resource()` / `list_prompts()` | ✅ 依赖这套 |
| **2.3.0** | 上述方法全部移除；handler 签名改为 `(ctx, params)`，返回值要求 `ReadResourceResult` 等 | ❌ `AttributeError` |

实测：

```
>>> import sys; sys.path.insert(0,'src'); import server
AttributeError: 'Server' object has no attribute 'list_tools'
```

原先 `requirements.txt` 写 `mcp>=1.0.0`（无上界）→ 全新环境必然安装 2.x → Server 无法启动。
现已收紧为 `mcp>=1.29.0,<2.0.0`（A5 曾提到 1.30，A9 实测后改回 1.29），
安装到 1.30.0 / 1.29.0 后 `import server` 均正常。

---

## 5. 待办清单

| 优先级 | 事项 | 章节 |
|---|---|---|
| ✅ 已完成 | 失败返回 MCP 协议层 `isError` | 2.1 |
| ✅ 已完成 | `read_resource` 返回 `ReadResourceContents` | 2.3 |
| ✅ 已完成 | 依赖锁定 `mcp<2.0.0` | 4 |
| ✅ 已完成 | 参数名统一 `tool_id`，修正 prompt 模板 | - |
| ✅ 已完成 | `lastSynced` 写真实 ISO 8601 时间（`%Y-%m-%dT%H:%M:%SZ`） | 3.4 |
| ✅ 已完成 | 数据文件读写补齐：坏 JSON 跳过 + 原子写盘 | code-review D12/D13 |
| ✅ 已完成 | Tool `annotations` 下发 + `destructive` 转发门禁 | 2.4 |
| ✅ 已完成 | 工具条目 schema 校验（数据格式不再是纸面标准） | code-review A1 |
| ✅ 已完成 | `resources/read` 参数归一化（`AnyUrl` 不等于 str） | code-review D25 |
| ✅ 已完成 | `prompts/get` 返回 `GetPromptResult` | code-review D26 |
| ✅ 已完成 | 依赖下限 `>=1.30`（A9 实测后改回 `>=1.29`），启用 `structuredContent` + `outputSchema` | 2.2 |
| P2 | 同步后端工具时保留 `title` / `annotations` / `outputSchema` | 3.3 |
| P3 | 工具集动态化时补 `notifications/tools/list_changed` | 1.2 |
| P3 | 考虑接入标准 logging capability | 1.3 |
