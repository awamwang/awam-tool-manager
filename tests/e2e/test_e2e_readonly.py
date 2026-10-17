"""场景②：只读工具在真实会话里的调用（覆盖 R4）。

**验什么**：「查数据」这条最高频链路在真实 stdio 下真的跑通——`search_tools`
命中、`get_tool_info` 回传一致、以及 `launch_tool` 的硬契约：**只返回命令，
绝不真的拉起进程**（`registry.py:114` 的 docstring 是一句承诺，此前没有真实
会话验证过；一旦有人把它改成"顺手帮你启动"，破坏的是最小权限前提）。

**为什么所有条目都手写进 tmp 副本**：真实 `data/tools/` 里是 Quicker /
everything 这类本机工具，装没装不可控（设计 §4.5）。手写的探针条目用一个
**不存在的可执行文件**作 launch.command——它既能证明"命令被原样返回"，又能
用进程表直接证明"它没被执行"（这个名字在任何机器上都不该出现）。

预算 ≤2s。
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .helpers import call_tool, stdio_session, write_tool_entry  # noqa: E402

# 独有标记：搜索命中用它，避免与仓库真实条目撞名
MARKER = "ZzzE2EProbeMarker"
# 一个在任何机器上都不存在的可执行文件——只用于证明"没被真的执行"
FAKE_EXE = "awam-e2e-no-such-launcher-xyz.exe"

PROBE_SEARCH = {
    "id": "_e2e_probe_search",
    "name": "AwamE2EProbeSearch",
    "displayName": "E2E 搜索探针",
    "kind": "app",
    "status": "installed",
    "description": f"临时探针：含独有关键词 {MARKER}，用于真实会话里验证 search_tools",
    "tags": ["探针", "e2e"],
    "readOnly": True,
    "destructive": False,
}

PROBE_LAUNCH = {
    "id": "_e2e_probe_launch",
    "name": "AwamE2EProbeLaunch",
    "displayName": "E2E 启动探针",
    "kind": "app",
    "status": "installed",
    "description": "临时探针：launch.command 指向一个不存在的文件，验证 launch_tool 只返回不执行",
    "tags": ["探针", "e2e"],
    # 关键：registry.launch_tool 读的是 launch 字段（不是 launcher）
    "launch": {"type": "executable", "command": FAKE_EXE, "args": ["-m"]},
    "readOnly": True,
    "destructive": False,
}


def _running_process_names() -> set[str]:
    """当前进程表名集合（小写）；非 Windows 返回空集，由调用方 skip。"""
    if os.name != "nt":
        return set()
    proc = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    names = set()
    for line in proc.stdout.splitlines():
        row = line.strip().strip('"').split('","')
        if row and row[0]:
            names.add(row[0].lower())
    return names


async def _run(repo, runtime):
    write_tool_entry(repo, PROBE_SEARCH)
    write_tool_entry(repo, PROBE_LAUNCH)

    started = time.monotonic()
    async with stdio_session(repo, runtime) as (session, log):
        search_err, search = await call_tool(session, "search_tools", {"keyword": MARKER})
        info_err, info = await call_tool(
            session, "get_tool_info", {"tool_id": PROBE_SEARCH["id"]}
        )
        launch_err, launch = await call_tool(
            session, "launch_tool", {"tool_id": PROBE_LAUNCH["id"]}
        )
    elapsed = time.monotonic() - started
    return (search_err, search, info_err, info, launch_err, launch), log, elapsed


@pytest.mark.e2e
def test_e2e_readonly_tools_real_call(e2e_repo, e2e_runtime):
    (search_err, search, info_err, info, launch_err, launch), log, elapsed = asyncio.run(
        _run(e2e_repo, e2e_runtime)
    )
    print(f"\n[场景②] 只读真调耗时 {elapsed:.2f}s（预算 2s）")
    tail = log.tail(20)

    # 1) search_tools：真实会话里命中手写探针，且不报错
    assert not search_err, f"search_tools 返回 isError：{search}\n{tail}"
    assert search.get("success") is True, f"search_tools success 不为 True：{search}"
    hits = search.get("tools") or search.get("results") or []
    hit_ids = {t.get("id") for t in hits if isinstance(t, dict)}
    assert PROBE_SEARCH["id"] in hit_ids, (
        f"search_tools 未命中手写探针（关键词 {MARKER}）：命中 {sorted(hit_ids)}"
    )

    # 2) get_tool_info：回传的 id 与请求一致（真实通道下参数透传未变形）
    assert not info_err, f"get_tool_info 返回 isError：{info}\n{tail}"
    assert info.get("id") == PROBE_SEARCH["id"], (
        f"get_tool_info 回传的 id 不一致：{info.get('id')!r}"
    )

    # 3) launch_tool：只返回命令……
    assert not launch_err, f"launch_tool 返回 isError：{launch}\n{tail}"
    assert launch.get("success") is True, f"launch_tool success 不为 True：{launch}"
    assert launch.get("launch", {}).get("command") == FAKE_EXE, (
        f"launch_tool 回传的 command 被改写：{launch.get('launch')!r}"
    )

    # 4) ……而且**没有真的执行**：进程表里不该出现这个独有的可执行文件名。
    #    这是本场景最硬的断言——命令被提交给 shell 的话，即便启动失败，
    #    进程表里也会闪现这个名字（失败前的进程创建是既成事实）。
    if os.name != "nt":
        pytest.skip("进程表检查仅在 Windows 上可做（设计 scope 边界第 4 条）")
    assert FAKE_EXE.lower() not in _running_process_names(), (
        f"launch_tool 竟然真的执行了 {FAKE_EXE}——「只返回命令不执行」的硬契约被破坏"
    )
