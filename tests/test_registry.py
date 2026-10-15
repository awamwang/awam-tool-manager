"""测试工具注册中心"""
import sys
from pathlib import Path

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from store import JsonStore
from registry import ToolRegistry


def test_store_launchers():
    """测试启动器配置读取"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    launchers = store.list_launchers()
    assert "everything" in launchers, "everything 启动器应该存在"

    config = store.load_launcher("everything")
    assert config["id"] == "everything"
    assert config["installed"] is True


def test_registry_list_launchers():
    """测试列出启动器"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    registry = ToolRegistry(store)

    launchers = registry.list_launchers()
    assert len(launchers) >= 1
    assert any(l["id"] == "everything" for l in launchers)


def test_registry_search_tools():
    """测试搜索工具"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    registry = ToolRegistry(store)

    results = registry.search_tools("搜索")
    assert len(results) >= 1
    assert any(t["id"] == "everything:main" for t in results)


def test_registry_get_tool():
    """测试获取工具详情"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    registry = ToolRegistry(store)

    tool = registry.get_tool("everything:main")
    assert tool is not None
    assert tool["name"] == "Everything"
    assert tool["kind"] == "app"
    assert "launch" in tool["actions"]


def test_registry_list_tools():
    """测试列出所有工具"""
    data_dir = Path(__file__).parent.parent / "data"
    store = JsonStore(data_dir)
    registry = ToolRegistry(store)

    tools = registry.list_all_tools()
    assert len(tools) >= 1
    assert any(t["id"] == "everything:main" for t in tools)
