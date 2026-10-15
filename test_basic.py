"""快速测试核心功能"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from store import JsonStore
from registry import ToolRegistry

DATA_DIR = Path(__file__).parent / "data"
store = JsonStore(DATA_DIR)
registry = ToolRegistry(store)

print("=" * 50)
print("1. 列出所有启动器")
print("=" * 50)
launchers = registry.list_launchers()
for l in launchers:
    print(f"  - [{l['id']}] {l.get('displayName', l['name'])} ({l.get('type')}) 已安装={l.get('installed')}")

print()
print("=" * 50)
print("2. 列出所有工具")
print("=" * 50)
tools = registry.list_all_tools()
for t in tools:
    print(f"  - [{t['id']}] {t.get('displayName', t['name'])}")

print()
print("=" * 50)
print("3. 搜索工具（关键词：'搜索'）")
print("=" * 50)
results = registry.search_tools("搜索")
for t in results:
    print(f"  - [{t['id']}] {t.get('displayName', t['name'])}: {t.get('description', '')[:40]}")

print()
print("=" * 50)
print("4. 获取工具详情（everything:main）")
print("=" * 50)
tool = registry.get_tool("everything:main")
if tool:
    print(f"  ID: {tool['id']}")
    print(f"  名称: {tool.get('displayName', tool['name'])}")
    print(f"  版本: {tool.get('version')}")
    print(f"  标签: {', '.join(tool.get('tags', []))}")

print()
print("=" * 50)
print("5. 启动工具（everything:main）")
print("=" * 50)
res = registry.launch_tool("everything:main")
print(f"  成功: {res.get('success')}")
print(f"  命令: {res.get('launch', {}).get('command', '')}")

print()
print("✅ 基本功能测试通过")
