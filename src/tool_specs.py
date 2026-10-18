"""对外 MCP 工具的声明式注册表（P1-04，SSOT）。

搬迁前，同一个工具名在 server.py 里有四个平行维护点：`TOOL_ANNOTATION_HINTS`
注解表、`list_tools()` 里的 `Tool(...)` 字面量、`dispatch_tool` 的 if/elif
分支、分支内的 `json.dumps`，四者无任何机制保证一致。本模块把前三者收敛为
**一条 `ServerToolSpec` 记录**（annotations 与工具同条记录，与工具名同生共死），
序列化收敛到 server.dispatch_tool 的一处。

标准动作：新增一个对外工具 = 在本文件加「1 条 ServerToolSpec + 1 个
async def _handle_xxx」，`build_mcp_tools()` / `dispatch_tool` /
`conftest.EXPECTED_TOOL_NAMES` 的守卫测试全部自动跟随。

与 `schema.py` 的分工：schema.py 管辖**数据条目**（`data/tools/*.json`），
本模块管辖**对外 MCP 工具**（协议层暴露的 15 个 `tools/call` 入口）。
两者字段名有重叠但语义不同，互不 import、互不引用。
"""

from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from mcp.types import Tool

try:  # Tool annotations 同样是 mcp>=1.9 才引入的能力
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    ToolAnnotations = None

from errors import ErrorType, ToolError, fail, ensure_success  # noqa: F401（handler 侧经本模块转手）
from app_launcher import launch_app
from launcher_client import LauncherError
from policy import decide_destructive
from tool_help import (
    backend_summary,
    entry_help,
    launcher_summary,
    resolve_help_command,
    run_help_command,
)

if TYPE_CHECKING:  # 运行期零耦合：handler 只从 ctx 取 manager，不 import 它们
    from backend_client import BackendManager
    from launcher_client import LauncherManager
    from registry import ToolRegistry
    from store import JsonStore


@dataclass(frozen=True)
class ToolContext:
    """handler 的依赖注入容器。

    handlers **不 import** 任何 manager，只从 `ctx` 取——这是让「handler 可搬走」
    且「不产生循环 import」的关键。`frozen=True`：容器在 server import 期构造
    一次，运行期不可变；测试 monkeypatch 的是 manager 对象的属性，ctx 持有的
    是同一引用，patch 照样生效。
    """

    registry: "ToolRegistry"
    backend_manager: "BackendManager"
    launcher_manager: "LauncherManager"
    store: "JsonStore"
    log: Callable[[str], None]


@dataclass(frozen=True)
class ServerToolSpec:
    """一条对外工具声明。

    字段语义：
    - `annotations`：`dict | None`。**`{}` 与 `None` 都表示不挂注解**——逐一
      复现搬迁前现状：`set_backend_log_level` 的注解就是 `{}`，而原
      `list_tools()` 的 `if hints:` 对空 dict 取假从而不挂。
    - `output_schema`：`dict | None`，`None` → 回落 `SHARED_OUTPUT_SCHEMA`。
      为将来某个工具需要差异化输出声明时预留。
    - `handler`：`async (arguments: dict, ctx: ToolContext) -> Any`，返回**载荷
      对象**（dict），不返回 JSON 文本——序列化统一由 `server.dispatch_tool`
      负责。迁移期（M03 分批搬迁）未搬迁的条目为 `None`，由 dispatch 的脚手架
      回落到旧 if/elif；`validate_specs()` 在最终态强制非空。
    """

    name: str
    description: str
    input_schema: dict
    annotations: dict | None = None
    output_schema: dict | None = None
    handler: Callable[..., Any] | None = None


