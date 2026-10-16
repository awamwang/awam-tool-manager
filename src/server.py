"""awam-tool-manager MCP Server

统一工具管理的 MCP 服务，提供工具搜索、列出、启动等能力。
"""

import asyncio
import contextlib
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.types import (
    Resource,
    Prompt,
    PromptMessage,
    TextContent,
    Tool,
    CallToolResult,
)
import mcp.server.stdio

try:  # mcp >=1.9 起才有 ReadResourceContents，老版本退回纯文本返回形式
    from mcp.server.lowlevel.helper_types import ReadResourceContents
except ImportError:  # pragma: no cover
    ReadResourceContents = None

try:  # Tool annotations 同样是 1.9 才引入的能力
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    ToolAnnotations = None

try:  # prompts/get 的返回类型：2025-06-18 之后要求 GetPromptResult
    from mcp.types import GetPromptResult
except ImportError:  # pragma: no cover
    GetPromptResult = None

# 添加 src 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from store import JsonStore
from runtime_paths import get_runtime_dir, migrate_legacy_runtime
from registry import ToolRegistry
from backend_client import BackendClient, BackendManager, ensure_npm_bin_on_path
from launcher_client import LauncherError, LauncherManager
from schema import schema_backend_available
# P1-04：业务错误三件套搬到 errors.py（tool_specs 的 handlers 要用，留在
# server.py 会形成 server → tool_specs → server 循环 import），这里继续
# re-export，`from server import ToolError` 等外部引用拿到的仍是同一个类对象。
from errors import ToolError, fail, ensure_success
# P1-04：对外工具声明式注册表——list_tools 的定义源与 dispatch 的查表目标。
# SHARED_OUTPUT_SCHEMA 已随常量搬到 tool_specs（连注释一起），这里 re-export
# 保护外部引用。
from tool_specs import SHARED_OUTPUT_SCHEMA, ToolContext, build_mcp_tools  # noqa: F401

# 数据目录：DATA_DIR 放人写的声明（进版本库），RUNTIME_DIR 放程序生成的
# 运行时数据（本机应用数据目录）。两者此前混在 data/ 下，导致活实例改写
# lastSynced 时工作区永远脏。见 runtime_paths.py。
DATA_DIR = Path(__file__).parent.parent / "data"
RUNTIME_DIR = get_runtime_dir()

# 初始化
store = JsonStore(DATA_DIR, runtime_dir=RUNTIME_DIR)
registry = ToolRegistry(store)
backend_manager = BackendManager(store)
launcher_manager = LauncherManager(registry, store)

# 创建 MCP Server
app = Server("awam-tool-manager")


def log(msg: str):
    """日志输出到 stderr，不污染 stdout 协议通道"""
    print(f"[awam-tool-manager] {msg}", file=sys.stderr, flush=True)


# P1-04：handler 依赖注入容器。tool_specs 的 handlers 不 import 任何 manager，
# 只从 ctx 取；测试 monkeypatch 的是 manager 对象属性，ctx 持有同一引用，
# patch 照样生效。
_TOOL_CONTEXT = ToolContext(
    registry=registry,
    backend_manager=backend_manager,
    launcher_manager=launcher_manager,
    store=store,
    log=log,
)


