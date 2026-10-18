"""P3 使用统计：`usage.py` 单元面 + dispatch 埋点集成面。

分两层写是有原因的：`record_call` / `aggregate` 单测全绿**并不代表调用真的
被记进了日志**——埋点长在 `server.dispatch_tool` 里，只有走真实分发才证明
「成功 / 未知工具 / 被策略拒绝 / handler 抛业务错 / 意外异常」五条出口
各记了一行。所以本文件既有纯函数用例，也有 dispatch 级别的金丝雀用例。

`usage.USAGE_LOG` 一律 monkeypatch 到 tmp_path（模块全局，按模块引用才能在
调用点取到新值；`from usage import USAGE_LOG` 那种按值 import 打不中）。
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402
import usage  # noqa: E402
from errors import ToolError  # noqa: E402
from tool_specs import _handle_get_usage_stats  # noqa: E402


def run(coro):
    """统一的事件循环入口（本仓库不装 pytest-asyncio）"""
    return asyncio.run(coro)


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    """把统计日志钉到 tmp，返回该路径"""
    path = tmp_path / "usage.log"
    monkeypatch.setattr(usage, "USAGE_LOG", path)
    return path


def rows_of(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def ts(days_ago: int) -> str:
    day = datetime.now().astimezone().date() - timedelta(days=days_ago)
    stamp = datetime.combine(day, datetime.min.time()).replace(hour=12)
    return stamp.astimezone().isoformat(timespec="seconds")


def row(tool="list_tools", *, ok=True, target="", ms=1, days_ago=0, error_type=None):
    data = {"tool": tool, "ok": ok, "ms": ms, "target": target, "ts": ts(days_ago)}
    if error_type:
        data["errorType"] = error_type
    return data


# ---------------- record_call ----------------


def test_record_call_row_shape(log_path):
    usage.record_call("launch_tool", {"tool_id": "everything"}, ok=True, duration_ms=42)
    usage.record_call(
        "stop_backend", {"tool_id": "vscode"}, ok=False, error_type="runtime_error", duration_ms=7
    )

    rows = rows_of(log_path)
    assert [r["tool"] for r in rows] == ["launch_tool", "stop_backend"]
    assert set(rows[0]) == {"ts", "tool", "target", "ok", "ms"}
    assert rows[0]["target"] == "everything" and rows[0]["ms"] == 42
    # errorType 只在失败行出现：成功行带 `"errorType": null` 会让「有没有错」
    # 在数据里变成三态（字段缺失 / null / 有值）
    assert "errorType" not in rows[0]
    assert rows[1]["errorType"] == "runtime_error"


def test_record_call_never_captures_arguments(log_path):
    """参数刻意不落盘：关键词、本机路径、动作正文都可能出现在参数里"""
    usage.record_call("search_tools", {"keyword": "机密词", "tool_id": "x"}, ok=True)

    assert set(rows_of(log_path)[0]) == {"ts", "tool", "target", "ok", "ms"}
    assert "机密词" not in log_path.read_text(encoding="utf-8")


def test_record_call_tolerates_missing_and_odd_arguments(log_path):
    usage.record_call("list_tools", None, ok=True)
    usage.record_call("list_tools", {"tool_id": None}, ok=True)
    usage.record_call("list_tools", {"tool_id": 12}, ok=True)

    assert [r["target"] for r in rows_of(log_path)] == ["", "", "12"]


def test_record_call_swallows_write_failure(tmp_path, monkeypatch, capsys):
    """统计写不进去 ≠ 工具调用失败：目标是已存在的目录时 open 抛 IsADirectoryError"""
    monkeypatch.setattr(usage, "USAGE_LOG", tmp_path)

    usage.record_call("list_tools", {}, ok=True)

    assert "统计落盘失败" in capsys.readouterr().err


# ---------------- error_type_from ----------------


@pytest.mark.parametrize(
    "body,expected",
    [
        (json.dumps({"success": False, "errorType": "not_found"}), "not_found"),
        (json.dumps({"success": False}), "unknown_error"),
        ("not json at all", "unknown_error"),
        (json.dumps(["a", "list"]), "unknown_error"),
    ],
)
def test_error_type_from(body, expected):
    assert usage.error_type_from(ToolError(body)) == expected


# ---------------- 读取与窗口 ----------------


def test_read_rows_missing_file_is_empty(log_path):
    assert usage.read_rows() == []


def test_read_rows_skips_bad_lines(log_path, capsys):
    log_path.write_text(
        '{"tool": "list_tools", "ok": true, "ts": "2026-10-18T10:00:00"}\n'
        "\n"
        "{ broken json\n"
        '"just a string"\n'
        '{"tool": "search_tools", "ok": false, "ts": "2026-10-18T10:00:01"}\n',
        encoding="utf-8",
    )

    rows = usage.read_rows()

    assert [r["tool"] for r in rows] == ["list_tools", "search_tools"]
    # 坏行汇总告警一次，而不是静默丢掉：静默丢会让「数少了」无从归因
    assert "2 行无法解析" in capsys.readouterr().err


@pytest.mark.parametrize(
    "given,expected",
    [(None, usage.DEFAULT_DAYS), (0, 1), (-5, 1), (3, 3), (9999, usage.MAX_DAYS)],
)
def test_normalize_days_clamps(given, expected):
    assert usage.normalize_days(given) == expected


def test_window_self_documents_range():
    today = datetime.now().astimezone().date()

    assert usage.window(3) == {
        "days": 3,
        "since": (today - timedelta(days=2)).isoformat(),
        "until": today.isoformat(),
    }


def test_read_rows_windows_by_calendar_day(log_path):
    # 8 天前的行：7 天窗口含今天，所以它正好落在窗外
    write_rows(log_path, [row(tool=f"t{d}", days_ago=d) for d in (0, 1, 6, 7)])

    assert len(usage.read_rows(7)) == 3
    assert len(usage.read_rows(1)) == 1
    assert len(usage.read_rows()) == 4


def test_read_rows_keeps_undated_lines_out_of_window(log_path):
    log_path.write_text('{"tool": "t", "ok": true}\n', encoding="utf-8")

    assert usage.read_rows() == [{"tool": "t", "ok": True}]
    assert usage.read_rows(7) == []


# ---------------- aggregate ----------------


def test_aggregate_shapes():
    rows = [
        row("launch_tool", target="everything", ms=100, days_ago=1),
        row("launch_tool", ok=False, target="everything", ms=20, days_ago=1,
            error_type="runtime_error"),
        row("launch_tool", target="vscode", ms=30),
        row("list_tools", ms=5),
        row("stop_backend", ok=False, ms=3, error_type="runtime_error"),
    ]

    stats = usage.aggregate(rows)

    assert (stats["total"], stats["success"], stats["failed"]) == (5, 3, 2)
    assert stats["byName"][0] == {
        "tool": "launch_tool", "count": 3, "failed": 1, "msMax": 100, "msAvg": 50.0,
    }
    assert [b["tool"] for b in stats["byName"]] == ["launch_tool", "list_tools", "stop_backend"]
    # targets 只收有 tool_id 的调用，按「哪个登记工具被动得多」排序
    assert stats["targets"] == [
        {"toolId": "everything", "count": 2, "failed": 1},
        {"toolId": "vscode", "count": 1, "failed": 0},
    ]
    assert stats["byDay"] == [
        {"day": ts(1)[:10], "count": 2},
        {"day": ts(0)[:10], "count": 3},
    ]
    assert stats["errorTypes"] == [{"errorType": "runtime_error", "count": 2}]


def test_aggregate_empty_rows():
    assert usage.aggregate([]) == {
        "total": 0, "success": 0, "failed": 0,
        "byName": [], "targets": [], "byDay": [], "errorTypes": [],
    }


# ---------------- dispatch 埋点（集成面）----------------


def test_dispatch_records_success_and_failure_rows(log_path):
    run(server.dispatch_tool("list_tools", {}))
    with pytest.raises(ToolError):
        run(server.dispatch_tool("__no_such_tool__", {}))
    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("get_tool_info", {"tool_id": "__no_such_id__"}))

    by_tool = {r["tool"]: r for r in rows_of(log_path)}
    assert len(rows_of(log_path)) == 3
    assert by_tool["list_tools"]["ok"] is True and "errorType" not in by_tool["list_tools"]
    assert (by_tool["__no_such_tool__"]["ok"],
            by_tool["__no_such_tool__"]["errorType"],
            by_tool["__no_such_tool__"]["target"]) == (False, "unknown_tool", "")
    # handler 抛的业务错：errorType 与客户端看到的载荷同源，不另编一份
    assert by_tool["get_tool_info"]["ok"] is False
    assert by_tool["get_tool_info"]["errorType"] == json.loads(str(exc.value))["errorType"]
    assert by_tool["get_tool_info"]["target"] == "__no_such_id__"


def test_dispatch_records_policy_denial(log_path, monkeypatch):
    monkeypatch.setattr(
        server, "check_call", lambda name: {"reason": "策略拒绝：演示", "rule_hit": "deny"}
    )

    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("list_tools", {}))

    [recorded] = rows_of(log_path)
    assert recorded["tool"] == "list_tools"
    assert recorded["ok"] is False
    assert recorded["errorType"] == "permission_denied"
    assert json.loads(str(exc.value))["errorType"] == "permission_denied"


def test_dispatch_records_internal_error(log_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise ValueError("意外炸了")

    # spec 是 frozen dataclass，换不掉 handler → 从 handler 依赖的对象下手，
    # 让它抛一个既非 ToolError 也非已分类异常的意外错
    monkeypatch.setattr(server._TOOL_CONTEXT.registry, "list_all_tools", _boom)

    with pytest.raises(ValueError):
        run(server.dispatch_tool("list_tools", {}))

    [recorded] = rows_of(log_path)
    assert (recorded["ok"], recorded["errorType"]) == (False, "internal_error")


def test_dispatch_instrumentation_does_not_change_contract(log_path):
    """埋点是旁路：返回值仍是那份 JSON 文本，异常仍是原对象"""
    text = run(server.dispatch_tool("list_tools", {}))
    assert isinstance(text, str) and json.loads(text)["success"] is True

    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("__no_such_tool__", {}))
    assert json.loads(str(exc.value))["errorType"] == "unknown_tool"


# ---------------- get_usage_stats handler ----------------


def test_usage_stats_handler_reports_window_and_rows(log_path):
    write_rows(log_path, [
        row("launch_tool", target="everything", ms=12),
        row("launch_tool", target="everything", ms=12),
        row("list_tools", ok=False, ms=3, error_type="not_found"),
    ])

    payload = run(_handle_get_usage_stats({}, None))

    assert payload["success"] is True  # 信封字段必须是 bool，不能是成功次数
    assert payload["window"] == usage.window(None)
    assert (payload["stats"]["total"], payload["stats"]["failed"]) == (3, 1)
    assert payload["stats"]["success"] == 2
    assert payload["stats"]["targets"] == [{"toolId": "everything", "count": 2, "failed": 0}]
    assert payload["stats"]["byName"][0]["tool"] == "launch_tool"
    assert payload["logPath"] == str(log_path)
    assert "note" not in payload
    assert "toolId" not in payload


def test_usage_stats_handler_narrows_by_tool_id(log_path):
    write_rows(log_path, [
        row("launch_tool", target="everything"),
        row("launch_tool", target="vscode"),
    ])

    payload = run(_handle_get_usage_stats({"tool_id": "  everything  "}, None))

    assert payload["toolId"] == "everything"
    assert payload["stats"]["total"] == 1
    assert payload["stats"]["targets"] == [{"toolId": "everything", "count": 1, "failed": 0}]


def test_usage_stats_handler_explains_empty_window(log_path):
    """0 次调用必须自带口径说明，否则会被读成「这工具没人用」"""
    payload = run(_handle_get_usage_stats({"days": 30}, None))

    assert payload["stats"]["total"] == 0
    assert payload["window"]["days"] == 30
    assert "经本 server 分发" in payload["note"]


def test_usage_stats_is_read_only_spec():
    """新工具进声明表：只读注解 + 参数面（days / tool_id，无必填）"""
    from tool_specs import SERVER_TOOL_SPECS, build_mcp_tools

    names = [s.name for s in SERVER_TOOL_SPECS]
    assert names[-1] == "get_usage_stats"
    spec = SERVER_TOOL_SPECS[-1]
    assert spec.annotations == {"readOnlyHint": True, "destructiveHint": False}
    assert set(spec.input_schema["properties"]) == {"days", "tool_id"}
    assert "required" not in spec.input_schema
    assert build_mcp_tools()[-1].name == "get_usage_stats"
