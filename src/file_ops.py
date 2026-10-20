"""`open_path` 的文件系统执行面。

两条纪律与 `app_launcher.py` 一致：

- **不走 shell**：预览只做 `Path` / `stat` 读取；执行用 `os.startfile`
  （ShellExecute）或 `explorer.exe /select,<path>` 的 argv 形式，路径里的
  元字符无机可乘；
- **spawn 的 stdio 全 DEVNULL**：explorer 不能继承本服务的协议管道（A8）。

默认路径（`execute=false`）由 handler 只调 `describe_path()`，本模块的
`open_target()` 只在显式执行时被调用。
"""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path


class FileOpError(Exception):
    """文件系统操作的业务级失败（路径为空 / 不存在 / ShellExecute 拒绝）"""


def resolve_path(raw: object) -> Path:
    """展开环境变量与 `~`，相对路径按服务进程 cwd 归一成绝对路径。"""
    text = str(raw or "").strip()
    if not text:
        raise FileOpError("path 不能为空")
    expanded = os.path.expandvars(os.path.expanduser(text))
    path = Path(expanded)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def describe_path(path: Path) -> dict:
    """只读元信息；不存在也如实返回（预览要能告诉调用方差在哪）。"""
    info = {
        "path": str(path),
        "exists": path.exists(),
        "isFile": False,
        "isDir": False,
        "size": None,
        "modified": None,
    }
    if not info["exists"]:
        return info
    try:
        stat = path.stat()
    except OSError:
        # 存在性探测与 stat 之间被删掉、或权限不足：仍按「存在但读不到」返回
        info["error"] = "路径存在但读取元信息失败"
        return info
    info["isFile"] = path.is_file()
    info["isDir"] = path.is_dir()
    info["size"] = stat.st_size
    info["modified"] = datetime.datetime.fromtimestamp(
        stat.st_mtime
    ).astimezone().isoformat(timespec="seconds")
    return info


async def open_target(path: Path, reveal: bool = False) -> dict:
    """真正打开路径（或定位到所在文件夹），返回投递结果。

    - `reveal=True`：`explorer.exe /select,<path>`，只定位不打开目标文件；
      explorer 的退出码不可信（成功也常返回非 0），因此按「已投递」报告；
    - `reveal=False`：`os.startfile()` 走 ShellExecute（文件 / 文件夹 / 已关联
      程序均可），调用返回即代表系统已接受。
    """
    if os.name != "nt":
        raise FileOpError("open_path 仅支持 Windows（依赖 ShellExecute / explorer）")
    if not path.exists():
        raise FileOpError(f"路径不存在: {path}")

    if reveal:
        try:
            process = await asyncio.create_subprocess_exec(
                "explorer.exe",
                f"/select,{path}",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise FileOpError(f"无法启动 explorer.exe: {exc}") from exc
        return {
            "delivered": True,
            "pid": process.pid,
            "command": f'explorer.exe "/select,{path}"',
            "note": "已请求资源管理器定位到该路径；explorer 的退出码不可信，不做成败判定",
        }

    try:
        # Windows-only API；非 Windows 已在上面拦掉
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError as exc:
        raise FileOpError(f"ShellExecute 无法打开 {path}: {exc}") from exc
    return {
        "delivered": True,
        "command": f'ShellExecute "{path}"',
        "note": "已交给系统默认程序打开（本服务无法确认目标程序最终是否呈现）",
    }
