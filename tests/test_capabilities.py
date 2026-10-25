"""Provider / Capability / Runtime 路由层的守卫。

这一层是本次重构的核心：对外只有 tools，往里是「工具（provider）→ 能力
（capability）→ 运行时（runtime）」三层。这里直接喂 ctx 假体，不经由 MCP
会话——会话层的接线由 test_server_dispatch / test_protocol_roundtrip 管。

钉住的契约：

1. 描述符形状：每条能力必须带完整 `inputSchema`，risk/execution 按同一张
   判定表给出；MCP 能力还要标注它对应的意图工具（`exposedAs`）。
2. 默认执行语义：只读能力不传 execute 也执行；写/破坏性能力默认只预览。
3. 门禁：destructive 能力仍需 `allowDestructive=true`，错误载荷带
   toolId / capabilityId / inputSchema，调用方拿得到自救信息。
4. 解析失败与参数校验失败都带输入 schema，而不是只有一句「失败了」。
5. 启动器（Steam delivery_only）与应用的固定 `launch` 能力按各自语义返回。
6. 同步按 provider 路由：MCP 先拉运行时再刷目录，启动器重扫，应用明确报错。
7. 硬切换：旧工具名（backend/launcher 时代）一律 `unknown_tool`。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import capabilities  # noqa: E402
import server  # noqa: E402
from errors import ErrorType, ToolError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def payload_of(exc_info) -> dict:
    return json.loads(str(exc_info.value))


# ---------------------------------------------------------------- 假 ctx


class FakeRegistry:
    def __init__(self, tools: dict):
        self.tools = tools

    def get_tool(self, tool_id: str):
        return self.tools.get(tool_id)


class FakeStore:
    def __init__(self, backend_tools=None, backend_catalog=None, launcher_catalog=None):
        self.backend_tools = backend_tools or {}
        self.backend_catalog = backend_catalog or {}
        self.launcher_catalog = launcher_catalog or {}

    def load_backend_tools(self, tool_id: str):
        return self.backend_tools.get(tool_id, [])

    def load_backend_catalog(self, tool_id: str):
        return self.backend_catalog.get(tool_id)

    def load_launcher_catalog(self, tool_id: str):
        return self.launcher_catalog.get(tool_id)


class FakeBackendManager:
    def __init__(self, status: str = "running", tools=None):
        self.status = status
        self.tools = tools or []
        self.calls: list[tuple] = []
        self.starts: list[str] = []
        self.syncs: list[str] = []

    def get_backend_status(self, tool_id=None):
        return {
            "success": True,
            "status": {"status": self.status, "tools_count": len(self.tools)},
        }

    async def list_backend_tools(self, tool_id, force_refresh=False):
        return {"success": True, "tools": list(self.tools)}

    async def start_backend(self, tool_id):
        self.starts.append(tool_id)
        self.status = "running"
        return {"success": True, "status": {"status": "running"}}

    async def call_backend_tool(self, tool_id, tool_name, arguments):
        self.calls.append((tool_id, tool_name, arguments))
        return {"success": True, "result": json.dumps({"echo": tool_name}, ensure_ascii=False)}

    async def sync_tools_catalog(self, tool_id):
        self.syncs.append(tool_id)
        return {"success": True, "count": len(self.tools)}


class FakeLauncherManager:
    def __init__(self, actions=None, run_result=None):
        self.actions = actions or []
        self.run_result = run_result or {"success": True, "message": "已执行"}
        self.runs: list[tuple] = []
        self.syncs: list[str] = []

    def list_actions(self, tool_id):
        return list(self.actions)

    async def run_action(self, tool_id, action_id=None, action_name=None, param=None, wait=None):
        self.runs.append((tool_id, action_id, param))
        return dict(self.run_result)

    def sync(self, tool_id):
        self.syncs.append(tool_id)
        return {"success": True, "count": len(self.actions), "source": "test"}


class FakeCtx:
    def __init__(self, tool, *, store=None, backend=None, launcher=None):
        self.registry = FakeRegistry({tool["id"]: tool})
        self.store = store or FakeStore()
        self.backend_manager = backend or FakeBackendManager()
        self.launcher_manager = launcher or FakeLauncherManager()
        self.logs: list[str] = []

    def log(self, message: str) -> None:
        self.logs.append(message)


MCP_TOOL = {
    "id": "demo-mcp",
    "kind": "mcp",
    "name": "demo-mcp",
    "destructive": True,
    "destructiveTools": ["nuke"],
}

MCP_TOOLS = [
    {
        "name": "search_files",
        "description": "搜文件",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "apply-settings",
        "description": "改设置",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "nuke",
        "description": "危险操作",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def make_mcp_ctx(*, status="running", tools=None) -> FakeCtx:
    items = list(MCP_TOOLS if tools is None else tools)
    return FakeCtx(
        MCP_TOOL,
        store=FakeStore(backend_tools={MCP_TOOL["id"]: items}),
        backend=FakeBackendManager(status=status, tools=items),
    )


# ---------------------------------------------------------------- 描述符


def test_mcp_descriptors_carry_schema_risk_and_exposed_as():
    ctx = make_mcp_ctx()

    payload = run(capabilities.list_capabilities(ctx, MCP_TOOL["id"]))

    assert payload["success"] is True
    assert payload["kind"] == "mcp"
    assert payload["provider"]["toolId"] == MCP_TOOL["id"]
    by_id = {c["capabilityId"]: c for c in payload["capabilities"]}
    assert set(by_id) == {"search_files", "apply-settings", "nuke"}

    assert by_id["search_files"]["risk"] == "read"
    assert by_id["apply-settings"]["risk"] == "write"
    assert by_id["nuke"]["risk"] == "destructive"
    # 完整 inputSchema 是「先看能力再调」的前提，不能在清单里省掉
    assert by_id["search_files"]["inputSchema"]["required"] == ["query"]
    assert by_id["search_files"]["domain"] == "file"
    assert by_id["search_files"]["targetKind"] == "path"
    for descriptor in by_id.values():
        assert descriptor["source"] == "mcp"
        assert descriptor["execution"] == "inline"
        assert descriptor["targetKind"] == capabilities._DOMAIN_TARGET_KIND[
            descriptor["domain"]
        ]


def test_descriptor_exposed_as_uses_declared_intent_tools():
    tool = {
        "id": capabilities.EVERYTHING_TOOL_ID,
        "kind": "mcp",
        **{k: v for k, v in MCP_TOOL.items() if k not in {"id", "kind"}},
    }
    ctx = FakeCtx(
        tool,
        store=FakeStore(backend_tools={tool["id"]: MCP_TOOLS}),
    )

    payload = run(capabilities.list_capabilities(ctx, tool["id"]))
    by_id = {c["capabilityId"]: c for c in payload["capabilities"]}

    assert by_id["search_files"]["exposedAs"] == ["search_files"]
    assert by_id["nuke"]["exposedAs"] == []


def test_list_capabilities_filters_keyword_and_limit():
    ctx = make_mcp_ctx()

    payload = run(capabilities.list_capabilities(ctx, MCP_TOOL["id"], keyword="设置", limit=1))

    assert [c["capabilityId"] for c in payload["capabilities"]] == ["apply-settings"]
    assert payload["count"] == 1


# ---------------------------------------------------------------- 默认执行语义


def test_read_capability_executes_without_explicit_execute():
    ctx = make_mcp_ctx()

    payload = run(
        capabilities.call_capability(
            ctx, MCP_TOOL["id"], "search_files", {"query": "报告"}
        )
    )

    assert payload["success"] is True
    assert payload["executed"] is True
    assert payload["risk"] == "read"
    assert ctx.backend_manager.calls == [
        (MCP_TOOL["id"], "search_files", {"query": "报告"})
    ]


def test_write_capability_defaults_to_preview():
    ctx = make_mcp_ctx()

    payload = run(capabilities.call_capability(ctx, MCP_TOOL["id"], "apply-settings"))

    assert payload["executed"] is False
    assert payload["risk"] == "write"
    assert payload["inputSchema"] == {"type": "object", "properties": {}}
    assert "execute=true" in payload["note"]
    assert ctx.backend_manager.calls == [], "预览不得转发到后端"


def test_destructive_requires_explicit_allow_destructive():
    ctx = make_mcp_ctx()

    with pytest.raises(ToolError) as exc:
        run(capabilities.call_capability(ctx, MCP_TOOL["id"], "nuke", execute=True))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.DESTRUCTIVE_BLOCKED
    assert payload["toolId"] == MCP_TOOL["id"]
    assert payload["capabilityId"] == "nuke"
    assert "inputSchema" in payload, "拒绝时也要给 schema，调用方才可能改参数重试"
    assert ctx.backend_manager.calls == []


def test_destructive_executes_after_confirmation():
    ctx = make_mcp_ctx()

    payload = run(
        capabilities.call_capability(
            ctx, MCP_TOOL["id"], "nuke", execute=True, allow_destructive=True
        )
    )

    assert payload["executed"] is True
    assert "destructive" in payload["warning"]
    assert ctx.backend_manager.calls == [(MCP_TOOL["id"], "nuke", {})]


# ---------------------------------------------------------------- 失败载荷


def test_unknown_capability_reports_both_ids():
    ctx = make_mcp_ctx()

    with pytest.raises(ToolError) as exc:
        run(capabilities.call_capability(ctx, MCP_TOOL["id"], "ghost"))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.NOT_FOUND
    assert payload["toolId"] == MCP_TOOL["id"]
    assert payload["capabilityId"] == "ghost"
    assert "list_capabilities" in payload["error"]


def test_invalid_arguments_echo_input_schema():
    ctx = make_mcp_ctx()

    with pytest.raises(ToolError) as exc:
        run(
            capabilities.call_capability(
                ctx, MCP_TOOL["id"], "search_files", {}, execute=True
            )
        )

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.INVALID_PARAMS
    assert payload["missing"] == ["query"]
    assert payload["inputSchema"]["required"] == ["query"]
    assert ctx.backend_manager.calls == []


def test_arguments_type_mismatch_is_rejected_before_backend():
    ctx = make_mcp_ctx()

    with pytest.raises(ToolError) as exc:
        run(
            capabilities.call_capability(
                ctx, MCP_TOOL["id"], "search_files", {"query": 12}, execute=True
            )
        )

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.INVALID_PARAMS
    assert payload["argument"] == "query"
    assert ctx.backend_manager.calls == []


def test_unknown_tool_is_not_found():
    ctx = make_mcp_ctx()

    with pytest.raises(ToolError) as exc:
        run(capabilities.list_capabilities(ctx, "ghost"))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.NOT_FOUND
    assert payload["toolId"] == "ghost"


# ---------------------------------------------------------------- 启动器与应用


STEAM_TOOL = {
    "id": "steam",
    "kind": "launcher",
    "name": "Steam",
    "destructive": True,
    "launcher": {"type": "steam"},
}


def test_steam_capability_is_delivery_only():
    ctx = FakeCtx(
        STEAM_TOOL,
        launcher=FakeLauncherManager(
            actions=[{"id": "app:400040", "name": "某游戏", "type": "game"}]
        ),
    )

    listed = run(capabilities.list_capabilities(ctx, "steam"))
    descriptor = listed["capabilities"][0]
    assert descriptor["capabilityId"] == "app:400040"
    assert descriptor["domain"] == "game"
    assert descriptor["risk"] == "destructive"
    assert descriptor["execution"] == "delivery_only"
    assert descriptor["deliveryOnly"] is True
    assert descriptor["exposedAs"] == ["search_games", "open_game"]

    preview = run(capabilities.call_capability(ctx, "steam", "app:400040"))
    assert preview["executed"] is False
    assert "无从回传" in preview["note"]
    assert ctx.launcher_manager.runs == []


def test_launcher_capability_forwards_param_after_confirmation():
    tool = {"id": "quicker", "kind": "launcher", "name": "Quicker", "launcher": {"type": "quicker"}}
    ctx = FakeCtx(
        tool,
        launcher=FakeLauncherManager(
            actions=[{"id": "a1", "name": "动作", "type": "XAction"}]
        ),
    )

    payload = run(
        capabilities.call_capability(
            ctx, "quicker", "a1", {"param": "hello"}, execute=True
        )
    )

    assert payload["executed"] is True
    assert ctx.launcher_manager.runs == [("quicker", "a1", "hello")]


APP_TOOL = {
    "id": "demo-app",
    "kind": "app",
    "name": "Demo",
    "displayName": "演示应用",
    "launch": {"type": "executable", "command": "C:\\Tools\\demo.exe", "args": ["--fast"]},
}


def test_app_provider_has_single_launch_capability():
    ctx = FakeCtx(APP_TOOL)

    listed = run(capabilities.list_capabilities(ctx, "demo-app"))

    [descriptor] = listed["capabilities"]
    assert descriptor["capabilityId"] == "launch"
    assert descriptor["exposedAs"] == ["launch_tool", "open_app"]
    assert descriptor["metadata"]["commandPreview"] == r"C:\Tools\demo.exe --fast"

    preview = run(capabilities.call_capability(ctx, "demo-app", "launch"))
    assert preview["executed"] is False
    assert "execute=true" in preview["note"]


def test_app_call_rejects_non_launch_capability():
    ctx = FakeCtx(APP_TOOL)

    with pytest.raises(ToolError) as exc:
        run(capabilities.call_capability(ctx, "demo-app", "quit"))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.NOT_FOUND
    assert payload["availableCapabilities"] == ["launch"]


# ---------------------------------------------------------------- 同步路由


def test_sync_mcp_starts_runtime_then_refreshes_catalog():
    ctx = make_mcp_ctx(status="stopped")

    payload = run(capabilities.sync_capabilities(ctx, MCP_TOOL["id"]))

    assert payload["success"] is True
    assert ctx.backend_manager.starts == [MCP_TOOL["id"]], "MCP 同步前必须先拉起运行时"
    assert ctx.backend_manager.syncs == [MCP_TOOL["id"]]
    assert payload["count"] == len(MCP_TOOLS)


def test_sync_launcher_rescans_local_data():
    ctx = FakeCtx(
        {"id": "quicker", "kind": "launcher", "launcher": {"type": "quicker"}},
        launcher=FakeLauncherManager(actions=[{"id": "a1"}]),
    )

    payload = run(capabilities.sync_capabilities(ctx, "quicker"))

    assert payload["kind"] == "launcher"
    assert payload["count"] == 1
    assert ctx.launcher_manager.syncs == ["quicker"]


def test_sync_app_is_config_error():
    ctx = FakeCtx(APP_TOOL)

    with pytest.raises(ToolError) as exc:
        run(capabilities.sync_capabilities(ctx, "demo-app"))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.CONFIG_ERROR
    assert payload["toolId"] == "demo-app"


# ---------------------------------------------------------------- 硬切换


@pytest.mark.parametrize(
    "old_name",
    [
        "call_backend_tool",
        "list_backend_tools",
        "sync_backend_tools",
        "start_backend",
        "stop_backend",
        "get_backend_status",
        "get_backend_logs",
        "set_backend_log_level",
        "list_launcher_actions",
        "run_launcher_action",
        "sync_launcher_actions",
    ],
)
def test_retired_tool_names_are_unknown_tool(monkeypatch, old_name):
    """不考虑兼容：旧名既不在清单里，也不能被 dispatch 认出来（full 模式同样）"""
    monkeypatch.setattr(server, "EXPOSURE_MODE", "full")

    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool(old_name, {"tool_id": "x"}))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.UNKNOWN_TOOL
    assert payload["toolName"] == old_name
