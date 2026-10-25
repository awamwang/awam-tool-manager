"""场景③：仓内假 MCP 后端全链路（覆盖 R2——后端子进程生命周期）。

**此前为什么测不到**：`tests/test_backend_client.py` 里后端全靠替身
（`_FakeSession` 恒返回空工具列表，且没有 `send_ping()` / `call_tool()`），
于是下面每一条在真实世界里都没跑过：

- 真实的 `initialize()` 握手（实测 2~3s，替身是零耗时的）——超时分支从未触发；
- 真实 `list_tools()` 返回**非空**结果后 `sync_tools_catalog` 的落盘路径
  （替身恒空，只有 Q3「空列表保留原缓存」那条分支被覆盖过）；
- 真实 stdin EOF 退出 + **进程树收尸**（孤儿进程是 R2 的核心风险）。

**防竞态的关键设计**（设计 §4.2）：`connection.command` 用 `sys.executable`
（绝对路径）→ `_target_available()` 为真 → `ensure_installed()` 在
`backend_client.py:494` 直接短路，**根本不进安装 / 持锁路径**，因此不存在
「启动预热 vs start_backend」的竞态。

**两处为压耗时而做的环境设定**（都不改变被测链路，详见被测方注释）：
1. 网关的 PATH 裁掉 npm（`env_without_npm()`）——本机 `npm prefix -g` 实测
   **6.5s**，且预热与 start_backend 会各跑一次，不裁就是 11s 起步；
2. 「进程是否存活」用 `OpenProcess` 判定而不是 `tasklist`——后者单次
   0.7~1.5s，轮询时测量手段比被测事件还慢。

预算 ≤8s（唯一允许超 5s 的场景）。
"""

import asyncio
import json
import os
import time
import urllib.parse
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
ECHO_TEXT = "awam-e2e-hello"


async def _status_of(session, tool_id: str) -> str | None:
    """当前后端状态；拿不到返回 None（调用失败 / 后端未配置）。"""
    err, payload = await call_tool(session, "get_runtime_status", {"tool_id": tool_id})
    if err:
        return None
    return (payload.get("status") or {}).get("status")


async def _poll_status(session, tool_id: str, want: str, timeout: float = 10.0) -> str:
    """轮询到目标状态。

    之所以包一层：`wait_until` 以「取值是否为真」判定，而 `"starting"` 本身
    也是真值——直接拿状态字符串当 predicate 会第一次就返回。这里只在**等于
    目标**时回传，否则回 None。
    """

    async def probe():
        current = await _status_of(session, tool_id)
        return current if current == want else None

    return await wait_until(
        probe, timeout=timeout, interval=0.2, description=f"后端状态变为 {want}"
    )


def _call_count(path: Path) -> int:
    """调用计数文件的有效行数（假后端每次被调用 append 一行 JSON）。"""
    if not path.exists():
        return 0
    return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])


def _desired_file(runtime: Path, tool_id: str) -> Path:
    """期望状态文件路径（与 `backend_client._desired_file_name` 同款编码）。"""
    return runtime / "desired" / f"{urllib.parse.quote(tool_id, safe='')}.json"


