"""`src/app_launcher.py` 的守卫（`launch_tool` execute=true 的执行路径）。

探针脚本全部现场写进 tmp——被测进程只接触 tmp 世界（e2e 同款原则），
绝不拿仓库里或机器上真实的 Everything.exe 开刀。三个探针：

- `probe_ok.py`：写 marker 文件后退出码 0（启动即成功路径）；
- `probe_fail.py`：直接退出码 3（启动即失败路径，errorType=runtime_error）；
- `probe_sleep.py`：写 marker 后睡 30s（观察窗口超时 → running=true 路径）。

另外钉住两件容易腐烂的事：`_clamp_wait` 的归一化（默认 / 封顶 / 负数），
以及保活注册表的修剪（无界增长是内存泄漏，退出进程必须被清掉）。
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import app_launcher
from app_launcher import LAUNCH_WAIT_DEFAULT, LAUNCH_WAIT_MAX, launch_app
from registry import ToolRegistry
from store import JsonStore
from tool_specs import ToolContext, _handle_launch_tool

# 探针源码（现场写进 tmp，不落仓库）
PROBE_OK = """import sys
from pathlib import Path
Path(sys.argv[1]).write_text("ok", encoding="utf-8")
"""

PROBE_FAIL = """import sys
sys.exit(3)
"""

PROBE_SLEEP = """import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text("sleeping", encoding="utf-8")
time.sleep(30)
"""


def _write_probe(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _launch(launch, tool_id="probe", tool_name="探针", wait=None):
    return asyncio.run(launch_app(launch, tool_id, tool_name, wait))


def test_launch_app_executes_and_reports_exit_code(tmp_path):
    """ok 探针：真的被 spawn（marker 落地）、exitCode=0、running=False"""
    script = _write_probe(tmp_path, "probe_ok.py", PROBE_OK)
    marker = tmp_path / "marker.txt"

    result = _launch(
        {"type": "executable", "command": sys.executable, "args": [str(script), str(marker)]},
        wait=LAUNCH_WAIT_MAX,  # 沙箱子进程可能被拖慢，窗口给足
    )

    assert result["success"] is True, result
    assert result["executed"] is True
    assert result["running"] is False
    assert result["exitCode"] == 0
    assert result["pid"] > 0
    assert "已启动" in result["message"]
    assert marker.exists(), "ok 探针没被真正执行（marker 未落地）"
    assert marker.read_text(encoding="utf-8") == "ok"


def test_launch_app_nonzero_exit_is_failure(tmp_path):
    """fail 探针：进程跑了但退出码非 0 → success=false + runtime_error"""
    script = _write_probe(tmp_path, "probe_fail.py", PROBE_FAIL)

    result = _launch(
        {"type": "executable", "command": sys.executable, "args": [str(script)]},
        wait=LAUNCH_WAIT_MAX,
    )

    assert result["success"] is False, result
    assert result["errorType"] == "runtime_error"
    assert result["executed"] is True, "进程确实跑起来了，executed 不该是 false"
    assert result["exitCode"] == 3
    assert "退出码 3" in result["error"]


def test_launch_app_reports_running_when_window_expires(tmp_path):
    """sleep 探针：窗口超时仍运行 → running=true、exitCode=None、放任不杀"""
    script = _write_probe(tmp_path, "probe_sleep.py", PROBE_SLEEP)
    marker = tmp_path / "marker.txt"

    result = _launch(
        {"type": "executable", "command": sys.executable, "args": [str(script), str(marker)]},
        wait=1,
    )
    try:
        assert result["success"] is True, result
        assert result["running"] is True
        assert result["exitCode"] is None
        assert result["pid"] > 0
        assert marker.exists(), "sleep 探针没被真正执行（marker 未落地）"
        # 保活注册表必须持有它——否则 transport 被 GC 时进程会被杀掉
        assert any(p.pid == result["pid"] for p in app_launcher._LAUNCHED), (
            "运行中的进程没进保活注册表（transport 一旦被 GC 回收就会杀掉它）"
        )
    finally:
        # 探针睡 30s，用例收尾必须杀掉，不能留给机器
        os.kill(result["pid"], signal.SIGTERM)


def test_launch_app_registry_prunes_exited_processes(tmp_path):
    """保活注册表修剪：连续启动 3 个会自行退出的探针，表里最多多 1 条。

    不修剪的话每次启动都追加一条，服务跑久了就是无界增长（内存泄漏）。
    用增量口径而非绝对值：注册表是模块级的，前面用例的残留（如被 kill 的
    sleep 探针——它的事件循环已关，returncode 永远补不上）不该让本用例
    依赖执行顺序。
    """
    script = _write_probe(tmp_path, "probe_ok.py", PROBE_OK)
    before = len(app_launcher._LAUNCHED)

    for i in range(3):
        marker = tmp_path / f"marker{i}.txt"
        result = _launch(
            {"type": "executable", "command": sys.executable, "args": [str(script), str(marker)]},
            wait=LAUNCH_WAIT_MAX,
        )
        assert result["success"] is True, result
        assert marker.exists()

    # 前两个已在后续启动时被修剪；第三个虽已退出但还没轮到修剪，留 1 条
    assert len(app_launcher._LAUNCHED) <= before + 1, (
        f"注册表未按预期修剪：启动前 {before} 条，3 次启动后 "
        f"{len(app_launcher._LAUNCHED)} 条"
    )


def test_missing_command_is_config_error():
    """命令不存在 → config_error，且 executed=false（什么都没跑）"""
    result = _launch(
        {"type": "executable", "command": "awam-no-such-exe-xyz", "args": []},
        wait=1,
    )
    assert result["success"] is False
    assert result["errorType"] == "config_error"
    assert result["executed"] is False
    assert "不存在或不在 PATH" in result["error"]


def test_missing_command_field_is_config_error():
    """条目没写 launch.command → config_error（不是 KeyError）"""
    result = _launch({"type": "executable"}, wait=1)
    assert result["success"] is False
    assert result["errorType"] == "config_error"
    assert "launch.command" in result["error"]


def test_unsupported_launch_type_is_config_error():
    """protocol / command 等类型暂不支持 → config_error（不擅自 shell 化）"""
    result = _launch({"type": "protocol", "command": "steam://", "args": []}, wait=1)
    assert result["success"] is False
    assert result["errorType"] == "config_error"
    assert "暂不支持的启动类型" in result["error"]

    result = _launch({"type": "", "command": sys.executable, "args": []}, wait=1)
    assert result["success"] is False
    assert result["errorType"] == "config_error"


def test_clamp_wait_normalization():
    """wait 归一化：None→默认、非数值→默认、负数→0、超封顶→封顶"""
    assert app_launcher._clamp_wait(None) == LAUNCH_WAIT_DEFAULT
    assert app_launcher._clamp_wait("不是数字") == LAUNCH_WAIT_DEFAULT
    assert app_launcher._clamp_wait(-5) == 0.0
    assert app_launcher._clamp_wait(0) == 0.0
    assert app_launcher._clamp_wait(2.5) == 2.5
    assert app_launcher._clamp_wait(99999) == LAUNCH_WAIT_MAX


def test_wait_zero_reports_running_without_waiting(tmp_path):
    """wait<=0：不等待，一律按运行中报告（无法区分已退出）"""
    script = _write_probe(tmp_path, "probe_ok.py", PROBE_OK)
    result = _launch(
        {"type": "executable", "command": sys.executable, "args": [str(script)]},
        wait=0,
    )
    assert result["success"] is True
    assert result["running"] is True
    assert result["exitCode"] is None
    assert "未等待" in result["message"]


def test_args_are_coerced_to_strings(tmp_path):
    """args 里的非字符串元素（如 int）被强转，不把 TypeError 暴露给客户端"""
    script = _write_probe(tmp_path, "probe_ok.py", PROBE_OK)
    marker = tmp_path / "marker.txt"
    result = _launch(
        {
            "type": "executable",
            "command": sys.executable,
            "args": [str(script), str(marker), 7],  # 7 是非字符串元素
        },
        wait=LAUNCH_WAIT_MAX,
    )
    assert result["success"] is True, result
    assert marker.exists()


# ---- handler 级：_handle_launch_tool 的 execute 分支与门禁 ----
#
# 直接构造 ToolContext（tmp JsonStore），不走 server.dispatch_tool——
# 后者用仓库真实 data/tools/，execute=true 会把本机 Everything 真拉起来。
# dispatch / 协议层的覆盖在 e2e 场景⑦（真实 stdio 会话 + tmp 仓库副本）。


def _write_entry(tmp_path: Path, entry: dict) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / f"{entry['id']}.json").write_text(
        json.dumps(entry, ensure_ascii=False), encoding="utf-8"
    )


def _ctx_for(tmp_path: Path) -> ToolContext:
    store = JsonStore(tmp_path)
    return ToolContext(
        registry=ToolRegistry(store),
        backend_manager=None,
        launcher_manager=None,
        store=store,
        log=lambda _msg: None,
    )


def _probe_entry(script: Path, marker: Path, tool_id="probe", **extra) -> dict:
    return {
        "id": tool_id,
        "name": tool_id,
        "displayName": "启动探针",
        "kind": "app",
        "description": "handler 级启动探针",
        "launch": {
            "type": "executable",
            "command": sys.executable,
            "args": [str(script), str(marker)],
        },
        "readOnly": True,
        "destructive": False,
        **extra,
    }


def test_handler_default_path_does_not_execute(tmp_path):
    """execute 缺省：只返回命令——没有 executed 字段、探针没被跑"""
    script = _write_probe(tmp_path, "probe_ok.py", PROBE_OK)
    marker = tmp_path / "marker.txt"
    _write_entry(tmp_path, _probe_entry(script, marker))

    res = asyncio.run(_handle_launch_tool({"tool_id": "probe"}, _ctx_for(tmp_path)))

    assert res["success"] is True
    assert "executed" not in res, "默认路径不该出现执行字段"
    assert res["launch"]["command"] == sys.executable
    assert not marker.exists(), "默认路径竟然执行了探针"


def test_handler_execute_runs_probe(tmp_path):
    """execute=true：真跑探针，回传 executed/exitCode/running"""
    script = _write_probe(tmp_path, "probe_ok.py", PROBE_OK)
    marker = tmp_path / "marker.txt"
    _write_entry(tmp_path, _probe_entry(script, marker))

    res = asyncio.run(
        _handle_launch_tool(
            {"tool_id": "probe", "execute": True, "wait": LAUNCH_WAIT_MAX},
            _ctx_for(tmp_path),
        )
    )

    assert res["success"] is True, res
    assert res["executed"] is True
    assert res["exitCode"] == 0
    assert res["running"] is False
    assert marker.exists(), "execute=true 没真正执行探针"


def test_handler_execute_failure_raises_typed_error(tmp_path):
    """退出码非 0 → ToolError（isError），载荷带 errorType/exitCode"""
    script = _write_probe(tmp_path, "probe_fail.py", PROBE_FAIL)
    marker = tmp_path / "marker.txt"
    _write_entry(tmp_path, _probe_entry(script, marker))

    with pytest.raises(Exception) as exc:
        asyncio.run(
            _handle_launch_tool(
                {"tool_id": "probe", "execute": True, "wait": LAUNCH_WAIT_MAX},
                _ctx_for(tmp_path),
            )
        )
    payload = json.loads(str(exc.value))
    assert payload["success"] is False
    assert payload["errorType"] == "runtime_error"
    assert payload["executed"] is True
    assert payload["exitCode"] == 3


def test_handler_execute_gated_by_entry_destructive(tmp_path):
    """条目标 destructive：无 allowDestructive 拦截（探针没跑）；带了才放行"""
    script = _write_probe(tmp_path, "probe_ok.py", PROBE_OK)
    marker = tmp_path / "marker.txt"
    _write_entry(
        tmp_path, _probe_entry(script, marker, destructive=True)
    )
    ctx = _ctx_for(tmp_path)

    with pytest.raises(Exception) as exc:
        asyncio.run(
            _handle_launch_tool({"tool_id": "probe", "execute": True}, ctx)
        )
    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "destructive_blocked"
    assert not marker.exists(), "被门禁拦截了却仍然执行了探针"

    res = asyncio.run(
        _handle_launch_tool(
            {"tool_id": "probe", "execute": True, "allowDestructive": True,
             "wait": LAUNCH_WAIT_MAX},
            ctx,
        )
    )
    assert res["success"] is True, res
    assert marker.exists(), "显式确认后探针应被真正执行"


def test_handler_execute_missing_command_raises_config_error(tmp_path):
    """execute=true 但命令不存在 → config_error（不是原生 OSError 文案）"""
    entry = _probe_entry(Path("no-such.py"), Path("no-marker.txt"))
    entry["launch"]["command"] = "awam-no-such-exe-xyz"
    _write_entry(tmp_path, entry)

    with pytest.raises(Exception) as exc:
        asyncio.run(
            _handle_launch_tool({"tool_id": "probe", "execute": True}, _ctx_for(tmp_path))
        )
    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "config_error"
    assert payload["executed"] is False
