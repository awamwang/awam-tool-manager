"""e2e 场景⑦的失败探针（**被测方**，不是产品代码）。

被 `launch_tool(execute=true)` 跑起来时以退出码 3 结束——验证「进程
真的被 spawn 了、但它自己失败」这条路：`errorType=runtime_error`、
退出码透传，且与「命令不存在」的 `config_error` 可区分。

约束与 `launch_probe_marker.py` / `fake_backend.py` 相同：
1. **stdout 零输出**——万一被接进什么管道，不能污染任何协议通道；
2. 不写任何文件——失败路径的副作用就是退出码本身。
"""

import sys

if __name__ == "__main__":
    sys.exit(3)