async def _run(repo, runtime, tmp_path):
    fixtures = tmp_path / "fixtures"
    copy_fixture(fixtures, "fake_backend.py")
    copy_fixture(fixtures, "launch_detached.py")
    pid_file = tmp_path / "pid.json"
    calls_file = tmp_path / "calls.log"
    write_tool_entry(repo, fake_backend_entry(fixtures, pid_file, calls_file))

    observations: dict = {"marks": {}}
    started = time.monotonic()

    def mark(step: str) -> None:
        """记录每步耗时：e2e 超预算时，没有分步耗时就只能靠猜。"""
        observations["marks"][step] = round(time.monotonic() - started, 2)

    async with stdio_session(repo, runtime, env_without_npm()) as (session, log):
        mark("session_ready")

        # 1) 启动：start_runtime 成功后会自动同步一次工具目录
        observations["start_err"], observations["start"] = await call_tool(
            session, "start_runtime", {"tool_id": TOOL_ID}
        )
        mark("start_runtime")
        observations["status_running"] = await _poll_status(session, TOOL_ID, "running")
        mark("status_running")

        # 2) 状态详情：tools_count 在替身时代恒为 0，这里第一次拿到真值
        _, status_payload = await call_tool(
            session, "get_runtime_status", {"tool_id": TOOL_ID}
        )
        observations["status_payload"] = status_payload
        observations["desired_after_start"] = _desired_file(runtime, TOOL_ID).exists()

        # 3) 进程树：launcher（子进程）与 backend（孙进程）都必须活着
        await wait_until(
            lambda: pid_file.exists() or None,
            timeout=5.0,
            interval=0.1,
            description="pid.json 被假后端写出",
        )
        observations["pids"] = json.loads(pid_file.read_text(encoding="utf-8"))
        pids = observations["pids"]
        observations["alive_before_stop"] = {
            "launcher": pid_alive(pids["launcher_pid"]),
            "backend": pid_alive(pids["backend_pid"]),
        }

        # 4) 真实调用往返
        observations["call_err"], observations["call"] = await call_tool(
            session,
            "call_capability",
            {"tool_id": TOOL_ID, "capability_id": "echo",
             "arguments": {"text": ECHO_TEXT}, "execute": True},
        )
        observations["calls_after_call"] = _call_count(calls_file)
        mark("call_capability")

        # 5) 同步目录：Q3 裁决——从 sync_capabilities 的返回值反推，
        #    不手工维护后端工具清单，也不修产品代码
        observations["sync_err"], observations["sync"] = await call_tool(
            session, "sync_capabilities", {"tool_id": TOOL_ID}
        )
        cache_path = runtime / "backend-tools" / f"{TOOL_ID}.json"
        observations["cache_exists"] = cache_path.exists()
        observations["cache_names"] = (
            [t.get("name") for t in json.loads(cache_path.read_text(encoding="utf-8")).get("tools", [])]
            if cache_path.exists()
            else []
        )

        # 6) 子工具清单（真实 schema，替身时代恒空）
        observations["list_err"], observations["list"] = await call_tool(
            session, "list_capabilities", {"tool_id": TOOL_ID}
        )

        # 7) 停止 → 进程树收尸
        observations["stop_err"], observations["stop"] = await call_tool(
            session, "stop_runtime", {"tool_id": TOOL_ID}
        )
        mark("stop_runtime")
        observations["status_stopped"] = await _poll_status(session, TOOL_ID, "stopped")
        observations["desired_after_stop"] = _desired_file(runtime, TOOL_ID).exists()
        mark("status_stopped")

        # 无残留：两个 pid 都必须消失（backend 是孙进程，活着即说明收尸只做了一半）
        pids = observations["pids"]

        def all_dead() -> bool | None:
            return True if (
                not pid_alive(pids["launcher_pid"]) and not pid_alive(pids["backend_pid"])
            ) else None

        await wait_until(
            all_dead,
            timeout=10.0,
            interval=0.2,
            description="stop 之后后端进程树全部退出",
        )
        mark("pids_dead")
        observations["alive_after_stop"] = {
            "launcher": pid_alive(pids["launcher_pid"]),
            "backend": pid_alive(pids["backend_pid"]),
        }

    observations["elapsed"] = time.monotonic() - started
    return observations, log


