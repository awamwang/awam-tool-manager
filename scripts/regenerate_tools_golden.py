"""重新采集 `tests/golden/tools_list.json`（对外 tools/list 基线）。

golden 是 `test_build_mcp_tools_matches_golden` 的比对基准：五字段
（name / description / inputSchema / annotations / outputSchema）按列表顺序
逐位全等。**只有对外契约发生有意变化时才该重采**——重采等于承认这次漂移是
设计意图，改错了就会把回归洗成基线。

序列化规则与测试里的 `_fields()` 同源（本脚本 import 测试模块取同一个函数，
不另抄一份），避免「脚本产出的格式测试永远比不过」。

用法：
    python scripts/regenerate_tools_golden.py            # 重写基线
    python scripts/regenerate_tools_golden.py --check     # 只比对，不落盘
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

# 复用测试里的抽取规则：基线与守卫必须同源
_spec = importlib.util.spec_from_file_location("test_tool_specs", ROOT / "tests" / "test_tool_specs.py")
test_tool_specs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_tool_specs)

from tool_specs import build_mcp_tools  # noqa: E402

GOLDEN_PATH = ROOT / "tests" / "golden" / "tools_list.json"


def current() -> list:
    return [test_tool_specs._fields(tool) for tool in build_mcp_tools()]


def main() -> int:
    check = "--check" in sys.argv[1:]
    text = json.dumps(current(), ensure_ascii=False, indent=2)
    existing = GOLDEN_PATH.read_text(encoding="utf-8") if GOLDEN_PATH.exists() else None

    if existing == text:
        print(f"基线已是最新（{len(json.loads(text))} 个工具），无需重写")
        return 0
    if check:
        print("--check：基线与当前 tools/list 不一致，请运行本脚本（不带 --check）重采")
        return 1

    # 按字节写：Windows 上 write_text 会把 \n 翻成 \r\n，而基线（与仓库其余
    # JSON 一样）是 LF——换行符漂移会让下次 diff 整篇重排。
    GOLDEN_PATH.write_bytes(text.encode("utf-8"))
    print(f"已重写 {GOLDEN_PATH.relative_to(ROOT)}（{len(json.loads(text))} 个工具）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
