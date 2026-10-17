"""场景②：只读工具在真实会话里的调用（覆盖 R4）。

**验什么**：「查数据」这条最高频链路在真实 stdio 下真的跑通——`search_tools`
命中、`get_tool_info` 回传一致、以及 `launch_tool` 的硬契约：**只返回命令，
绝不真的拉起进程**（`registry.py:114` 的 docstring 是一句承诺，此前没有真实
会话验证过；一旦有人把它改成"顺手帮你启动"，破坏的是最小权限前提）。

**为什么所有条目都手写进 tmp 副本**：真实 `data/tools/` 里是 Quicker /
everything 这类本机工具，装没装不可控（设计 §4.5）。

**launch 探针怎么证明"没被真的执行"（2026-10-18 加固，QA #29 遗留项）**：
第一版用「不存在的可执行文件名 + 进程表检查」，被 QA 实证有盲区——不存在
的命令正是进程创建的失败点：直接 Popen 时 CreateProcess 失败、零进程诞生；
`shell=True` 时诞生的是 cmd.exe 且毫秒级退出。两种违约形式进程表都抓不到。
现改为**双轨取证**，探针 `command` 用 `sys.executable`（真实存在），`args`
指向复制进 tmp 的 `launch_probe_marker.py`（被跑时往 marker 文件 append
一行）：

- **快通道（marker 文件）**：宽限窗口内文件出现 = 命令真的被执行了。
- **慢通道（进程扫描）**：窗口内 marker 没出现，再扫一遍进程表，命令行里
  含探针脚本路径 = 有人把它拉起来了（哪怕还没轮到执行）。

**为什么必须双轨（本机沙箱实测，勿简化回单轨）**：本沙箱会把「被测服务
进程拉起的子进程」拖慢约 10 倍（实测 ~10s 才放行完成，普通进程同操作
~0.5s）。最自然的违约写法 `Popen([command, *args])`（继承服务的 stdio
管道）在正常机器上 ~0.5s 就留下 marker，在本沙箱要 ~10s——只靠 marker
单轨的话，要么窗口大到荒谬（每次合规运行都白等 10s+），要么在本机漏放
这类违约。扫描直接看 spawn 事实，不等执行完成；正常机器上同样成立
（子进程存活期间扫得到，已完成退场的由快通道覆盖）。红绿验证两轮
（继承管道 / 重定向 stdio）均按预期变红后才收工。

**另一条已写进代码的坑**：mcp 客户端在 Windows 上用 Job Object 装服务
进程（KILL_ON_JOB_CLOSE）——会话一关，服务拉的整棵子树一起收尸。所以
两条取证轨道都必须在**会话关闭前**完成，出了会话连证据都被杀光。

预算：工具调用 ~2s + 宽限 2s + 扫描 ~1s ≈ 5s（仅 print，不断言；为证据
完整性接受超预算，同场景⑤先例）。
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from .helpers import (
    call_tool,
    copy_fixture,
    stdio_session,
    write_tool_entry,
)

# 独有标记：搜索命中用它，避免与仓库真实条目撞名
MARKER = "ZzzE2EProbeMarker"

# marker 脚本与被跑证据文件的文件名（运行时复制/落 tmp，绝不落仓库）
MARKER_SCRIPT = "launch_probe_marker.py"
MARKER_FILE = "launch_probe_marker.txt"

# 快通道宽限窗口：违约子进程留下 marker 的最坏等待时间。
# 依据：普通进程里实测 python 子进程启动+退出 ~0.5s（5 次 0.44~0.50s），
# 脚本写入是启动后第一个动作；取 4 倍余量覆盖负载波动。沙箱慢通道
# （~10s）不靠这个窗口兜——交给进程扫描。
SPAWN_GRACE = 2.0

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


def _probe_launch_entry(fixtures_dir: Path, marker_file: Path) -> dict:
    """launch 探针条目：command 是**真实存在**的 `sys.executable`。

    它被真跑时，args 里的 marker 脚本会往 marker 文件 append 一行——
    文件存在即「launch_tool 违约执行了」的快通道铁证（对直接 Popen /
    shell=True / run 任何 spawn 形式都成立）。
    """
    return {
        "id": "_e2e_probe_launch",
        "name": "AwamE2EProbeLaunch",
        "displayName": "E2E 启动探针",
        "kind": "app",
        "status": "installed",
        "description": "临时探针：launch.command 指向 marker 脚本，验证 launch_tool 只返回不执行",
        "tags": ["探针", "e2e"],
        # 关键：registry.launch_tool 读的是 launch 字段（不是 launcher）
        "launch": {
            "type": "executable",
            "command": sys.executable,
            "args": [str(fixtures_dir / MARKER_SCRIPT), str(marker_file)],
        },
        "readOnly": True,
        "destructive": False,
    }


def _processes_with_cmdline_substring(needle: str) -> list[str] | None:
    """慢通道：命令行里含 `needle` 的进程列表（spawn 证据）。

    返回 None = 「扫不了」（pywin32 缺失或 WMI 查询失败），由调用方降级：
    打印警告后只依赖快通道——宁可放过一个沙箱慢通道，不可让 COM 抽风
    把合规运行误杀成红。

    pywin32 不在 requirements.txt：mcp 自己的 Windows 进程管理
    （`mcp.os.win32.utilities` 的 Job Object）也是可选依赖它，本机随 mcp
    一起装着；按同样的 guarded-import 模式处理。
    """
    if os.name != "nt":
        return []
    try:
        import win32com.client
    except ImportError:
        print("[场景②] pywin32 缺失：spawn 证据慢通道降级（仅 marker 快通道）")
        return None
    try:
        service = win32com.client.Dispatch("WbemScripting.SWbemLocator").ConnectServer(
            ".", "root\\cimv2"
        )
        hits = []
        for proc in service.ExecQuery("SELECT CommandLine FROM Win32_Process"):
            cmdline = proc.CommandLine
            if cmdline and needle in cmdline:
                hits.append(cmdline)
        return hits
    except Exception as exc:  # noqa: BLE001 —— WMI/COM 失败形态多样，统一降级不误杀
        print(f"[场景②] WMI 进程扫描失败：{exc!r}（慢通道降级）")
        return None


async def _run(repo, runtime, launch_entry, script_path, marker_file):
    write_tool_entry(repo, PROBE_SEARCH)
    write_tool_entry(repo, launch_entry)

    started = time.monotonic()
    async with stdio_session(repo, runtime) as (session, log):
        search_err, search = await call_tool(session, "search_tools", {"keyword": MARKER})
        info_err, info = await call_tool(
            session, "get_tool_info", {"tool_id": PROBE_SEARCH["id"]}
        )
        launch_err, launch = await call_tool(
            session, "launch_tool", {"tool_id": launch_entry["id"]}
        )
        # ── 取证轨道①（快通道）：会话内等 marker 出现（原因见模块文档：
        # Job Object 会在会话关闭时收掉整棵子树，出了会话连证据都没了）。
        grace_deadline = time.monotonic() + SPAWN_GRACE
        while time.monotonic() < grace_deadline and not marker_file.exists():
            await asyncio.sleep(0.05)
        # ── 取证轨道②（慢通道）：marker 没出现 ≠ 没被执行——本沙箱会把
        # 违约子进程拖慢 ~10 倍。扫进程表找「有没有人把探针脚本拉起来」。
        spawn_evidence = None
        if not marker_file.exists():
            spawn_evidence = _processes_with_cmdline_substring(str(script_path))
    elapsed = time.monotonic() - started
    return (
        (search_err, search, info_err, info, launch_err, launch),
        log,
        elapsed,
        spawn_evidence,
    )


@pytest.mark.e2e
def test_e2e_readonly_tools_real_call(e2e_repo, e2e_runtime, tmp_path):
    fixtures_dir = tmp_path / "fixtures"
    marker_file = tmp_path / MARKER_FILE
    copy_fixture(fixtures_dir, MARKER_SCRIPT)
    launch_entry = _probe_launch_entry(fixtures_dir, marker_file)

    (search_err, search, info_err, info, launch_err, launch), log, elapsed, spawn_evidence = (
        asyncio.run(
            _run(
                e2e_repo,
                e2e_runtime,
                launch_entry,
                fixtures_dir / MARKER_SCRIPT,
                marker_file,
            )
        )
    )
    print(
        f"\n[场景②] 只读真调耗时 {elapsed:.2f}s"
        f"（含 {SPAWN_GRACE}s 宽限 + 进程扫描）"
    )
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

    # 3) launch_tool：只返回命令——回传的 launch 与条目逐字一致（无改写、无注入）
    assert not launch_err, f"launch_tool 返回 isError：{launch}\n{tail}"
    assert launch.get("success") is True, f"launch_tool success 不为 True：{launch}"
    assert launch.get("launch") == launch_entry["launch"], (
        f"launch_tool 回传的 launch 被改写：{launch.get('launch')!r}"
        f"（期望 {launch_entry['launch']!r}）"
    )

    # 4) ……而且**没有真的执行**：双轨取证（marker 文件 = 执行证据；
    #    进程扫描 = spawn 证据，兜住沙箱里被拖慢 ~10 倍的违约子进程）。
    #    任一轨道有发现，即「只返回命令不执行」的硬契约被破坏。
    assert not marker_file.exists() and not spawn_evidence, (
        "launch_tool 竟然真的执行了探针命令——「只返回命令不执行」的硬契约被破坏；"
        + (
            f"marker 文件内容：{marker_file.read_text(encoding='utf-8')!r}"
            if marker_file.exists()
            else ""
        )
        + (
            f"仍在运行的探针进程：{spawn_evidence}"
            if spawn_evidence
            else ""
        )
    )
