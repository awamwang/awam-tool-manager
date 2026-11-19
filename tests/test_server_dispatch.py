"""测试 server.py 的工具分发与资源读取（D19）

固化 12 条分发路径、错误载荷、资源返回结构。

mcp 2.x 迁移要点：lowlevel Server 的 handler 不再是「无参装饰器函数」，而是
`async (ctx, params) -> 完整 Result`；且 handler 抛出的异常不再被 SDK 自动转成
isError 结果。因此：
- 业务失败在 call_tool 入口被重包成 CallToolResult(is_error=True)，正文仍是给
  客户端解析的 JSON —— 对外 wire 与 1.x 完全一致（见 server.py 的 call_tool）。
- 资源 / 模板不存在走协议错误 MCPError(-32602)，不再是裸 ToolError。

dispatch_tool（分发层，返回 JSON 文本、业务失败抛 ToolError）签名不变，所以
基于它的用例与迁移前逐字一致。所有异步函数都用 asyncio.run 驱动。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    GetPromptRequestParams,
    ReadResourceRequestParams,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# backend_client 直接 import：本文件其它 src import 带的 noqa 标记在这里不加，
# 因为 E402 在本仓 ruff 配置里未启用，加了反而多一条 RUF100。
# （注意：别在注释里写出完整的 noqa 指令字面量，ruff 会把注释里的它当成
#   directive，报 "Invalid noqa directive" 警告。）
import backend_client
import server  # noqa: E402
from server import ToolError, dispatch_tool  # noqa: E402


def run(coro):
    """统一的事件循环入口"""
    return asyncio.run(coro)


def payload_of(exc_info) -> dict:
    """取出 ToolError 携带的 JSON 载荷"""
    return json.loads(str(exc_info.value))


# ---- v2 handler 调用薄封装（构造协议 params，省去每处样板）----


def call_tool(name: str, arguments: dict | None = None) -> CallToolResult:
    return run(
        server.call_tool(
            None, CallToolRequestParams(name=name, arguments=arguments or {})
        )
    )


def list_tools():
    return run(server.list_tools(None, None)).tools


def read_resource(uri: str):
    return run(server.read_resource(None, ReadResourceRequestParams(uri=uri))).contents


def list_resources():
    return run(server.list_resources(None, None)).resources


def list_prompts():
    return run(server.list_prompts(None, None)).prompts


def get_prompt(name: str):
    return run(
        server.get_prompt(None, GetPromptRequestParams(name=name, arguments=None))
    )


def test_dispatch_read_only_paths():
    """纯读路径：list / search / get_tool_info 都应返回 success JSON"""

    async def go():
        listed = json.loads(await dispatch_tool("list_tools", {}))
        searched = json.loads(await dispatch_tool("search_tools", {"keyword": "搜索"}))
        info = json.loads(await dispatch_tool("get_tool_info", {"tool_id": "everything"}))
        return listed, searched, info

    listed, searched, info = run(go())

    assert listed["success"] is True
    assert listed["count"] >= 1

    assert searched["success"] is True
    assert searched["keyword"] == "搜索"

    assert info["success"] is True
    assert info["id"] == "everything"
    assert info["kind"] == "app"


def test_dispatch_kind_filter():
    mcp_only = json.loads(run(dispatch_tool("list_tools", {"kind": "mcp"})))
    ids = [t["id"] for t in mcp_only["tools"]]
    # 断言筛选语义而非固定清单：`data/tools/` 会随接入进度增加工具
    # （2026-10-16 接入 winget-mcp 时，写死 ids == [...] 的旧断言立刻变红）。
    assert "everything-search-mcp" in ids, f"kind 筛选漏了已接入的 MCP 工具: {ids}"
    assert all(t["kind"] == "mcp" for t in mcp_only["tools"]), f"混入非 mcp: {ids}"
    assert "everything" not in ids, "kind=app 的工具不应出现在 kind=mcp 的结果里"


def test_unknown_tool_raises_typed_error():
    with pytest.raises(ToolError) as exc:
        run(dispatch_tool("no_such_tool", {}))

    payload = payload_of(exc)
    assert payload["success"] is False
    assert payload["errorType"] == "unknown_tool"


def test_missing_tool_id_raises_not_found():
    with pytest.raises(ToolError) as exc:
        run(dispatch_tool("get_tool_info", {"tool_id": "ghost"}))

    assert payload_of(exc)["errorType"] == "not_found"


def test_launch_unknown_tool_raises():
    with pytest.raises(ToolError):
        run(dispatch_tool("launch_tool", {"tool_id": "ghost"}))


def test_capability_on_app_tool_only_has_launch():
    """everything 是 app 类型，只有 launch 能力；未知能力 → not_found"""
    with pytest.raises(ToolError) as exc:
        run(dispatch_tool(
            "call_capability",
            {"tool_id": "everything", "capability_id": "anything"}
        ))

    payload = payload_of(exc)
    assert payload["errorType"] == "not_found"
    assert payload["capabilityId"] == "anything"


def test_runtime_tools_reject_non_mcp_provider():
    """应用 / 启动器没有运行时，start_runtime 应给 config_error"""
    with pytest.raises(ToolError) as exc:
        run(dispatch_tool("start_runtime", {"tool_id": "everything"}))

    assert payload_of(exc)["errorType"] == "config_error"


def test_runtime_status_and_logs():
    async def go():
        status = json.loads(await dispatch_tool("get_runtime_status", {}))
        logs = json.loads(await dispatch_tool(
            "get_runtime_logs", {"tool_id": "everything-search-mcp", "limit": 10}
        ))
        return status, logs

    status, logs = run(go())

    assert status["success"] is True
    assert "backends" in status
    assert logs["success"] is True
    assert isinstance(logs["logs"], list)


def test_set_log_level_dispatch():
    async def go():
        ok = json.loads(await dispatch_tool(
            "set_runtime_log_level",
            {"tool_id": "everything-search-mcp", "level": "warning"}
        ))
        # 复原，避免影响同进程内的其他断言
        await dispatch_tool(
            "set_runtime_log_level",
            {"tool_id": "everything-search-mcp", "level": "info"}
        )
        return ok

    assert run(go())["success"] is True

    with pytest.raises(ToolError) as exc:
        run(dispatch_tool(
            "set_runtime_log_level",
            {"tool_id": "everything-search-mcp", "level": "verbose"}
        ))
    assert payload_of(exc)["success"] is False


def _fake_mcp_capability(monkeypatch, name: str = "x") -> None:
    """让 call_capability 能解析到能力并跳过真实启动。"""
    monkeypatch.setattr(
        server.store,
        "load_backend_tools",
        lambda tool_id: [
            {"name": name, "description": "", "inputSchema": {"type": "object"}}
        ],
    )
    monkeypatch.setattr(
        server.backend_manager,
        "get_backend_status",
        lambda tool_id=None: {"success": True, "status": {"status": "running"}},
    )


def test_internal_exception_becomes_typed_error(monkeypatch):
    """后端抛非业务异常时，协议入口 call_tool 把它转成 internal_error 的 is_error 结果

    转换发生在 call_tool（协议入口）而不是 dispatch_tool：分发层保持「抛什么是什么」，
    统一包装交给入口。v2 起异常不再是 isError，call_tool 显式接管这条转换。
    """
    _fake_mcp_capability(monkeypatch)

    async def boom(tool_id, tool_name, arguments):
        raise ValueError("模拟后端崩了")

    monkeypatch.setattr(server.backend_manager, "call_backend_tool", boom)

    result = call_tool(
        "call_capability",
        {"tool_id": "everything-search-mcp", "capability_id": "x", "execute": True},
    )
    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["errorType"] == "internal_error"
    assert "模拟后端崩了" in payload["error"]


def test_backend_busy_maps_to_typed_error(monkeypatch):
    """A3：后端并发被拒时，协议入口返回 backend_busy 的 is_error 结果

    后端并发被拒绝（BackendBusyError）→ manager 返回 errorType=backend_busy
    → server 经 ensure_success 抛 ToolError → call_tool 转 is_error=True。
    """
    _fake_mcp_capability(monkeypatch)

    async def busy(tool_id, tool_name, arguments):
        return {
            "success": False,
            "errorType": "backend_busy",
            "error": "后端工具 'everything-search-mcp' 正在处理其他调用，请稍后重试",
        }

    monkeypatch.setattr(server.backend_manager, "call_backend_tool", busy)

    result = call_tool(
        "call_capability",
        {"tool_id": "everything-search-mcp", "capability_id": "x", "execute": True},
    )
    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["errorType"] == "backend_busy"
    assert payload["success"] is False


def test_call_tool_wrapper_returns_text_content():
    """MCP 入口：成功回包是 CallToolResult，content 为单条 TextContent，且带 structured_content

    文本通道保留（向后兼容），同时附带结构化结果（1.30 起的 structuredContent 能力，
    v2 字段名 structured_content，wire 仍 structuredContent）。
    """
    result = call_tool("list_tools", {})
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text)["success"] is True
    assert isinstance(result.structured_content, dict)
    assert result.structured_content["success"] is True


def test_call_tool_wrapper_propagates_tool_error():
    """失败转成 is_error=True 的结果，正文是可解析的 JSON（v1 由 SDK 转，v2 由 call_tool 自己转）"""
    result = call_tool("ghost_tool", {})
    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["errorType"] == "unknown_tool"


def test_tool_definitions():
    """对外工具定义齐全（清单见 conftest.EXPECTED_TOOL_NAMES），且参数名统一为 tool_id"""
    from conftest import EXPECTED_TOOL_NAMES

    tools = list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOL_NAMES, (
        f"工具集与预期不一致：多出 {names - EXPECTED_TOOL_NAMES}，"
        f"缺少 {EXPECTED_TOOL_NAMES - names}"
    )

    dumped = json.dumps([t.model_dump() for t in tools], ensure_ascii=False)
    assert "mcp_tool_id" not in dumped, "遗留参数名 mcp_tool_id 不应再出现"
    assert "launcher_id" not in dumped, "遗留参数名 launcher_id 不应再出现"

    by_name = {t.name: t for t in tools}
    for name in ("search_tools", "list_capabilities", "start_runtime", "stop_runtime"):
        assert name in by_name
        assert by_name[name].input_schema["type"] == "object"


def test_read_resource_returns_contents_objects():
    """资源返回带 .text/.mime_type 的 TextResourceContents 条目"""

    for uri in ("awam://tools", "awam://mcp-servers", "awam://backends/status"):
        items = read_resource(uri)
        assert len(items) == 1
        item = items[0]
        assert isinstance(item.text, str), "text 不是字符串"
        assert item.mime_type == "application/json"
        json.loads(item.text)  # 必须是可解析的 JSON


def test_read_unknown_resource_raises():
    with pytest.raises(MCPError) as exc:
        read_resource("awam://nope")

    payload = json.loads(exc.value.message)
    assert payload["errorType"] == "unknown_resource"


def test_read_resource_uri_is_str():
    """v2：params.uri 已是 str（v1 的 AnyUrl 归一化坑不复存在），直接传 str 即可命中"""
    items = read_resource("awam://tools")
    assert json.loads(items[0].text)


def test_list_resources_definitions():
    resources = list_resources()
    uris = {str(r.uri) for r in resources}

    assert uris == {"awam://tools", "awam://mcp-servers", "awam://backends/status"}
    assert all(r.mime_type == "application/json" for r in resources)


def test_prompts_do_not_teach_legacy_params():
    """D16 回归：prompt 里不能把已废弃的参数名当用法教给 AI"""
    prompts = {p.name for p in list_prompts()}
    assert prompts == {"getting_started", "how_to_search", "how_to_call_capability"}

    for name in prompts:
        result = get_prompt(name)
        text = result.messages[0].content.text
        assert "launcher_id=" not in text
        assert "mcp_tool_id=" not in text

    how_to = get_prompt("how_to_call_capability")
    body = how_to.messages[0].content.text
    assert "tool_id=" in body
    assert "capability_id=" in body
    assert "search_files" in body


def test_get_unknown_prompt_raises():
    with pytest.raises(MCPError) as exc:
        get_prompt("nope")

    payload = json.loads(exc.value.message)
    assert payload["errorType"] == "unknown_prompt"


def test_call_tool_returns_structured_result():
    """call_tool 返回 CallToolResult，并附带 structured_content

    dispatch_tool 仍返回 JSON 文本（单测依赖它），call_tool 在其上包一层
    CallToolResult：文本通道保留，同时给出结构化结果。
    """
    res = call_tool("list_tools", {})

    assert isinstance(res, CallToolResult), f"call_tool 未返回 CallToolResult: {type(res)}"
    assert res.is_error is False
    assert isinstance(res.structured_content, dict)
    assert res.structured_content["success"] is True
    # 文本通道保留（向后兼容）
    assert "tools" in json.loads(res.content[0].text)


# ---- A6：启动预热 ----
#
# _prewarm_backends 在服务启动时后台执行：只装不启、异常全吞。
# 最坏情况必须退化为「首次 start_backend 时现场装」，绝不能拖垮服务。


def test_prewarm_logs_install_summary(monkeypatch, capsys):
    """预热装了什么要在启动日志里说清楚"""

    async def fake_ensure_all_installed():
        return {"success": True, "total": 3, "available": 2, "installed": 1, "details": []}

    monkeypatch.setattr(
        server.backend_manager, "ensure_all_installed", fake_ensure_all_installed
    )
    run(server._prewarm_backends())

    err = capsys.readouterr().err
    assert "已自动安装 1 个后端依赖" in err
    assert "共 3 个 MCP 工具" in err


def test_prewarm_logs_when_everything_ready(monkeypatch, capsys):
    """全部就绪时也留一行日志，避免「静默成功」无法确认预热跑过"""

    async def fake_ensure_all_installed():
        return {"success": True, "total": 2, "available": 2, "installed": 0, "details": []}

    monkeypatch.setattr(
        server.backend_manager, "ensure_all_installed", fake_ensure_all_installed
    )
    run(server._prewarm_backends())

    assert "依赖均已就绪" in capsys.readouterr().err


def test_prewarm_blocked_logs_missing_and_switch(monkeypatch, capsys):
    """P0：未授权时预热不安装，但必须把「缺谁 + 怎么开」写进启动日志"""

    async def fake_ensure_all_installed():
        return {
            "success": True,
            "total": 2,
            "available": 1,
            "installed": 0,
            "blocked": 1,
            "details": [
                {"tool_id": "everything-search-mcp", "installBlocked": "unauthorized"},
                {"tool_id": "winget-mcp", "available": True},
            ],
        }

    monkeypatch.setattr(
        server.backend_manager, "ensure_all_installed", fake_ensure_all_installed
    )
    run(server._prewarm_backends())

    err = capsys.readouterr().err
    assert "未授权自动安装" in err
    assert "everything-search-mcp" in err
    assert "不执行任何安装命令" in err
    assert server.BackendClient.INSTALL_ENV_VAR in err


def test_prewarm_swallows_failures(monkeypatch, capsys):
    """预热异常必须被吞掉：服务照常就绪，退化为首次调用时按需安装"""

    async def boom():
        raise RuntimeError("模拟预热炸了")

    monkeypatch.setattr(server.backend_manager, "ensure_all_installed", boom)
    run(server._prewarm_backends())  # 不抛异常即通过

    err = capsys.readouterr().err
    assert "启动预热失败" in err
    assert "不影响服务" in err


def test_prewarm_does_not_probe_npm_unconditionally(monkeypatch):
    """#28 续：预热**不再**无条件补 PATH——自愈已下放到 ensure_installed 慢路径

    原用例是 `test_prewarm_patches_path_before_installing`（A7：必须先补 PATH
    再判断依赖，断言 `order == ["path", "install"]`）。那条契约在 #28 之后被
    取代：PATH 自愈改由 `BackendClient.ensure_installed()` 在「当前 PATH 判不
    到」时才做，预热不再替所有条目预付这次 `npm prefix -g`（实测 1.8s，冷机
    可达 6.5s）。

    这里**反向钉住新契约**：预热一次都不该碰 PATH 自愈。反向而不是删掉，是
    因为「预热里又加回一次无条件自愈」是很容易悄悄回来的性能退化，而它不会
    让任何功能断言变红——只有这种计数断言抓得住。
    """
    probes: list[str] = []
    order: list[str] = []

    async def spy_ensure_npm_bin():
        probes.append("path")
        return None

    async def fake_ensure_all_installed():
        order.append("install")
        return {"success": True, "total": 1, "available": 1, "installed": 0, "details": []}

    # 钉在 backend_client 命名空间：ensure_installed 的慢路径调的是模块全局，
    # 预热若哪天又加回无条件自愈，拿到的就是这个 spy，必然变红。
    monkeypatch.setattr(backend_client, "ensure_npm_bin_on_path", spy_ensure_npm_bin)
    monkeypatch.setattr(
        server.backend_manager, "ensure_all_installed", fake_ensure_all_installed
    )
    run(server._prewarm_backends())

    assert order == ["install"], f"预热没走完依赖检测：{order}"
    assert probes == [], f"预热又在做无条件 PATH 自愈了（性能退化）：{probes}"
