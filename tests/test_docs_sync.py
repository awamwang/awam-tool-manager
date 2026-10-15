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
    validate_tool_entry,
)

DOC_PATH = ROOT / "docs" / "format.md"
DATA_TOOLS_DIR = ROOT / "data" / "tools"
DATA_BACKEND_DIR = ROOT / "data" / "backend-tools"
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
    # 23 个顶层字段 + launch(3) / connection(5) / update(2) / install(2) 的子字段
    assert len(rows) == 35


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


@pytest.mark.parametrize("path", sorted(DATA_BACKEND_DIR.glob("*.json")))
def test_real_backend_catalogs_are_valid(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    error = validate_backend_catalog(data)
    assert error is None, f"{path.name} 不合规: {error}"


# ---- 4. 校验器自身的分档行为 ----

def test_backend_catalog_rejects_broken_shape():
    """子工具缺 name 会让 call_backend_tool 的查表静默失效，必须在校验期拦下"""
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
