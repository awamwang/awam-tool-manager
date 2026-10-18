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


# ---- 筛选维度（status / publisher）----
#
# 全部用 tmp_path 造数据：仓库 `data/tools/` 里 5 条清一色 `status: installed`，
# 拿真数据筛 `not_installed` 只会得到空集，那种断言红了也不知道为什么红。


def _filter_store(tmp_path):
    """四条组合覆盖筛选边界：有 status / 无 status / 另一种 status / publisher 带括号"""
    store = JsonStore(tmp_path / "data")
    for entry in (
        {"id": "a-installed", "name": "a-installed", "kind": "app",
         "status": "installed", "publisher": "voidtools", "tags": ["demo"]},
        {"id": "b-not", "name": "b-not", "kind": "app",
         "status": "not_installed", "publisher": "Valve Software", "tags": ["demo"]},
        # 既不写 status 也不写 publisher：筛 installed 必须命中（缺省口径与投影一致），
        # 筛任何 publisher 必须不命中
        {"id": "c-bare", "name": "c-bare", "kind": "mcp", "tags": ["demo"]},
        {"id": "d-unknown", "name": "d-unknown", "kind": "mcp",
         "status": "unknown", "publisher": "Some Corp (Beta)", "tags": ["demo"]},
    ):
        store.save_tool(entry["id"], entry)
    return store


def _ids(tools):
    """glob 出来的顺序不保证，一律比集合"""
    return {t["id"] for t in tools}


def test_status_filter_matches_the_same_default_as_the_projection(tmp_path):
    """条目没写 status 时按 installed 计——否则「列表里显示 installed、按 installed 筛不到」自相矛盾"""
    registry = ToolRegistry(_filter_store(tmp_path))

    assert _ids(registry.list_all_tools(status="installed")) == {"a-installed", "c-bare"}
    assert _ids(registry.list_all_tools(status="not_installed")) == {"b-not"}
    assert _ids(registry.list_all_tools(status="unknown")) == {"d-unknown"}


def test_status_filter_is_case_and_whitespace_tolerant(tmp_path):
    """筛选值走 strip + lower：上游模型写 ` INSTALLED ` 不该静默返回空集"""
    registry = ToolRegistry(_filter_store(tmp_path))
    assert _ids(registry.list_all_tools(status=" INSTALLED ")) == {"a-installed", "c-bare"}


def test_blank_filters_are_no_ops(tmp_path):
    """空串 / 纯空白 / None 一律当「不设该筛选」，不能把整张名单筛空

    纯空白这条是真踩过的：`if status:` 对 `"  "` 是 truthy，拿归一化出来的空串
    去精确匹配会筛出空集；`publisher` 因为子串语义又恰好成了 no-op——
    两个维度行为不一致，调用方只会认为筛选不可信。
    """
    registry = ToolRegistry(_filter_store(tmp_path))
    everything = _ids(registry.list_all_tools())

    assert everything == {"a-installed", "b-not", "c-bare", "d-unknown"}
    assert _ids(registry.list_all_tools(status="")) == everything
    assert _ids(registry.list_all_tools(status="   ")) == everything
    assert _ids(registry.list_all_tools(publisher="")) == everything
    assert _ids(registry.list_all_tools(publisher="   ")) == everything
    assert _ids(registry.list_all_tools(status=None, publisher=None)) == everything


def test_publisher_filter_is_substring_and_case_insensitive(tmp_path):
    """发布者写法多样（`Valve Software` / `Some Corp (Beta)`），子串才不用抄准原文；缺字段的条目永不命中"""
    registry = ToolRegistry(_filter_store(tmp_path))

    assert _ids(registry.list_all_tools(publisher="VOID")) == {"a-installed"}
    assert _ids(registry.list_all_tools(publisher="software")) == {"b-not"}
    assert _ids(registry.list_all_tools(publisher="(beta")) == {"d-unknown"}
    # 任何 publisher 值都不会把 c-bare（无 publisher）带进来
    assert "c-bare" not in _ids(registry.list_all_tools(publisher="o"))