# MCP Tool annotations：把数据里的 `readOnly` / `destructive`（见 docs/format.md）
# 语义映射到协议层 hints，客户端可据此决定是否要二次确认。
# 只有能静态确定的才标：`call_backend_tool` 转发到任意后端子工具，
# 其副作用取决于运行时的 tool_name，因此只标 openWorldHint 而不断言只读/破坏性。
TOOL_ANNOTATION_HINTS: dict[str, dict[str, bool]] = {
    "search_tools": {"readOnlyHint": True, "destructiveHint": False},
    "list_tools": {"readOnlyHint": True, "destructiveHint": False},
    "get_tool_info": {"readOnlyHint": True, "destructiveHint": False},
    "launch_tool": {"readOnlyHint": True, "destructiveHint": False},
    "list_backend_tools": {"readOnlyHint": True, "destructiveHint": False},
    "get_backend_status": {"readOnlyHint": True, "destructiveHint": False},
    "get_backend_logs": {"readOnlyHint": True, "destructiveHint": False},
    "start_backend": {"idempotentHint": True},
    "stop_backend": {"idempotentHint": True},
    "sync_backend_tools": {"idempotentHint": True},
    # 启动器：列/同步是只读或幂等；执行动作会真的跑起来，只标 openWorld
    "list_launcher_actions": {"readOnlyHint": True, "destructiveHint": False},
    "sync_launcher_actions": {"idempotentHint": True},
    "run_launcher_action": {"openWorldHint": True},
    "set_backend_log_level": {},
    # 转发到后端 MCP，属于"与外部世界交互"但静态无法判定副作用
    "call_backend_tool": {"openWorldHint": True},
}


def _needs_destructive_confirm(tool: dict | None, sub_tool: str) -> bool:
    """判断转发到 `sub_tool` 是否需要 `allowDestructive` 显式确认。

    A12（2026-10-16）：破坏性实际属于「子工具」，不是「整个后端」。
    接入 winget-mcp 时实测——它同时提供纯查询 `find-winget-packages` 与
    会装软件的 `install-winget-package`，而条目级 `destructive=true` 一刀切，
    导致连查询都被拦（错误信息还笼统地指向整个后端）。

    规则：
    - 条目未标 `destructive` → 不拦（保持既有行为）
    - 标了 `destructive` 且未配 `destructiveTools` → 整后端都拦（旧语义，不回退）
    - 标了 `destructive` 且配了 `destructiveTools` → 只拦清单内的子工具
    """
    if not tool or not tool.get("destructive"):
        return False
    listed = tool.get("destructiveTools")
    if listed is None:
        return True
    return sub_tool in listed


@app.list_tools()
async def list_tools() -> list[Tool]:
    """列出所有可用工具"""
    log("收到 list_tools 请求")
    # P1-04：定义由 tool_specs 声明表统一产出（annotations / outputSchema 在
    # build_mcp_tools() 内挂载），此处只保留两行日志。
    tools = build_mcp_tools()

    log(f"返回 {len(tools)} 个工具定义")
    return tools


def _result_to_structured(result: str) -> dict | None:
    """把 dispatch_tool 返回的 JSON 文本解析成结构化结果（dict）。

    返回 None 表示无法解析（理论上不会发生：dispatch_tool 永远 json.dumps）。
    此时调用方应跳过 structuredContent，避免把非法结构透传给客户端。
    """
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    # 个别工具万一返回非对象（不应发生），用 {"value": ...} 包一层，保证结构化可用
    return {"value": parsed}


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    """MCP tools/call 入口：包成 CallToolResult 并附带结构化结果。

    dispatch_tool 始终返回 JSON 文本；这里在不破坏既有文本输出的前提下，
    额外附带 structuredContent（dict），让 MCP 客户端能可靠地按结构解析，
    而不必抓取文本。成功路径直接返回 CallToolResult，由 SDK 原样透传；
    失败路径仍抛 ToolError，由 SDK 转成 isError=True。
    """
    log(f"收到工具调用: {name} | 参数: {arguments}")

    try:
        result = await dispatch_tool(name, arguments or {})
    except ToolError:
        # 业务错误：交给 SDK 转成 CallToolResult(isError=True)
        raise
    except Exception as e:
        log(f"工具 {name} 执行异常: {type(e).__name__}: {e}")
        fail(f"内部错误: {type(e).__name__}: {e}", error_type="internal_error")

    structured = _result_to_structured(result)
    if structured is None:
        # 兜底：极少数情况下解析失败，退化为纯文本（不挂结构化，避免触发 outputSchema 校验失败）
        log(f"⚠️ 工具 {name} 的返回无法解析为 JSON，仅返回文本")
        return CallToolResult(content=[TextContent(type="text", text=result)], isError=False)

    log(f"工具 {name} 执行完成，返回 {len(result)} 字符")
    return CallToolResult(
        content=[TextContent(type="text", text=result)],
        structuredContent=structured,
        isError=False,
    )


