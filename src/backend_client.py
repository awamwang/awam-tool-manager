"""后端 MCP 客户端：管理到各个后端 MCP Server 的连接和调用转发"""

import asyncio
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from store import JsonStore


class BackendClient:
    """单个后端 MCP Server 的客户端（懒加载，按需连接）"""

    def __init__(self, launcher_id: str, connection_config: dict):
        self.launcher_id = launcher_id
        self.connection_config = connection_config
        self.session: ClientSession | None = None
        self._connected = False
        self._lock = asyncio.Lock()
        self._stdio_context = None
        self._client_context = None

    async def _ensure_connected(self):
        """确保已连接到后端 MCP"""
        if self._connected and self.session:
            return

        async with self._lock:
            if self._connected:
                return

            conn_type = self.connection_config.get("type", "stdio")
            if conn_type != "stdio":
                raise NotImplementedError(f"暂不支持的连接类型: {conn_type}")

            command = self.connection_config.get("command")
            args = self.connection_config.get("args", [])
            env = self.connection_config.get("env")

            params = StdioServerParameters(command=command, args=args, env=env)

            # 启动子进程并连接
            self._stdio_context = stdio_client(params)
            read_stream, write_stream = await self._stdio_context.__aenter__()

            self._client_context = ClientSession(read_stream, write_stream)
            await self._client_context.__aenter__()
            self.session = self._client_context

            await self.session.initialize()
            self._connected = True

    async def list_tools(self) -> list[dict]:
        """列出后端 MCP 提供的所有工具"""
        await self._ensure_connected()
        tools = await self.session.list_tools()
        return [{"name": t.name, "description": t.description} for t in tools.tools]

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用后端 MCP 的工具"""
        await self._ensure_connected()
        result = await self.session.call_tool(tool_name, arguments)
        # 提取文本结果
        texts = []
        for content in result.content:
            if hasattr(content, "text"):
                texts.append(content.text)
        return "\n".join(texts)

    async def close(self):
        """关闭连接"""
        if not self._connected:
            return
        try:
            if self._client_context:
                await self._client_context.__aexit__(None, None, None)
            if self._stdio_context:
                await self._stdio_context.__aexit__(None, None, None)
        except Exception:
            pass
        self._connected = False
        self.session = None


class BackendManager:
    """后端 MCP 连接管理器"""

    def __init__(self, store: JsonStore):
        self.store = store
        self.clients: dict[str, BackendClient] = {}

    def get_client(self, launcher_id: str) -> BackendClient | None:
        """获取指定启动器的后端客户端（懒加载）"""
        if launcher_id in self.clients:
            return self.clients[launcher_id]

        launcher_config = self.store.load_launcher(launcher_id)
        if not launcher_config:
            return None

        mcp_config = launcher_config.get("mcp", {})
        connection = mcp_config.get("connection")
        if not connection:
            return None

        client = BackendClient(launcher_id, connection)
        self.clients[launcher_id] = client
        return client

    async def call_backend_tool(self, launcher_id: str, tool_name: str, arguments: dict) -> dict:
        """
        路由调用：转发到后端 MCP
        返回: { success: bool, result: str, error: str }
        """
        client = self.get_client(launcher_id)
        if not client:
            return {"success": False, "error": f"启动器 {launcher_id} 未配置后端 MCP 连接"}

        try:
            result = await client.call_tool(tool_name, arguments)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": f"调用后端失败: {str(e)}"}

    async def list_backend_tools(self, launcher_id: str) -> dict:
        """列出后端 MCP 提供的工具"""
        client = self.get_client(launcher_id)
        if not client:
            return {"success": False, "error": f"启动器 {launcher_id} 未配置后端 MCP 连接"}

        try:
            tools = await client.list_tools()
            return {"success": True, "tools": tools}
        except Exception as e:
            return {"success": False, "error": f"获取后端工具列表失败: {str(e)}"}

    async def close_all(self):
        """关闭所有连接"""
        for client in self.clients.values():
            await client.close()
        self.clients.clear()