def test_kind_status_publisher_compose(tmp_path):
    """三个维度是交集：kind 先按类型切，status/publisher 在其上继续收窄"""
    registry = ToolRegistry(_filter_store(tmp_path))

    assert _ids(registry.list_all_tools(kind="mcp", status="unknown")) == {"d-unknown"}
    assert _ids(registry.list_all_tools(kind="app", status="installed")) == {"a-installed"}
    # c-bare 是唯一「mcp 且按缺省算 installed」的条目
    assert _ids(registry.list_all_tools(kind="mcp", status="installed")) == {"c-bare"}
    # 交集为空时返回空集，不是「忽略掉筛不通的那个维度」
    assert registry.list_all_tools(status="installed", publisher="valve") == []


def test_search_tool_filters_narrow_after_keyword_match(tmp_path):
    """搜索与筛选正交：关键词先命中，status/publisher 再收窄"""
    registry = ToolRegistry(_filter_store(tmp_path))

    assert _ids(registry.search_tools("demo")) == {"a-installed", "b-not", "c-bare", "d-unknown"}
    assert _ids(registry.search_tools("demo", status="installed")) == {"a-installed", "c-bare"}
    assert _ids(registry.search_tools("demo", kind="mcp", publisher="corp")) == {"d-unknown"}
    # 关键词本身不匹配时，加筛选不会凭空多出结果
    assert registry.search_tools("nomatch", status="installed") == []


# ---- handler 透传 ----
#
# 用假 registry 记录收到的参数，而不是再跑一遍真筛选：筛选语义上面已经锁死，
# 这里要锁的是「arguments 里的 status/publisher 真的送到了 registry」——
# 漏传一个参数是这类改动最容易犯的错，且不会被语义测试发现。


class _RecordingRegistry:
    def __init__(self):
        self.calls = []

    def list_all_tools(self, kind=None, status=None, publisher=None):
        self.calls.append(("list", kind, status, publisher))
        return []

    def search_tools(self, keyword, kind=None, status=None, publisher=None):
        self.calls.append(("search", keyword, kind, status, publisher))
        return []


def _ctx_with(registry):
    from tool_specs import ToolContext

    return ToolContext(
        registry=registry,
        backend_manager=None,
        launcher_manager=None,
        store=None,
        log=lambda _m: None,
    )


def test_list_tools_handler_forwards_all_filter_dimensions():
    import asyncio

    from tool_specs import _handle_list_tools

    registry = _RecordingRegistry()
    ctx = _ctx_with(registry)

    asyncio.run(_handle_list_tools(
        {"kind": "app", "status": "installed", "publisher": "voidtools"}, ctx
    ))
    asyncio.run(_handle_list_tools({}, ctx))

    assert registry.calls == [
        ("list", "app", "installed", "voidtools"),
        # 不传就是 None，不能凭空填个默认值把结果筛窄
        ("list", None, None, None),
    ]


def test_search_tools_handler_forwards_all_filter_dimensions():
    import asyncio

    from tool_specs import _handle_search_tools

    registry = _RecordingRegistry()
    ctx = _ctx_with(registry)

    asyncio.run(_handle_search_tools(
        {"keyword": "搜索", "kind": "mcp", "status": "unknown", "publisher": "Corp"}, ctx
    ))
    asyncio.run(_handle_search_tools({"keyword": "搜索"}, ctx))

    assert registry.calls == [
        ("search", "搜索", "mcp", "unknown", "Corp"),
        ("search", "搜索", None, None, None),
    ]


def test_list_projection_shows_every_filterable_dimension(tmp_path):
    """能筛的维度必须同时可见，否则调用方只能猜 publisher 的写法

    用真 registry + 真数据走一遍 handler：这里锁的是**返回体形状**，
    上面两条锁的是**参数转发**，两回事。
    """
    import asyncio

    from tool_specs import _handle_list_tools, _handle_search_tools

    ctx = _ctx_with(ToolRegistry(_filter_store(tmp_path)))

    res = asyncio.run(_handle_list_tools({}, ctx))
    by_id = {t["id"]: t for t in res["tools"]}

    assert res["count"] == 4
    assert by_id["a-installed"]["publisher"] == "voidtools"
    assert by_id["a-installed"]["status"] == "installed"
    # 条目没写的维度投影成空串，不是缺键——调用方才能把可见值直接回填给筛选参数
    assert by_id["c-bare"]["publisher"] == ""
    assert by_id["c-bare"]["status"] == "installed"

    narrowed = asyncio.run(_handle_search_tools({"keyword": "demo"}, ctx))
    assert {t["publisher"] for t in narrowed["tools"]} == {
        "voidtools", "Valve Software", "", "Some Corp (Beta)"
    }

