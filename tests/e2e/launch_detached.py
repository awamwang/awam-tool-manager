"""把假后端拉成**孙进程**，并把两个 pid 落到 pid.json。

**为什么要多这一层**：如果让网关直接 spawn `fake_backend.py`，那么
「`stop_runtime` 后无残留子进程」只能证明**直接子进程**死了，证明不了**整棵
进程树**被收尸——而 R2 的核心风险恰恰是孤儿进程树（后端自己再生子进程）。
多这一层之后，pid.json 里 `launcher_pid` 与 `backend_pid` **必须都死**，断言
才成立：`backend_pid` 是网关的孙进程，它活着就说明收尸只做了一半。

用法（由被测网关的 `StdioServerParameters` 调用）：

    python launch_detached.py <fake_backend.py> <pid.json> <calls.log>

实现要点：
- **stdio 必须继承**：网关的 JSON-RPC 管道就是本进程的 stdin/stdout，子进程
  继承同一组句柄才能直接与网关对话；重定向成 PIPE 会让本进程被迫做转发代理。
- **不读 stdin**：本进程只负责拉起与等待，读一个字节都会抢走后端的请求。
- **活到子进程结束**：本进程提前退出会关掉继承来的 stdout，网关会误判成
  「后端主动断开」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 与 fake_backend.py 约定：调用计数文件的路径走这个环境变量
CALLS_ENV_VAR = "FAKE_BACKEND_CALLS"


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(
            "用法: launch_detached.py <fake_backend.py> <pid.json> <calls.log>",
            file=sys.stderr,
        )
        return 2

    backend = Path(argv[1])
    pid_file = Path(argv[2])
    calls_file = Path(argv[3])

    # 环境变量传参：不占后端的 argv，避免与 FastMCP 的命令行处理打架
    os.environ[CALLS_ENV_VAR] = str(calls_file)

    proc = subprocess.Popen([sys.executable, str(backend)])

    pid_file.write_text(
        json.dumps(
            {"launcher_pid": os.getpid(), "backend_pid": proc.pid},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 阻塞到子进程结束，并把它的退出码原样带出（保持语义透明）
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
