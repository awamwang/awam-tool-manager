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
    """app 类型的工具没有运行时 → config_error（此前这类失败会被当成成功）"""
    result = run(_call("get_runtime_status", {"tool_id": "everything"}))

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
    assert tools["call_capability"].annotations.openWorldHint is True
    # 能力面：列能力只读、同步幂等，调用只标 openWorld（副作用运行时才可知）
    assert tools["list_capabilities"].annotations.readOnlyHint is True
    assert tools["sync_capabilities"].annotations.idempotentHint is True


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
            got = await session.get_prompt("how_to_call_capability")
            return listed.prompts, got.messages

    prompts, messages = run(go())

    assert {p.name for p in prompts} == {
        "getting_started",
        "how_to_search",
        "how_to_call_capability",
    }

    text = messages[0].content.text
    # 只禁止"把它当参数用"的写法；正文里出现"不存在 launcher_id 参数"这种
    # 反向提醒是有意保留的（AI 若按旧文档调用会撞上它）
    assert "launcher_id=" not in text
    assert "mcp_tool_id=" not in text
    assert "tool_id=" in text
    assert "capability_id=" in text
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


def test_usage_stats_call_carries_boolean_success_over_wire():
    """P3 使用统计走真实会话：顶层 success 必须是 bool（outputSchema 唯一必填）

    钉的是一个真实踩过的坑：聚合视图里也有个 `success`（成功次数，int），
    平铺进载荷会把信封的 `success: true` 覆盖成整数——客户端按布尔读就全错。
    嵌进 `stats` 后信封语义与 outputSchema 的声明恢复一致。
    """
    result = run(_call("get_usage_stats", {"days": 30}))

    assert not result.isError, f"统计查询被标成错误: {result.content}"
    sc = result.structuredContent
    assert sc["success"] is True
    assert isinstance(sc["stats"]["success"], int)
    assert set(sc["stats"]) == {
        "total", "success", "failed", "byName", "targets", "capabilities",
        "byDay", "errorTypes",
    }
    assert sc["window"]["days"] == 30


def test_discover_tools_call_carries_success_envelope_without_spawning(
    tmp_path, monkeypatch
):
    """P1 工具发现走真实会话：信封 success 为 bool，且默认路径一次都不 spawn

    清单指向 tmp 合成目录（**不探测用户真机**——本期真机结果由客户端自己调
    `discover_tools` 得到）。这里要证的是协议面：载荷能过 outputSchema、
    `run_commands` 缺省为 false 时 subprocess 完全没被碰过。
    """
    import discovery

    here = tmp_path / "here"
    here.mkdir()
    # 把 PATH 换成 tmp 里的一个假垫片：命令「可用」但不该被跑，才是门禁的正证
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for nm in ("herecli", "herecli.cmd"):
        bin_dir.joinpath(nm).write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bin_dir))
    manifest = tmp_path / "discovery.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "candidates": [
                    {"tool_id": "here", "name": "Here",
                     "probes": [{"probe": "path_exists", "path": str(here)}],
                     "command": {"argv": ["herecli", "--version"]}},
                    {"tool_id": "gone", "name": "Gone",
                     "probes": [{"probe": "path_exists",
                                 "path": str(tmp_path / "gone")}]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(discovery.DISCOVERY_ENV_VAR, str(manifest))
    calls = []
    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **k: calls.append(a))

    result = run(_call("discover_tools", {}))

    assert not result.isError, f"工具发现被标成错误: {result.content}"
    sc = result.structuredContent
    assert sc["success"] is True
    assert sc["runCommands"] is False
    assert calls == []
    by_id = {c["toolId"]: c for c in sc["candidates"]}
    assert (by_id["here"]["status"], by_id["gone"]["status"]) == ("installed", "not_installed")
    here_cmd = by_id["here"]["command"]
    assert here_cmd["preview"] == "herecli --version"
    assert here_cmd["run"] is False and here_cmd["available"] is True
    assert here_cmd["path"].startswith(str(bin_dir))


def test_discover_tools_rejects_unknown_status_on_protocol_path():
    """`status` 带 enum：拼错的值在协议面就是 isError，不会静默返回空列表

    与 NE-8 同一条理由：筛选类接口最坏的失败是让调用方把「参数打错」读成
    「本机没装」。dispatch 层不校验，只有走真实会话才证明 enum 真的下发了。
    """
    result = run(_call("discover_tools", {"status": "insatlled"}))

    assert result.isError, "拼错的 status 应该被 schema 的 enum 拦下"
    assert "not one of" in result.content[0].text


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
