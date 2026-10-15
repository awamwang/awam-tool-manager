"""Everything 启动器适配器"""

import shutil
from pathlib import Path

from adapters.base import LauncherAdapter


class EverythingAdapter(LauncherAdapter):
    """Everything 文件搜索适配器"""

    @property
    def launcher_id(self) -> str:
        return "everything"

    def detect(self) -> dict:
        """检测本机是否安装了 Everything"""
        # 常见安装路径
        common_paths = [
            Path(r"C:\Program Files\Everything\Everything.exe"),
            Path(r"C:\Program Files (x86)\Everything\Everything.exe"),
        ]

        for p in common_paths:
            if p.exists():
                return {
                    "installed": True,
                    "installPath": str(p),
                    "version": self._get_version(p),
                }

        # 也可以通过 where 命令找
        found = shutil.which("Everything.exe")
        if found:
            return {
                "installed": True,
                "installPath": found,
                "version": "unknown",
            }

        return {"installed": False, "installPath": None, "version": None}

    def get_mcp_recommendation(self) -> dict:
        """推荐适配的 MCP Server"""
        return {
            "serverName": "everything-mcp",
            "installMethod": "npm",
            "installCommand": "npm i -g everything-mcp",
            "packageName": "everything-mcp",
            "description": "Everything 的 MCP Server，支持文件搜索",
            "capabilities": ["search", "get_info"],
        }

    def _get_version(self, exe_path: Path) -> str:
        """获取版本号（简化实现）"""
        # TODO: 实际读取文件版本信息
        return "1.4.1"