async def dispatch_tool(name: str, arguments: dict) -> str:
    """按工具名分发，返回 JSON 文本"""
    result = ""

    if name == "search_tools":
        keyword = arguments.get("keyword", "")
        kind = arguments.get("kind")
        tools = registry.search_tools(keyword, kind)
        result = json.dumps({
            "success": True,
            "keyword": keyword,
            "count": len(tools),
            "tools": [
                {
                    "id": t["id"],
                    "name": t.get("displayName", t["name"]),
                    "kind": t.get("kind", "unknown"),
                    "description": t.get("description", "")[:100],
                    "status": t.get("status", "installed")
                }
                for t in tools
            ]
        }, ensure_ascii=False, indent=2)

    elif name == "list_tools":
        kind = arguments.get("kind")
        tools = registry.list_all_tools(kind)
        result = json.dumps({
            "success": True,
            "count": len(tools),
            "tools": [
                {
                    "id": t["id"],
                    "name": t.get("displayName", t["name"]),
                    "kind": t.get("kind", "unknown"),
                    "description": t.get("description", "")[:100],
                    "status": t.get("status", "installed")
                }
                for t in tools
            ]
        }, ensure_ascii=False, indent=2)

    elif name == "get_tool_info":
        tool_id = arguments.get("tool_id", "")
        tool = registry.get_tool(tool_id)

        if not tool:
            fail(f"未找到工具: {tool_id}", error_type="not_found", toolId=tool_id)

        kind = tool.get("kind", "unknown")
        result_data = {
            "success": True,
            "id": tool["id"],
            "name": tool.get("displayName", tool["name"]),
            "kind": kind,
            "status": tool.get("status", "unknown"),
            "description": tool.get("description", "")
        }

        if kind == "app":
            result_data["version"] = tool.get("version", "未知")
            result_data["tags"] = tool.get("tags", [])
            if tool.get("launch"):
                result_data["launch"] = tool["launch"]

        elif kind == "mcp":
            backend_res = backend_manager.get_backend_status(tool_id)
            if backend_res.get("success"):
                result_data["backend"] = backend_res.get("status", {})

            backend_tools = registry.list_backend_tools(tool_id)
            result_data["backendTools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", "")
                }
                for t in backend_tools
            ]

        result = json.dumps(result_data, ensure_ascii=False, indent=2, default=str)

    elif name == "launch_tool":
        tool_id = arguments.get("tool_id", "")
        res = ensure_success(registry.launch_tool(tool_id))
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "call_backend_tool":
        tool_id = arguments.get("tool_id", "")
        tool_name = arguments.get("tool_name", "")
        backend_args = arguments.get("arguments") or {}

        # A2：数据里的 destructive 之前只是个装饰字段，被无差别转发。
        # 这里把门禁落到转发前：命中判定的调用必须显式确认，否则默认拒绝
        # （A12 起判定下放到子工具粒度，见 _needs_destructive_confirm）。
        target_tool = registry.get_tool(tool_id)
        if _needs_destructive_confirm(target_tool, tool_name):
            if not arguments.get("allowDestructive"):
                fail(
                    f"目标工具 '{tool_id}' 的子工具 '{tool_name}' 被判定为 destructive，"
                    f"需显式传 allowDestructive=true 才会转发调用",
                    error_type="destructive_blocked",
                    toolId=tool_id,
                    toolName=tool_name,
                )
            log(f"⚠️ 已确认转发到 destructive 工具: {tool_id}/{tool_name}")

        log(f"路由调用: {tool_id}/{tool_name}")
        res = ensure_success(
            await backend_manager.call_backend_tool(tool_id, tool_name, backend_args)
        )
        # 与上面的拦截判定保持同源：免确认的子工具不该被标注成 destructive
        if _needs_destructive_confirm(target_tool, tool_name):
            res["warning"] = f"目标工具 '{tool_id}/{tool_name}' 被判定为 destructive"
        result = json.dumps(res, ensure_ascii=False, indent=2, default=str)

    elif name == "list_backend_tools":
        tool_id = arguments.get("tool_id", "")
        force_refresh = arguments.get("force_refresh", False)
        res = ensure_success(
            await backend_manager.list_backend_tools(tool_id, force_refresh=force_refresh)
        )
        result = json.dumps(res, ensure_ascii=False, indent=2, default=str)

    elif name == "start_backend":
        tool_id = arguments.get("tool_id", "")
        log(f"启动后端: {tool_id}")
        res = ensure_success(await backend_manager.start_backend(tool_id))
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "stop_backend":
        tool_id = arguments.get("tool_id", "")
        log(f"停止后端: {tool_id}")
        res = ensure_success(await backend_manager.stop_backend(tool_id))
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "get_backend_status":
        tool_id = arguments.get("tool_id")
        res = ensure_success(backend_manager.get_backend_status(tool_id))
        result = json.dumps(res, ensure_ascii=False, indent=2, default=str)

    elif name == "get_backend_logs":
        tool_id = arguments.get("tool_id", "")
        level = arguments.get("level")
        limit = arguments.get("limit", 50)
        res = ensure_success(backend_manager.get_backend_logs(tool_id, level, limit))
        result = json.dumps(res, ensure_ascii=False, indent=2, default=str)

    elif name == "set_backend_log_level":
        tool_id = arguments.get("tool_id", "")
        level = arguments.get("level", "")
        res = ensure_success(backend_manager.set_backend_log_level(tool_id, level))
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "sync_backend_tools":
        tool_id = arguments.get("tool_id", "")
        log(f"手动同步后端工具目录: {tool_id}")
        res = ensure_success(await backend_manager.sync_tools_catalog(tool_id))
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "sync_launcher_actions":
        tool_id = arguments.get("tool_id", "")
        log(f"手动同步启动器动作清单: {tool_id}")
        res = ensure_success(launcher_manager.sync(tool_id))
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "list_launcher_actions":
        tool_id = arguments.get("tool_id", "")
        keyword = arguments.get("keyword")
        limit = arguments.get("limit")
        # A13：默认读缓存（陈旧会自动重扫）；refresh=true 强制重扫
        refresh = bool(arguments.get("refresh"))
        # list_actions 返回的是数组而非结果字典，不能用 ensure_success 包；
        # 启动器自己的错误转成带 errorType 的失败，而不是落到内部异常兜底
        try:
            actions = launcher_manager.list_actions(
                tool_id, keyword=keyword, limit=limit, refresh=refresh
            )
        except LauncherError as exc:
            fail(str(exc), error_type="launcher_error", toolId=tool_id)
        # 带上 lastSynced / source：调用方要能判断这份清单有多旧、是否完整，
        # 否则「自动刷新」只是把不透明变得更不透明。
        catalog = store.load_launcher_catalog(tool_id) or {}
        result = json.dumps(
            {
                "success": True,
                "toolId": tool_id,
                "count": len(actions),
                "lastSynced": catalog.get("lastSynced"),
                "source": catalog.get("source"),
                "actions": actions,
            },
            ensure_ascii=False,
            indent=2,
        )

    elif name == "run_launcher_action":
        tool_id = arguments.get("tool_id", "")
        target_tool = registry.get_tool(tool_id)
        # 执行动作是真的会跑起来（Quicker 动作可以是任何操作），
        # 与 call_backend_tool 一致：条目标了 destructive 就要显式确认。
        if target_tool and target_tool.get("destructive"):
            if not arguments.get("allowDestructive"):
                fail(
                    f"启动器 '{tool_id}' 被标记为 destructive，"
                    f"需显式传 allowDestructive=true 才会执行动作",
                    error_type="destructive_blocked",
                    toolId=tool_id,
                )
            log(f"⚠️ 已确认执行 destructive 启动器动作: {tool_id}")
        res = ensure_success(
            await launcher_manager.run_action(
                tool_id,
                action_id=arguments.get("action_id"),
                action_name=arguments.get("action_name"),
                param=arguments.get("param"),
                wait=arguments.get("wait"),
            )
        )
        result = json.dumps(res, ensure_ascii=False, indent=2)

    else:
        fail(f"未知工具: {name}", error_type="unknown_tool", toolName=name)

    return result


