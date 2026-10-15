"""测试注册中心的语义修正与新增的合规策略（D18 / A1 / A2）

- D18：搜索要覆盖 `id`
- A1：不合规的工具条目被跳过，而不是让 `t["id"]` 在运行期炸掉
- A2：`destructive` 不再是装饰字段，转发前有门禁；annotations 已下发
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import server  # noqa: E402
from registry import ToolRegistry  # noqa: E402
from schema import validate_tool_entry  # noqa: E402
from store import JsonStore  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _write(tmp_path, tool_id: str, data: dict):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / f"{tool_id}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _valid_app(tool_id: str = "demo") -> dict:
    return {
        "id": tool_id,
        "name": tool_id,
        "displayName": "演示工具",
        "kind": "app",
        "description": "用于测试的条目",
        "launch": {"type": "executable", "command": "demo.exe", "args": []},
        "readOnly": True,
        "destructive": False,
    }


# ---- A1：条目校验 ----


def test_validate_accepts_valid_entry():
    assert validate_tool_entry(_valid_app()) is None


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.pop("id"), "id"),
    (lambda d: d.pop("name"), "name"),
    (lambda d: d.update(kind="whatever"), "kind"),
    (lambda d: d.update(id="含 空格"), "id"),
])
def test_validate_rejects_broken_entry(mutate, expect):
    data = _valid_app()
    mutate(data)
    error = validate_tool_entry(data)
    assert error is not None, f"{expect} 非法却通过了校验"
    assert expect in error


def test_validate_requires_command_for_mcp():
    data = _valid_app("mcp-demo")
    data["kind"] = "mcp"
    data["connection"] = {"type": "stdio"}  # 缺 command

    assert validate_tool_entry(data) is not None

    data["connection"]["command"] = "cmd.exe"
    assert validate_tool_entry(data) is None


def test_invalid_entries_are_skipped(tmp_path):
    """一条脏数据不能让 tools/list 整体失败"""
    _write(tmp_path, "good", _valid_app("good"))
    _write(tmp_path, "bad", {"name": "缺 id 的条目", "kind": "app"})
    _write(tmp_path, "badkind", {**_valid_app("badkind"), "kind": "未知类型"})

    registry = ToolRegistry(JsonStore(tmp_path))
    ids = [t["id"] for t in registry.list_all_tools()]

    assert ids == ["good"], f"不合规条目应被跳过，实际: {ids}"


def test_get_tool_returns_none_for_invalid(tmp_path):
    _write(tmp_path, "bad", {"name": "缺 id", "kind": "app"})

    registry = ToolRegistry(JsonStore(tmp_path))
    assert registry.get_tool("bad") is None


def test_launch_tool_errors_carry_error_type(tmp_path):
    """失败结果也要有 errorType，才能被 server 层统一转成协议错误"""
    _write(tmp_path, "nolanch", {**_valid_app("nolanch"), "launch": {}})

    registry = ToolRegistry(JsonStore(tmp_path))
    missing = registry.launch_tool("ghost")
    no_launch = registry.launch_tool("nolanch")

    assert missing["errorType"] == "not_found"
    assert no_launch["errorType"] == "config_error"


# ---- D18：搜索覆盖 id ----


def test_search_matches_tool_id():
    """ID 里带关键词时（everything-search-mcp）必须搜得到"""
    registry = ToolRegistry(JsonStore(Path(__file__).parent.parent / "data"))
    results = registry.search_tools("everything-search-mcp")

    assert any(t["id"] == "everything-search-mcp" for t in results)


# ---- A2：destructive 门禁与 annotations ----


def test_destructive_call_is_blocked_without_confirmation(monkeypatch):
    monkeypatch.setattr(
        server.registry,
        "get_tool",
        lambda tool_id: {"id": tool_id, "kind": "mcp", "destructive": True},
    )

    async def should_not_be_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("未确认的 destructive 调用不应被转发")

    monkeypatch.setattr(server.backend_manager, "call_backend_tool", should_not_be_called)

    with pytest.raises(server.ToolError) as exc:
        run(server.dispatch_tool(
            "call_backend_tool",
            {"tool_id": "boom", "tool_name": "nuke"},
        ))

    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "destructive_blocked"


def test_destructive_call_allowed_with_flag(monkeypatch):
    monkeypatch.setattr(
        server.registry,
        "get_tool",
        lambda tool_id: {"id": tool_id, "kind": "mcp", "destructive": True},
    )

    async def fake_call(tool_id, tool_name, arguments):
        return {"success": True, "result": "done"}

    monkeypatch.setattr(server.backend_manager, "call_backend_tool", fake_call)

    text = run(server.dispatch_tool(
        "call_backend_tool",
        {"tool_id": "boom", "tool_name": "nuke", "allowDestructive": True},
    ))
    payload = json.loads(text)

    assert payload["success"] is True
    assert "destructive" in payload["warning"]


def test_read_only_tools_are_annotated():
    """只读工具必须带 readOnlyHint，转发类只标 openWorldHint"""
    tools = {t.name: t for t in run(server.list_tools())}

    for name in ("search_tools", "list_tools", "get_tool_info", "get_backend_status"):
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} 缺少 annotations"
        assert annotations.readOnlyHint is True

    forwarded = tools["call_backend_tool"].annotations
    assert forwarded is not None
    assert forwarded.openWorldHint is True
    assert forwarded.readOnlyHint is None, "副作用静态不可知，不应断言只读"
