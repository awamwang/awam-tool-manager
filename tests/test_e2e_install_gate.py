"""P0 修复的真实 stdio 端到端验证（一次性手工脚本，非 pytest 用例）

背景：tests/ 里没有任何用例跑真实 stdio 传输（protocol_roundtrip 走内存
通道），而 ARC-P0-01 的修复恰恰发生在「服务启动」与「start_backend」两条
真实路径上。这里用真实子进程 + 真实 MCP 会话验一遍门禁在端到端下的行为。

做法：
  1. 往 data/tools/ 放一个探针条目（启动命令是一个不存在的二进制，
     install.command 指向一个不存在的 npm 包）；
  2. 用真实 stdio 拉起 server，调用 start_backend；
  3. 分「未授权」与「已授权」两种环境各跑一次，比对返回与 stderr 日志；
  4. 结束时删除探针条目，保证不留痕迹。

运行（在仓库根）：
  "<pyenv 3.12 python>" tests/test_e2e_install_gate.py
预期输出末尾：BOTH SCENARIOS OK
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

PYTHON = r"C:\Users\Administrator\.pyenv\pyenv-win\versions\3.12.0\python.exe"
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


def _write_probe() -> Path:
    path = ROOT / "data" / "tools" / f"{PROBE_ID}.json"
    path.write_text(json.dumps(PROBE, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _remove_probe() -> None:
    path = ROOT / "data" / "tools" / f"{PROBE_ID}.json"
    if path.exists():
        path.unlink()


async def _call_start_backend(authorized: bool):
    """真实 stdio 会话里调一次 start_backend，返回 (isError, payload, stderr)"""
    env = dict(os.environ)
    env.pop("AWAM_AUTO_INSTALL", None)  # 先确保干净
    if authorized:
        env["AWAM_AUTO_INSTALL"] = "1"

    params = StdioServerParameters(command=PYTHON, args=[str(ROOT / "src" / "server.py")], env=env)
    stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")

    async with stdio_client(params, errlog=stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("start_backend", {"tool_id": PROBE_ID})

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
    _write_probe()
    failures = []
    try:
        # 场景 1：未授权 → 必须拒绝执行，返回 install_required
        is_error, payload, log = await _call_start_backend(authorized=False)
        print("── 场景 1：未授权 ─────────────────────────────")
        print(f"isError      : {is_error}")
        print(f"errorType    : {payload.get('errorType')}")
        print(f"message      : {payload.get('message')}")
        print(f"stderr 命中  : 跳过自装={'跳过自装' in log}")
        print(f"stderr 无执行 : {'执行安装' not in log}")

        if not is_error:
            failures.append("场景1：未授权时 start_backend 竟然成功了")
        if payload.get("errorType") != "install_required":
            failures.append(f"场景1：errorType 应为 install_required，实为 {payload.get('errorType')!r}")
        if "执行安装" in log:
            failures.append("场景1：未授权时 stderr 出现「执行安装」——门禁失效！")

        # 场景 2：已授权 → 命令真的被提交给 shell（--offline 立刻失败，无副作用）
        is_error, payload, log = await _call_start_backend(authorized=True)
        print("\n── 场景 2：已授权（AWAM_AUTO_INSTALL=1）──────────")
        print(f"isError      : {is_error}")
        # errorType 受「预热安装 vs start_backend 竞态」影响：服务启动预热
        # 持锁安装中时，start 走 A14 返回 backend_installing；安装已结束则是
        # A11 的 runtime_error——两个取值都合法，故本场景不对该字段断言
        # （2026-10-17 QA 连跑 4 次实测 3×runtime_error / 1×backend_installing）。
        print(f"errorType    : {payload.get('errorType')}")
        print(f"message      : {payload.get('message')}")
        print(f"stderr 命中  : 执行安装={'执行安装' in log}")

        if "执行安装" not in log:
            failures.append("场景2：已授权时没有执行安装命令——授权通道失效")
        if "跳过自装" in log:
            failures.append("场景2：已授权时仍走了「跳过自装」分支")
    finally:
        _remove_probe()

    print("\n── 探针条目已删除：" + ("是" if not (ROOT / 'data' / 'tools' / f'{PROBE_ID}.json').exists() else "否"))
    if failures:
        print("\n❌ FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n✅ BOTH SCENARIOS OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
