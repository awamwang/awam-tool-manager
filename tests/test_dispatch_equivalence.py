"""dispatch 表驱动化的等价性守卫（P1-04 M04）。

钉住两件事：
1. 未知工具名仍走 `unknown_tool`——错误消息与 errorType 与重构前
   （server.py 旧 if/elif 全链落空后的 else 分支）逐字一致；
2. 15 个已注册名字都不落 `unknown_tool`——查表分发没漏搬任何一个 handler。

**NE-1 留痕（U6 裁决要求）**：重构后 `json.dumps` 统一带 `default=str`
（旧代码只有 5 个工具带）。这是**有意的单向放宽**：这 10 个受影响工具的载荷
全部由「JSON 文件读出的数据 + 字面量」构成，`default=str` 在可达输入上是
no-op；唯一理论差异是「以前会抛 TypeError → 被 call_tool 转成 internal_error
的情况，现在会成功序列化」，方向是更不容易失败。不启用 `strict_dumps`
回退方案（那会把隐藏的不一致固化成永久分支逻辑）。

探针只验证「查表分发接上了」，不真连后端 / 启动器：backend 与 launcher
两个 manager 的方法全部打成良性失败（返回体经 ensure_success 转 ToolError，
errorType=probe_blocked ≠ unknown_tool）。ctx 持有的是同一批 manager 对象，
monkeypatch 改对象属性即生效。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402
from server import ToolError  # noqa: E402
from tool_specs import SERVER_TOOL_SPECS  # noqa: E402


def run(coro):
    """统一的事件循环入口"""
    return asyncio.run(coro)


def payload_of(exc_info) -> dict:
    """取出 ToolError 携带的 JSON 载荷"""
    return json.loads(str(exc_info.value))


def test_unknown_tool_raises_unknown_tool():
    """未知工具：errorType 与消息文案和重构前逐字一致"""
    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("__no_such_tool__", {}))

    payload = payload_of(exc)
    assert payload["success"] is False
    assert payload["errorType"] == "unknown_tool"
    assert payload["error"] == "未知工具: __no_such_tool__"
    assert payload["toolName"] == "__no_such_tool__"


@pytest.fixture()
def _probe_managers(monkeypatch):
    """把两个 manager 的动作类方法打成良性失败，探针不产生真实副作用。

    fake 的同步/异步属性必须与原方法一致：handler 对异步 manager 方法有
    `await`，对同步方法直接调用。
    """

    def _fail_sync(*args, **kwargs):
        return {"success": False, "error": "probe", "errorType": "probe_blocked"}

    async def _fail_async(*args, **kwargs):
        return {"success": False, "error": "probe", "errorType": "probe_blocked"}

    def _empty_list(*args, **kwargs):
        return []

    # handler 里被 await 的异步方法
    for meth in (
        "call_backend_tool",
        "list_backend_tools",
        "start_backend",
        "stop_backend",
        "sync_tools_catalog",
    ):
        monkeypatch.setattr(server.backend_manager, meth, _fail_async)
    # handler 里直接调用的同步方法
    for meth in (
        "get_backend_status",
        "get_backend_logs",
        "set_backend_log_level",
    ):
        monkeypatch.setattr(server.backend_manager, meth, _fail_sync)
    monkeypatch.setattr(server.launcher_manager, "list_actions", _empty_list)
    monkeypatch.setattr(server.launcher_manager, "sync", _fail_sync)
    monkeypatch.setattr(server.launcher_manager, "run_action", _fail_async)


def test_every_spec_name_is_dispatchable(_probe_managers, monkeypatch):
    """每个 spec 名字各调一次 dispatch_tool：允许业务失败，只禁止 unknown_tool

    声明表含 lazy/default/full 三层工具，这里切到 full 模式，确保全部
    spec 的 handler 全部接上；暴露切片本身由 test_exposure.py 单独守卫。
    """
    monkeypatch.setattr(server, "EXPOSURE_MODE", "full")
    for spec in SERVER_TOOL_SPECS:
        try:
            run(server.dispatch_tool(spec.name, {"tool_id": "__probe_no_such__"}))
        except ToolError as exc:
            payload = json.loads(str(exc))
            assert payload.get("errorType") != "unknown_tool", (
                f"{spec.name} 落到了 unknown_tool：handler 没接上"
            )
