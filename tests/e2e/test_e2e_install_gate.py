"""场景：未授权时 `install.command` 绝不被执行（ARC-P0-01 门禁的真实护栏）。

**来源**：从 `tests/e2e/manual_install_gate.py`（原 `tests/test_e2e_install_gate.py`）
拆出来的「未授权」那一半——它无副作用、秒级、断言确定，正适合做成回归护栏。
另一半「已授权 → npm 真的被提交给 shell」依赖本机 npm，且 `errorType` 受
「启动预热 vs start_backend」竞态影响不能断言，保留手工（两个脚本互相在
文件头指路）。

**本场景要改正的一条**（设计 §3 隔离）：探针条目写进 **tmp 副本**
（`write_tool_entry`），**绝不写仓库 `data/`**——原脚本把探针写进真实
`data/tools/` 再在 `finally` 里删，既污染仓库又可能因异常留下残留。

为什么这条值得自动化：`install.command` 写在**进版本库**的 `data/tools/*.json`
里，一旦门禁被绕过，任何一次 `start_backend` 都会在用户机器上执行一条来自
数据文件的 shell 命令。这条断言是唯一能在真实进程里拦住它的护栏。

断言点：
1. `start_backend` 返回 `isError=true` 且 `errorType == "install_required"`；
2. `status == "error"`（Q1 回归：status 是状态机值，不能误用 errorType 的取值）；
3. stderr 出现「跳过自装」且**不出现**「执行安装」——门禁真的拦住了执行；
4. 仓库 `data/tools/` 里**没有**探针文件（探针只活在 tmp 副本里）。

预算 ≤4s。
"""

import asyncio
import time
from pathlib import Path

import pytest

from .helpers import (  # noqa: E402
    ROOT,
    call_tool,
    env_without_npm,
    has_real_traceback,
    stdio_session,
    write_tool_entry,
)

PROBE_ID = "_p0_gate_probe"
# 启动命令不存在 → 依赖缺失；install.command 指向不存在的包（--offline 秒级失败）
PROBE = {
    "id": PROBE_ID,
    "name": "P0 Gate Probe",
    "displayName": "P0 门禁探针",
    "kind": "mcp",
    "status": "not_installed",
    "description": "临时探针：启动命令不存在 + install.command 指向不存在的 npm 包",
    "connection": {"type": "stdio", "command": "awam-no-such-binary-xyz", "args": []},
    "install": {
        "method": "npm",
        "command": "npm i -g --offline awam-p0-probe-does-not-exist",
    },
    "readOnly": False,
    "destructive": False,
}


async def _run(repo, runtime):
    write_tool_entry(repo, PROBE)
    started = time.monotonic()

    # env_without_npm()：本场景验的是「未授权不执行 install.command」，与 npm
    # 在哪无关；而 `ensure_installed()` 会先真跑一次 `npm prefix -g`
    # （backend_client.py:493，本机实测 6.5s），不裁掉就是 5s 起步。
    async with stdio_session(repo, runtime, env_without_npm()) as (session, log):
        # build_env 已清掉 AWAM_AUTO_INSTALL → 未授权路径
        is_error, payload = await call_tool(
            session, "start_backend", {"tool_id": PROBE_ID}
        )

    return is_error, payload, log, time.monotonic() - started


@pytest.mark.e2e
def test_e2e_install_gate_blocks_unauthorized(e2e_repo, e2e_runtime):
    is_error, payload, log, elapsed = asyncio.run(_run(e2e_repo, e2e_runtime))
    tail = log.tail(30)

    print(f"\n[install gate] 未授权门禁耗时 {elapsed:.2f}s（预算 4s）")

    # 1) 门禁拦下：结构化失败 + errorType=install_required
    assert is_error, f"未授权时 start_backend 竟然成功：{payload}\n{tail}"
    assert payload.get("errorType") == "install_required", (
        f"errorType 应为 install_required，实为 {payload.get('errorType')!r}：{payload}"
    )

    # 2) Q1 回归：status 是状态机会话值，不能误挂 errorType 的取值
    assert payload.get("status") == "error", (
        f"status 应为 'error'（状态机值），实为 {payload.get('status')!r}"
    )

    # 3) 命令**没有**被交给 shell——这是本用例的命门
    text = log.text()
    assert "跳过自装" in text, f"stderr 没有「跳过自装」（门禁可能没走到）：\n{tail}"
    assert "执行安装" not in text, "stderr 出现「执行安装」——门禁失效，命令被执行了！"

    # 4) 探针只存在于 tmp 副本：仓库 data/ 必须零残留
    repo_probe = ROOT / "data" / "tools" / f"{PROBE_ID}.json"
    assert not repo_probe.exists(), (
        f"探针写进了仓库 data/tools/（{repo_probe}）——隔离被破坏，请手工删除"
    )

    # 5) 全程无未捕获异常
    assert not has_real_traceback(text), (
        "stderr 出现真实异常（已剔除 Windows asyncio 关闭噪音）：\n" + tail
    )