# 所有工具的成功输出都带顶层 `success`（bool），其余字段随工具而异（如 list 类
# 工具的 `tools` 数组、backend 类工具的 `result` 自由结构）。用一张宽松但准确的
# outputSchema 声明这一不变式：既让客户端能可靠按 `success` 分支，又不会因
# 工具自有的自由字段触发 mcp 1.30 的 outputSchema 校验失败（additionalProperties=true）。
# 注意：只要在 list_tools 里给工具挂了 outputSchema，mcp 就要求每次调用都返回
# structuredContent，否则会返回 "Output validation error"，因此 call_tool 必须为
# 每个成功结果附带 structuredContent（见 server.py 的 _result_to_structured）。
SHARED_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {"success": {"type": "boolean"}},
    "required": ["success"],
    "additionalProperties": True,
}

# ---- handlers（M03 分批搬迁）----
# 分支体从 server.dispatch_tool 逐字搬运，只做五类机械替换：
#   registry. → ctx.registry.、backend_manager. → ctx.backend_manager.、
#   launcher_manager. → ctx.launcher_manager.、store. → ctx.store.、log( → ctx.log(
# handler 返回**载荷对象**（dict）而非 JSON 文本——序列化统一由
# server.dispatch_tool 负责。本模块禁止出现裸 manager 名（R1：漏做替换
# 必报 NameError，第一批测试即红）。


def _lookup_action_type(
    ctx: ToolContext, tool_id: str, action_id: str | None, action_name: str | None
) -> str:
    """从动作清单缓存里取动作的 `type`（id 优先，其次 name）；找不到返回空串。

    `type` 只用于 `destructiveActions` 的 glob 匹配；动作清单是 partial 时
    （只有 ID、没有名称/类型）返回空串，匹配自然落空——附录 A 的既定语义：
    宁可漏拦（未命中）也不误放，部署者需按实际清单核对模式。
    缓存缺失 / 损坏不应影响门禁主流程，吞掉异常按空处理。
    """
    try:
        catalog = ctx.store.load_launcher_catalog(tool_id) or {}
    except Exception:  # noqa: BLE001 —— 缓存问题不阻断门禁
        return ""
    for action in catalog.get("actions") or []:
        if action_id and action.get("id") == action_id:
            return action.get("type") or ""
        if action_name and action.get("name") == action_name:
            return action.get("type") or ""
    return ""


async def _handle_search_tools(arguments: dict, ctx: ToolContext) -> dict:
    keyword = arguments.get("keyword", "")
    kind = arguments.get("kind")
    tools = ctx.registry.search_tools(
        keyword,
        kind,
        arguments.get("status"),
        arguments.get("publisher"),
    )
    return {
        "success": True,
        "keyword": keyword,
        "count": len(tools),
        "tools": [
            {
                "id": t["id"],
                "name": t.get("displayName", t["name"]),
                "kind": t.get("kind", "unknown"),
                "description": t.get("description", "")[:100],
                "status": t.get("status", "installed"),
                # 可筛选的维度必须同时可见：只让按 publisher 筛却不返回它，
                # 调用方就只能猜发布者写法
                "publisher": t.get("publisher", "")
            }
            for t in tools
        ]
    }


async def _handle_list_tools(arguments: dict, ctx: ToolContext) -> dict:
    kind = arguments.get("kind")
    tools = ctx.registry.list_all_tools(
        kind,
        arguments.get("status"),
        arguments.get("publisher"),
    )
    return {
        "success": True,
        "count": len(tools),
        "tools": [
            {
                "id": t["id"],
                "name": t.get("displayName", t["name"]),
                "kind": t.get("kind", "unknown"),
                "description": t.get("description", "")[:100],
                "status": t.get("status", "installed"),
                "publisher": t.get("publisher", "")
            }
            for t in tools
        ]
    }


