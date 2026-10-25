"""文档与 schema 的一致性（A4：消除「文档是纸面标准、data/ 是事实标准」的双真相源）

三条防线：

1. `docs/format.md` 的两张字段表必须与 `src/schema.py` 生成结果逐字节一致，
   手写改文档会直接失败；
2. 文档里的 JSON 示例必须真的能通过校验，示例不能比代码更容易腐烂；
3. `data/` 下的真实文件必须全部通过校验——这才是"格式规范"最终要保证的东西。
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from schema import (  # noqa: E402
    TOOL_ENTRY_SCHEMA,
    validate_backend_catalog,
    validate_launcher_catalog,
    validate_tool_entry,
)
from store import JsonStore  # noqa: E402

DOC_PATH = ROOT / "docs" / "format.md"
# 人写的声明在仓库里（必须校验）；程序生成的运行时缓存已迁到本机应用数据目录，
# 改由写入侧用例覆盖，见 test_generated_*_is_valid。
DATA_TOOLS_DIR = ROOT / "data" / "tools"
SCRIPT_PATH = ROOT / "scripts" / "generate_format_doc.py"


def _load_generator():
    """按文件路径加载 scripts/generate_format_doc.py（它不是包，不能直接 import）"""
    spec = importlib.util.spec_from_file_location("generate_format_doc", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_blocks(text: str) -> list:
    """抽出 markdown 里所有 ```json 代码块的内容"""
    return [m.group(1) for m in re.finditer(r"```json\n(.*?)```", text, re.DOTALL)]


# ---- 1. 生成块不得漂移 ----

def test_format_doc_matches_generated_tables():
    """文档里的字段表必须与 schema 生成结果一致"""
    generator = _load_generator()
    current = DOC_PATH.read_text(encoding="utf-8")
    assert generator.generate() == current, (
        "docs/format.md 的字段表已落后于 src/schema.py，"
        "请运行 `python scripts/generate_format_doc.py` 后重新提交"
    )


def test_doc_has_both_generated_blocks():
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "<!-- BEGIN:generated:tool-entry -->" in text
    assert "<!-- BEGIN:generated:backend-catalog -->" in text


def test_every_schema_field_is_documented():
    """schema 里每个字段都得有 description，否则生成的表会出现空白说明"""
    missing = []

    def walk(properties: dict, prefix: str = ""):
        for name, prop in properties.items():
            path = f"{prefix}{name}"
            if not prop.get("description"):
                missing.append(path)
            if prop.get("properties"):
                walk(prop["properties"], path + ".")
            items = prop.get("items") or {}
            if items.get("properties"):
                walk(items["properties"], path + "[].")

    walk(TOOL_ENTRY_SCHEMA["properties"])
    assert not missing, f"这些字段缺 description: {missing}"


def test_generated_table_covers_every_field():
    """生成的表行数应与 schema 摊平后的字段数相等（漏渲染 = 静默少写文档）"""
    generator = _load_generator()
    table = generator.render_table(TOOL_ENTRY_SCHEMA)
    rows = [ln for ln in table.splitlines() if ln.startswith("| `")]
    # 期望值从 schema 动态摊平：顶层字段 + 嵌套 object 的子字段。
    # 原来写死 35（23 顶层 + launch3/connection5/update2/install2），
    # 2026-10-16 新增 destructiveTools 字段后立刻变红——行数不该随字段增减手改。
    expected = 0
    for spec in TOOL_ENTRY_SCHEMA["properties"].values():
        expected += 1
        if spec.get("type") == "object" and "properties" in spec:
            expected += len(spec["properties"])
    assert len(rows) == expected, f"表行数 {len(rows)} != schema 摊平字段数 {expected}"


# ---- 2. 文档里的示例必须能过校验 ----

def test_doc_examples_pass_validation():
    """format.md 里的工具条目 / 后端缓存示例都要合法"""
    blocks = _json_blocks(DOC_PATH.read_text(encoding="utf-8"))
    assert blocks, "文档里没找到任何 json 示例，测试可能解析错了"

    checked = 0
    for raw in blocks:
        data = json.loads(raw)  # 先保证文档里的 JSON 本身没写坏
        if not isinstance(data, dict):
            continue
        if "kind" in data:  # 工具条目示例
            assert validate_tool_entry(data) is None, f"工具条目示例不合规: {validate_tool_entry(data)}"
            checked += 1
        elif "parent" in data:  # 后端子工具缓存示例
            assert validate_backend_catalog(data) is None, (
                f"后端缓存示例不合规: {validate_backend_catalog(data)}"
            )
            checked += 1

    assert checked >= 3, f"只校验了 {checked} 个示例，文档示例可能被动过"


