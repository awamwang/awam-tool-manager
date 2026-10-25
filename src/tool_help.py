"""工具帮助信息的获取（P1「工具帮助文档查看」的底座）。

分层来源，**零副作用优先**（与 `launch_tool` 默认只返回命令同一取舍）：

1. 条目自身：`description` / `docUrl` / `website`；
2. `kind=mcp` → 后端子工具缓存（子工具名 + 描述 + 参数名摘要）；
3. `kind=launcher` → 动作清单缓存（动作名 + 类型）；
4. `kind=app` → 先给出**将要执行**的帮助命令预览，调用方显式 `run=true`
   才真的 spawn 并捕获输出。

跑 `--help` 同样是副作用：`shutil.which` 预检、无 shell（`exec` 收 argv 列表）、
`stdin=DEVNULL`；与 `launch_app` 的「放任继续」相反，这里是**必须等结果**——
超时就把进程杀掉并报超时（挂住的帮助命令留着没有意义）。

本模块只做「取数」和「跑命令」，破坏性门禁与协议层错误出口在
`tool_specs.py` 的 handler 里（`policy.decide_destructive` + `fail`）。
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import anyio

from errors import ErrorType

# Windows 图形程序常是 `/?`，控制台程序常是 `--help`；条目 `help.args` 可覆盖
DEFAULT_HELP_ARGS = ["--help"]
# 帮助命令本应即时返回；窗口给 5s 足够，封顶防调用方传超大值把 MCP 调用堵死
HELP_TIMEOUT_DEFAULT = 5.0
HELP_TIMEOUT_MAX = 30.0
# 返回体要进 MCP 消息，不能塞进整本手册
HELP_OUTPUT_MAX_CHARS = 20000


def _clamp_timeout(timeout: object) -> float:
    """归一化调用方传入的 timeout：None/非数值 → 默认；负数 → 0；超封顶 → 封顶。"""
    if timeout is None:
        return HELP_TIMEOUT_DEFAULT
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return HELP_TIMEOUT_DEFAULT
    return max(0.0, min(value, HELP_TIMEOUT_MAX))


def resolve_help_command(tool: dict, timeout: object = None) -> dict[str, Any] | None:
    """解析该条目的帮助命令（不执行）。

    优先条目 `help.command`，回落 `launch.command`；参数优先 `help.args`，
    回落 `DEFAULT_HELP_ARGS`。两者都取不到 command → 返回 None（该条目没有
    可执行的帮助来源）。`timeout` 是调用方本次传入的覆盖值（优先级高于条目
    `help.timeout`），同样过归一化。
    """
    help_field = tool.get("help") or {}
    launch = tool.get("launch") or {}
    command = str(help_field.get("command") or launch.get("command") or "").strip()
    if not command:
        return None
    args = help_field.get("args")
    if args is None:
        args = list(DEFAULT_HELP_ARGS)
    return {
        "command": command,
        "args": [str(a) for a in args],
        "timeout": _clamp_timeout(
            timeout if timeout is not None else help_field.get("timeout")
        ),
    }


def entry_help(tool: dict) -> dict[str, Any]:
    """条目自身的帮助信息（文档链接 + 描述 + 将要执行的帮助命令预览）。"""
    data: dict[str, Any] = {
        "description": tool.get("description", ""),
        "docUrl": tool.get("docUrl", ""),
        "website": tool.get("website", ""),
    }
    resolved = resolve_help_command(tool)
    if resolved:
        data["helpCommand"] = {
            "command": resolved["command"],
            "args": resolved["args"],
            "timeout": resolved["timeout"],
        }
    return data


def backend_summary(tools: list[dict]) -> list[dict[str, Any]]:
    """后端子工具缓存 → 帮助摘要（名字 / 描述 / 参数名 / 必填参数）。

    不下发完整 `inputSchema`：那是 `list_capabilities` 的职责，帮助视图只要
    「这个后端能干什么、每个能力要哪些参数」。
    """
    summary = []
    for item in tools:
        schema = item.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        summary.append(
            {
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "params": list(properties.keys()),
                "required": list(schema.get("required") or []),
            }
        )
    return summary


def launcher_summary(actions: list[dict], limit: int = 50) -> list[dict[str, Any]]:
    """启动器动作清单 → 帮助摘要（截断到 limit，超出由调用方看 count）。"""
    summary = []
    for action in actions[:limit]:
        summary.append(
            {
                "id": action.get("id", ""),
                "name": action.get("name", ""),
                "type": action.get("type", ""),
            }
        )
    return summary


def _decode(raw: bytes) -> str:
    """按 UTF-8 严格解，失败回落 GBK（中文 Windows 控制台程序的常见输出）。

    两条都失败时用 replace 兜底——帮助文本读得出乱码总比整次调用报错好。
    """
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _failure(
    command: list[str], error_type: str, error: str, executed: bool = False
) -> dict[str, Any]:
    """失败载荷。

    `executed` 的语义是「**是否真的 spawn 过进程**」：预检失败（命令不存在 /
    无帮助来源）为 False；跑起来但没拿到输出、或超时被杀，进程副作用已经发生，
    必须报 True——否则调用方以为什么都没跑。
    """
    return {
        "success": False,
        "errorType": error_type,
        "error": error,
        "command": command,
        "executed": executed,
    }


async def run_help_command(resolved: dict[str, Any] | None) -> dict[str, Any]:
    """执行帮助命令并捕获输出（调用方已显式要求 `run=true` 才走到这里）。

    返回载荷：成功带 `output` / `exitCode` / `truncated`；失败带 `errorType`。
    非零退出但**有输出**时仍算成功（不少程序 `--help` 也返回非 0，文本本身
    才是调用方要的东西），用 `note` 标注退出码；非零且无输出才是真失败。
    """
    if not resolved:
        return _failure(
            [],
            ErrorType.CONFIG_ERROR,
            "条目没有可执行的帮助来源（既无 help.command 也无 launch.command）",
        )

    command = resolved["command"]
    args = resolved["args"]
    argv = [command, *args]
    resolved_path = shutil.which(command)
    if resolved_path is None:
        return _failure(
            argv, ErrorType.CONFIG_ERROR, f"帮助命令不存在或不在 PATH: {command}"
        )

    try:
        process = await asyncio.create_subprocess_exec(
            resolved_path,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return _failure(argv, ErrorType.CONFIG_ERROR, f"无法执行 {command}: {exc}")

    timeout = resolved["timeout"]
    try:
        with anyio.fail_after(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        # 挂住的帮助命令留着没意义：杀掉并报超时（已产出的文本一并回传）
        partial_out = partial_err = b""
        try:
            process.kill()
            partial_out, partial_err = await asyncio.wait_for(
                process.communicate(), timeout=2
            )
        except Exception:
            pass
        text = _combine(partial_out, partial_err)
        return _failure(
            argv,
            ErrorType.RUNTIME_ERROR,
            f"帮助命令超过 {timeout}s 未退出，已终止"
            + (f"；超时前的输出：\n{text}" if text else ""),
            executed=True,
        )

    exit_code = process.returncode
    text = _combine(stdout, stderr)
    truncated = len(text) > HELP_OUTPUT_MAX_CHARS
    if truncated:
        text = text[:HELP_OUTPUT_MAX_CHARS]

    if not text.strip():
        if exit_code == 0:
            return _failure(
                argv,
                ErrorType.RUNTIME_ERROR,
                "帮助命令无任何输出",
                executed=True,
            )
        return _failure(
            argv,
            ErrorType.RUNTIME_ERROR,
            f"帮助命令退出码 {exit_code} 且无输出",
            executed=True,
        )

    payload: dict[str, Any] = {
        "success": True,
        "executed": True,
        "command": argv,
        "exitCode": exit_code,
        "output": text,
        "truncated": truncated,
    }
    if exit_code != 0:
        payload["note"] = f"退出码 {exit_code}，但输出已捕获（部分程序的 --help 也非 0）"
    return payload


def _combine(stdout: bytes, stderr: bytes) -> str:
    """stdout 优先；stdout 为空时回落到 stderr（很多程序把 help 打到 stderr）。"""
    out = _decode(stdout).strip()
    err = _decode(stderr).strip()
    if out and err:
        return f"{out}\n\n[stderr]\n{err}"
    return out or err