# ============ Resources（资源） ============


@app.list_resources()
async def list_resources() -> list[Resource]:
    """列出所有可用资源"""
    log("收到 list_resources 请求")
    return [
        Resource(
            uri="awam://mcp-servers",
            name="MCP 服务器列表",
            description="所有已接入的 MCP 服务器",
            mimeType="application/json"
        ),
        Resource(
            uri="awam://tools",
            name="工具列表",
            description="所有已注册的工具",
            mimeType="application/json"
        ),
        Resource(
            uri="awam://backends/status",
            name="后端状态",
            description="所有后端 MCP Server 的运行状态",
            mimeType="application/json"
        ),
    ]


@app.read_resource()
async def read_resource(uri: str):
    """读取资源内容

    MCP 规范要求返回 str / bytes / Iterable[ReadResourceContents]，
    而不是 TextContent —— SDK 会读取每个条目的 .content 与 .mime_type 字段，
    返回 TextContent 会因为没有 .content 属性而报 AttributeError。

    另外 `req.params.uri` 是 pydantic 的 `AnyUrl`，**不是 str**：
    `AnyUrl("awam://tools") == "awam://tools"` 恒为 False（不会隐式比较），
    直接拿它和字符串比会导致三个资源全部落到"未知资源"分支。必须先 str() 归一化。
    """
    uri = str(uri)
    log(f"收到 read_resource 请求: {uri}")

    if uri == "awam://mcp-servers":
        payload = registry.list_all_tools(kind="mcp")

    elif uri == "awam://tools":
        payload = registry.list_all_tools()

    elif uri == "awam://backends/status":
        payload = backend_manager.get_backend_status()

    else:
        fail(f"未知资源: {uri}", error_type="unknown_resource", uri=str(uri))

    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    if ReadResourceContents is None:  # mcp <1.9，退回已废弃的 str 返回形式
        return text
    return [ReadResourceContents(content=text, mime_type="application/json")]


