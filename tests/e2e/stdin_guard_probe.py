"""e2e 场景⑨的发现命令探针（**被测方**，不是产品代码）。

`discover_tools` 的 `run_commands=true` 在 server 进程里 spawn 子进程，而 server 的
stdin 是上游网关的 MCP 协议管道。子进程若**继承**这个 stdin，就会把入站 JSON-RPC
读走（真机实测：`.cmd` 垫片经 cmd.exe 执行后整个 server 直接退出）。本脚本把
「我的 stdin 到底是什么」写进 marker，三种取值就是判定依据：

- `EOF` —— 立刻读到空字节：`stdin=DEVNULL` 的正常表现，**合格**；
- `BLOCKED` —— 0.5s 内读不出东西：继承了一个还在等的管道，**不合格**；
- `STOLEN=<n> bytes:<…>` —— 真读到了字节：协议输入被吞，**不合格**。

另照例往 stdout 打印版本号，验「输出真的被捕获并回传」，与场景⑧同款。
marker 写失败不抛（旁路记录），约束与 `help_probe.py` 相同。
"""

import os
import sys
import threading

READ_TIMEOUT = 0.5


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: stdin_guard_probe.py <marker_file>", file=sys.stderr)
        return
    marker = sys.argv[1]

    chunks: list[bytes] = []

    def steal_stdin() -> None:
        try:
            chunks.append(os.read(0, 65536))
        except OSError as exc:
            chunks.append(f"<error {exc}>".encode())

    thief = threading.Thread(target=steal_stdin, daemon=True)
    thief.start()
    thief.join(READ_TIMEOUT)

    if not chunks:
        verdict = "BLOCKED"
    elif chunks[0] == b"":
        verdict = "EOF"
    else:
        verdict = f"STOLEN={len(chunks[0])} bytes:{chunks[0][:40]!r}"

    try:
        with open(marker, "a", encoding="utf-8") as f:
            f.write(verdict + "\n")
    except OSError as exc:
        print(f"stdin guard probe marker write failed: {exc}", file=sys.stderr)

    print("StdinGuardProbe 1.2.3")


if __name__ == "__main__":
    main()
