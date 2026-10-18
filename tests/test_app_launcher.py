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
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import app_launcher
from app_launcher import LAUNCH_WAIT_DEFAULT, LAUNCH_WAIT_MAX, launch_app

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