# ============ Prompts（提示模板） ============

@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    """列出所有可用提示模板"""
    log("收到 list_prompts 请求")
    return [
        Prompt(
            name="getting_started",
            description="awam-tool-manager 快速入门指南"
        ),
        Prompt(
            name="how_to_search",
            description="如何搜索和查找工具"
        ),
        Prompt(
            name="how_to_call_backend",
            description="如何调用后端 MCP 工具"
        ),
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None) -> list[PromptMessage]:
    """获取提示模板内容"""
    log(f"收到 get_prompt 请求: {name}")
    arguments = arguments or {}

    if name == "getting_started":
        text = """# awam-tool-manager 快速入门

awam-tool-manager 是一个统一的工具管理器，聚合各种 MCP 工具和应用。

## 核心能力

1. **搜索工具**：用 `search_tools` 按关键词搜索已注册的工具
2. **查看详情**：用 `get_tool_info` 查看工具的详细信息
3. **列出工具**：用 `list_tools` 查看所有已注册的工具（可按类型筛选）
4. **调用后端**：用 `call_backend_tool` 路由调用后端 MCP 的工具
5. **管理后端**：用 `start_backend` / `stop_backend` / `get_backend_status` 管理后端连接

## 快速流程

1. 先 `list_tools` 看看有哪些工具
2. 然后 `list_backend_tools` 看看某个 MCP 工具下有什么子工具
3. 最后 `call_backend_tool` 调用具体工具
"""

    elif name == "how_to_search":
        text = """# 如何搜索工具

## 基本搜索

调用 `search_tools`，传入关键词：
- 关键词会匹配工具的名称、显示名、描述、标签
- 可以用 `kind` 参数按类型筛选（app = 普通应用，mcp = MCP 工具）

## 列出所有工具

调用 `list_tools`，可按同样的 `kind` 筛选。

## 查看工具详情

调用 `get_tool_info`，传入工具 ID（即 `data/tools/` 下的文件名去掉 .json）。

## 示例

1. 搜索"文件"相关工具：`search_tools(keyword="文件")`
2. 只看 MCP 类工具：`list_tools(kind="mcp")`
3. 查看某个工具详情：`get_tool_info(tool_id="everything")`
"""

    elif name == "how_to_call_backend":
        text = """# 如何调用后端 MCP 工具

只有 `kind=mcp` 的工具才有后端连接，这里以 `everything-search-mcp` 为例。

## 步骤

1. **查看后端状态**：`get_backend_status(tool_id="everything-search-mcp")`
2. **如果未运行，先启动**：`start_backend(tool_id="everything-search-mcp")`
3. **列出后端工具**：`list_backend_tools(tool_id="everything-search-mcp")`
   - 这会返回每个子工具的名称、描述、参数 schema
4. **调用工具**：`call_backend_tool(tool_id="everything-search-mcp", tool_name="search_files", arguments={"query": "测试"})`

## 注意事项

- 全部后端工具统一使用 `tool_id` 指定目标工具，不存在 `launcher_id` 参数
- 子工具名必须和 `list_backend_tools` 返回的一致（注意大小写）
- 参数必须匹配 inputSchema 的定义，必填参数不能少
- 如果连接失败，先 `get_backend_logs` 看看日志
- 可以用 `set_backend_log_level` 调整日志级别

## 错误处理

调用失败时本服务会返回 MCP 协议层的错误（isError），文本部分是 JSON：

- `connection_error`：后端未运行，用 `start_backend` 启动
- `runtime_error`：参数错误或工具执行失败，检查参数
- `config_error`：工具缺少 connection 配置，或 kind 不是 mcp
- `not_found`：工具 ID 不存在
- `unknown_tool`：工具名不存在
"""

    else:
        fail(f"未知提示模板: {name}", error_type="unknown_prompt", promptName=name)

    messages = [PromptMessage(role="user", content=TextContent(type="text", text=text))]
    if GetPromptResult is None:  # 老版本 SDK 接受裸列表
        return messages
    return GetPromptResult(description=f"{name} 提示模板", messages=messages)


