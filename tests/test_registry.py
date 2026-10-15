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


def test_registry_backend_tools():
    """测试后端 MCP 子工具"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    registry = ToolRegistry(store)

    backend_tools = registry.list_backend_tools("everything-search-mcp")
    assert len(backend_tools) >= 1
    tool_names = [t["name"] for t in backend_tools]
    assert "search_files" in tool_names
