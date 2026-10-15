"""测试 server.py 的工具分发与资源读取（D19）

server.py 之前只有一次性脚本验证过。这里把 12 条分发路径、错误载荷、
资源返回结构固化成用例 —— 尤其要钉住「失败必须抛异常」这条约定：
mcp 1.x 会把正常返回值恒包装成 isError=False，只有抛异常客户端才能看到失败。

所有异步函数都用 asyncio.run 驱动，避免为了跑测试再引入 pytest-asyncio。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import server  # noqa: E402
from server import ToolError, dispatch_tool, list_tools, read_resource  # noqa: E402


def run(coro):
    """统一的事件循环入口"""
    return asyncio.run(coro)


def payload_of(exc_info) -> dict:
    """取出 ToolError 携带的 JSON 载荷"""
    return json.loads(str(exc_info.value))


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
    assert ids == ["everything-search-mcp"], f"kind 筛选失效: {ids}"


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


def test_backend_call_on_app_tool_raises_config_error():
    """everything 是 app 类型，没有后端连接 → config_error"""
    with pytest.raises(ToolError) as exc:
        run(dispatch_tool(
            "call_backend_tool",
            {"tool_id": "everything", "tool_name": "anything"}
        ))

    assert payload_of(exc)["errorType"] == "config_error"


def test_backend_status_and_logs():
    async def go():
        status = json.loads(await dispatch_tool("get_backend_status", {}))
        logs = json.loads(await dispatch_tool(
            "get_backend_logs", {"tool_id": "everything-search-mcp", "limit": 10}
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
            "set_backend_log_level",
            {"tool_id": "everything-search-mcp", "level": "warning"}
        ))
        # 复原，避免影响同进程内的其他断言
        await dispatch_tool(
            "set_backend_log_level",
            {"tool_id": "everything-search-mcp", "level": "info"}
        )
        return ok

    assert run(go())["success"] is True

    with pytest.raises(ToolError) as exc:
        run(dispatch_tool(
            "set_backend_log_level",
            {"tool_id": "everything-search-mcp", "level": "verbose"}
        ))
    assert payload_of(exc)["success"] is False


def test_internal_exception_becomes_typed_error(monkeypatch):
    """后端抛出非业务异常时，MCP 入口要把它转成 internal_error

    注意转换发生在 call_tool（协议入口）而不是 dispatch_tool：
    分发层保持「抛什么是什么」，统一包装交给入口，便于单测定位真实原因。
    """

    async def boom(tool_id, tool_name, arguments):
        raise ValueError("模拟后端崩了")

    monkeypatch.setattr(server.backend_manager, "call_backend_tool", boom)

    with pytest.raises(ToolError) as exc:
        run(server.call_tool(
            "call_backend_tool",
            {"tool_id": "everything-search-mcp", "tool_name": "x"}
        ))

    payload = payload_of(exc)
    assert payload["errorType"] == "internal_error"
    assert "模拟后端崩了" in payload["error"]


def test_call_tool_wrapper_returns_text_content():
    """MCP 入口：成功回包必须是 TextContent 列表"""
    contents = run(server.call_tool("list_tools", {}))
    assert len(contents) == 1
    assert contents[0].type == "text"
    assert json.loads(contents[0].text)["success"] is True


def test_call_tool_wrapper_propagates_tool_error():
    """失败必须冒泡给 SDK，由它转成 CallToolResult(isError=True)"""
    with pytest.raises(ToolError):
        run(server.call_tool("ghost_tool", {}))


def test_tool_definitions():
    """12 个工具定义齐全，且参数名统一为 tool_id"""
    tools = run(list_tools())
    assert len(tools) == 12

    dumped = json.dumps([t.model_dump() for t in tools], ensure_ascii=False)
    assert "mcp_tool_id" not in dumped, "遗留参数名 mcp_tool_id 不应再出现"
    assert "launcher_id" not in dumped, "遗留参数名 launcher_id 不应再出现"

    by_name = {t.name: t for t in tools}
    for name in ("search_tools", "call_backend_tool", "start_backend", "stop_backend"):
        assert name in by_name
        assert by_name[name].inputSchema["type"] == "object"


def test_read_resource_returns_contents_objects():
    """资源必须返回带 .content/.mime_type 的条目

    返回 TextContent 会让 SDK 在读 .content 时抛 AttributeError。
    """

    async def go():
        results = []
        for uri in ("awam://tools", "awam://mcp-servers", "awam://backends/status"):
            results.append(list(await read_resource(uri)))
        return results

    for items in run(go()):
        assert len(items) == 1
        item = items[0]
        assert isinstance(item.content, str), "content 不是字符串"
        assert item.mime_type == "application/json"
        json.loads(item.content)  # 必须是可解析的 JSON


def test_read_unknown_resource_raises():
    with pytest.raises(ToolError) as exc:
        run(read_resource("awam://nope"))

    assert payload_of(exc)["errorType"] == "unknown_resource"


def test_read_resource_accepts_anyurl():
    """SDK 传进来的是 pydantic AnyUrl，不是 str

    `AnyUrl("awam://tools") == "awam://tools"` 恒为 False，不归一化会让
    三个资源全部落到「未知资源」分支 —— 这个坑只有真实会话调用才会暴露。
    """
    from mcp.types import AnyUrl

    items = list(run(read_resource(AnyUrl("awam://tools"))))
    assert json.loads(items[0].content)


def test_list_resources_definitions():
    resources = run(server.list_resources())
    uris = {str(r.uri) for r in resources}

    assert uris == {"awam://tools", "awam://mcp-servers", "awam://backends/status"}
    assert all(r.mimeType == "application/json" for r in resources)


def test_prompts_do_not_teach_legacy_params():
    """D16 回归：prompt 里不能把已废弃的参数名当用法教给 AI"""
    prompts = {p.name for p in run(server.list_prompts())}
    assert prompts == {"getting_started", "how_to_search", "how_to_call_backend"}

    for name in prompts:
        messages = run(server.get_prompt(name))
        # 新版 SDK 返回 GetPromptResult，老版本返回裸列表，两种都要能取到文本
        text = (
            messages.messages[0].content.text
            if hasattr(messages, "messages")
            else messages[0].content.text
        )
        assert "launcher_id=" not in text
        assert "mcp_tool_id=" not in text

    how_to = run(server.get_prompt("how_to_call_backend"))
    messages = how_to.messages if hasattr(how_to, "messages") else how_to
    body = messages[0].content.text
    assert "tool_id=" in body
    assert "search_files" in body


def test_get_unknown_prompt_raises():
    with pytest.raises(ToolError) as exc:
        run(server.get_prompt("nope"))

    assert payload_of(exc)["errorType"] == "unknown_prompt"
