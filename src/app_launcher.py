"""`kind=app` 工具的启动执行（`launch_tool` 的 `execute=true` 路径）。

与 `registry.launch_tool` 的分工：registry 只**返回**启动命令（数据层、
零副作用，默认路径的硬契约）；本模块在调用方**显式**传 `execute=true`
之后才真正 spawn 进程。两条路径的取舍与 ARC-P0-01 的 install 门禁同源：

- install.command 当年的病根是「无调用方意图」地被无条件执行；本路径
  每次 spawn 都来自一次显式工具调用 + 显式 `execute=true`，意图在调用侧；
- **不走 shell**：`create_subprocess_exec` 收 argv 列表，条目里的
  `command` / `args` 是结构化字段而非 shell 字符串，元字符无机可乘；
- stdio 三件套全 DEVNULL（A8：绝不继承 server 的 stdio 管道）；
- 条目级 `destructive` 门禁在 `tool_specs` 的 handler 里过
  （`policy.decide_destructive("app", ...)`），本模块不重复判定。

**进程保活（易漏的坑）**：CPython 的 `BaseSubprocessTransport.close()`
会把仍在运行的子进程 `kill()` 掉——「启动后放任继续」的进程一旦被 GC
收走 transport，等于把刚启动的应用杀掉。因此运行中的进程必须由本模块
的注册表强引用持有；已退出的顺手修剪，防止列表无界增长。服务进程退出
时运行中的子进程会随 transport 关闭被杀（与后端子进程同一已知边界，
见 TASKS.md「已知边界」）。
"""

from __future__ import annotations

import asyncio
import shutil

import anyio

from errors import ErrorType

# 观察窗口：spawn 后等待「早退」的时间。窗口内退出 → 报退出码（捕捉
# 启动即失败，如命令存在但一跑就崩）；窗口到时仍在运行 → 报
# running=true 并放任继续（应用启动本就该长跑，杀了它就没了意义）。
LAUNCH_WAIT_DEFAULT = 5.0
# 封顶：调用方给个 wait=600 不该把 MCP 调用堵死十分钟（uTools 的
# UTOOLS_MAX_OBSERVE_SECONDS 同款考虑）。
LAUNCH_WAIT_MAX = 30.0

# 运行中（或尚未被修剪的已退出）启动进程注册表。强引用是保活手段，
# 不是管理面：不提供枚举/杀灭 API，进程生命周期交还操作系统。
_LAUNCHED: list[asyncio.subprocess.Process] = []


def _clamp_wait(wait: object) -> float:
    """把调用方传入的 wait 归一化到 [0, LAUNCH_WAIT_MAX]。

    None → 默认值；非数值 → 默认值（客户端乱传不炸）；负数 → 0
    （不等待）；超过封顶 → 封顶。
    """
    if wait is None:
        return LAUNCH_WAIT_DEFAULT
    try:
        value = float(wait)
    except (TypeError, ValueError):
        return LAUNCH_WAIT_DEFAULT
    return max(0.0, min(value, LAUNCH_WAIT_MAX))


def _remember(process: asyncio.subprocess.Process) -> None:
    """把已 spawn 的进程登记进保活注册表，顺手修剪已退出的。

    修剪条件 `returncode is not None`：asyncio 的 child watcher 会在
    进程退出后把 returncode 补上，已退出的进程没有 transport 被杀的风险，
    留着只会让列表无界增长（每次启动应用都追加一条）。
    """
    _LAUNCHED[:] = [p for p in _LAUNCHED if p.returncode is None]
    _LAUNCHED.append(process)


def _failure(tool_id: str, error_type: str, error: str) -> dict:
    """spawn 之前的失败（什么都没跑）：与 registry.launch_tool 的失败
    载荷同构——带 success/errorType/error，由 handler 的 ensure_success
    统一转成协议层 isError。"""
    return {
        "success": False,
        "errorType": error_type,
        "error": error,
        "toolId": tool_id,
        "executed": False,
    }


async def launch_app(
    launch: dict | None,
    tool_id: str,
    tool_name: str = "",
    wait: object = None,
) -> dict:
    """按条目的 `launch` 配置真正启动一个应用，返回执行结果载荷。

    成功（退出码 0 或窗口后仍运行）与失败（退出码非 0）都走返回体，
    不抛异常——与 `registry.launch_tool` / `LauncherClient.run_action`
    的失败载荷约定一致，由 handler 统一 `ensure_success`。

    Args:
        launch: 工具条目的 `launch` 字段（type/command/args）。
        tool_id: 工具 ID（载荷回显）。
        tool_name: 显示名（消息文案用）。
        wait: 观察窗口秒数；None → 默认 5s；<=0 → 不等待（无法区分
            「已退出」与「运行中」，一律按运行中报告）。
    """
    launch = launch or {}
    launch_type = str(launch.get("type") or "")
    if launch_type != "executable":
        return _failure(
            tool_id,
            ErrorType.CONFIG_ERROR,
            f"暂不支持的启动类型: {launch_type or '(未配置)'}（当前仅支持 executable）",
        )

    command = str(launch.get("command") or "").strip()
    if not command:
        return _failure(tool_id, ErrorType.CONFIG_ERROR, "条目未配置 launch.command")
    # args 元素建议统一写字符串，但整数之类的也照样传（exec 收 str）
    args = [str(a) for a in (launch.get("args") or [])]

    # which 预检：命令不存在时给结构化失败，而不是把 OSError 原生文案
    # 直接暴露给客户端（install 路径的 _target_available 同款思路）
    resolved = shutil.which(command)
    if resolved is None:
        return _failure(
            tool_id,
            ErrorType.CONFIG_ERROR,
            f"启动命令不存在或不在 PATH: {command}",
        )

    wait_seconds = _clamp_wait(wait)
    try:
        process = await asyncio.create_subprocess_exec(
            resolved,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return _failure(tool_id, ErrorType.CONFIG_ERROR, f"无法启动 {command}: {exc}")

    # 先登记再等待：等待窗口里被取消（客户端断开）时进程同样有人持有，
    # 不会因 transport 被 GC 而遭池鱼之殃
    _remember(process)

    base = {
        "toolId": tool_id,
        "toolName": tool_name,
        "executed": True,
        "pid": process.pid,
    }

    if wait_seconds <= 0:
        return {
            **base,
            "success": True,
            "running": True,
            "exitCode": None,
            "message": f"已启动（未等待退出，进程应在运行）: {tool_name or tool_id}",
        }

    try:
        with anyio.fail_after(wait_seconds):
            exit_code = await process.wait()
    except TimeoutError:
        # 窗口到时仍在运行：应用启动的常态，放任继续（不 kill）
        return {
            **base,
            "success": True,
            "running": True,
            "exitCode": None,
            "message": f"已启动（进程仍在运行）: {tool_name or tool_id}",
        }

    if exit_code == 0:
        return {
            **base,
            "success": True,
            "running": False,
            "exitCode": 0,
            "message": f"已启动: {tool_name or tool_id}",
        }
    return {
        **base,
        "success": False,
        "errorType": ErrorType.RUNTIME_ERROR,
        "error": f"启动命令退出码 {exit_code}: {command}",
        "running": False,
        "exitCode": exit_code,
    }