# ---- 3. 真实数据文件必须全部通过校验 ----

@pytest.mark.parametrize("path", sorted(DATA_TOOLS_DIR.glob("*.json")))
def test_real_tool_entries_are_valid(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    error = validate_tool_entry(data)
    assert error is None, f"{path.name} 不合规: {error}"
    # 文件名即 tool_id，两者不一致会让 get_tool(文件名) 取不到 id
    assert data["id"] == path.stem, f"{path.name} 的 id 与文件名不一致: {data['id']}"


def test_generated_backend_catalog_is_valid(tmp_path):
    """写入侧产出的后端缓存必须合规。

    原先这里是 glob `data/backend-tools/*.json` 参数化，但运行时缓存已迁出仓库
    （2026-10-16，见 runtime_paths.py），目录空了参数化会**静默消失**——不报错，
    却等于悄悄丢了这条校验。改成校验生成器本身产出什么：与磁盘上有无缓存无关，
    也比事后扫盘更能防住「写出的缓存不合 schema」。
    """
    store = JsonStore(tmp_path / "data")
    store.save_backend_tools("sample", [{"name": "t", "description": "d"}])
    written = tmp_path / "data" / "backend-tools" / "sample.json"
    data = json.loads(written.read_text(encoding="utf-8"))
    assert validate_backend_catalog(data) is None, (
        f"save_backend_tools 写出的缓存不合规: {validate_backend_catalog(data)}"
    )


def test_generated_launcher_catalog_is_valid(tmp_path):
    """同上：动作清单缓存的写入侧也要合规"""
    store = JsonStore(tmp_path / "data")
    store.save_launcher_actions("sample", [{"id": "abc"}], "partial")
    written = tmp_path / "data" / "launcher-actions" / "sample.json"
    data = json.loads(written.read_text(encoding="utf-8"))
    assert validate_launcher_catalog(data) is None, (
        f"save_launcher_actions 写出的缓存不合规: {validate_launcher_catalog(data)}"
    )


# ---- 4. 校验器自身的分档行为 ----

def test_backend_catalog_rejects_broken_shape():
    """子工具缺 name 会让 call_capability 的查表静默失效，必须在校验期拦下"""
    assert validate_backend_catalog({"parent": "x", "tools": [{"description": "没名字"}]}) is not None
    assert validate_backend_catalog({"parent": "x", "tools": "不是数组"}) is not None
    assert validate_backend_catalog("不是对象") is not None
    assert validate_backend_catalog({"parent": "x", "tools": []}) is None


def test_update_and_install_subfields_are_checked():
    """新增的 update / install 子字段也要真的参与校验，而不是只出现在文档里"""
    base = {"id": "t", "name": "T", "kind": "app"}
    assert validate_tool_entry({**base, "update": {"method": "winget"}}) is None
    assert validate_tool_entry({**base, "update": {"method": "apt"}}) is not None
    assert validate_tool_entry({**base, "install": {"command": "npm i -g x"}}) is None
    assert validate_tool_entry({**base, "install": {"command": 123}}) is not None


# ---- 5. SKILL.md 对外工具表 vs tool_specs 声明表（P1-04）----


def test_skill_tool_table_matches_server_tool_specs():
    """SKILL.md「对外 MCP 工具」表里的工具名集合必须与 SERVER_TOOL_SPECS 一致。

    P1-04 把 tools/list 的定义源收敛到 src/tool_specs.py 后，SKILL.md 的
    工具表是最后一份手写副本。本期不做生成器（U2），只加「漂移即红」守卫：
    加了工具忘了改文档（或反过来），这里就红。
    """
    from tool_specs import SERVER_TOOL_SPECS  # noqa: E402

    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    section = skill.split("## 对外 MCP 工具", 1)[1].split("\n## ", 1)[0]
    # 表格首列形如 `| `search_tools` | ...`
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE))

    spec_names = {s.name for s in SERVER_TOOL_SPECS}
    assert documented, "SKILL.md 工具表没解析到任何工具名，测试可能解析错了"
    assert documented == spec_names, (
        f"SKILL.md 工具表与 tool_specs 声明表不一致："
        f"文档多出 {documented - spec_names}，spec 多出 {spec_names - documented}"
    )
