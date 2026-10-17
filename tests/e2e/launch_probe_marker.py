"""e2e 场景②的启动探针 marker 脚本（**被测方**，不是产品代码）。

`launch_tool` 的硬契约是「只返回命令，绝不真的拉起进程」
（`registry.py:114`）。本脚本就是这条契约的被测物：它由
`copy_fixture()` 复制进 tmp fixtures，探针条目的 `launch.args` 指向它；
**它一旦真的被跑起来，就会往 marker 文件 append 一行**——文件存在即
「launch_tool 违约执行了」的铁证。

**为什么需要这个脚本（2026-10-18 加固，QA #29 遗留项）**：第一版探针用
「不存在的可执行文件名 + 进程表检查」，被 QA 实证有盲区——不存在的命令
正是进程创建的失败点：直接 Popen 时 CreateProcess 失败、零进程诞生；
`shell=True` 时诞生的是 cmd.exe 且毫秒级退出。两种违约形式进程表都抓
不到。改用副作用型检测后，对任何 spawn 形式（直接 / shell / run）都
确定成立，且无进程表竞态。详见 `test_e2e_readonly.py` 文档字符串。

约束与 `fake_backend.py` 相同：
1. **stdout 零输出**——万一被接进什么管道，不能污染任何协议通道；
2. 日志走 stderr；
3. 写 marker 失败不能抛——旁路记录，不能把现场搞挂。
"""

import sys
import time


def main() -> None:
    """被跑时往 argv[1] 指定的 marker 文件 append 一行。"""
    if len(sys.argv) < 2:
        print("usage: launch_probe_marker.py <marker_file>", file=sys.stderr)
        return
    marker = sys.argv[1]
    line = f"{time.time()}\tlaunched\t{' '.join(sys.argv)}\n"
    try:
        with open(marker, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError as exc:
        print(f"launch probe marker write failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
