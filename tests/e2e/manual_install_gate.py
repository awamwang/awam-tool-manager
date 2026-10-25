"""P0 install 门禁的**手工**端到端验证（依赖本机 npm，不进 pytest）

**指路（本脚本为什么还在、其余场景去哪了）**：

- 自动化 e2e 框架在 `tests/e2e/`（`helpers.py` 提供 stdio 会话 / tmp 仓库副本
  / stderr 采集）。跑法：
  `"C:\\Users\\Administrator\\.pyenv\\pyenv-win\\versions\\3.12.0\\python.exe" -m pytest -m e2e -q`
- **未授权 → `install_required`** 这条已自动化，见 `tests/e2e/test_e2e_install_gate.py`
  （无副作用、秒级、断言确定，正适合做回归护栏）。
- **已授权（`AWAM_AUTO_INSTALL=1`）→ npm 真的被提交给 shell** 这条**保留手工**，
  就是本脚本，原因有二：
  1. 它依赖本机 npm，验的是「命令确实被 shell 执行」这一副作用面；
  2. `errorType` 受「启动预热 vs start_runtime」竞态影响，**不能断言**（见下）。
  做成自动用例只会引入 flaky 与本机依赖。

运行（在仓库根）：
  "C:\\Users\\Administrator\\.pyenv\\pyenv-win\\versions\\3.12.0\\python.exe" tests/e2e/manual_install_gate.py
预期输出末尾：`MANUAL SCENARIO OK`

**文件名没有 `test_` 前缀是刻意的**：带前缀会被 pytest 收集时 import 一次
（虽然 collect 0 条用例，但白跑一遍 `sys.path` 注入与 mcp 导入）。
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
# 复用 e2e 框架的仓库副本 / 环境构造：探针写进 **tmp 副本**，
# 绝不写仓库 `data/`（原脚本把探针写进真实 data/tools/ 再删，C 期已改正）
sys.path.insert(0, str(HERE))
from helpers import build_env, repo_copy, write_tool_entry  # noqa: E402

PROBE_ID = "_p0_gate_probe"
PROBE = {
    "id": PROBE_ID,
    "name": "P0 Gate Probe",
    "displayName": "P0 门禁探针",
    "kind": "mcp",
    "status": "not_installed",
    "description": "临时探针：启动命令不存在 + install.command 指向不存在的 npm 包",
    "connection": {
        "type": "stdio",
        "command": "awam-no-such-binary-xyz",
        "args": [],
    },
    "install": {
        "method": "npm",
        # --offline：授权场景下让 npm 立刻失败，不打网络、不留副作用，
        # 但足以证明「命令真的被提交给了 shell」。
        "command": "npm i -g --offline awam-p0-probe-does-not-exist",
    },
    "readOnly": False,
    "destructive": False,
}


async def _call_start_runtime(repo_dir: Path, runtime_dir: Path, authorized: bool):
    """真实 stdio 会话里调一次 start_runtime，返回 (isError, payload, stderr)"""
    env = build_env(runtime_dir, {"AWAM_AUTO_INSTALL": "1"} if authorized else None)

    params = StdioServerParameters(
        command=sys.executable, args=[str(repo_dir / "src" / "server.py")], env=env
    )
    stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")

    async with stdio_client(params, errlog=stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("start_runtime", {"tool_id": PROBE_ID})

    stderr.seek(0)
    log = stderr.read()
    stderr.close()

    text = result.content[0].text if result.content else ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"_raw": text}
    return result.isError, payload, log


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="awam-manual-gate-"))
    failures = []
    try:
        repo = repo_copy(tmp)
        runtime = tmp / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        write_tool_entry(repo, PROBE)

        # 场景：已授权（AWAM_AUTO_INSTALL=1）→ 命令真的被提交给 shell
        is_error, payload, log = await _call_start_runtime(repo, runtime, authorized=True)
        print("── 场景：已授权（AWAM_AUTO_INSTALL=1）──────────")
        print(f"isError      : {is_error}")
        # errorType 受「预热安装 vs start_runtime 竞态」影响：服务启动预热
        # 持锁安装中时，start 走 A14 返回 backend_installing；安装已结束则是
        # A11 的 runtime_error——两个取值都合法，故本场景不对该字段断言
        # （2026-10-17 QA 连跑 4 次实测 3×runtime_error / 1×backend_installing）。
        # ↑ 这段不确定性说明是保留本脚本为手工的理由之一，删了它就会被
        #   后来人误当成「可以自动化的稳定断言」。
        print(f"errorType    : {payload.get('errorType')}（不断言，见源码注释）")
        print(f"message      : {payload.get('message')}")
        print(f"stderr 命中  : 执行安装={'执行安装' in log}")

        if "执行安装" not in log:
            failures.append("已授权时没有执行安装命令——授权通道失效")
        if "跳过自装" in log:
            failures.append("已授权时仍走了「跳过自装」分支")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    repo_probe = HERE.parent.parent / "data" / "tools" / f"{PROBE_ID}.json"
    print("\n── 探针写在 tmp 副本（仓库 data/tools/ 未被触碰）："
          + ("是" if not repo_probe.exists() else "否 —— 仓库被污染了，请手工删除！"))
    if failures:
        print("\n❌ FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n✅ MANUAL SCENARIO OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
