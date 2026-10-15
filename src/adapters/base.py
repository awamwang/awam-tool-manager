"""启动器适配器基类"""

from abc import ABC, abstractmethod
from typing import Any


class LauncherAdapter(ABC):
    """启动器适配器抽象基类"""

    @property
    @abstractmethod
    def launcher_id(self) -> str:
        """启动器唯一标识"""
        ...

    @abstractmethod
    def detect(self) -> dict[str, Any]:
        """
        检测本机是否安装了该启动器
        返回: { installed: bool, installPath: str, version: str }
        """
        ...

    @abstractmethod
    def get_mcp_recommendation(self) -> dict[str, Any]:
        """
        推荐适配的 MCP Server
        返回: { serverName, installMethod, installCommand, description }
        """
        ...

    def sync_tools(self) -> list[dict[str, Any]]:
        """
        通过 MCP 拉取子工具列表并注册
        默认返回空，子类按需实现
        """
        return []
