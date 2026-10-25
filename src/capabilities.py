"""Provider / Capability / Runtime 路由层。

对外词汇收敛为三层：

- **Provider**：能力来源（`kind=app/mcp/launcher` 的工具条目）；
- **Capability**：可调用的具体操作（MCP 子工具 / 启动器动作 / 应用的 `launch`）；
- **Runtime**：只有 MCP Provider 才有的有状态进程。

本模块只做「解析目标 → 校验参数 → 门禁 → 调既有能力」，不复制后端实现。
`tool_specs.py` 的对外工具是这层的薄壳；单测可以直接调这里的函数。

刻意保留的边界：

- 能力清单**读盘优先**。MCP 的目录缓存来自上一次 sync，运行中的后端只在
  显式 `refresh=true` 或「按 ID 调能力但缓存里没有」时才会被问一次。
- 描述符里的 `risk` 只服务「默认是否要 execute」与调用方判断；真正的拦截
  仍走 `policy.decide_destructive`，两边不各写一套。
- 不记调用参数（与 usage 的取舍一致）；能力 ID 进统计走 `_usageCapability`。
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any

from app_launcher import launch_app
from errors import ErrorType, ensure_success, fail
from launcher_client import LauncherError
from policy import decide_destructive

EVERYTHING_TOOL_ID = "everything-search-mcp"
WINGET_TOOL_ID = "winget-mcp"
STEAM_TOOL_ID = "steam"

RUNTIME_RUNNING = "running"

PROVIDER_KINDS = ("app", "mcp", "launcher")

# 只读动词前缀：命中即认为该能力不产生副作用，call 默认 execute=true。
_READ_NAME_RE = re.compile(
    r"^(get|list|search|find|check|read|query|describe|inspect|status|show)[_-]",
    re.IGNORECASE,
)

# MCP 子工具 → 已有意图工具名。没有映射的能力只能走 call_capability。
_MCP_EXPOSED_AS: dict[str, dict[str, list[str]]] = {
    EVERYTHING_TOOL_ID: {
        "search_files": ["search_files"],
        "get_file_info": ["get_file_info"],
        "check_status": ["get_everything_status"],
    },
    WINGET_TOOL_ID: {
        "find-winget-packages": ["search_winget_packages"],
        "install-winget-package": ["install_winget_package"],
    },
}

_DOMAIN_TARGET_KIND = {
    "file": "path",
    "package": "package",
    "game": "game",
    "action": "action",
    "app": "app",
    "catalog": "catalog",
}

_MAX_LIMIT = 500


# ---------------------------------------------------------------- 基础设施


def _clamp_limit(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        fail(f"limit 必须是整数（收到: {value!r}）", error_type=ErrorType.INVALID_PARAMS)
    if number <= 0:
        fail("limit 必须是正整数", error_type=ErrorType.INVALID_PARAMS)
    return min(number, _MAX_LIMIT)


def provider_kind(tool: dict[str, Any] | None) -> str:
    kind = str((tool or {}).get("kind") or "")
    return kind if kind in PROVIDER_KINDS else ""


def _require_tool(ctx: Any, tool_id: Any) -> dict[str, Any]:
    tool_id = str(tool_id or "").strip()
    if not tool_id:
        fail("tool_id 不能为空", error_type=ErrorType.INVALID_PARAMS, toolId=tool_id)
    tool = ctx.registry.get_tool(tool_id)
    if not tool:
        fail(f"未找到工具: {tool_id}", error_type=ErrorType.NOT_FOUND, toolId=tool_id)
    return tool


def _provider_summary(tool: dict[str, Any], kind: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "toolId": tool.get("id"),
        "kind": kind,
        "name": tool.get("displayName") or tool.get("name") or "",
    }
    adapter = (tool.get("launcher") or {}).get("type")
    if adapter:
        summary["adapter"] = adapter
    return summary


def _runtime_summary(ctx: Any, tool_id: str) -> dict[str, Any]:
    res = ctx.backend_manager.get_backend_status(tool_id)
    if not res.get("success"):
        return {
            "available": False,
            "status": None,
            "errorType": res.get("errorType"),
            "error": res.get("error") or "",
        }
    status = res.get("status") or {}
    return {
        "available": True,
        "status": status.get("status"),
        "uptimeSeconds": status.get("uptime_seconds"),
        "toolsCount": status.get("tools_count"),
        "lastError": status.get("last_error"),
    }


# ---------------------------------------------------------------- 运行时


async def ensure_runtime(ctx: Any, tool_id: str) -> dict[str, Any]:
    """MCP 运行时未运行就拉起（`start_backend` 自身幂等）。"""
    tool = _require_tool(ctx, tool_id)
    kind = provider_kind(tool)
    if kind != "mcp":
        fail(
            f"工具 '{tool_id}' 的 kind={kind or 'unknown'} 没有可启动的运行时"
            "（只有 mcp 提供者有）",
            error_type=ErrorType.CONFIG_ERROR,
            toolId=tool_id,
        )

    status_res = ctx.backend_manager.get_backend_status(tool_id)
    if not status_res.get("success"):
        fail(
            status_res.get("error") or f"MCP 工具 '{tool_id}' 未配置",
            error_type=status_res.get("errorType") or ErrorType.CONFIG_ERROR,
            toolId=tool_id,
        )
    status = (status_res.get("status") or {}).get("status")
    if status == RUNTIME_RUNNING:
        return status_res.get("status") or {}

    ctx.log(f"自动启动运行时: {tool_id}（当前状态: {status}）")
    started = await ctx.backend_manager.start_backend(tool_id)
    if not started.get("success"):
        fail(
            started.get("error") or f"启动运行时失败: {tool_id}",
            error_type=started.get("errorType") or ErrorType.CONNECTION_ERROR,
            toolId=tool_id,
        )
    return started


# ---------------------------------------------------------------- 描述符


def _mcp_domain(tool_id: str, name: str) -> str:
    if tool_id == EVERYTHING_TOOL_ID:
        return "file"
    if tool_id == WINGET_TOOL_ID:
        return "package"
    lowered = name.lower()
    if any(word in lowered for word in ("file", "path", "directory", "folder")):
        return "file"
    if "package" in lowered or "winget" in lowered:
        return "package"
    return "catalog"


def _mcp_risk(tool: dict[str, Any], name: str) -> str:
    destructive = {str(item) for item in tool.get("destructiveTools") or []}
    if name in destructive:
        return "destructive"
    if _READ_NAME_RE.match(name):
        return "read"
    return "write"


def _mcp_descriptor(tool: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    tool_id = str(tool.get("id") or "")
    name = str(item.get("name") or "").strip()
    domain = _mcp_domain(tool_id, name)
    schema = item.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    return {
        "capabilityId": name,
        "name": name,
        "description": item.get("description") or "",
        "domain": domain,
        "targetKind": _DOMAIN_TARGET_KIND[domain],
        "risk": _mcp_risk(tool, name),
        "execution": "inline",
        "deliveryOnly": False,
        "source": "mcp",
        "inputSchema": schema,
        "exposedAs": list(_MCP_EXPOSED_AS.get(tool_id, {}).get(name, [])),
        "available": True,
        "metadata": {},
    }


def _launcher_destructive(tool: dict[str, Any], action: dict[str, Any]) -> bool:
    """与 `policy._decide_launcher` 同一张判定表（描述符只决定预览文案）。

    条目未标 destructive → 不拦；标了但没给 destructiveActions → 整条拦；
    给了清单 → 只有命中 glob 的动作才算破坏性。
    """
    if tool.get("destructive") is not True:
        return False
    patterns = tool.get("destructiveActions")
    if patterns is None:
        return True
    fields = [
        str(action.get("id") or ""),
        str(action.get("name") or ""),
        str(action.get("type") or ""),
    ]
    for pattern in patterns:
        lowered = str(pattern).lower()
        for field in fields:
            if field and fnmatch.fnmatchcase(field.lower(), lowered):
                return True
    return False


def _launcher_execution(adapter: str, action: dict[str, Any]) -> tuple[str, bool]:
    if adapter == "steam":
        return "delivery_only", True
    if adapter == "utools":
        return "fire_and_forget", False
    if action.get("deliveryOnly"):
        return "delivery_only", True
    return "execute", False


def _launcher_descriptor(tool: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    adapter = str((tool.get("launcher") or {}).get("type") or "")
    capability_id = str(action.get("id") or "").strip()
    name = str(action.get("name") or "").strip() or capability_id
    domain = "game" if adapter == "steam" else "action"
    execution, delivery_only = _launcher_execution(adapter, action)
    metadata = {
        key: value
        for key, value in action.items()
        if key not in {"id", "name", "description"} and value is not None
    }
    if adapter == "steam":
        exposed_as = ["search_games", "open_game"]
    else:
        exposed_as = ["search_actions", "run_action"]
    return {
        "capabilityId": capability_id,
        "name": name,
        "description": action.get("description") or "",
        "domain": domain,
        "targetKind": _DOMAIN_TARGET_KIND[domain],
        "risk": "destructive" if _launcher_destructive(tool, action) else "write",
        "execution": execution,
        "deliveryOnly": delivery_only,
        "source": "launcher",
        "inputSchema": {
            "type": "object",
            "properties": {
                "param": {
                    "type": "string",
                    "description": "可选，传给动作的参数（Quicker 拼成 ?param；Steam 会忽略并回显）",
                }
            },
        },
        "exposedAs": exposed_as,
        "available": True,
        "metadata": metadata,
    }


def _app_descriptor(tool: dict[str, Any]) -> dict[str, Any]:
    launch = tool.get("launch") or {}
    executable = " ".join(
        [str(launch.get("command") or ""), *(str(a) for a in launch.get("args") or [])]
    ).strip()
    return {
        "capabilityId": "launch",
        "name": "launch",
        "description": f"启动 {tool.get('displayName') or tool.get('name') or tool.get('id')}",
        "domain": "app",
        "targetKind": "app",
        "risk": "destructive" if tool.get("destructive") is True else "write",
        "execution": "execute",
        "deliveryOnly": False,
        "source": "entry",
        "inputSchema": {"type": "object", "properties": {}},
        "exposedAs": ["launch_tool", "open_app"],
        "available": bool(launch),
        "metadata": {"commandPreview": executable} if executable else {},
    }


def _matches(descriptor: dict[str, Any], needle: str) -> bool:
    haystack = [
        descriptor.get("capabilityId"),
        descriptor.get("name"),
        descriptor.get("description"),
        descriptor.get("domain"),
    ]
    metadata = descriptor.get("metadata") or {}
    for key, value in metadata.items():
        if isinstance(value, list):
            haystack.extend(value)
        else:
            haystack.append(f"{key}:{value}")
    return any(needle in str(item).lower() for item in haystack if item)


# ---------------------------------------------------------------- 列出能力


async def list_capabilities(
    ctx: Any,
    tool_id: str,
    *,
    keyword: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    tool = _require_tool(ctx, tool_id)
    kind = provider_kind(tool)
    if not kind:
        fail(
            f"工具 '{tool_id}' 的 kind 不是 app/mcp/launcher，无法列出能力",
            error_type=ErrorType.CONFIG_ERROR,
            toolId=tool_id,
        )

    keyword = str(keyword or "").strip().lower() or None
    limit = _clamp_limit(limit)
    payload: dict[str, Any] = {
        "success": True,
        "toolId": tool.get("id"),
        "provider": _provider_summary(tool, kind),
        "kind": kind,
    }

    if kind == "mcp":
        if refresh:
            hydrated = await ctx.backend_manager.list_backend_tools(
                tool_id, force_refresh=True
            )
            if not hydrated.get("success"):
                fail(
                    hydrated.get("error") or f"刷新工具 '{tool_id}' 的能力清单失败",
                    error_type=hydrated.get("errorType") or ErrorType.RUNTIME_ERROR,
                    toolId=tool_id,
                )
            items = hydrated.get("tools") or []
        else:
            items = ctx.store.load_backend_tools(tool_id)
        capabilities = [_mcp_descriptor(tool, item) for item in items]
        catalog = ctx.store.load_backend_catalog(tool_id) or {}
        payload["runtime"] = _runtime_summary(ctx, tool_id)
        payload["lastSynced"] = catalog.get("lastSynced")
        payload["source"] = catalog.get("source")

    elif kind == "launcher":
        try:
            actions = ctx.launcher_manager.list_actions(tool_id)
        except LauncherError as exc:
            fail(str(exc), error_type=ErrorType.LAUNCHER_ERROR, toolId=tool_id)
        capabilities = [_launcher_descriptor(tool, action) for action in actions]
        catalog = ctx.store.load_launcher_catalog(tool_id) or {}
        payload["lastSynced"] = catalog.get("lastSynced")
        payload["source"] = catalog.get("source")

    else:
        capabilities = [_app_descriptor(tool)] if tool.get("launch") else []

    if keyword:
        capabilities = [item for item in capabilities if _matches(item, keyword)]
    if limit is not None:
        capabilities = capabilities[:limit]

    payload["count"] = len(capabilities)
    payload["capabilities"] = capabilities

    if not capabilities:
        if kind == "mcp":
            payload["note"] = (
                "能力缓存为空：先 start_runtime(tool_id=...) 再 "
                "sync_capabilities(tool_id=...)，或传 refresh=true 直接向后端索取"
            )
        elif kind == "launcher":
            payload["note"] = (
                "动作清单为空：先 sync_capabilities(tool_id=...) 扫描启动器本地数据"
            )
        else:
            payload["note"] = "该工具没有可调用的能力"
    return payload


# ---------------------------------------------------------------- 参数校验


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True


def _validate_capability_arguments(
    schema: dict[str, Any],
    arguments: Any,
    *,
    tool_id: str,
    capability_id: str,
) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        fail(
            "arguments 必须是 JSON 对象",
            error_type=ErrorType.INVALID_PARAMS,
            toolId=tool_id,
            capabilityId=capability_id,
            inputSchema=schema,
        )

    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    missing = [name for name in required if name not in arguments]
    if missing:
        fail(
            f"缺少必填参数: {', '.join(missing)}",
            error_type=ErrorType.INVALID_PARAMS,
            toolId=tool_id,
            capabilityId=capability_id,
            missing=missing,
            inputSchema=schema,
        )

    if schema.get("additionalProperties") is False:
        unknown = [name for name in arguments if name not in properties]
        if unknown:
            fail(
                f"不接受未知参数: {', '.join(unknown)}",
                error_type=ErrorType.INVALID_PARAMS,
                toolId=tool_id,
                capabilityId=capability_id,
                unknown=unknown,
                inputSchema=schema,
            )

    for name, value in arguments.items():
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if isinstance(expected, list):
            expected_types = [str(item) for item in expected]
            ok = any(_type_ok(value, item) for item in expected_types)
        else:
            expected_types = [str(expected)] if expected else []
            ok = _type_ok(value, expected_types[0]) if expected_types else True
        if not ok:
            fail(
                f"参数 {name} 类型应为 {'/'.join(expected_types)}（收到: {type(value).__name__}）",
                error_type=ErrorType.INVALID_PARAMS,
                toolId=tool_id,
                capabilityId=capability_id,
                argument=name,
                inputSchema=schema,
            )
        enum = spec.get("enum")
        if isinstance(enum, list) and value not in enum:
            fail(
                f"参数 {name} 必须是 {enum} 之一（收到: {value!r}）",
                error_type=ErrorType.INVALID_PARAMS,
                toolId=tool_id,
                capabilityId=capability_id,
                argument=name,
                inputSchema=schema,
            )
    return arguments


def _find_capability(items: list[dict[str, Any]], capability_id: str) -> dict[str, Any] | None:
    for item in items:
        if str(item.get("name") or "") == capability_id:
            return item
    return None


async def _resolve_mcp_capability(
    ctx: Any, tool: dict[str, Any], capability_id: str
) -> dict[str, Any]:
    tool_id = str(tool.get("id") or "")
    found = _find_capability(ctx.store.load_backend_tools(tool_id), capability_id)
    if found:
        return found

    # 缓存可能落后于后端：仅在后端已运行时刷新一次，不为一次未知调用拉起进程。
    if _runtime_summary(ctx, tool_id).get("status") == RUNTIME_RUNNING:
        refreshed = await ctx.backend_manager.list_backend_tools(
            tool_id, force_refresh=True
        )
        if refreshed.get("success"):
            found = _find_capability(refreshed.get("tools") or [], capability_id)
            if found:
                return found

    fail(
        f"工具 '{tool_id}' 没有能力 '{capability_id}'；先 list_capabilities 查看可用能力",
        error_type=ErrorType.NOT_FOUND,
        toolId=tool_id,
        capabilityId=capability_id,
    )


def _merge_backend_result(payload: dict[str, Any], text: Any) -> dict[str, Any]:
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            payload["result"] = text
            return payload
    else:
        parsed = text
    if isinstance(parsed, dict):
        payload.update(parsed)
    else:
        payload["result"] = parsed
    return payload


def _preview_note(risk: str, execution: str) -> str:
    base = "未执行：传 execute=true 才会产生副作用"
    if risk == "destructive":
        base += "，且该能力被标记为 destructive，还需 allowDestructive=true"
    if execution == "fire_and_forget":
        base += "（目标启动器无结果回传，结果只在它自己的界面里）"
    elif execution == "delivery_only":
        base += "（目标只确认收到指令，实际是否启动无从回传）"
    return base


# ---------------------------------------------------------------- 调用能力


async def call_capability(
    ctx: Any,
    tool_id: str,
    capability_id: str,
    arguments: Any = None,
    *,
    execute: bool | None = None,
    allow_destructive: bool = False,
    wait: float | None = None,
) -> dict[str, Any]:
    tool = _require_tool(ctx, tool_id)
    kind = provider_kind(tool)
    capability_id = str(capability_id or "").strip()
    if not capability_id:
        fail(
            "capability_id 不能为空",
            error_type=ErrorType.INVALID_PARAMS,
            toolId=tool_id,
        )
    if not kind:
        fail(
            f"工具 '{tool_id}' 的 kind 不是 app/mcp/launcher，无法调用能力",
            error_type=ErrorType.CONFIG_ERROR,
            toolId=tool_id,
        )

    gate_args = {
        "allowDestructive": bool(allow_destructive),
        "execute": True if execute is None else bool(execute),
    }

    if kind == "mcp":
        return await _call_mcp_capability(
            ctx, tool, capability_id, arguments, execute, gate_args
        )
    if kind == "launcher":
        return await _call_launcher_capability(
            ctx, tool, capability_id, arguments, execute, wait, gate_args
        )
    return await _call_app_capability(
        ctx, tool, capability_id, execute, wait, gate_args
    )


async def _call_mcp_capability(
    ctx: Any,
    tool: dict[str, Any],
    capability_id: str,
    arguments: Any,
    execute: bool | None,
    gate_args: dict[str, Any],
) -> dict[str, Any]:
    tool_id = str(tool.get("id") or "")
    item = await _resolve_mcp_capability(ctx, tool, capability_id)
    descriptor = _mcp_descriptor(tool, item)
    schema = descriptor["inputSchema"]
    backend_args = _validate_capability_arguments(
        schema,
        arguments,
        tool_id=tool_id,
        capability_id=capability_id,
    )

    risk = descriptor["risk"]
    effective_execute = (risk == "read") if execute is None else bool(execute)
    payload: dict[str, Any] = {
        "success": True,
        "toolId": tool_id,
        "capabilityId": capability_id,
        "provider": _provider_summary(tool, "mcp"),
        "risk": risk,
        "execution": descriptor["execution"],
        "arguments": backend_args,
        "executed": False,
        "_usageTarget": tool_id,
        "_usageCapability": capability_id,
    }
    if not effective_execute:
        payload["inputSchema"] = schema
        payload["note"] = _preview_note(risk, descriptor["execution"])
        return payload

    decision = decide_destructive(
        "mcp", tool, {"tool_name": capability_id}, gate_args
    )
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
            capabilityId=capability_id,
            inputSchema=schema,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认调用 destructive 能力: {tool_id}/{capability_id}")

    ctx.log(f"调用能力: {tool_id}/{capability_id}")
    await ensure_runtime(ctx, tool_id)
    res = ensure_success(
        await ctx.backend_manager.call_backend_tool(tool_id, capability_id, backend_args)
    )
    _merge_backend_result(payload, res.get("result"))
    payload["executed"] = True
    if decision["required_confirm"]:
        payload["warning"] = (
            f"目标能力 '{tool_id}/{capability_id}' 被判定为 destructive"
        )
    return payload


async def _call_launcher_capability(
    ctx: Any,
    tool: dict[str, Any],
    capability_id: str,
    arguments: Any,
    execute: bool | None,
    wait: float | None,
    gate_args: dict[str, Any],
) -> dict[str, Any]:
    tool_id = str(tool.get("id") or "")
    try:
        actions = ctx.launcher_manager.list_actions(tool_id)
    except LauncherError as exc:
        fail(str(exc), error_type=ErrorType.LAUNCHER_ERROR, toolId=tool_id)

    action = next(
        (item for item in actions if str(item.get("id") or "") == capability_id),
        None,
    )
    if action is None:
        fail(
            f"工具 '{tool_id}' 没有能力 '{capability_id}'；先 list_capabilities 查看可用动作",
            error_type=ErrorType.NOT_FOUND,
            toolId=tool_id,
            capabilityId=capability_id,
        )

    descriptor = _launcher_descriptor(tool, action)
    schema = descriptor["inputSchema"]
    capability_args = _validate_capability_arguments(
        schema,
        arguments,
        tool_id=tool_id,
        capability_id=capability_id,
    )
    param = capability_args.get("param")

    risk = descriptor["risk"]
    effective_execute = (risk == "read") if execute is None else bool(execute)
    payload: dict[str, Any] = {
        "success": True,
        "toolId": tool_id,
        "capabilityId": capability_id,
        "provider": _provider_summary(tool, "launcher"),
        "capability": descriptor,
        "risk": risk,
        "execution": descriptor["execution"],
        "deliveryOnly": descriptor["deliveryOnly"],
        "arguments": capability_args,
        "executed": False,
        "_usageTarget": tool_id,
        "_usageCapability": capability_id,
    }
    if not effective_execute:
        payload["inputSchema"] = schema
        payload["note"] = _preview_note(risk, descriptor["execution"])
        return payload

    decision = decide_destructive(
        "launcher",
        tool,
        {
            "action_id": capability_id,
            "action_name": action.get("name") or None,
            "action_type": action.get("type") or "",
        },
        gate_args,
    )
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
            capabilityId=capability_id,
            inputSchema=schema,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行 destructive 能力: {tool_id}/{capability_id}")

    res = ensure_success(
        await ctx.launcher_manager.run_action(
            tool_id,
            action_id=capability_id,
            param=param,
            wait=wait,
        )
    )
    payload.update(res)
    payload["executed"] = True
    if decision["required_confirm"]:
        payload["warning"] = (
            f"目标能力 '{tool_id}/{capability_id}' 被判定为 destructive"
        )
    return payload


async def _call_app_capability(
    ctx: Any,
    tool: dict[str, Any],
    capability_id: str,
    execute: bool | None,
    wait: float | None,
    gate_args: dict[str, Any],
) -> dict[str, Any]:
    tool_id = str(tool.get("id") or "")
    if capability_id != "launch":
        fail(
            f"工具 '{tool_id}' 只有能力 'launch'（收到: {capability_id!r}）",
            error_type=ErrorType.NOT_FOUND,
            toolId=tool_id,
            capabilityId=capability_id,
            availableCapabilities=["launch"],
        )

    if not tool.get("launch"):
        fail(
            f"工具 '{tool_id}' 未配置 launch，无法启动",
            error_type=ErrorType.CONFIG_ERROR,
            toolId=tool_id,
            capabilityId=capability_id,
        )

    descriptor = _app_descriptor(tool)
    risk = descriptor["risk"]
    # 应用启动永远是副作用动作；只有条目被标 destructive 才需要额外确认。
    effective_execute = False if execute is None else bool(execute)
    payload: dict[str, Any] = {
        "success": True,
        "toolId": tool_id,
        "capabilityId": capability_id,
        "provider": _provider_summary(tool, "app"),
        "capability": descriptor,
        "risk": risk,
        "execution": descriptor["execution"],
        "executed": False,
        "_usageTarget": tool_id,
        "_usageCapability": capability_id,
    }
    if not effective_execute:
        payload["inputSchema"] = descriptor["inputSchema"]
        payload["note"] = _preview_note(risk, descriptor["execution"])
        return payload

    decision = decide_destructive("app", tool, {}, gate_args)
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
            capabilityId=capability_id,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行启动: {tool_id}")

    exec_res = await launch_app(
        tool.get("launch"), tool_id, tool.get("name") or "", wait
    )
    payload.update(exec_res)
    payload["executed"] = True
    return ensure_success(payload)


# ---------------------------------------------------------------- 同步能力


async def sync_capabilities(ctx: Any, tool_id: str) -> dict[str, Any]:
    tool = _require_tool(ctx, tool_id)
    kind = provider_kind(tool)
    if not kind:
        fail(
            f"工具 '{tool_id}' 的 kind 不是 app/mcp/launcher，无法同步能力",
            error_type=ErrorType.CONFIG_ERROR,
            toolId=tool_id,
        )

    payload: dict[str, Any] = {
        "success": True,
        "toolId": tool.get("id"),
        "provider": _provider_summary(tool, kind),
        "kind": kind,
    }

    if kind == "mcp":
        ctx.log(f"同步能力目录: {tool_id}")
        await ensure_runtime(ctx, tool_id)
        res = ensure_success(await ctx.backend_manager.sync_tools_catalog(tool_id))
        catalog = ctx.store.load_backend_catalog(tool_id) or {}
        payload["count"] = res.get("count", len(catalog.get("tools") or []))
        payload["lastSynced"] = catalog.get("lastSynced")
        payload["source"] = catalog.get("source")
        return payload

    if kind == "launcher":
        ctx.log(f"同步能力清单: {tool_id}")
        try:
            res = ensure_success(ctx.launcher_manager.sync(tool_id))
        except LauncherError as exc:
            fail(str(exc), error_type=ErrorType.LAUNCHER_ERROR, toolId=tool_id)
        catalog = ctx.store.load_launcher_catalog(tool_id) or {}
        payload["count"] = res.get("count", len(catalog.get("actions") or []))
        payload["lastSynced"] = catalog.get("lastSynced")
        payload["source"] = res.get("source") or catalog.get("source")
        return payload

    fail(
        f"工具 '{tool_id}' 的 kind=app 没有可同步的能力清单"
        "（应用只有一个固定能力 launch）",
        error_type=ErrorType.CONFIG_ERROR,
        toolId=tool_id,
    )
