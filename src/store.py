"""JSON 存储层：读写工具条目和后端 MCP 子工具"""

import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _warn(message: str) -> None:
    """存储层告警：走 stderr，绝不能污染 MCP 的 stdout 协议通道"""
    print(f"[awam:store][warning] {message}", file=sys.stderr, flush=True)


class JsonStore:
    """简单的 JSON 文件存储"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.tools_dir = self.base_dir / "tools"
        self.backend_tools_dir = self.base_dir / "backend-tools"
        self.launcher_actions_dir = self.base_dir / "launcher-actions"

    def load_json(self, path: Path) -> dict[str, Any] | None:
        """读取 JSON 文件

        - 文件不存在 → None（正常情况，语义等同「尚未配置」）
        - JSON 损坏 / 顶层不是对象 / 读不动 → None + stderr 告警

        这里必须容错而不是让异常冒泡：load_json 会被 list_tools() 对每个
        文件循环调用，而这些 list_* 又是 resources、prompts、几乎全部
        只读工具的共同底座。一个坏文件若抛异常，等于单点损坏升级成全表
        不可用。宁可跳过这一个文件，也不能让服务整体起不来。
        """
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            _warn(f"JSON 解析失败，已跳过该文件: {path} ({e})")
            return None
        except OSError as e:
            _warn(f"文件读取失败，已跳过: {path} ({e})")
            return None

        if not isinstance(data, dict):
            _warn(f"文件内容不是 JSON 对象，已跳过: {path}（实际为 {type(data).__name__}）")
            return None
        return data

    def save_json(self, path: Path, data: dict[str, Any]) -> None:
        """原子写入 JSON 文件

        先写同目录临时文件，再 os.replace 覆盖目标。

        为什么不能直接 open(path, "w")：本服务是被 MCP 客户端拉起的子进程，
        随时可能被 kill。截断写进行到一半时进程退出，就会在磁盘上留下一份
        半个 JSON；下次 load_json 读到的就是它 —— 配合上面的容错会让该工具
        静默消失。os.replace 在同一分区是原子的，读端只会看到旧版本或新版本，
        不存在中间态。
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            # 写入中途失败不要让半成品 / 临时文件留在数据目录里堆积
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    # ---- 工具条目（tools/ 目录）----

    def get_tool_path(self, tool_id: str) -> Path:
        return self.tools_dir / f"{tool_id}.json"

    def load_tool(self, tool_id: str) -> dict[str, Any] | None:
        """读取单个工具条目"""
        return self.load_json(self.get_tool_path(tool_id))

    def save_tool(self, tool_id: str, data: dict[str, Any]) -> None:
        """保存单个工具条目"""
        self.save_json(self.get_tool_path(tool_id), data)

    def list_tools(self) -> list[dict[str, Any]]:
        """列出所有工具条目"""
        if not self.tools_dir.exists():
            return []
        tools = []
        for f in self.tools_dir.glob("*.json"):
            data = self.load_json(f)
            if data:
                tools.append(data)
        return tools

    # ---- 后端 MCP 子工具（backend-tools/ 目录）----

    def get_backend_tools_path(self, parent_id: str) -> Path:
        return self.backend_tools_dir / f"{parent_id}.json"

    def load_backend_tools(self, parent_id: str) -> list[dict[str, Any]]:
        """读取某个 MCP 工具的所有子工具"""
        data = self.load_json(self.get_backend_tools_path(parent_id))
        if data is None:
            return []
        return data.get("tools", [])

    def save_backend_tools(self, parent_id: str, tools: list[dict[str, Any]]) -> None:
        """保存某个 MCP 工具的所有子工具"""
        data = {
            "parent": parent_id,
            # 真实 UTC 时间戳，格式与 docs/format.md 的示例一致
            # （历史上这里写死的是字面量 "now"，无法判断缓存有多旧）
            "lastSynced": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "auto",
            "tools": tools
        }
        self.save_json(self.get_backend_tools_path(parent_id), data)

    def list_backend_tools(self) -> dict[str, list[dict[str, Any]]]:
        """列出所有后端 MCP 的子工具"""
        if not self.backend_tools_dir.exists():
            return {}
        result = {}
        for f in self.backend_tools_dir.glob("*.json"):
            parent_id = f.stem
            result[parent_id] = self.load_backend_tools(parent_id)
        return result

    # ---- 启动器动作清单（launcher-actions/ 目录）----
    # 与 backend-tools 同构：都是自动生成的「第二层数据」，区别只在于
    # 来源不是 MCP 的 tools/list，而是启动器本地数据（Quicker 的 states）。

    def get_launcher_actions_path(self, parent_id: str) -> Path:
        return self.launcher_actions_dir / f"{parent_id}.json"

    def load_launcher_actions(self, parent_id: str) -> list[dict[str, Any]]:
        """读取某个启动器的所有动作"""
        data = self.load_json(self.get_launcher_actions_path(parent_id))
        if data is None:
            return []
        return data.get("actions", [])

    def save_launcher_actions(self, parent_id: str, actions: list[dict[str, Any]], source: str) -> None:
        """保存某个启动器的动作清单

        `source` 区分完整同步（`auto`）与只拿到部分字段（`partial`）——
        Quicker 的动作名来自其 state 缓存，缓存缺失时只剩动作 ID 也要能落盘。
        """
        data = {
            "parent": parent_id,
            "lastSynced": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source,
            "actions": actions,
        }
        self.save_json(self.get_launcher_actions_path(parent_id), data)

    def list_launcher_actions(self) -> dict[str, list[dict[str, Any]]]:
        """列出所有启动器的动作"""
        if not self.launcher_actions_dir.exists():
            return {}
        result = {}
        for f in self.launcher_actions_dir.glob("*.json"):
            parent_id = f.stem
            result[parent_id] = self.load_launcher_actions(parent_id)
        return result
