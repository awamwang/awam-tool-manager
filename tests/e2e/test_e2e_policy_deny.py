"""场景⑤：部署策略 deny 在**真实进程**里的表现（覆盖 R7）。

`tests/test_policy_config.py` 已覆盖「策略文件加载 / import 期快速失败」，
本场景验的是**运行期**：策略通过 `AWAM_POLICY_FILE` 生效后，被 deny 的工具
在真实 stdio 会话里到底怎么表现。

四条断言（设计 §2 场景⑤）：

1. 被 deny 的工具 → `isError=true` 且 `errorType == "permission_denied"`；
2. **后端零调用**（本场景最有价值的一条）：假后端每次被调用会往计数文件
   append 一行。deny 命中后行数**必须不变**——这证明 `check_call()` 发生在
   dispatch **前置**（`server.py:166`），handler 根本没跑，后端进程一个字节
   都没收到；
3. 拒绝文案**不提** `allowDestructive`（P1-05 Phase 2 的 H1 话术要求：客户端
   无法自行解除，提了等于误导）；
4. `<runtime>/audit.log` 新增一行 `decision=block`（`policy.py:61`、`:228`）。

**为什么 deny 名单里放 `stop_backend`**：它是唯一能证明「拒绝真的生效」的
负向证据——被拒之后后端**仍在 running**。若策略层被绕过，后端会被真的停掉，
状态立刻变 stopped，这条断言就红。

预算 ≤4s（独立进程）。
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from .helpers import (  # noqa: E402
    DEFAULT_FAKE_TOOL_ID,
    call_tool,
    copy_fixture,
    env_without_npm,
    fake_backend_entry,
    force_kill_tree,
    has_real_traceback,
    pid_alive,
    stdio_session,
    wait_until,
    write_tool_entry,
)

TOOL_ID = DEFAULT_FAKE_TOOL_ID

# 与 data/policy.json 同构（POLICY_SCHEMA 顶层 additionalProperties=false，
# 多一个字段就会在 import 期直接失败——那是 test_policy_config 的地盘）
POLICY = {
    "version": 1,
    "tools": {"deny": ["call_backend_tool", "stop_backend"], "allow": None},
    "destructive": {"mode": "deny"},
    "audit": {"destructive": True},
}


def _call_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])


def _audit_blocks(runtime: Path) -> list[dict]:
    """`<runtime>/audit.log` 里所有 `decision=block` 的审计行。"""
    path = runtime / "audit.log"
    if not path.exists():
        return []
    blocks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("decision") == "block":
            blocks.append(entry)
    return blocks


async def _run(repo, runtime, tmp_path):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(POLICY, ensure_ascii=False, indent=2), encoding="utf-8")

    fixtures = tmp_path / "fixtures"
    copy_fixture(fixtures, "fake_backend.py")
    copy_fixture(fixtures, "launch_detached.py")
    pid_file = tmp_path / "pid.json"
    calls_file = tmp_path / "calls.log"
    write_tool_entry(repo, fake_backend_entry(fixtures, pid_file, calls_file))

    obs: dict = {}
    started = time.monotonic()
    env = {"AWAM_POLICY_FILE": str(policy_path), **env_without_npm()}

    async with stdio_session(repo, runtime, env) as (session, log):
        # 先把后端真的拉起来：后端活着，「零调用」才是有意义的证据
        obs["start_err"], obs["start"] = await call_tool(
            session, "start_backend", {"tool_id": TOOL_ID}
        )

        async def status_is(want: str):
            async def probe():
                err, payload = await call_tool(
                    session, "get_backend_status", {"tool_id": TOOL_ID}
                )
                if err:
                    return None
                current = (payload.get("status") or {}).get("status")
                return current if current == want else None

            return await wait_until(
                probe, timeout=10.0, interval=0.2, description=f"后端状态变为 {want}"
            )

        obs["status_running"] = await status_is("running")

        await wait_until(
            lambda: pid_file.exists() or None,
            timeout=5.0,
            interval=0.05,
            description="pid.json 被假后端写出",
        )
        obs["pids"] = json.loads(pid_file.read_text(encoding="utf-8"))
        obs["backend_alive"] = pid_alive(obs["pids"]["backend_pid"])
        obs["calls_before"] = _call_count(calls_file)

        # 被 deny 的调用：命中 deny 名单 → permission_denied
        obs["denied_err"], obs["denied"] = await call_tool(
            session,
            "call_backend_tool",
            {"tool_id": TOOL_ID, "tool_name": "echo", "arguments": {"text": "hi"}},
        )
        obs["calls_after"] = _call_count(calls_file)

        # 被 deny 的 stop：后端应当**仍然在跑**（拒绝生效的负向证据）
        obs["stop_err"], obs["stop"] = await call_tool(
            session, "stop_backend", {"tool_id": TOOL_ID}
        )
        _, payload = await call_tool(session, "get_backend_status", {"tool_id": TOOL_ID})
        obs["status_after_denied_stop"] = (payload.get("status") or {}).get("status")

    # 网关进程退出 → Job Object 应当把后端进程树一并收掉（R2 的兜底路径）
    backend_pid = obs["pids"]["backend_pid"]
    await wait_until(
        lambda: (not pid_alive(backend_pid)) or None,
        timeout=10.0,
        interval=0.1,
        description="网关退出后后端进程被收尸",
    )
    obs["backend_dead_after_exit"] = not pid_alive(backend_pid)
    obs["audit_blocks"] = _audit_blocks(runtime)
    obs["elapsed"] = time.monotonic() - started
    return obs, log


@pytest.mark.e2e
def test_e2e_policy_deny_blocks_before_backend(e2e_repo, e2e_runtime, tmp_path):
    obs: dict = {"pids": {}}
    try:
        obs, log = asyncio.run(_run(e2e_repo, e2e_runtime, tmp_path))
        tail = log.tail(30)

        print(f"\n[场景⑤] policy deny 端到端耗时 {obs['elapsed']:.2f}s（预算 4s）")

        # ── 0) 前置：后端真的起来了（否则「零调用」是空话）────────────
        assert not obs["start_err"], f"start_backend 返回 isError：{obs['start']}\n{tail}"
        assert obs["status_running"] == "running", f"后端未进入 running：{obs['status_running']}"
        assert obs["backend_alive"] is True, "后端进程未存活——零调用无从谈起"

        # ── 1) permission_denied ──────────────────────────────────────
        assert obs["denied_err"] is True, (
            f"被 deny 的调用没有返回 isError：{obs['denied']}\n{tail}"
        )
        assert obs["denied"].get("errorType") == "permission_denied", (
            f"errorType 不是 permission_denied：{obs['denied']}"
        )

        # ── 2) 后端零调用（本场景最有价值的一条）──────────────────────
        assert obs["calls_before"] == obs["calls_after"], (
            f"后端被调用了：{obs['calls_before']} → {obs['calls_after']}——"
            f"策略检查没有发生在 dispatch 前置"
        )
        assert obs["calls_after"] == 0, (
            f"计数文件已有 {obs['calls_after']} 行——后端收到了本不该收到的请求"
        )

        # ── 3) 拒绝文案不提 allowDestructive（H1 话术）────────────────
        rendered = json.dumps(obs["denied"], ensure_ascii=False)
        assert "allowDestructive" not in rendered, (
            f"拒绝文案里出现了 allowDestructive——客户端无法自行解除，"
            f"提它等于误导：{rendered}"
        )

        # ── 4) 审计落盘 ──────────────────────────────────────────────
        blocks = obs["audit_blocks"]
        assert blocks, f"audit.log 里没有 decision=block 的审计行：{tail}"
        assert any(b.get("tool") == "call_backend_tool" for b in blocks), (
            f"审计行里没有 call_backend_tool：{blocks}"
        )

        # ── 5) 被 deny 的 stop 没生效：后端仍在运行 ───────────────────
        assert obs["stop_err"] is True, f"被 deny 的 stop_backend 没返回 isError：{obs['stop']}"
        assert obs["stop"].get("errorType") == "permission_denied", f"stop 的 errorType 不对：{obs['stop']}"
        assert obs["status_after_denied_stop"] == "running", (
            f"被 deny 的 stop 之后后端状态是 {obs['status_after_denied_stop']}"
            f"（应为 running——拒绝生效了，后端没被停）"
        )

        # ── 6) 网关退出后后端进程被收尸 ──────────────────────────────
        assert obs["backend_dead_after_exit"] is True, (
            f"网关退出后后端进程 {obs['pids']['backend_pid']} 仍存活——孤儿进程"
        )

        # ── 7) 全程无未捕获异常 ──────────────────────────────────────
        assert not has_real_traceback(log.text()), (
            "stderr 出现真实异常（已剔除 Windows asyncio 关闭噪音）：\n" + tail
        )
    finally:
        force_kill_tree(obs.get("pids", {}).get("launcher_pid", 0))
        force_kill_tree(obs.get("pids", {}).get("backend_pid", 0))
