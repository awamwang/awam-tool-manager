"""tool_specs 声明层守卫（P1-04 M02 起）。

核心是 **golden 五字段比对**：`build_mcp_tools()` 与
`tests/golden/tools_list.json` 按列表顺序逐位全等。该 golden 在重构前
未改一行代码的 HEAD 上采集（M00，commit 5f06756），是「声明搬家无损」
的独立基线——抄错一个字段、错一个顺序，这里就红。

只比对五字段（name / description / inputSchema / annotations /
outputSchema）而不做全量 model_dump：Tool 的字段集随 mcp 版本变化，
全量 dump 会因解释器里装的 mcp 小版本不同而误报；五字段恰是本次重构
负责的全部面，既够严又不脆弱。
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
from tool_specs import SERVER_TOOL_SPECS, build_mcp_tools  # noqa: E402

GOLDEN_PATH = ROOT / "tests" / "golden" / "tools_list.json"
# lazy 的 golden 是改造前 HEAD 采集的独立基线；default/full 是新模式的
# 切片基线（由同一张声明表生成，逐位锁定导出顺序与字段）。
GOLDEN_PATHS = {
    "lazy": GOLDEN_PATH,
    "default": ROOT / "tests" / "golden" / "tools_list.default.json",
    "full": ROOT / "tests" / "golden" / "tools_list.full.json",
}

# MCP 协议 hints 全集。这里**独立手写**而不从 tool_specs  import：两边各自
# 持一份，任一边漂移都会让下面的断言变红。
PROTOCOL_ANNOTATION_KEYS = {
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
}


def _fields(tool):
    """抽取行为等价关注的五个字段（与 M00 采集脚本同一序列化规则）。

    annotations 序列化成「仅含显式设置的 hint 键」的 dict（与 MCP 线上
    exclude_none 的序列化一致）；未挂注解记为 None。
    """
    annotations = None
    if tool.annotations is not None:
        # v2 字段是 snake_case；by_alias=True 还原成线上 camelCase（readOnlyHint…），
        # 与 M00 采集的 golden 保持同一序列化口径，golden 无需重生成。
        annotations = tool.annotations.model_dump(exclude_none=True, by_alias=True)
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "annotations": annotations,
        "outputSchema": tool.output_schema,
    }


@pytest.mark.parametrize("mode", ["lazy", "default", "full"])
def test_build_mcp_tools_matches_golden(mode):
    """声明层无损：该模式下全部 Tool 的五字段与 golden 逐位全等（含顺序）"""
    golden = json.loads(GOLDEN_PATHS[mode].read_text(encoding="utf-8"))
    actual = [_fields(t) for t in build_mcp_tools(mode)]
    assert actual == golden, (
        f"build_mcp_tools({mode!r}) 与 golden 不一致（字段或顺序漂移）"
    )


def test_spec_names_three_way_consistency(monkeypatch):
    """三方一致：spec 表 == 手写全量清单 == 各模式下 list_tools() 的切片。

    conftest 的清单按模式分层手写，断言它与 spec 表、与协议出口逐模式一致
    ——忘了改会红。
    """
    from conftest import (
        EXPECTED_TOOL_NAMES_DEFAULT,
        EXPECTED_TOOL_NAMES_FULL,
        EXPECTED_TOOL_NAMES_LAZY,
    )

    spec_names = {s.name for s in SERVER_TOOL_SPECS}
    assert spec_names == EXPECTED_TOOL_NAMES_FULL, (
        f"spec 表与手写清单不一致：多出 {spec_names - EXPECTED_TOOL_NAMES_FULL}，"
        f"缺少 {EXPECTED_TOOL_NAMES_FULL - spec_names}"
    )
    for mode, expected in (
        ("lazy", EXPECTED_TOOL_NAMES_LAZY),
        ("default", EXPECTED_TOOL_NAMES_DEFAULT),
        ("full", EXPECTED_TOOL_NAMES_FULL),
    ):
        monkeypatch.setattr(server, "EXPOSURE_MODE", mode)
        listed_names = {t.name for t in asyncio.run(server.list_tools(None, None)).tools}
        assert listed_names == expected, (
            f"list_tools() 在 {mode} 模式下与手写清单不一致："
            f"多出 {listed_names - expected}，缺少 {expected - listed_names}"
        )


def test_every_spec_is_complete():
    """spec 完整性：handler 可调用、inputSchema 结构合法、annotations 键合法。

    本期明确**不加入参运行时校验**（缺 tool_id → not_found 是既有行为），
    这里只做结构守卫：schema 是可机检数据，坏结构在搬迁时就会暴露。
    """
    for spec in SERVER_TOOL_SPECS:
        assert callable(spec.handler), f"{spec.name} 缺 handler"

        schema = spec.input_schema
        assert schema["type"] == "object", f"{spec.name} 的 inputSchema type 不是 object"
        properties = schema.get("properties", {})
        for prop_name, prop in properties.items():
            assert "type" in prop, f"{spec.name} 的参数 {prop_name} 缺 type"
        required = schema.get("required", [])
        assert set(required) <= set(properties), (
            f"{spec.name} 的 required 含 properties 里不存在的参数: "
            f"{set(required) - set(properties)}"
        )

        if spec.annotations:
            illegal = set(spec.annotations) - PROTOCOL_ANNOTATION_KEYS
            assert not illegal, f"{spec.name} 的 annotations 含非法键: {illegal}"


def test_validate_specs_rejects_duplicate_names(monkeypatch):
    """NE-3/U5：spec 表重名必须 import 期快速失败（字典索引会静默丢工具）"""
    spec = SERVER_TOOL_SPECS[0]
    monkeypatch.setattr(tool_specs, "SERVER_TOOL_SPECS", (spec, spec))
    with pytest.raises(RuntimeError, match="重复"):
        tool_specs.validate_specs()


def test_validate_specs_rejects_missing_handler(monkeypatch):
    """NE-3/U5：新增工具忘写 handler，服务必须起不来（有意的快速失败）"""
    broken = SERVER_TOOL_SPECS[0].__class__(
        name="__broken__",
        description="缺 handler 的坏 spec",
        input_schema={"type": "object", "properties": {}},
    )
    monkeypatch.setattr(tool_specs, "SERVER_TOOL_SPECS", (broken,))
    with pytest.raises(RuntimeError, match="handler"):
        tool_specs.validate_specs()


def test_validate_specs_rejects_illegal_annotation_key(monkeypatch):
    """NE-3/U5：annotations 键拼错必须 import 期炸，而不是运行期才暴露"""
    broken = SERVER_TOOL_SPECS[0].__class__(
        name="__broken__",
        description="注解键非法的坏 spec",
        input_schema={"type": "object", "properties": {}},
        annotations={"readOnlyHints": True},  # 正确的键是 readOnlyHint
        handler=_noop_handler,
    )
    monkeypatch.setattr(tool_specs, "SERVER_TOOL_SPECS", (broken,))
    with pytest.raises(RuntimeError, match="annotations"):
        tool_specs.validate_specs()


async def _noop_handler(arguments: dict, ctx) -> dict:
    """仅供坏 spec 占位的空 handler"""
    return {"success": True}


UNIGET_READONLY_TOOLS = {
    "list_package_managers",
    "search_uniget_packages",
    "list_uniget_packages",
    "get_package_details",
}


def test_uniget_readonly_tools_are_full_and_honestly_annotated():
    """四个 UniGetUI 只读工具只进 full；会 spawn 安装器并联网，所以标
    openWorldHint=true，且**不声明 readOnlyHint**（那会把「起子进程、联网」说成纯读）。"""
    by_name = {spec.name: spec for spec in SERVER_TOOL_SPECS}
    for name in UNIGET_READONLY_TOOLS:
        spec = by_name[name]
        assert spec.exposure == ("full",), f"{name} 应只在 full 暴露"
        assert spec.annotations.get("openWorldHint") is True, name
        assert "readOnlyHint" not in spec.annotations, (
            f"{name} 会 spawn UniGetUI 并联网查远端索引，不能声称只读"
        )
