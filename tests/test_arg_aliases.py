"""入参别名与必填自描述错误的守卫。

背景（真实会话实测）：宿主与 mcp SDK **两道**入站校验都按 `inputSchema` 的
`required` 拦请求，不合格的入参根本到不了 handler。Agent 按直觉写
`{"query": "everything"}` / `{"id": "everything"}` 时，拿到的是
`params must have required property 'keyword' / 'tool_id'` 这种它无法自救的
错误，于是退化成自己起 shell 探路径。本文件钉住修复后的五面：

1. 别名经**真实协议会话**能通到 handler 并返回与规范名等价的结果（不是只到
   dispatch 层——SDK 的入站校验在这一层，漏了这里就是假绿）；
2. 必填缺失 / 规范名与别名取值冲突 → `invalid_params`，且消息自描述（列出
   可接受参数名 + 示例）。边界见 `test_empty_arguments_is_caught_upstream_of_
   our_handler`：`{}` 会被入站校验先拦下，我们的文案覆盖「传了键但值为空」与
   不做校验的客户端；
3. 使用统计按**归一后**的参数计，别名调用不会漏进 `targets` 维度；
4. 退役参数名（`mcp_tool_id` / `launcher_id`）不得借别名复活；
5. 别名表与声明表不冲突（别名不撞已声明参数），导出 schema 不改动声明表。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402
import tool_specs  # noqa: E402
import usage  # noqa: E402
from errors import ErrorType, ToolError  # noqa: E402
from tool_specs import (  # noqa: E402
    RETIRED_PARAM_NAMES,
    SERVER_TOOL_SPECS,
    build_mcp_tools,
    export_input_schema,
    get_spec,
    normalize_arguments,
)

from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session,
)


def run(coro):
    return asyncio.run(coro)


def payload_of(exc_info) -> dict:
    return json.loads(str(exc_info.value))


async def _call(name: str, arguments: dict):
    """走真实内存会话调用（含 SDK 的入站 schema 校验）"""
    async with create_connected_server_and_client_session(server.app) as session:
        return await session.call_tool(name, arguments)


def _result_payload(result) -> dict:
    return json.loads(result.content[0].text)


# ---- 1. 别名经协议层可达，且与规范名等价 ----


@pytest.mark.parametrize(
    "tool_name, alias_args",
    [
        ("search_tools", {"query": "文件"}),
        ("search_tools", {"q": "文件"}),
        ("get_tool_info", {"id": "everything"}),
        ("get_tool_info", {"name": "everything"}),
        ("get_tool_info", {"toolId": "everything"}),
    ],
)
def test_alias_call_survives_protocol_and_matches_canonical(tool_name, alias_args):
    """别名调用不被入站校验拦掉，且结果与规范名调用逐字等价"""
    canon_args = {"tool_id": "everything"} if tool_name == "get_tool_info" else {"keyword": "文件"}

    alias_result = run(_call(tool_name, alias_args))
    canon_result = run(_call(tool_name, canon_args))

    assert not alias_result.isError, f"别名调用被当成错误: {alias_result.content}"
    assert _result_payload(alias_result) == _result_payload(canon_result), (
        f"{tool_name} 的别名 {list(alias_args)} 与规范名结果不等价"
    )


def test_canonical_only_call_still_works():
    """规范名路径零回归：别名只是加法，不是替换"""
    result = run(_call("get_tool_info", {"tool_id": "everything"}))
    assert not result.isError
    assert _result_payload(result)["success"] is True


def test_normalize_does_not_mutate_caller_arguments():
    """归一化返回新 dict，不改调用方对象（dispatch 之外复用时的前提）"""
    spec = get_spec("get_tool_info")
    original = {"id": "everything", "extra": 1}
    normalized = normalize_arguments(spec, original)

    assert original == {"id": "everything", "extra": 1}
    assert normalized == {"tool_id": "everything", "extra": 1}


# ---- 2. 必填缺失 / 冲突 → 自描述 invalid_params ----


def test_missing_required_is_self_describing():
    """一个参数都不给：报 invalid_params，并把可接受参数名和示例写进消息"""
    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("get_tool_info", {}))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.INVALID_PARAMS
    assert payload["param"] == "tool_id"
    assert payload["acceptedParams"] == ["tool_id", "id", "toolId", "name"]
    assert payload["example"] == {"tool_id": "everything"}
    assert "缺少必填参数 tool_id" in payload["error"]
    assert "查看工具的详细信息" in payload["error"], "消息没带上工具用途，Agent 无从纠偏"


def test_blank_alias_counts_as_missing():
    """`{"id": ""}` 不是有效目标：与不给参数同样处理"""
    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("get_tool_info", {"id": "  "}))

    assert payload_of(exc)["errorType"] == ErrorType.INVALID_PARAMS


def test_conflicting_canonical_and_alias_is_rejected():
    """规范名与别名同时给且取值不同 → 不猜意图，直接报错"""
    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("get_tool_info", {"tool_id": "everything", "id": "quicker"}))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.INVALID_PARAMS
    assert payload["param"] == "tool_id"
    assert "取值不同" in payload["error"]


def test_empty_arguments_is_caught_upstream_of_our_handler():
    """一个参数都不给时，**校验型客户端**看到的是 SDK 的入站拒绝，不是我们的文案。

    这是 anyOf 的代价，也是有意的取舍：保留 `required` 语义（守不写别名的客户端
    也知道这个参数必填），代价是 `{}` 这种「什么都没传」的请求到不了 handler。
    真实会话里的失败形态是**参数名猜错**（`{"query": ...}` / `{"id": ...}`），
    那类别名面已经接管；本用例只钉住「谁在拦」这个边界不漂移。
    """
    result = run(_call("get_tool_info", {}))

    assert result.isError
    text = result.content[0].text
    assert "not valid" in text, f"预期是 SDK 的入站校验拒绝，实际: {text}"
    assert '"acceptedParams"' not in text


def test_blank_alias_reaches_our_self_describing_error():
    """`{"id": ""}` 过得了 schema（键在、类型对），此时由我们接管并给出自救信息"""
    result = run(_call("get_tool_info", {"id": ""}))

    assert result.isError
    payload = _result_payload(result)
    assert payload["errorType"] == ErrorType.INVALID_PARAMS
    assert payload["acceptedParams"] == ["tool_id", "id", "toolId", "name"]
    assert payload["example"] == {"tool_id": "everything"}


# ---- 3. 别名调用不漏计使用统计 ----


def test_alias_call_is_counted_under_canonical_target(tmp_path, monkeypatch):
    """归一化排在埋点之前：`{"id": "everything"}` 的 `target` 记成 `everything`。

    `usage.record_call` 的 target 取的是 `args.get("tool_id")`（usage.py:43），
    所以埋点若排在归一化之前，按别名来的调用会全记成空 target，
    `get_usage_stats` 的 `targets` 维度就漏了这批真实使用。
    """
    monkeypatch.setattr(usage, "USAGE_LOG", tmp_path / "usage.log")

    run(server.dispatch_tool("get_tool_info", {"id": "everything"}))

    assert [r["target"] for r in usage.read_rows()] == ["everything"]


# ---- 4. 退役名不得复活 ----


def test_retired_names_never_appear_in_exported_schema():
    """`mcp_tool_id` / `launcher_id` 是已统一掉的历史名，不得作为别名再出现"""
    dumped = json.dumps(
        [export_input_schema(s) for s in SERVER_TOOL_SPECS], ensure_ascii=False
    )
    for retired in RETIRED_PARAM_NAMES:
        assert retired not in dumped, f"退役参数名 {retired} 借别名复活了"


# ---- 5. 别名表与声明表自洽 ----


def test_alias_never_collides_with_declared_param():
    """别名不得与本工具任何已声明参数同名（否则一个名字两个语义）"""
    for spec in SERVER_TOOL_SPECS:
        declared = set(spec.input_schema.get("properties", {}))
        for canon, aliases in tool_specs.aliases_for(spec).items():
            overlap = set(aliases) & declared
            assert not overlap, f"{spec.name}: {canon} 的别名 {overlap} 与已声明参数同名"


def test_exported_alias_properties_follow_declaration_order():
    """别名属性按声明顺序追写，导出逐字节稳定。

    golden 是按文本比对的，`_declared_params` 一旦退回 `set`，`properties`
    顺序就随进程哈希漂移，同一份代码两次运行能产出两份基线。
    """
    for spec in SERVER_TOOL_SPECS:
        declared = list(spec.input_schema.get("properties", {}))
        expected = list(declared)
        for canon in declared:
            expected.extend(tool_specs.aliases_for(spec).get(canon, ()))

        assert list(export_input_schema(spec).get("properties", {})) == expected, spec.name


def test_exported_schema_declares_every_alias_as_property():
    """凡出现在任选组里的名字都必须在 properties 里有声明（否则客户端不知道能传什么）"""
    for spec in SERVER_TOOL_SPECS:
        exported = export_input_schema(spec)
        props = set(exported.get("properties", {}))
        for group in exported.get("anyOf", []) + [g for sub in exported.get("allOf", []) for g in sub["anyOf"]]:
            missing = set(group["required"]) - props
            assert not missing, f"{spec.name}: 任选组里的 {missing} 没有对应属性声明"


def test_export_does_not_mutate_declaration_table():
    """导出是纯函数：声明表里的 required 仍是规范名，不含别名"""
    spec = get_spec("get_tool_info")
    export_input_schema(spec)

    assert spec.input_schema["required"] == ["tool_id"]
    assert set(spec.input_schema["properties"]) == {"tool_id"}


def test_tools_list_schema_and_normalizer_agree():
    """对外 schema 承认的每个别名，归一化都必须认（防两边各自漂移）"""
    for spec in SERVER_TOOL_SPECS:
        exported = export_input_schema(spec)
        groups = exported.get("anyOf", []) + [
            g for sub in exported.get("allOf", []) for g in sub["anyOf"]
        ]
        alias_names = {
            name
            for group in groups
            for name in group["required"]
            if name not in spec.input_schema.get("required", [])
        }
        accepted = {a for aliases in tool_specs.aliases_for(spec).values() for a in aliases}
        assert alias_names <= accepted, f"{spec.name}: schema 承认 {alias_names - accepted} 但归一化不认"


def test_validate_specs_rejects_retired_alias(monkeypatch):
    """坏 spec 必须 import 期炸：把退役名写成别名"""
    broken = SERVER_TOOL_SPECS[0].__class__(
        name="__broken__",
        description="别名用了退役名",
        input_schema={
            "type": "object",
            "properties": {"tool_id": {"type": "string"}},
            "required": ["tool_id"],
        },
        arg_aliases={"tool_id": ("mcp_tool_id",)},
        handler=_noop_handler,
    )
    monkeypatch.setattr(tool_specs, "SERVER_TOOL_SPECS", (broken,))
    with pytest.raises(RuntimeError, match="退役"):
        tool_specs.validate_specs()


def test_validate_specs_rejects_alias_on_unknown_canonical(monkeypatch):
    """坏 spec 必须 import 期炸：arg_aliases 的键拼错（覆盖永不生效）"""
    broken = SERVER_TOOL_SPECS[0].__class__(
        name="__broken__",
        description="别名键拼错",
        input_schema={
            "type": "object",
            "properties": {"tool_id": {"type": "string"}},
            "required": ["tool_id"],
        },
        arg_aliases={"toolid": ("id",)},
        handler=_noop_handler,
    )
    monkeypatch.setattr(tool_specs, "SERVER_TOOL_SPECS", (broken,))
    with pytest.raises(RuntimeError, match="不是已声明的参数"):
        tool_specs.validate_specs()


async def _noop_handler(arguments: dict, ctx) -> dict:
    return {"success": True}


def test_build_mcp_tools_still_covers_all_specs():
    """别名改造没碰工具集本身：22 个工具一个不少、顺序不变"""
    assert [t.name for t in build_mcp_tools()] == [s.name for s in SERVER_TOOL_SPECS]
