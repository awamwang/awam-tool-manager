"""协议层回环测试：用真实 MCP 会话驱动 server，不 mock SDK

为什么需要这一层：`tests/test_server_dispatch.py` 只验证到 `dispatch_tool` 抛
`ToolError` / `call_tool` 返回 is_error 结果，但**「server 产出的结果」到
「客户端实际收到的 isError / content / structuredContent」这一步是 SDK 做的**，
本项目控制不了。这里用 v2 的一等 `Client` 起一个内存里的真实 client/server
会话（无 transport），验证客户端**实际收到的** `is_error`、`content`、`annotations`。

mcp 2.x 迁移：
- v1 的 `create_connected_server_and_client_session` 已移除，改用 `Client(server.app)`。
- 客户端返回对象的字段全部 snake_case（`is_error` / `structured_content` /
  `input_schema` / `output_schema` / `annotations.read_only_hint`），wire 仍是 camelCase。
- v2 不再做入站 JSON-Schema 校验，`status` 等 enum 拼错的拦截现在由本服务
  `normalize_arguments` 补回（见 tool_specs.py），仍表现为客户端可见的 isError。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import server  # noqa: E402
from mcp import Client  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _call(name: str, arguments: dict):
    """通过真实内存会话调用一次工具，返回 CallToolResult"""
    async with Client(server.app) as client:
        return await client.call_tool(name, arguments)


def test_successful_call_reports_no_error():
    """成功调用：is_error 必须是 falsy，content 里是 JSON 文本"""
    result = run(_call("search_tools", {"keyword": "文件"}))

    assert not result.is_error, f"成功调用被标成了错误: {result.content}"
    payload = json.loads(result.content[0].text)
    assert payload["success"] is True


def test_business_failure_is_protocol_error():
    """D24 的核心回归：业务失败必须让客户端看到 is_error=True"""
    result = run(_call("get_tool_info", {"tool_id": "ghost"}))

    assert result.is_error, "业务失败没有变成 isError 结果"
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["errorType"] == "not_found"


def test_unknown_tool_is_protocol_error():
    result = run(_call("no_such_tool", {}))

    assert result.is_error
    assert json.loads(result.content[0].text)["errorType"] == "unknown_tool"


def test_config_error_surfaces_over_protocol():
    """app 类型的工具没有运行时 → config_error（此前这类失败会被当成成功）"""
    result = run(_call("get_runtime_status", {"tool_id": "everything"}))

    assert result.is_error
    assert json.loads(result.content[0].text)["errorType"] == "config_error"


def test_status_enum_rejects_typos_over_protocol():
    """`status` 的 enum 不是装饰：拼错的值在校验层就报错，不会静默返回空列表

    v2 起 SDK 不再自动校验，这条由本服务 normalize_arguments 补回（保持 1.x 时
    「拼错即拒、而非静默空集」的对外行为）。没有这层，`status="isntalled"` 会让
    调用方以为「本机没装任何工具」，而真实原因是参数打错了——静默空集是筛选类
    接口最坏的一种失败。
    """
    result = run(_call("list_tools", {"status": "isntalled"}))

    assert result.is_error, f"拼错的 status 没有在校验层被拒: {result.content}"
    assert "is not one of" in result.content[0].text


def test_new_filter_dimensions_narrow_real_results():
    """筛选维度在真实协议链路上确实生效：带 publisher 的调用返回的是收窄后的名单"""
    everything = run(_call("list_tools", {}))
    voidtools = run(_call("list_tools", {"publisher": "VOID"}))

    assert not everything.is_error and not voidtools.is_error
    all_ids = {t["id"] for t in json.loads(everything.content[0].text)["tools"]}
    hit = json.loads(voidtools.content[0].text)["tools"]

    assert len(all_ids) >= 2, "名单太窄，这条测试失去意义"
    assert [t["id"] for t in hit] == ["everything"], f"子串+大小写不敏感没生效: {hit}"
    assert hit[0]["status"] == "installed"


def test_tools_list_carries_annotations():
    """客户端在 tools/list 里应能读到 annotations"""

    async def go():
        async with Client(server.app) as client:
            return (await client.list_tools()).tools

    from conftest import EXPECTED_TOOL_NAMES

    tools = {t.name: t for t in run(go())}

    assert set(tools) == EXPECTED_TOOL_NAMES
    assert tools["search_tools"].annotations.read_only_hint is True
    assert tools["call_capability"].annotations.open_world_hint is True
    # 能力面：列能力只读、同步幂等，调用只标 openWorld（副作用运行时才可知）
    assert tools["list_capabilities"].annotations.read_only_hint is True
    assert tools["sync_capabilities"].annotations.idempotent_hint is True


def test_resource_read_over_protocol():
    """D23 的协议级回归：resources/read 必须真能读到内容

    此前返回 `list[TextContent]`，SDK 读 `.content` 会 AttributeError，
    而这个能力从未被实际调用过，缺陷一直潜伏。
    """

    async def go():
        async with Client(server.app) as client:
            listed = await client.list_resources()
            read = await client.read_resource("awam://tools")
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
        async with Client(server.app) as client:
            listed = await client.list_prompts()
            got = await client.get_prompt("how_to_call_capability")
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
    """structured_content：客户端应收到可解析的结构化结果，而非只有文本

    dispatch_tool 仍返回 JSON 文本（既有的纯文本通道保持不变，向后兼容），
    call_tool 额外附带 structured_content（v2 字段名，wire 仍 structuredContent）。
    """

    async def go():
        async with Client(server.app) as client:
            return await client.call_tool("list_tools", {})

    result = run(go())

    assert not result.is_error
    # 文本通道仍然可用（向后兼容）
    payload = json.loads(result.content[0].text)
    assert payload["success"] is True
    # 结构化通道：客户端无需再 json.loads 文本
    sc = result.structured_content
    assert isinstance(sc, dict), f"structured_content 不是 dict: {sc!r}"
    assert sc["success"] is True
    assert isinstance(sc["tools"], list)
    assert len(sc["tools"]) == len(payload["tools"])


def test_usage_stats_call_carries_boolean_success_over_wire():
    """P3 使用统计走真实会话：顶层 success 必须是 bool（output_schema 唯一必填）

    钉的是一个真实踩过的坑：聚合视图里也有个 `success`（成功次数，int），
    平铺进载荷会把信封的 `success: true` 覆盖成整数——客户端按布尔读就全错。
    嵌进 `stats` 后信封语义与 output_schema 的声明恢复一致。
    """
    result = run(_call("get_usage_stats", {"days": 30}))

    assert not result.is_error, f"统计查询被标成错误: {result.content}"
    sc = result.structured_content
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
    `discover_tools` 得到）。这里要证的是协议面：载荷能过 output_schema、
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

    assert not result.is_error, f"工具发现被标成错误: {result.content}"
    sc = result.structured_content
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

    v2 起这条由本服务 normalize_arguments 的 enum 校验把关（取代 SDK 入站校验）。
    """
    result = run(_call("discover_tools", {"status": "insatlled"}))

    assert result.is_error, "拼错的 status 应该被 enum 校验拦下"
    assert "not one of" in result.content[0].text


def test_tools_list_carries_output_schema():
    """output_schema：客户端在 tools/list 里应读到每个工具的 output_schema

    本项目用统一的 SHARED_OUTPUT_SCHEMA 声明所有工具输出都含顶层
    success(bool)，其余字段随工具而异（additionalProperties=true）。
    """

    async def go():
        async with Client(server.app) as client:
            return (await client.list_tools()).tools

    from conftest import EXPECTED_TOOL_NAMES

    tools = {t.name: t for t in run(go())}
    assert set(tools) == EXPECTED_TOOL_NAMES
    for t in tools.values():
        assert t.output_schema is not None, f"{t.name} 缺少 output_schema"
        assert t.output_schema["required"] == ["success"]
        assert t.output_schema["properties"]["success"]["type"] == "boolean"
