"""测试工具注册中心"""
import sys
from pathlib import Path

# 添加 src 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from store import JsonStore
from registry import ToolRegistry


def test_store_tools():
    """测试工具条目读取"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    tools = store.list_tools()
    assert len(tools) >= 1
    # 应该有 everything 和 everything-search-mcp
    ids = [t["id"] for t in tools]
    assert "everything" in ids
    assert "everything-search-mcp" in ids


def test_registry_list_tools():
    """测试列出所有工具"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    registry = ToolRegistry(store)

    tools = registry.list_all_tools()
    assert len(tools) >= 1

    # 按类型筛选
    app_tools = registry.list_all_tools(kind="app")
    assert any(t["id"] == "everything" for t in app_tools)

    mcp_tools = registry.list_all_tools(kind="mcp")
    assert any(t["id"] == "everything-search-mcp" for t in mcp_tools)


def test_registry_search_tools():
    """测试搜索工具"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    registry = ToolRegistry(store)

    results = registry.search_tools("搜索")
    assert len(results) >= 1
    assert any(t["id"] == "everything" for t in results)


def test_registry_get_tool():
    """测试获取工具详情"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    registry = ToolRegistry(store)

    tool = registry.get_tool("everything")
    assert tool is not None
    assert tool["kind"] == "app"
    assert tool["status"] == "installed"


def test_registry_backend_tools(tmp_path):
    """registry 能读到后端 MCP 的子工具清单

    自己造数据而不是读仓库里的 `data/backend-tools/`：后者是**运行时缓存**
    （2026-10-16 已迁到本机应用数据目录，见 runtime_paths.py），依赖它就等于
    让单测依赖「本机同步过后端」这个前提——换个机器、清一次缓存就红。
    """
    store = JsonStore(tmp_path / "data")
    store.save_backend_tools(
        "everything-search-mcp",
        [
            {"name": "search_files", "description": "搜索文件"},
            {"name": "get_file_info", "description": "文件信息"},
        ],
    )
    registry = ToolRegistry(store)

    backend_tools = registry.list_backend_tools("everything-search-mcp")
    assert len(backend_tools) >= 1
    tool_names = [t["name"] for t in backend_tools]
    assert "search_files" in tool_names
