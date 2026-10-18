"""场景⑧：`get_tool_help` 在真实会话里「默认零副作用 / 显式 run 才 spawn」。

**验什么**（与场景⑦同构，验的是第三条执行路径）：

1. **默认路径**（不带 `run`）：返回文档层 + `helpCommand` **预览**，marker
   **不出现**——这条契约的破口与 `launch_tool` 一样是「顺手执行」，必须用
   副作用型检测当场取证；
2. **`run=true`**：`executed=true`、stdout 被捕获回传（含 `Usage:`）、marker
   落地。被执行的是 `help.command` + `help.args`；`launch.args` 设成无害的
   `-c pass`，只用于说明「启动参数与帮助参数是两个来源」。参数来源的排他
   性（绝不复用 `launch.args`）由 `tests/test_tool_help.py` 的两条用例锁定，
   真实会话这层验的是「按条目配置真的跑对了命令」；
3. **destructive 门禁**：条目标 `destructive` 时 `run=true` 需
   `allowDestructive=true`；拦截时 marker 不出现，且拒绝文案说的是
   「执行帮助命令」（审计标注同样跟随入口，见 `test_tool_help.py`）；
4. **非 app 的 `run=true` 不 spawn**：`kind=launcher` 只回数据层 + note；
5. **无帮助来源**：既无 `launch` 又无 `help` 的 app 条目 `run=true` →
   `config_error`，而不是空成功。

预算：会话启动 ~2s + 6 次调用（其中 3 次真 spawn，探针 `timeout=8` 覆盖
沙箱减速；helpers.CALL_TIMEOUT=10s 是硬顶）≈ 12~20s。
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

from .helpers import (
    call_tool,
    copy_fixture,
    has_real_traceback,
    stdio_session,
    write_tool_entry,
)

HELP_SCRIPT = "help_probe.py"
# 沙箱会把「服务拉起的子进程」拖慢（场景②实测 ~10x）；8s 给探针留余量，
# 又留在 helpers.CALL_TIMEOUT=10s 之下——再大就是调用本身先超时。
HELP_TIMEOUT = 8


def _app_entry(
    entry_id: str,
    script: Path,
    marker: Path,
    *,
    destructive: bool = False,
    with_help: bool = True,
) -> dict:
    """help 探针条目。

    `launch.args` 用无害的 `-c pass`（会话里真被启动也不留痕），`help.command`
    / `help.args` 才是本场景要执行的那条命令；两个参数来源分设，避免「跑对了
    帮助命令」和「跑了启动命令」在现场证据上长得一样。
    """
    entry = {
        "id": entry_id,
        "name": f"AwamE2E{entry_id.title()}",
        "displayName": f"E2E {entry_id}",
        "kind": "app",
        "status": "installed",
        "description": f"临时探针：get_tool_help 路径（{entry_id}）",
        "website": "https://example.com/awam-e2e",
        "docUrl": "https://example.com/awam-e2e/doc",
        "tags": ["探针", "e2e"],
        "launch": {
            "type": "executable",
            "command": sys.executable,
            "args": ["-c", "pass"],
        },
        "readOnly": True,
        "destructive": destructive,
    }
    if with_help:
        entry["help"] = {
            "command": sys.executable,
            "args": [str(script), str(marker)],
            "timeout": HELP_TIMEOUT,
        }
    else:
        # 无 launch 也无 help → run=true 应报 config_error
        entry.pop("launch")
    return entry


async def _run(repo, runtime, entries, marker, boom_marker):
    for entry in entries:
        write_tool_entry(repo, entry)

    started = time.monotonic()
    async with stdio_session(repo, runtime) as (session, log):
        # 1) 默认路径：只取数、不 spawn
        default_err, default = await call_tool(
            session, "get_tool_help", {"tool_id": "help_probe"}
        )
        marker_at_default = marker.exists()
        # 2) run=true：真执行 + 捕获输出
        run_err, run = await call_tool(
            session, "get_tool_help", {"tool_id": "help_probe", "run": True}
        )
        # 3) destructive 拦截（当场取证）
        blocked_err, blocked = await call_tool(
            session, "get_tool_help", {"tool_id": "boom_probe", "run": True}
        )
        boom_marker_at_block = boom_marker.exists()
        # 4) destructive 放行
        allowed_err, allowed = await call_tool(
            session,
            "get_tool_help",
            {"tool_id": "boom_probe", "run": True, "allowDestructive": True},
        )
        # 5) 非 app：run=true 也不 spawn
        launcher_err, launcher = await call_tool(
            session, "get_tool_help", {"tool_id": "q_probe", "run": True}
        )
        # 6) 无帮助来源：config_error
        config_err, config = await call_tool(
            session, "get_tool_help", {"tool_id": "nolaunch_probe", "run": True}
        )
    elapsed = time.monotonic() - started
    return (
        (
            default_err, default, marker_at_default,
            run_err, run,
            blocked_err, blocked, boom_marker_at_block,
            allowed_err, allowed,
            launcher_err, launcher,
            config_err, config,
        ),
        log,
        elapsed,
    )


@pytest.mark.e2e
def test_e2e_get_tool_help_real_session(e2e_repo, e2e_runtime, tmp_path):
    fixtures_dir = tmp_path / "fixtures"
    script = copy_fixture(fixtures_dir, HELP_SCRIPT)
    marker = tmp_path / "help_marker.txt"
    boom_marker = tmp_path / "boom_marker.txt"

    entries = [
        _app_entry("help_probe", script, marker),
        _app_entry("boom_probe", script, boom_marker, destructive=True),
        _app_entry("nolaunch_probe", script, tmp_path / "none_marker.txt", with_help=False),
        {
            "id": "q_probe",
            "name": "AwamE2EQProbe",
            "displayName": "E2E q_probe",
            "kind": "launcher",
            "status": "installed",
            "description": "临时探针：get_tool_help 的非 app 数据层",
            "tags": ["探针", "e2e"],
        },
    ]

    (
        (
            default_err, default, marker_at_default,
            run_err, run,
            blocked_err, blocked, boom_marker_at_block,
            allowed_err, allowed,
            launcher_err, launcher,
            config_err, config,
        ),
        log,
        elapsed,
    ) = asyncio.run(_run(e2e_repo, e2e_runtime, entries, marker, boom_marker))
    print(f"\n[场景⑧] get_tool_help 真调耗时 {elapsed:.2f}s（6 次调用）")
    tail = log.tail(20)

    assert not has_real_traceback(tail), f"服务 stderr 出现真 Traceback：\n{tail}"

    # 1) 默认路径：文档层 + 预览，且没 spawn
    assert not default_err, f"默认路径返回 isError：{default}\n{tail}"
    assert default.get("success") is True
    assert default.get("kind") == "app"
    assert default.get("docUrl") == "https://example.com/awam-e2e/doc"
    assert "executed" not in default, "默认路径不该出现执行字段"
    assert not marker_at_default, "默认路径竟然执行了帮助命令"

    # 2) run=true：真执行、输出被捕获（且用的 help.args 不是 launch.args）
    assert not run_err, f"run=true 返回 isError：{run}\n{tail}"
    assert run.get("executed") is True
    output = run.get("output") or ""
    assert "Usage: help_probe.py" in output, f"帮助输出未回传：{run}"
    assert marker.exists(), "帮助命令没被真正执行（marker 未落地）"

    # 3) destructive 门禁：拦截且没 spawn，文案说的是帮助命令
    assert blocked_err, f"destructive 条目应当被拦截，实际放行：{blocked}\n{tail}"
    assert blocked.get("errorType") == "destructive_blocked"
    assert "执行帮助命令" in blocked.get("error", ""), f"拒绝文案不对：{blocked}"
    assert not boom_marker_at_block, "被门禁拦截了却仍然执行了帮助命令"

    # 4) 显式确认后放行
    assert not allowed_err, f"确认后仍返回 isError：{allowed}\n{tail}"
    assert allowed.get("executed") is True
    assert boom_marker.exists(), "显式确认后帮助命令应被真正执行"

    # 5) 非 app：run=true 只回数据层，不 spawn
    assert not launcher_err, f"launcher 条目返回 isError：{launcher}\n{tail}"
    assert "executed" not in launcher
    assert "没有可执行的帮助命令" in launcher.get("note", "")

    # 6) 无帮助来源：config_error（不是空成功）
    assert config_err, f"无帮助来源应当报错，实际成功：{config}\n{tail}"
    assert config.get("errorType") == "config_error"
