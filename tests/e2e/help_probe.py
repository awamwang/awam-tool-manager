"""e2e 场景⑧的帮助命令探针（**被测方**，不是产品代码）。

`get_tool_help` 的硬契约是「默认只取数、显式 `run=true` 才 spawn」。本脚本
同时提供两类证据，任缺其一都验不干净：

1. **stdout 打印 Usage 文本** —— 证明输出真的被捕获并回传（不是只跑了进程）；
2. **写 marker 文件** —— 证明进程确实诞生（与场景②同款副作用型检测，
   对任何 spawn 形式都成立，无进程表竞态）。

约束与 `fake_backend.py` 相同：marker 写失败不抛（旁路记录），
arg 缺失只往 stderr 打 usage。
"""

import sys
import time


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: help_probe.py <marker_file>", file=sys.stderr)
        return
    marker = sys.argv[1]
    print("Usage: help_probe.py [--thing]")
    print("  --thing  do the thing")
    try:
        with open(marker, "a", encoding="utf-8") as f:
            f.write(f"{time.time()}\t帮助命令已执行\n")
    except OSError as exc:
        print(f"help probe marker write failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
