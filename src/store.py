"""JSON 存储层：读写工具条目和后端 MCP 子工具"""

import json
from pathlib import Path
from typing import Any


class JsonStore:
    """简单的 JSON 文件存储"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.tools_dir = self.base_dir / "tools"
        self.backend_tools_dir = self.base_dir / "backend-tools"

    def load_json(self, path: Path) -> dict[str, Any] | None:
        """读取 JSON 文件，不存在返回 None"""
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_json(self, path: Path, data: dict[str, Any]) -> None:
        """写入 JSON 文件"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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
            "lastSynced": "now",
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
