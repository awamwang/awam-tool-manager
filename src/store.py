"""JSON 存储层：读写启动器配置和工具条目"""

import json
from pathlib import Path
from typing import Any


class JsonStore:
    """简单的 JSON 文件存储"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.launchers_dir = self.base_dir / "launchers"
        self.tools_dir = self.base_dir / "tools"

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

    # ---- 启动器 ----

    def get_launcher_path(self, launcher_id: str) -> Path:
        return self.launchers_dir / f"{launcher_id}.json"

    def load_launcher(self, launcher_id: str) -> dict[str, Any] | None:
        return self.load_json(self.get_launcher_path(launcher_id))

    def save_launcher(self, launcher_id: str, data: dict[str, Any]) -> None:
        self.save_json(self.get_launcher_path(launcher_id), data)

    def list_launchers(self) -> list[str]:
        """列出所有已注册的启动器 ID"""
        if not self.launchers_dir.exists():
            return []
        return [f.stem for f in self.launchers_dir.glob("*.json")]

    # ---- 工具 ----

    def get_tools_path(self, source: str) -> Path:
        return self.tools_dir / f"{source}.json"

    def load_tools(self, source: str) -> list[dict[str, Any]]:
        """读取某个启动器的所有工具"""
        data = self.load_json(self.get_tools_path(source))
        if data is None:
            return []
        return data.get("tools", [])

    def save_tools(self, source: str, tools: list[dict[str, Any]]) -> None:
        """保存某个启动器的所有工具"""
        data = {
            "schemaVersion": "1.0",
            "source": source,
            "tools": tools
        }
        self.save_json(self.get_tools_path(source), data)
