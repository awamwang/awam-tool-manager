"""e2e 专用的仓内最小 FastMCP 假后端（场景③ / ⑤ 的真实被测后端）。

**为什么要有它**：`backend_client` 的整套后端生命周期（spawn → 握手 → 调用
→ 拆除 → 收尸）此前只被替身覆盖过——`tests/test_backend_client.py` 里的
`_FakeSession` 恒返回空工具列表，且没有 `send_ping()` / `call_tool()`。真实
握手耗时、真实 stdin EOF 退出、真实进程树收尸三条链路从未被测过（设计文档
R2）。本文件就是那条链路的被测方。

三条硬约束（违反任何一条都会让场景变 flaky 或假绿）：

1. **绝不往 stdout 写任何东西**。stdout 是 JSON-RPC 通道：mcp 服务端只把
   `sys.stdout.buffer` 包成 utf-8（`mcp/server/stdio.py:47-49`），而 `print()`
   走的 `sys.stdout` 在无 `PYTHONUTF8` 的 Windows 子进程里是 **cp936**——
   一句中文 print 就会产出 GBK 字节，客户端按 utf-8 strict 解码失败，连接
   当场断。要输出一律写 stderr。
2. **每次工具调用往计数文件 append 一行 JSON**。场景⑤ 靠它证明「策略拒绝
   发生在 dispatch 前置，后端根本没被调用」——行数不变就是最强证据。
3. **不依赖 argv 传参**（由父进程 `launch_detached.py` 注入环境变量），避免
   与 FastMCP 自身的命令行处理抢 `sys.argv`。

零新依赖：`FastMCP` 是 mcp 1.29 自带的。
"""

from __future__ import annotations

import json
import os
import time

from mcp.server.fastmcp import FastMCP

# 调用计数文件的路径，由 launch_detached.py 注入
CALLS_ENV_VAR = "FAKE_BACKEND_CALLS"

mcp = FastMCP("awam-e2e-fake-backend")


def _record(tool_name: str, arguments: dict) -> None:
    """把一次工具调用 append 成一行 JSON（旁路：写失败不能把调用搞挂）。"""
    path = os.environ.get(CALLS_ENV_VAR, "").strip()
    if not path:
        return
    line = json.dumps(
        {"ts": time.time(), "tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
    )
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except OSError:
        pass


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the input text."""
    _record("echo", {"text": text})
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    _record("add", {"a": a, "b": b})
    return a + b


def main() -> None:
    """以 stdio 传输跑起来（stdout 只走 JSON-RPC，见模块文档第 1 条）。"""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
