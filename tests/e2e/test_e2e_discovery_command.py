"""场景⑨：`discover_tools` 的 `run_commands=true` 不得吃掉网关的 stdio 输入。

**验什么**（四件事，缺一验不干净）：

1. **默认零 spawn**：不带 `run_commands` 时 marker 不出现——`launch_tool` 的
   `execute` / `get_tool_help` 的 `run` 同一条门禁纪律，在发现路径上也要落地；
2. **显式开才跑**：`run_commands=true` 真的执行命令并把版本号解析回传；
3. **子进程 stdin 必须是 DEVNULL**：marker 记 `EOF`。记 `BLOCKED` / `STOLEN=` 就说明
   子进程继承的是网关的 stdio 协议管道——真机实测整个 server 会因此退出；
4. **跑完会话仍然可用**：再取一次 `tools/list` 拿满 18 个工具。这是对「入站
   JSON-RPC 被吞」最直接的现场证据，比读日志可靠。

预算：会话启动 ~2s + 3 次调用（其中 1 次真 spawn，探针自身只等 0.5s）≈ 6~10s。
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from .helpers import (
    call_tool,
    copy_fixture,
    has_real_traceback,
    stdio_session,
)

PROBE_SCRIPT = "stdin_guard_probe.py"
VERSION = "1.2.3"


def write_manifest(path: Path, script: Path, marker: Path) -> None:
    """一份只含单个候选的发现清单，命令面就是探针本体。

    `probes` 留空：本场景要验的是**命令执行面**，文件系统探针在单测里已经覆盖，
    混进来只会让「为什么判 installed」这条结论多一个来源。
    """
    manifest = {
        "version": 1,
        "candidates": [
            {
                "tool_id": "stdin_guard",
                "name": "Stdin Guard Probe",
                "kind": "app",
                "probes": [],
                "command": {
                    "argv": [sys.executable, str(script), str(marker)],
                    "preview": "stdin_guard_probe.py",
                    "version_pattern": r"StdinGuardProbe ([0-9.]+)",
                },
            }
        ],
    }
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run(repo, runtime, manifest_path, marker):
    started = time.monotonic()
    async with stdio_session(repo, runtime, {"AWAM_DISCOVERY_FILE": str(manifest_path)}) as (
        session,
        log,
    ):
        default_err, default = await call_tool(session, "discover_tools", {})
        # 当场取证：等两次调用都跑完再看文件，「默认不 spawn」就永远验不出红
        marker_at_default = marker.exists()
        run_err, run = await call_tool(
            session, "discover_tools", {"run_commands": True}
        )
        # 会话还活着吗：入站管道被子进程吃掉时，这一步会直接炸
        after = await asyncio.wait_for(session.list_tools(), timeout=10)
    return (
        default_err,
        default,
        marker_at_default,
        run_err,
        run,
        len(after.tools),
        log,
        time.monotonic() - started,
    )


@pytest.mark.e2e
def test_e2e_discover_tools_keeps_transport_intact(e2e_repo, e2e_runtime, tmp_path):
    script = copy_fixture(tmp_path / "fixtures", PROBE_SCRIPT)
    marker = tmp_path / "stdin_marker.txt"
    manifest_path = tmp_path / "discovery.json"
    write_manifest(manifest_path, script, marker)

    (
        default_err,
        default,
        marker_at_default,
        run_err,
        run,
        tools_after,
        log,
        elapsed,
    ) = asyncio.run(_run(e2e_repo, e2e_runtime, manifest_path, marker))
    print(f"\n[场景⑨] discover_tools 真调耗时 {elapsed:.2f}s（3 次调用）")
    tail = log.tail(20)

    assert not has_real_traceback(tail), f"服务 stderr 出现真 Traceback：\n{tail}"

    # 1) 默认路径：可用与否给得出，但不 spawn
    assert not default_err, f"默认路径返回 isError：{default}\n{tail}"
    assert default.get("success") is True
    assert default.get("runCommands") is False
    assert not marker_at_default, "默认路径竟然执行了发现命令"

    # 2) 显式 run_commands=true：真的跑并解析出版本号
    assert not run_err, f"run_commands=true 返回 isError：{run}\n{tail}"
    assert run.get("runCommands") is True
    command = (run.get("candidates") or [{}])[0].get("command") or {}
    assert command.get("run") is True and command.get("ok") is True, f"命令面结果不对：{run}"
    assert command.get("version") == VERSION, f"版本号未解析回传：{command}"

    # 3) 子进程 stdin 只能是 DEVNULL（读到 EOF）
    verdicts = marker.read_text(encoding="utf-8").split() if marker.exists() else []
    assert verdicts == ["EOF"], (
        f"子进程 stdin 不是 DEVNULL，而是继承/吞掉了网关的协议管道：{verdicts}；"
        f"stderr 尾部：\n{tail}"
    )

    # 4) 跑完之后会话仍然可用（18 个工具定义拿得回来）
    assert tools_after == 18, f"执行发现命令后服务会话已不可用（tools/list 返回 {tools_after} 个）"