async def _prewarm_backends():
    """启动预热：补 PATH + 后台检测各后端依赖，不阻塞协议服务就绪

    依赖自装（BackendClient.ensure_installed）保证「start_backend 时缺
    什么装什么」，但安装是重操作（npm i -g 可达数十秒），第一次调用就要
    现场等。这里在服务启动时后台预热一遍，把安装成本提前到启动阶段。

    预热前先做 PATH 自愈（ensure_npm_bin_on_path）：上游网关可能用裁剪过
    的环境启动本服务（实测 toolport 传入的 PATH 不含 npm 全局 bin），不补
    的话 which 会把已装好的包误判成缺失，白跑一次数十秒的安装。

    只检不启、异常全吞：预热失败不能拖垮服务本体——最坏情况退化为
    「首次 start_backend 时按需处理」，与没有预热时完全一致。

    **P0 之后预热默认不执行安装**：install.command 来自进版本库的数据文件，
    旧逻辑让它在服务启动时被无条件执行（无门禁）。现在默认只做检测，
    把「谁缺依赖、缺什么」如实写进启动日志；要恢复启动时自动安装，由
    部署者显式设 `AWAM_AUTO_INSTALL=1`（或逐条目配 `install.auto=true`）。
    """
    try:
        await ensure_npm_bin_on_path()
        result = await backend_manager.ensure_all_installed()
        if result.get("installed"):
            log(
                f"启动预热：已自动安装 {result['installed']} 个后端依赖"
                f"（共 {result['total']} 个 MCP 工具，其余已就绪）"
            )
        elif result.get("blocked"):
            missing = [
                d["tool_id"]
                for d in result.get("details", [])
                if d.get("installBlocked")
            ]
            log(
                f"启动预热：{len(missing)} 个 MCP 工具依赖缺失但未授权自动安装"
                f"（{', '.join(missing)}）——本次不执行任何安装命令；"
                f"如需启动时自动安装，设环境变量 {BackendClient.INSTALL_ENV_VAR}=1"
            )
        elif result.get("total"):
            log(f"启动预热：{result['total']} 个 MCP 工具依赖均已就绪")
        else:
            log("启动预热：没有需要处理的 MCP 工具")
    except Exception as e:
        log(f"启动预热失败（不影响服务，将退化为首次调用时按需安装）: {type(e).__name__}: {e}")


