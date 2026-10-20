"""重新采集 `tests/golden/tools_list*.json`（三种暴露模式的 tools/list 基线）。

golden 是 `test_build_mcp_tools_matches_golden` 的比对基准：五字段
（name / description / inputSchema / annotations / outputSchema）按列表顺序
逐位全等。**只有对外契约发生有意变化时才该重采**——重采等于承认这次漂移是
设计意图，改错了就会把回归洗成基线。

三种模式各自一份基线（`lazy` 用旧文件名 `tools_list.json`），一次全量重采/检查。

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

GOLDEN_PATHS = {
    "lazy": ROOT / "tests" / "golden" / "tools_list.json",
    "default": ROOT / "tests" / "golden" / "tools_list.default.json",
    "full": ROOT / "tests" / "golden" / "tools_list.full.json",
}


def current(mode: str) -> list:
    return [test_tool_specs._fields(tool) for tool in build_mcp_tools(mode)]


def main() -> int:
    check = "--check" in sys.argv[1:]
    stale = []
    for mode, path in GOLDEN_PATHS.items():
        text = json.dumps(current(mode), ensure_ascii=False, indent=2)
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == text:
            print(f"{mode}: 基线已是最新（{len(json.loads(text))} 个工具）")
            continue
        if check:
            print(f"{mode}: 基线与当前 tools/list 不一致")
            stale.append(mode)
            continue
        # 按字节写：Windows 上 write_text 会把 \n 翻成 \r\n，而基线（与仓库其余
        # JSON 一样）是 LF——换行符漂移会让下次 diff 整篇重排。
        path.write_bytes(text.encode("utf-8"))
        print(f"{mode}: 已重写 {path.relative_to(ROOT)}（{len(json.loads(text))} 个工具）")

    if stale:
        print(f"--check：{', '.join(stale)} 与当前 tools/list 不一致，请运行本脚本（不带 --check）重采")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