async def _handle_get_tool_info(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    tool = ctx.registry.get_tool(tool_id)

    if not tool:
        fail(f"未找到工具: {tool_id}", error_type=ErrorType.NOT_FOUND, toolId=tool_id)

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
        backend_res = ctx.backend_manager.get_backend_status(tool_id)
        if backend_res.get("success"):
            result_data["backend"] = backend_res.get("status", {})

        backend_tools = ctx.registry.list_backend_tools(tool_id)
        result_data["backendTools"] = [
            {
                "name": t["name"],
                "description": t.get("description", "")
            }
            for t in backend_tools
        ]

    return result_data


async def _handle_launch_tool(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    res = ensure_success(ctx.registry.launch_tool(tool_id))
    if not arguments.get("execute"):
        # 默认路径逐字保持旧行为：只返回命令，不 spawn（e2e 场景②钉住的
        # 硬契约）。execute 缺省/False 时本函数与改造前完全等价。
        return res
    # 执行路径：调用方显式 execute=true 才启动。条目级 destructive 门禁走
    # policy（与 call_backend_tool / run_launcher_action 同一判定入口）；
    # install 门禁（ARC-P0-01）防的是「无意图执行」，本路径的意图就是
    # 这次显式调用本身。
    target_tool = ctx.registry.get_tool(tool_id)
    decision = decide_destructive("app", target_tool, {}, arguments)
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行启动: {tool_id}")

    ctx.log(f"执行启动: {tool_id}")
    exec_res = await launch_app(
        res["launch"], tool_id, res.get("toolName", ""), arguments.get("wait")
    )
    res.update(exec_res)
    # 执行失败（退出码非 0 等）与其它工具的失败载荷同路：转协议层 isError
    return ensure_success(res)


async def _handle_get_tool_help(arguments: dict, ctx: ToolContext) -> dict:
    """取工具的帮助信息。

    默认路径**零副作用**：文档链接 + 能力清单（mcp 子工具 / launcher 动作）+
    将要执行的帮助命令预览。显式 `run=true` 且 `kind=app` 才真的 spawn 捕获输出
    ——与 `launch_tool` 的「默认只返回」同一取舍。
    """
    tool_id = arguments.get("tool_id", "")
    tool = ctx.registry.get_tool(tool_id)
    if not tool:
        fail(f"未找到工具: {tool_id}", error_type=ErrorType.NOT_FOUND, toolId=tool_id)

    kind = tool.get("kind", "unknown")
    payload = {
        "success": True,
        "toolId": tool_id,
        "toolName": tool.get("displayName", tool.get("name", "")),
        "kind": kind,
    }
    payload.update(entry_help(tool))

    if kind == "mcp":
        backend_tools = ctx.registry.list_backend_tools(tool_id)
        payload["backendTools"] = backend_summary(backend_tools)
        payload["backendToolCount"] = len(backend_tools)
        if not backend_tools:
            payload["note"] = (
                "后端子工具缓存为空：先 start_backend 或 sync_backend_tools 再取帮助"
            )
    elif kind == "launcher":
        catalog = ctx.store.load_launcher_catalog(tool_id) or {}
        actions = catalog.get("actions") or []
        payload["actions"] = launcher_summary(actions)
        payload["actionCount"] = len(actions)
        payload["lastSynced"] = catalog.get("lastSynced")
        payload["source"] = catalog.get("source")
        if not actions:
            payload["note"] = "动作清单缓存为空：先 sync_launcher_actions 再取帮助"

    if not arguments.get("run"):
        return payload

    if kind != "app":
        # 缓存里已有的就是它的全部「帮助」，没有可执行的 --help 这回事
        payload["note"] = f"kind={kind} 没有可执行的帮助命令，已返回数据层来源"
        return payload

    resolved = resolve_help_command(tool, arguments.get("timeout"))
    if not resolved:
        fail(
            f"工具 '{tool_id}' 未配置帮助来源（launch.command / help.command 均缺失）",
            error_type=ErrorType.CONFIG_ERROR,
            toolId=tool_id,
        )

    # 跑 --help 同样是一次 spawn：条目 destructive → 需 allowDestructive
    # （与 launch_tool execute 走同一判定入口）。这不是过度设防——图形程序
    # 不认 --help 时会把它当普通启动参数跑起来。
    decision = decide_destructive("app", tool, {"action": "get_tool_help"}, arguments)
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行帮助命令: {tool_id}")

    ctx.log(f"执行帮助命令: {tool_id}")
    exec_res = await run_help_command(resolved)
    payload.update(exec_res)
    # 帮助命令自身失败（命令缺失 / 无输出 / 超时）照旧转协议层 isError
    return ensure_success(payload)


async def _handle_get_backend_status(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id")
    res = ensure_success(ctx.backend_manager.get_backend_status(tool_id))
    return res


async def _handle_call_backend_tool(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    tool_name = arguments.get("tool_name", "")
    backend_args = arguments.get("arguments") or {}

    # A2/A12 → ARC-P1-05 Phase 1：判定收敛到 policy.decide_destructive
    # （判定表与审计见 src/policy.py）。mcp 未配置 destructiveTools 时整后端拦，
    # 与接入前逐字等价；判定只算一次，转发后的 warning 复用同一结果。
    target_tool = ctx.registry.get_tool(tool_id)
    decision = decide_destructive(
        "mcp", target_tool, {"tool_name": tool_name}, arguments
    )
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
            toolName=tool_name,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认转发到 destructive 工具: {tool_id}/{tool_name}")

    ctx.log(f"路由调用: {tool_id}/{tool_name}")
    res = ensure_success(
        await ctx.backend_manager.call_backend_tool(tool_id, tool_name, backend_args)
    )
    # 与上面的拦截判定保持同源：免确认的子工具不该被标注成 destructive
    if decision["required_confirm"]:
        res["warning"] = f"目标工具 '{tool_id}/{tool_name}' 被判定为 destructive"
    return res


async def _handle_list_backend_tools(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    force_refresh = arguments.get("force_refresh", False)
    res = ensure_success(
        await ctx.backend_manager.list_backend_tools(tool_id, force_refresh=force_refresh)
    )
    return res


async def _handle_start_backend(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"启动后端: {tool_id}")
    res = ensure_success(await ctx.backend_manager.start_backend(tool_id))
    return res


async def _handle_stop_backend(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"停止后端: {tool_id}")
    res = ensure_success(await ctx.backend_manager.stop_backend(tool_id))
    return res


async def _handle_get_backend_logs(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    level = arguments.get("level")
    limit = arguments.get("limit", 50)
    res = ensure_success(ctx.backend_manager.get_backend_logs(tool_id, level, limit))
    return res


async def _handle_set_backend_log_level(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    level = arguments.get("level", "")
    res = ensure_success(ctx.backend_manager.set_backend_log_level(tool_id, level))
    return res


async def _handle_sync_backend_tools(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"手动同步后端工具目录: {tool_id}")
    res = ensure_success(await ctx.backend_manager.sync_tools_catalog(tool_id))
    return res


async def _handle_sync_launcher_actions(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"手动同步启动器动作清单: {tool_id}")
    res = ensure_success(ctx.launcher_manager.sync(tool_id))
    return res


async def _handle_list_launcher_actions(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    keyword = arguments.get("keyword")
    limit = arguments.get("limit")
    # A13：默认读缓存（陈旧会自动重扫）；refresh=true 强制重扫
    refresh = bool(arguments.get("refresh"))
    # list_actions 返回的是数组而非结果字典，不能用 ensure_success 包；
    # 启动器自己的错误转成带 errorType 的失败，而不是落到内部异常兜底
    try:
        actions = ctx.launcher_manager.list_actions(
            tool_id, keyword=keyword, limit=limit, refresh=refresh
        )
    except LauncherError as exc:
        fail(str(exc), error_type=ErrorType.LAUNCHER_ERROR, toolId=tool_id)
    # 带上 lastSynced / source：调用方要能判断这份清单有多旧、是否完整，
    # 否则「自动刷新」只是把不透明变得更不透明。
    catalog = ctx.store.load_launcher_catalog(tool_id) or {}
    return {
        "success": True,
        "toolId": tool_id,
        "count": len(actions),
        "lastSynced": catalog.get("lastSynced"),
        "source": catalog.get("source"),
        "actions": actions,
    }


async def _handle_run_launcher_action(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    target_tool = ctx.registry.get_tool(tool_id)
    # 执行动作是真的会跑起来（Quicker 动作可以是任何操作）：
    # 条目未标 destructive → 放行；配了 destructiveActions → 只拦命中动作
    # （glob）；未配 → 回退条目级全拦（与接入前逐字等价）。
    action_id = arguments.get("action_id")
    action_name = arguments.get("action_name")
    sub_op = {"action_id": action_id, "action_name": action_name, "action_type": ""}
    # 动作 type 只服务 glob 匹配；未配置 destructiveActions 时不引入额外 IO
    if target_tool and target_tool.get("destructiveActions") is not None:
        sub_op["action_type"] = _lookup_action_type(ctx, tool_id, action_id, action_name)
    decision = decide_destructive("launcher", target_tool, sub_op, arguments)
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行 destructive 启动器动作: {tool_id}")
    res = ensure_success(
        await ctx.launcher_manager.run_action(
            tool_id,
            action_id=action_id,
            action_name=action_name,
            param=arguments.get("param"),
            wait=arguments.get("wait"),
        )
    )
    return res


# 顺序即 tools/list 对外可见顺序（golden 按列表逐位锁定，保序即保行为）。
SERVER_TOOL_SPECS: tuple[ServerToolSpec, ...] = (
    ServerToolSpec(
        name="search_tools",
        handler=_handle_search_tools,
        description="搜索已注册的工具，按关键词匹配名称、描述、标签，可按类型、安装状态、发布者筛选",
        input_schema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词"
                },
                "kind": {
                    "type": "string",
                    # NE-5：driver 历史漂移已于 2026-10-17 修正为 launcher（§3.1 #20；golden 同步重新基线）
                    "description": "可选，按工具类型筛选（如 app、mcp、launcher）"
                },
                "status": {
                    "type": "string",
                    "enum": ["installed", "not_installed", "unknown"],
                    "description": "可选，按安装状态精确筛选（条目未写 status 时按 installed 计）"
                },
                "publisher": {
                    "type": "string",
                    "description": "可选，按发布者筛选（子串匹配、大小写不敏感；条目没写 publisher 一律不命中）"
                }
            },
            "required": ["keyword"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="list_tools",
        handler=_handle_list_tools,
        description="列出所有已注册的工具，可按类型、安装状态、发布者筛选",
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    # NE-5：driver 历史漂移已于 2026-10-17 修正为 launcher（§3.1 #20；golden 同步重新基线）
                    "description": "可选，按工具类型筛选（如 app、mcp、launcher）"
                },
                "status": {
                    "type": "string",
                    "enum": ["installed", "not_installed", "unknown"],
                    "description": "可选，按安装状态精确筛选（条目未写 status 时按 installed 计）"
                },
                "publisher": {
                    "type": "string",
                    "description": "可选，按发布者筛选（子串匹配、大小写不敏感；条目没写 publisher 一律不命中）"
                }
            }
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="get_tool_info",
        handler=_handle_get_tool_info,
        description="查看工具的详细信息",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID，如 everything、everything-search-mcp"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="launch_tool",
        handler=_handle_launch_tool,
        description="取指定工具的启动命令；传 execute=true 可真正执行启动（默认只返回命令，不执行）",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID"
                },
                "execute": {
                    "type": "boolean",
                    "description": "是否真正执行启动（默认 false：只返回命令、不 spawn 进程）"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，执行后等待早退的观察窗口秒数（默认 5，封顶 30）；窗口内退出即报退出码，超时仍运行则报 running=true"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "工具被标记为 destructive 时，执行启动需显式传 true"
                }
            },
            "required": ["tool_id"]
        },
        # execute=true 会真的 spawn 进程，与 run_launcher_action 同款只标 openWorld
        annotations={"openWorldHint": True},
    ),
    ServerToolSpec(
        name="call_backend_tool",
        handler=_handle_call_backend_tool,
        description="路由调用：转发请求到指定 MCP 工具的后端 Server 执行具体操作",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID（如 everything-search-mcp）"
                },
                "tool_name": {
                    "type": "string",
                    "description": "要调用的后端工具名"
                },
                "arguments": {
                    "type": "object",
                    "description": "调用参数"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "目标工具被标记为 destructive 时，需显式传 true 才会转发"
                }
            },
            "required": ["tool_id", "tool_name"]
        },
        # 转发到后端 MCP，属于"与外部世界交互"但静态无法判定副作用
        annotations={"openWorldHint": True},
    ),
    ServerToolSpec(
        name="list_backend_tools",
        handler=_handle_list_backend_tools,
        description="列出指定 MCP 工具的后端提供的所有子工具（含完整参数 schema）",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                },
                "force_refresh": {
                    "type": "boolean",
                    "description": "是否强制刷新缓存"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="start_backend",
        handler=_handle_start_backend,
        description="启动指定的后端 MCP Server 并建立连接",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"idempotentHint": True},
    ),
    ServerToolSpec(
        name="stop_backend",
        handler=_handle_stop_backend,
        description="停止指定的后端 MCP Server",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"idempotentHint": True},
    ),
    ServerToolSpec(
        name="get_backend_status",
        handler=_handle_get_backend_status,
        description="获取后端 MCP Server 的运行状态",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID，不填则返回所有后端状态"
                }
            }
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="get_backend_logs",
        handler=_handle_get_backend_logs,
        description="获取后端 MCP Server 的运行日志",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                },
                "level": {
                    "type": "string",
                    "description": "日志级别过滤（debug/info/warning/error）"
                },
                "limit": {
                    "type": "number",
                    "description": "返回条数上限，默认 50"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="set_backend_log_level",
        handler=_handle_set_backend_log_level,
        description="设置后端 MCP Server 的日志级别",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                },
                "level": {
                    "type": "string",
                    "description": "日志级别（debug/info/warning/error）"
                }
            },
            "required": ["tool_id", "level"]
        },
        # 搬迁前即不挂注解（TOOL_ANNOTATION_HINTS 里是 {}），保留原样
        annotations={},
    ),
    ServerToolSpec(
        name="sync_backend_tools",
        handler=_handle_sync_backend_tools,
        description="手动同步后端 MCP 的工具目录（自动获取所有工具的参数 schema 并保存）",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"idempotentHint": True},
    ),
    ServerToolSpec(
        name="sync_launcher_actions",
        handler=_handle_sync_launcher_actions,
        description="手动同步启动器的动作清单（从启动器本地数据扫描，Quicker 的动作名依赖其缓存，可能只拿到 ID）",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "启动器工具 ID"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"idempotentHint": True},
    ),
    ServerToolSpec(
        name="list_launcher_actions",
        handler=_handle_list_launcher_actions,
        description="列出/搜索启动器（如 Quicker）的动作：按名称、描述、所属程序或 ID 关键词过滤",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "启动器工具 ID"
                },
                "keyword": {
                    "type": "string",
                    "description": "可选，按名称/描述/所属程序/ID 过滤"
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多返回多少条"
                },
                "refresh": {
                    "type": "boolean",
                    "description": "可选，强制重新扫描启动器本地数据（默认读缓存，缓存超过 5 分钟会自动重扫）"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="run_launcher_action",
        handler=_handle_run_launcher_action,
        description="调用启动器的动作（如执行一个 Quicker 动作），可传参并等待返回结果",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "启动器工具 ID"
                },
                "action_id": {
                    "type": "string",
                    "description": "动作 ID（推荐：稳定不变；二选一，与 action_name 至少给一个）"
                },
                "action_name": {
                    "type": "string",
                    "description": "动作名称（Quicker 要求名称不重复且不含空格）"
                },
                "param": {
                    "type": "string",
                    "description": "可选，传给动作的参数（Quicker 用 ? 分隔符，不支持换行）"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，最长等待秒数（默认取 launcher.timeout，缺省 20）"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "启动器被标记为 destructive 时，需显式传 true 才会执行"
                }
            },
            "required": ["tool_id"]
        },
        # 执行动作会真的跑起来，只标 openWorld
        annotations={"openWorldHint": True},
    ),
    ServerToolSpec(
        name="get_tool_help",
        handler=_handle_get_tool_help,
        description="取工具的帮助信息（文档链接 / 后端能力清单 / 启动器动作清单）；kind=app 传 run=true 可执行帮助命令并捕获输出",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID"
                },
                "run": {
                    "type": "boolean",
                    "description": "是否真的执行帮助命令（默认 false：只返回文档链接、能力清单与命令预览，不 spawn）"
                },
                "timeout": {
                    "type": "number",
                    "description": "可选，执行帮助命令的超时秒数（覆盖条目 help.timeout；默认 5，封顶 30）"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "工具被标记为 destructive 时，执行帮助命令需显式传 true"
                }
            },
            "required": ["tool_id"]
        },
        # 默认路径是只读取数，但 run=true 会 spawn 子进程 → 与 launch_tool 同款只标 openWorld
        annotations={"openWorldHint": True},
    ),
)