async def _restore_backends():
    """状态恢复：把上次明确启动过的后端重新拉起来（A10）

    背景：toolport 网关周期性整池回收它拉起的 server 进程（实测约每几分钟
    一批）。每次回收后新 server 世代内存状态清零，所有后端回到 stopped——
    用户视角就是「启动后保不住，每次都得重新初始化」。start_backend 成功时
    会把启动意图写进 backend_desired_state.json，这里在每个新世代启动时
    据此恢复。

    与预热的分工：预热只装不启（所有 MCP 工具）；恢复只启 desired=running
    的（用户明确要过的）。恢复失败同样不阻断服务——最坏是用户下次调用时
    看到 stopped，再手启一次（意图仍在，下代还会尝试）。
    """
    try:
        result = await backend_manager.restore_desired_backends()
        if result:
            ok = sum(1 for r in result.values() if r.get("success"))
            log(f"状态恢复：{ok}/{len(result)} 个 desired 后端已重新拉起")
        else:
            log("状态恢复：没有需要恢复的后端")
    except Exception as e:
        log(f"状态恢复失败（不影响服务）: {type(e).__name__}: {e}")


async def main():
    """启动 MCP Server"""
    log("=" * 50)
    log("awam-tool-manager MCP Server 启动中...")
    log(f"数据目录: {DATA_DIR}")
    log(f"运行时目录: {RUNTIME_DIR}")

    # 旧版本把运行时数据写在仓库里（data/backend-tools、data/launcher-actions、
    # 根目录的 backend_desired_state.json），这里搬到运行时目录。只搬目标不存在的，
    # 失败不阻断启动。
    moved = migrate_legacy_runtime(DATA_DIR, RUNTIME_DIR, log=log)
    if moved:
        log(f"已迁移旧运行时数据到 {RUNTIME_DIR}: {', '.join(moved)}")

    all_tools = registry.list_all_tools()
    mcp_tools = [t for t in all_tools if t.get("kind") == "mcp"]
    log(f"已加载 {len(all_tools)} 个工具: {', '.join([t['id'] for t in all_tools])}")
    log(f"其中 MCP 类工具: {len(mcp_tools)} 个")
    log(
        "条目校验: "
        + ("jsonschema 完整校验" if schema_backend_available() else "基础校验（未安装 jsonschema）")
    )
    log("等待客户端连接...")
    log("=" * 50)

    # 后台预热后端依赖：安装是重操作，不能阻塞协议服务就绪
    prewarm_task = asyncio.create_task(_prewarm_backends())
    # 后台恢复上次启动过的后端：网关回收 server 进程后状态自愈（A10）
    restore_task = asyncio.create_task(_restore_backends())

    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options()
            )
    finally:
        for task in (prewarm_task, restore_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log("服务退出，关闭所有后端连接...")
        await backend_manager.close_all()
        log("清理完成")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log(f"服务异常退出: {e}")
        raise
