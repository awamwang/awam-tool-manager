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

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402
from tool_specs import SERVER_TOOL_SPECS, build_mcp_tools  # noqa: E402

GOLDEN_PATH = ROOT / "tests" / "golden" / "tools_list.json"


def _fields(tool):
    """抽取行为等价关注的五个字段（与 M00 采集脚本同一序列化规则）。

    annotations 序列化成「仅含显式设置的 hint 键」的 dict（与 MCP 线上
    exclude_none 的序列化一致）；未挂注解记为 None。
    """
    annotations = None
    if tool.annotations is not None:
        annotations = tool.annotations.model_dump(exclude_none=True)
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.inputSchema,
        "annotations": annotations,
        "outputSchema": tool.outputSchema,
    }


def test_build_mcp_tools_matches_golden():
    """声明层无损：15 个 Tool 的五字段与 HEAD 采集的 golden 逐位全等（含顺序）"""
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    actual = [_fields(t) for t in build_mcp_tools()]
    assert actual == golden, "build_mcp_tools() 与 golden 不一致（字段或顺序漂移）"


def test_spec_names_three_way_consistency():
    """三方一致：spec 表名集合 == conftest 手写清单 == list_tools() 名集合。

    conftest.EXPECTED_TOOL_NAMES 保持手写不动（不许改既有断言），由本测试
    断言它与 spec 表、与协议出口三方一致——忘了改会红。
    """
    from conftest import EXPECTED_TOOL_NAMES

    spec_names = {s.name for s in SERVER_TOOL_SPECS}
    listed_names = {t.name for t in asyncio.run(server.list_tools())}
    assert spec_names == EXPECTED_TOOL_NAMES, (
        f"spec 表与手写清单不一致：多出 {spec_names - EXPECTED_TOOL_NAMES}，"
        f"缺少 {EXPECTED_TOOL_NAMES - spec_names}"
    )
    assert listed_names == EXPECTED_TOOL_NAMES, (
        f"list_tools() 与手写清单不一致：多出 {listed_names - EXPECTED_TOOL_NAMES}，"
        f"缺少 {EXPECTED_TOOL_NAMES - listed_names}"
    )
