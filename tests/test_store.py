"""测试 JsonStore 的容错与原子写

这一层之所以要专门用测例钉住：data/ 目录里的 JSON 是人工编辑 + 程序写入
共用的，任何一个坏都不应该让整个 MCP Server 起不来。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from store import JsonStore  # noqa: E402


def _make_store(tmp_path: Path) -> JsonStore:
    return JsonStore(tmp_path)


def test_load_missing_file_returns_none(tmp_path):
    """文件不存在是正常情况，等同于「尚未配置」"""
    store = _make_store(tmp_path)
    assert store.load_json(tmp_path / "nope.json") is None


def test_damaged_json_is_skipped_not_raised(tmp_path):
    """D12：坏 JSON 返回 None，不抛异常"""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "broken.json").write_text('{"id": "broken", "name": }', encoding="utf-8")

    store = _make_store(tmp_path)
    assert store.load_json(tools_dir / "broken.json") is None


def test_one_bad_file_does_not_break_listing(tmp_path):
    """D12：一个坏文件不能拖垮 list_tools —— 其他工具仍要可用"""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
    (tools_dir / "good.json").write_text(
        json.dumps({"id": "good", "kind": "app"}), encoding="utf-8"
    )

    store = _make_store(tmp_path)
    tools = store.list_tools()

    assert len(tools) == 1, f"坏文件应被跳过，实际返回: {tools}"
    assert tools[0]["id"] == "good"


def test_non_object_json_is_skipped(tmp_path):
    """顶层不是对象（例如数组）同样跳过，避免下游 t.get() 炸 AttributeError"""
    f = tmp_path / "arr.json"
    f.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    store = _make_store(tmp_path)
    assert store.load_json(f) is None


def test_save_json_is_atomic_and_leaves_no_temp(tmp_path):
    """D13：写完后数据目录里不应残留临时文件"""
    store = _make_store(tmp_path)
    target = tmp_path / "tools" / "x.json"
    store.save_json(target, {"id": "x"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"id": "x"}
    leftovers = [p.name for p in target.parent.iterdir() if p.name != "x.json"]
    assert leftovers == [], f"残留临时文件: {leftovers}"


def test_failed_write_keeps_old_content(tmp_path):
    """D13：写入失败时旧内容必须完好，不能被截断成空文件"""
    store = _make_store(tmp_path)
    target = tmp_path / "tools" / "keep.json"
    store.save_json(target, {"version": 1})

    try:
        store.save_json(target, {"bad": object()})  # 不可序列化 → 必然抛错
    except TypeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("预期不可序列化数据写入会失败")

    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}
    leftovers = [p.name for p in target.parent.iterdir() if p.name != "keep.json"]
    assert leftovers == []


def test_overwrite_existing_file(tmp_path):
    """重复保存（换目录区县 bigger payload）应完整覆盖，不出现拼接残留"""
    store = _make_store(tmp_path)
    target = tmp_path / "tools" / "y.json"
    store.save_json(target, {"v": "短"})
    store.save_json(target, {"v": "很长的一段内容" * 50})

    assert json.loads(target.read_text(encoding="utf-8"))["v"].startswith("很长")


def test_last_synced_is_real_timestamp(tmp_path):
    """D15：lastSynced 必须是可解析的时间，不再是字面量 "now" """
    store = _make_store(tmp_path)
    store.save_backend_tools("demo", [{"name": "t"}])

    data = json.loads(
        (tmp_path / "backend-tools" / "demo.json").read_text(encoding="utf-8")
    )
    last_synced = data["lastSynced"]

    assert last_synced != "now"
    parsed = time.strptime(last_synced, "%Y-%m-%dT%H:%M:%SZ")
    assert parsed is not None
    assert data["tools"] == [{"name": "t"}]
