"""场景⑦：`launch_tool` 的 `execute=true` 在真实会话里真的启动进程。

**验什么**：默认路径「只返回命令、绝不 spawn」由场景②双轨取证钉住；
本场景验**显式 `execute=true`** 的新副作用面在真实 stdio 会话下成立：

1. **ok 探针**（复用场景②的 marker 脚本）：`execute=true` → `success`、
   `executed=true`、`exitCode=0`，且 marker 文件真出现——与场景②是
   同一个探针、相反的断言（那边「出现即违约」，这边「出现才对」）；
2. **fail 探针**（新 fixture，退出码 3）：`isError` +
   `errorType=runtime_error` + 退出码透传——「进程跑了但失败」与
   「命令不存在（config_error）」在客户端侧可区分；
3. **destructive 门禁**：条目标 `destructive` 时，无 `allowDestructive`
   → `isError` + `destructive_blocked` 且 marker **不出现**（门禁在
   spawn 之前）；显式传 `allowDestructive=true` → 真执行、marker 出现。

**为什么 marker 探针在真实会话里仍然够用**：场景②的坑（沙箱把服务
拉起的子进程拖慢 ~10x、Job Object 会话关即收尸）对本场景是利好——
证据（marker）在**调用返回时**就已成立（进程退出才返回 exitCode），
不需要宽限窗口，也不跨会话。

预算：会话启动 ~2s + 5 次调用（ok/boom 两个探针各给 wait=8 覆盖沙箱
减速；fail 探针即退；default/blocked 不 spawn）≈ 10~20s。
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

MARKER_SCRIPT = "launch_probe_marker.py"
FAIL_SCRIPT = "launch_probe_fail.py"

# ok 类探针的观察窗口：沙箱会把「服务拉起的子进程」拖慢（场景②实测
# ~10x），8s 是留了余量的经验值；helpers.CALL_TIMEOUT=10s 是硬顶，
# 再大就连调用本身都超时了。
OK_WAIT = 8


def _probe_entry(
    entry_id: str,
    fixtures_dir: Path,
    marker_file: Path,
    script: str,
    destructive: bool = False,
) -> dict:
    """launch 探针条目：command 是**真实存在**的 `sys.executable`。"""
    return {
        "id": entry_id,
        "name": f"AwamE2E{entry_id.title()}",
        "displayName": f"E2E {entry_id}",
        "kind": "app",
        "status": "installed",
        "description": f"临时探针：launch_tool execute 路径（{entry_id}）",
        "tags": ["探针", "e2e"],
        # 关键：registry.launch_tool 读的是 launch 字段（不是 launcher）
        "launch": {
            "type": "executable",
            "command": sys.executable,
            "args": [str(fixtures_dir / script), str(marker_file)],
        },
        "readOnly": True,
        "destructive": destructive,
    }


async def _run(repo, runtime, entries, fixtures_dir, boom_marker):
    for entry in entries:
        write_tool_entry(repo, entry)

    started = time.monotonic()
    async with stdio_session(repo, runtime) as (session, log):
        # 0) 默认路径复核：不带 execute → 只返回命令（无 executed 字段）
        default_err, default = await call_tool(
            session, "launch_tool", {"tool_id": "ok_probe"}
        )
        # 1) ok 探针：execute=true → 真跑，退出码 0
        ok_err, ok = await call_tool(
            session, "launch_tool", {"tool_id": "ok_probe", "execute": True, "wait": OK_WAIT}
        )
        # 2) fail 探针：进程跑了但退出码 3 → isError + runtime_error
        fail_err, fail = await call_tool(
            session, "launch_tool", {"tool_id": "fail_probe", "execute": True, "wait": OK_WAIT}
        )
        # 3) destructive 门禁：拦截（不 spawn）。取证必须**当场**做——
        #    后面的放行调用会真写 marker，事后再看就分不清是谁写的了。
        blocked_err, blocked = await call_tool(
            session, "launch_tool", {"tool_id": "boom_probe", "execute": True, "wait": 1}
        )
        boom_marker_at_block = boom_marker.exists()
        # 4) destructive 门禁：显式确认后放行
        allowed_err, allowed = await call_tool(
            session,
            "launch_tool",
            {
                "tool_id": "boom_probe",
                "execute": True,
                "allowDestructive": True,
                "wait": OK_WAIT,
            },
        )
    elapsed = time.monotonic() - started
    return (
        default_err, default,
        ok_err, ok,
        fail_err, fail,
        blocked_err, blocked, boom_marker_at_block,
        allowed_err, allowed,
    ), log, elapsed


@pytest.mark.e2e
def test_e2e_launch_execute_real_session(e2e_repo, e2e_runtime, tmp_path):
    fixtures_dir = tmp_path / "fixtures"
    ok_marker = tmp_path / "ok_marker.txt"
    boom_marker = tmp_path / "boom_marker.txt"
    copy_fixture(fixtures_dir, MARKER_SCRIPT)
    copy_fixture(fixtures_dir, FAIL_SCRIPT)

    entries = [
        _probe_entry("ok_probe", fixtures_dir, ok_marker, MARKER_SCRIPT),
        _probe_entry("fail_probe", fixtures_dir, tmp_path / "fail_marker.txt", FAIL_SCRIPT),
        _probe_entry(
            "boom_probe", fixtures_dir, boom_marker, MARKER_SCRIPT, destructive=True
        ),
    ]

    (
        (default_err, default, ok_err, ok, fail_err, fail,
         blocked_err, blocked, boom_marker_at_block, allowed_err, allowed),
        log,
        elapsed,
    ) = asyncio.run(_run(e2e_repo, e2e_runtime, entries, fixtures_dir, boom_marker))
    print(f"\n[场景⑦] launch execute 真调耗时 {elapsed:.2f}s（5 次调用）")
    tail = log.tail(20)

    # 服务侧不许有真异常（新代码路径在真实会话里炸过没有）
    assert not has_real_traceback(tail), f"服务 stderr 出现真 Traceback：\n{tail}"

    # 0) 默认路径：只返回命令——没有 executed 字段（场景②另有双轨取证）
    assert not default_err, f"默认路径返回 isError：{default}\n{tail}"
    assert default.get("success") is True
    assert "executed" not in default, "默认路径不该出现执行字段"
    assert default.get("launch", {}).get("command") == sys.executable

    # 1) ok 探针：真执行 + 退出码 0 + marker 落地（真实 spawn 铁证）
    assert not ok_err, f"ok 探针 execute=true 返回 isError：{ok}\n{tail}"
    assert ok.get("success") is True, f"ok 探针执行结果异常：{ok}"
    assert ok.get("executed") is True
    assert ok.get("exitCode") == 0, f"退出码未透传: {ok}"
    assert ok.get("running") is False
    assert ok_marker.exists(), "ok 探针没被真正执行（marker 未落地）"
    assert "launched" in ok_marker.read_text(encoding="utf-8")

    # 2) fail 探针：isError + runtime_error + 退出码透传
    assert fail_err, f"fail 探针应当 isError，实际成功返回：{fail}\n{tail}"
    assert fail.get("errorType") == "runtime_error", f"错误类型不对: {fail}"
    assert fail.get("exitCode") == 3, f"退出码未透传: {fail}"
    assert fail.get("executed") is True, "进程确实跑起来了，executed 不该是 false"

    # 3) destructive 门禁：拦截且没 spawn（marker 不出现）
    assert blocked_err, f"destructive 条目应当被拦截，实际放行：{blocked}\n{tail}"
    assert blocked.get("errorType") == "destructive_blocked", f"错误类型不对: {blocked}"
    assert not boom_marker_at_block, "被门禁拦截了却仍然执行了探针"

    # 4) 显式确认后放行：真执行 + marker 落地
    assert not allowed_err, f"确认后仍返回 isError：{allowed}\n{tail}"
    assert allowed.get("success") is True, f"确认后执行结果异常：{allowed}"
    assert allowed.get("executed") is True
    assert boom_marker.exists(), "显式确认后探针应被真正执行（marker 未落地）"
