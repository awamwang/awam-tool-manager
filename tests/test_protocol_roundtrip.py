"""协议层回环测试：用真实 MCP 会话驱动 server，不 mock SDK

为什么需要这一层：`tests/test_server_dispatch.py` 只验证到 `dispatch_tool` 抛
`ToolError`，但**"抛异常"到"客户端看到 isError=true"这一步是 SDK 做的**，本项目
控制不了。mcp 1.x 的 `call_tool` handler 正常返回值会被硬编码成 `isError=False`，
一旦 SDK 行为变化（或升级到 2.x），错误处理会静默失效 —— 而这正是 D24 的修复根基。

这里用 `create_connected_server_and_client_session` 起一个内存里的真实
client/server 会话，验证客户端**实际收到的** `isError`、`content`、`annotations`。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import server  # noqa: E402

from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session,
)


def run(coro):
    return asyncio.run(coro)


async def _call(name: str, arguments: dict):
    """通过真实会话调用一次工具，返回 CallToolResult"""
    async with create_connected_server_and_client_session(server.app) as session:
        return await session.call_tool(name, arguments)


def test_successful_call_reports_no_error():
    """成功调用：isError 必须是 falsy，content 里是 JSON 文本"""
    result = run(_call("search_tools", {"keyword": "文件"}))

    assert not result.isError, f"成功调用被标成了错误: {result.content}"
    payload = json.loads(result.content[0].text)
    assert payload["success"] is True


def test_business_failure_is_protocol_error():
    """D24 的核心回归：业务失败必须让客户端看到 isError=True"""
    result = run(_call("get_tool_info", {"tool_id": "ghost"}))

    assert result.isError, "业务失败没有变成协议层错误"
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["errorType"] == "not_found"


def test_unknown_tool_is_protocol_error():
    result = run(_call("no_such_tool", {}))

    assert result.isError
    assert json.loads(result.content[0].text)["errorType"] == "unknown_tool"


def test_config_error_surfaces_over_protocol():
    """app 类型的工具没有后端连接 → config_error（此前这类失败会被当成成功）"""
    result = run(_call(
        "call_backend_tool",
        {"tool_id": "everything", "tool_name": "anything"},
    ))

    assert result.isError
    assert json.loads(result.content[0].text)["errorType"] == "config_error"


def test_status_enum_rejects_typos_over_protocol():
    """`status` 的 enum 不是装饰：拼错的值在校验层就报错，不会静默返回空列表

    没有这层的话，`status="isntalled"` 会让调用方以为「本机没装任何工具」，
    而真实原因是参数打错了——静默空集是筛选类接口最坏的一种失败。
    """
    result = run(_call("list_tools", {"status": "isntalled"}))

    assert result.isError, f"拼错的 status 没有在校验层被拒: {result.content}"
    assert "is not one of" in result.content[0].text


def test_new_filter_dimensions_narrow_real_results():
    """筛选维度在真实协议链路上确实生效：带 publisher 的调用返回的是收窄后的名单"""
    everything = run(_call("list_tools", {}))
    voidtools = run(_call("list_tools", {"publisher": "VOID"}))

    assert not everything.isError and not voidtools.isError
    all_ids = {t["id"] for t in json.loads(everything.content[0].text)["tools"]}
    hit = json.loads(voidtools.content[0].text)["tools"]

    assert len(all_ids) >= 2, "名单太窄，这条测试失去意义"
    assert [t["id"] for t in hit] == ["everything"], f"子串+大小写不敏感没生效: {hit}"
    assert hit[0]["status"] == "installed"


def test_tools_list_carries_annotations():
    """客户端在 tools/list 里应能读到 annotations"""

    async def go():
        async with create_connected_server_and_client_session(server.app) as session:
            return (await session.list_tools()).tools

    from conftest import EXPECTED_TOOL_NAMES

    tools = {t.name: t for t in run(go())}

    assert set(tools) == EXPECTED_TOOL_NAMES
    assert tools["search_tools"].annotations.readOnlyHint is True
    assert tools["call_backend_tool"].annotations.openWorldHint is True
    # 启动器：列/同步只读或幂等，执行动作只标 openWorld（副作用运行时才可知）
    assert tools["list_launcher_actions"].annotations.readOnlyHint is True
    assert tools["run_launcher_action"].annotations.openWorldHint is True


def test_resource_read_over_protocol():
    """D23 的协议级回归：resources/read 必须真能读到内容

    此前返回 `list[TextContent]`，SDK 读 `.content` 会 AttributeError，
    而这个能力从未被实际调用过，缺陷一直潜伏。
    """

    async def go():
        async with create_connected_server_and_client_session(server.app) as session:
            listed = await session.list_resources()
            read = await session.read_resource("awam://tools")
            return listed.resources, read.contents

    resources, contents = run(go())

    assert {str(r.uri) for r in resources} == {
        "awam://tools",
        "awam://mcp-servers",
        "awam://backends/status",
    }
    text = contents[0].text
    payload = json.loads(text)
    assert isinstance(payload, list) and len(payload) >= 1


def test_prompt_over_protocol_teaches_real_params():
    """D16 的协议级回归：prompt 里不能出现已废弃的参数名"""

    async def go():
        async with create_connected_server_and_client_session(server.app) as session:
            listed = await session.list_prompts()
            got = await session.get_prompt("how_to_call_backend")
            return listed.prompts, got.messages

    prompts, messages = run(go())

    assert {p.name for p in prompts} == {
        "getting_started",
        "how_to_search",
        "how_to_call_backend",
    }

    text = messages[0].content.text
    # 只禁止"把它当参数用"的写法；正文里出现"不存在 launcher_id 参数"这种
    # 反向提醒是有意保留的（AI 若按旧文档调用会撞上它）
    assert "launcher_id=" not in text
    assert "mcp_tool_id=" not in text
    assert "tool_id=" in text
    assert "search_files" in text


def test_successful_call_carries_structured_content():
    """structuredContent：客户端应收到可解析的结构化结果，而非只有文本

    这是 mcp 1.30 的 structuredContent 能力。dispatch_tool 仍返回 JSON 文本
    （既有的纯文本通道保持不变，向后兼容），call_tool 额外附带 structuredContent。
    """

    async def go():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool("list_tools", {})

    result = run(go())

    assert not result.isError
    # 文本通道仍然可用（向后兼容）
    payload = json.loads(result.content[0].text)
    assert payload["success"] is True
    # 结构化通道：客户端无需再 json.loads 文本
    sc = result.structuredContent
    assert isinstance(sc, dict), f"structuredContent 不是 dict: {sc!r}"
    assert sc["success"] is True
    assert isinstance(sc["tools"], list)
    assert len(sc["tools"]) == len(payload["tools"])


def test_tools_list_carries_output_schema():
    """outputSchema：客户端在 tools/list 里应读到每个工具的 outputSchema

    本项目用统一的 SHARED_OUTPUT_SCHEMA 声明所有工具输出都含顶层
    success(bool)，其余字段随工具而异（additionalProperties=true）。
    """

    async def go():
        async with create_connected_server_and_client_session(server.app) as session:
            return (await session.list_tools()).tools

    from conftest import EXPECTED_TOOL_NAMES

    tools = {t.name: t for t in run(go())}
    assert set(tools) == EXPECTED_TOOL_NAMES
    for t in tools.values():
        assert t.outputSchema is not None, f"{t.name} 缺少 outputSchema"
        assert t.outputSchema["required"] == ["success"]
        assert t.outputSchema["properties"]["success"]["type"] == "boolean"