@pytest.mark.e2e
def test_e2e_backend_full_lifecycle(e2e_repo, e2e_runtime, tmp_path):
    obs: dict = {"pids": {}}
    try:
        obs, log = asyncio.run(_run(e2e_repo, e2e_runtime, tmp_path))
        tail = log.tail(30)
        pids = obs["pids"]
        print(f"\n[场景③] 后端全链路耗时 {obs['elapsed']:.2f}s（预算 8s）")
        if os.environ.get("AWAM_E2E_DEBUG"):
            print(f"[场景③] 分步耗时 {obs['marks']}")

        # ── 1) 启动成功，且自动同步过工具目录 ──────────────────────────
        assert not obs["start_err"], f"start_runtime 返回 isError：{obs['start']}\n{tail}"
        assert obs["start"].get("success") is True, f"start_runtime 未成功：{obs['start']}"
        assert "工具目录已自动同步" in obs["start"].get("message", ""), (
            f"start_runtime 未自动同步工具目录：{obs['start'].get('message')!r}"
        )

        # ── 2) 状态真的走到 running，且 tools_count 非零 ────────────────
        assert obs["status_running"] == "running", f"后端未进入 running：{obs['status_running']}"
        status = obs["status_payload"].get("status", {})
        assert status.get("tools_count", 0) > 0, (
            f"tools_count 仍为 0（替身时代的盲区）：{status}"
        )
        # 启动意图落盘（A10）：下个 server 世代据此自愈
        assert obs["desired_after_start"] is True, "start 后期望状态文件不存在"

        # ── 3) 进程树起来了：子进程与孙进程都在 ────────────────────────
        assert obs["alive_before_stop"]["launcher"] is True, (
            f"launcher 进程 {pids['launcher_pid']} 未存活——后端根本没起来"
        )
        assert obs["alive_before_stop"]["backend"] is True, (
            f"backend 进程 {pids['backend_pid']} 未存活——孙进程没起来"
        )

        # ── 4) 真实调用往返一致，且后端确实被调用了一次 ────────────────
        assert not obs["call_err"], f"call_capability 返回 isError：{obs['call']}\n{tail}"
        assert obs["call"].get("success") is True, f"call_capability 未成功：{obs['call']}"
        assert ECHO_TEXT in (obs["call"].get("result") or ""), (
            f"往返结果不一致：{obs['call'].get('result')!r}"
        )
        assert obs["calls_after_call"] == 1, (
            f"假后端被调用了 {obs['calls_after_call']} 次（应为 1）"
        )

        # ── 5) 同步目录：返回值 + 落盘双向核对 ─────────────────────────
        assert not obs["sync_err"], f"sync_capabilities 返回 isError：{obs['sync']}\n{tail}"
        assert obs["sync"].get("success") is True, f"sync 未成功：{obs['sync']}"
        assert (obs["sync"].get("count") or 0) >= 1, f"同步到的工具数为 0：{obs['sync']}"
        # 静默失败观察点：success=true 但 count=0 时产品**不报错**（设计 Q3 裁决：
        # 本期只观察、不改产品代码）。上面断言 count>=1 已把这种情况钉住。
        assert obs["cache_exists"] is True, "backend-tools 缓存文件未落盘"
        assert "echo" in obs["cache_names"], f"缓存里的工具名不符：{obs['cache_names']}"

        # ── 6) 子工具清单（真实 inputSchema）────────────────────────────
        assert not obs["list_err"], f"list_capabilities 返回 isError：{obs['list']}\n{tail}"
        names = {t.get("capabilityId") for t in obs["list"].get("capabilities", [])}
        assert {"echo", "add"} <= names, f"后端子工具清单不符：{sorted(names)}"

        # ── 7) 停止：状态回 stopped、意图清除、进程树全灭 ───────────────
        assert not obs["stop_err"], f"stop_runtime 返回 isError：{obs['stop']}\n{tail}"
        assert obs["status_stopped"] == "stopped", f"后端未回到 stopped：{obs['status_stopped']}"
        assert obs["desired_after_stop"] is False, "stop 后期望状态文件未被清除"

        assert obs["alive_after_stop"]["launcher"] is False, (
            f"stop 之后 launcher 进程 {pids['launcher_pid']} 仍存活——子进程没收掉"
        )
        assert obs["alive_after_stop"]["backend"] is False, (
            f"stop 之后 backend 进程 {pids['backend_pid']} 仍存活——**孙进程成了孤儿**"
        )

        # ── 8) 全程无未捕获异常 ────────────────────────────────────────
        assert not has_real_traceback(log.text()), (
            "stderr 出现真实异常（已剔除 Windows asyncio 关闭噪音）：\n" + tail
        )
    finally:
        # 兜底：断言红 / 超时时也要把现场清干净，绝不留孤儿进程在机器上。
        # 注意——断言走的是上面的 pid_alive()，这里的强杀只做收尾，不作证据。
        force_kill_tree(obs.get("pids", {}).get("launcher_pid", 0))
        force_kill_tree(obs.get("pids", {}).get("backend_pid", 0))
