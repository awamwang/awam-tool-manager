"""由 src/schema.py 反向生成 docs/format.md 的字段表（A4）

为什么需要这个脚本：
    `docs/format.md` 曾经是"纸面标准"，`data/*.json` 是"事实标准"，两者已经分叉
    （文档写了字段、代码从不校验；代码认的字段、文档没写）。现在校验规则收拢到
    `src/schema.py`，文档如果还靠手写，分叉只是换个方向继续存在。

做法不是整份文档都生成（示例、对外契约、设计原则这些仍值得手写），而是：
    在 docs/format.md 里用 HTML 注释划出**生成区块**，脚本只替换这两个区块，
    其余部分照旧人工维护。这样"字段规范"只有 src/schema.py 一个真相源，
    而叙述性内容保留人写的可读性。

用法：
    python scripts/generate_format_doc.py            # 就地更新 docs/format.md
    python scripts/generate_format_doc.py --check    # 只检查是否漂移，漂移则退出码 1
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from schema import BACKEND_CATALOG_SCHEMA, TOOL_ENTRY_SCHEMA  # noqa: E402

DOC_PATH = ROOT / "docs" / "format.md"

BEGIN = "<!-- BEGIN:generated:{key} -->"
END = "<!-- END:generated:{key} -->"


# ---- 渲染 ----

def _type_label(prop: dict[str, Any]) -> str:
    """把 JSON Schema 的类型写成表格里的短标签"""
    type_ = prop.get("type", "any")
    if type_ == "array":
        item_type = (prop.get("items") or {}).get("type")
        return f"array<{item_type}>" if item_type else "array"
    return type_


def _suffix(prop: dict[str, Any]) -> str:
    """把约束（枚举 / 缺省 / 正则 / 下界）拼到说明后面

    这些约束本来就在 schema 里，文档再抄一遍必然漂移，所以由脚本统一渲染。
    """
    parts: list[str] = []
    if "enum" in prop:
        parts.append("取值：" + " / ".join(f"`{v}`" for v in prop["enum"]))
    if "default" in prop:
        parts.append(f"缺省 `{prop['default']}`")
    if "pattern" in prop:
        parts.append(f"须匹配 `{prop['pattern']}`")
    if "exclusiveMinimum" in prop:
        parts.append(f"必须 > {prop['exclusiveMinimum']}")
    return "；".join(parts)


def _flatten(
    properties: dict[str, Any], required: list[str], prefix: str = ""
) -> list[tuple[str, dict[str, Any], bool]]:
    """把嵌套 schema 摊平成表格行

    - 对象下钻：`launch` → `launch.command`
    - 数组元素是对象时：`tools` → `tools[].name`（与文档里既有的写法一致）
    """
    rows: list[tuple[str, dict[str, Any], bool]] = []
    for name, prop in properties.items():
        path = f"{prefix}{name}"
        rows.append((path, prop, name in required))

        if prop.get("type") == "object" and prop.get("properties"):
            rows.extend(_flatten(prop["properties"], prop.get("required", []), path + "."))

        items = prop.get("items") or {}
        if prop.get("type") == "array" and items.get("type") == "object" and items.get("properties"):
            rows.extend(_flatten(items["properties"], items.get("required", []), path + "[]."))
    return rows


def render_table(schema: dict[str, Any]) -> str:
    rows = _flatten(schema.get("properties", {}), schema.get("required", []))

    lines = ["| 字段 | 类型 | 必填 | 说明 |", "|---|---|---|---|"]
    for path, prop, is_required in rows:
        desc = prop.get("description", "")
        suffix = _suffix(prop)
        if suffix:
            desc = f"{desc}（{suffix}）" if desc else suffix
        lines.append(
            f"| `{path}` | `{_type_label(prop)}` | {'✅' if is_required else '-'} | {desc} |"
        )
    return "\n".join(lines)


# ---- 就地替换 ----

def replace_block(text: str, key: str, body: str) -> str:
    pattern = re.compile(
        re.escape(BEGIN.format(key=key)) + r".*?" + re.escape(END.format(key=key)),
        re.DOTALL,
    )
    if not pattern.search(text):
        raise SystemExit(f"docs/format.md 里找不到生成区块 {key}，请先补上标记注释")
    return pattern.sub(
        lambda _: BEGIN.format(key=key) + "\n\n" + body + "\n\n" + END.format(key=key),
        text,
        count=1,
    )


def generate() -> str:
    text = DOC_PATH.read_text(encoding="utf-8")
    text = replace_block(text, "tool-entry", render_table(TOOL_ENTRY_SCHEMA))
    text = replace_block(text, "backend-catalog", render_table(BACKEND_CATALOG_SCHEMA))
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="由 src/schema.py 生成 docs/format.md 的字段表")
    parser.add_argument("--check", action="store_true", help="只检查是否漂移，不写文件")
    args = parser.parse_args()

    rendered = generate()
    current = DOC_PATH.read_text(encoding="utf-8")

    if args.check:
        if rendered == current:
            print("docs/format.md 与 src/schema.py 一致")
            return 0
        print("docs/format.md 已落后于 src/schema.py，请运行：")
        print("    python scripts/generate_format_doc.py")
        return 1

    if rendered == current:
        print("docs/format.md 无变化")
        return 0
    DOC_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"已更新 {DOC_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