# ToolAnnotations 的合法键（MCP 协议 hints 全集）。validate_specs 用它拦
# 「注解键拼错 → 运行期才炸」。
_ANNOTATION_KEYS = frozenset(
    {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
)


def build_mcp_tools() -> list[Tool]:
    """由声明表统一构造 `Tool` 列表（annotations / outputSchema 在此挂载）。

    与搬迁前 `list_tools()` 的挂载逻辑逐行等价：
    - `if ToolAnnotations is not None and spec.annotations:` —— 可选导入的
      真值判断都原样保留（`{}` 取假，不挂注解）；
    - `tool.outputSchema = spec.output_schema or SHARED_OUTPUT_SCHEMA` ——
      本期 15 条全部回落共享 schema。
    """
    tools: list[Tool] = []
    for spec in SERVER_TOOL_SPECS:
        tool = Tool(
            name=spec.name,
            description=spec.description,
            inputSchema=spec.input_schema,
        )
        if ToolAnnotations is not None and spec.annotations:
            tool.annotations = ToolAnnotations(**spec.annotations)
        tool.outputSchema = spec.output_schema or SHARED_OUTPUT_SCHEMA
        tools.append(tool)
    return tools


def get_spec(name: str) -> ServerToolSpec | None:
    """按工具名查声明；未命中返回 None（分发层转 `unknown_tool`）。"""
    return _SPECS_BY_NAME.get(name)


def validate_specs() -> None:
    """import 期自检：spec 表自身损坏时快速失败，而不是带病运行。

    检查三类问题：
    - 重名：名字索引会静默丢掉一条，tools/list 少一个工具且无人察觉；
    - 空 handler：新增工具忘了写 handler，要到运行期才暴露；
    - annotations 键非法：`ToolAnnotations(**{...})` 拼错键运行期才炸。

    对合法输入零差异——这是本设计「把一致性从约定变成强制」的核心机制。
    """
    seen: set[str] = set()
    for spec in SERVER_TOOL_SPECS:
        if spec.name in seen:
            raise RuntimeError(f"tool_specs: 工具名重复: {spec.name}")
        seen.add(spec.name)
        if spec.handler is None:
            raise RuntimeError(f"tool_specs: {spec.name} 缺少 handler")
        if spec.annotations:
            unknown = set(spec.annotations) - _ANNOTATION_KEYS
            if unknown:
                raise RuntimeError(
                    f"tool_specs: {spec.name} 的 annotations 含非法键: "
                    f"{sorted(unknown)}"
                )


# 名字 → spec 索引（validate_specs 保证无重名，建索引才有意义）
_SPECS_BY_NAME: dict[str, ServerToolSpec] = {
    spec.name: spec for spec in SERVER_TOOL_SPECS
}

# import 期自检（NE-3）：spec 表自身损坏（重名 / 空 handler / 非法注解键）时
# 快速失败，而不是带病运行。对合法输入零差异；M03 迁移期曾容忍 handler=None
# （脚手架回落旧分支），脚手架删除后（M04）起强制完整。
validate_specs()
