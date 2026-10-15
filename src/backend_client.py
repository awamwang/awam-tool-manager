"""后端 MCP 客户端：管理到各个后端 MCP Server 的连接和调用转发

支持：启动/停止/状态/日志/心跳检测/工具列表缓存
"""

import asyncio
import time
from collections import deque
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from store import JsonStore


class BackendClient:
    """单个后端 MCP Server 的客户端（常驻连接，支持管理操作）"""

    STATUS_STOPPED = "stopped"
    STATUS_STARTING = "starting"
    STATUS_RUNNING = "running"
    STATUS_ERROR = "error"

    def __init__(self, mcp_tool_id: str, connection_config: dict):
        self.mcp_tool_id = mcp_tool_id
        self.connection_config = connection_config
        self.session: ClientSession | None = None
        self.status = self.STATUS_STOPPED
        self.last_error: str | None = None
        self.started_at: float | None = None

        # 日志
        self.logs: deque = deque(maxlen=500)  # 最多保留 500 条
        self.log_level = "info"  # debug / info / warning / error

        # 工具列表缓存
        self._cached_tools: list[dict] | None = None
        self._tools_cache_time: float = 0

        # 心跳
        self._heartbeat_task: asyncio.Task | None = None
        self._last_heartbeat: float = 0

        self._lock = asyncio.Lock()
        self._stdio_context = None
        self._client_context = None

    def _log(self, level: str, message: str):
        """记录日志"""
        import sys
        timestamp = time.strftime("%H:%M:%S")
        self.logs.append({"time": timestamp, "level": level, "message": message})
        # 打印到 stderr，不能污染 stdout 协议通道
        print(f"[awam:{self.mcp_tool_id}][{level}] {message}", file=sys.stderr, flush=True)

    async def start(self) -> dict:
        """启动后端 MCP Server 并建立连接"""
        if self.status == self.STATUS_RUNNING:
            return {"success": True, "message": "已在运行中"}

        async with self._lock:
            if self.status == self.STATUS_RUNNING:
                return {"success": True, "message": "已在运行中"}

            self.status = self.STATUS_STARTING
            self.last_error = None
            self._log("info", "正在启动后端 MCP...")

            try:
                conn_type = self.connection_config.get("type", "stdio")
                if conn_type != "stdio":
                    raise NotImplementedError(f"暂不支持的连接类型: {conn_type}")

                command = self.connection_config.get("command")
                args = self.connection_config.get("args", [])
                env = self.connection_config.get("env")

                self._log("info", f"启动命令: {command} {' '.join(args)}")

                params = StdioServerParameters(command=command, args=args, env=env)

                # 启动子进程并连接
                self._stdio_context = stdio_client(params)
                read_stream, write_stream = await self._stdio_context.__aenter__()

                self._client_context = ClientSession(read_stream, write_stream)
                await self._client_context.__aenter__()
                self.session = self._client_context

                await self.session.initialize()
                self.status = self.STATUS_RUNNING
                self.started_at = time.time()
                self._last_heartbeat = time.time()
                self._log("info", "✅ 连接成功")

                # 启动心跳检测
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                # 预热工具列表
                await self._refresh_tools_cache()

                return {"success": True, "message": "启动成功"}

            except Exception as e:
                self.status = self.STATUS_ERROR
                self.last_error = f"{type(e).__name__}: {e}"
                self._log("error", f"❌ 启动失败: {self.last_error}")
                # 清理资源
                await self._cleanup()
                return {"success": False, "error": self.last_error}

    async def stop(self) -> dict:
        """停止后端 MCP Server"""
        if self.status == self.STATUS_STOPPED:
            return {"success": True, "message": "已停止"}

        self._log("info", "正在停止...")

        # 取消心跳任务
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        await self._cleanup()
        self.status = self.STATUS_STOPPED
        self._cached_tools = None
        self._log("info", "已停止")
        return {"success": True, "message": "已停止"}

    async def _cleanup(self):
        """清理连接资源"""
        try:
            if self._client_context:
                await self._client_context.__aexit__(None, None, None)
                self._client_context = None
            if self._stdio_context:
                await self._stdio_context.__aexit__(None, None, None)
                self._stdio_context = None
        except Exception as e:
            self._log("warning", f"清理连接时出错: {e}")
        self.session = None

    async def _heartbeat_loop(self):
        """心跳检测循环"""
        while self.status == self.STATUS_RUNNING:
            try:
                await asyncio.sleep(30)  # 每 30 秒检测一次
                if self.session:
                    await self.session.ping()
                    self._last_heartbeat = time.time()
                    self._log("debug", "心跳正常")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log("warning", f"心跳失败: {e}")
                # 心跳失败，标记为错误
                self.status = self.STATUS_ERROR
                self.last_error = f"心跳失败: {e}"
                break

    async def _refresh_tools_cache(self):
        """刷新工具列表缓存"""
        if not self.session:
            return
        try:
            tools = await self.session.list_tools()
            self._cached_tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema
                }
                for t in tools.tools
            ]
            self._tools_cache_time = time.time()
            self._log("info", f"工具列表已刷新，共 {len(self._cached_tools)} 个工具")
        except Exception as e:
            self._log("warning", f"刷新工具列表失败: {e}")

    async def list_tools(self, force_refresh: bool = False) -> list[dict]:
        """列出后端 MCP 提供的所有工具（带缓存）"""
        if self.status != self.STATUS_RUNNING:
            raise ConnectionError(f"后端未运行，当前状态: {self.status}")

        # 缓存有效期 5 分钟
        cache_age = time.time() - self._tools_cache_time
        if force_refresh or not self._cached_tools or cache_age > 300:
            await self._refresh_tools_cache()

        return self._cached_tools or []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用后端 MCP 的工具"""
        if self.status != self.STATUS_RUNNING:
            raise ConnectionError(f"后端未运行，当前状态: {self.status}")

        self._log("debug", f"调用工具: {tool_name}")

        try:
            result = await self.session.call_tool(tool_name, arguments)
        except Exception as e:
            self._log("error", f"调用工具失败: {e}")
            raise RuntimeError(f"后端 MCP 调用工具 '{tool_name}' 失败: {type(e).__name__}: {e}") from e

        # 检查后端返回是否有错误
        if hasattr(result, "isError") and result.isError:
            error_texts = []
            for content in result.content:
                if hasattr(content, "text"):
                    error_texts.append(content.text)
            error_msg = f"后端 MCP 返回错误: {' | '.join(error_texts)}"
            self._log("error", error_msg)
            raise RuntimeError(error_msg)

        # 提取文本结果
        texts = []
        for content in result.content:
            if hasattr(content, "text"):
                texts.append(content.text)
        return "\n".join(texts)

    def get_status(self) -> dict:
        """获取状态信息"""
        uptime = None
        if self.started_at and self.status == self.STATUS_RUNNING:
            uptime = round(time.time() - self.started_at, 1)

        return {
            "mcp_tool_id": self.mcp_tool_id,
            "status": self.status,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "uptime_seconds": uptime,
            "log_level": self.log_level,
            "tools_count": len(self._cached_tools) if self._cached_tools else 0,
            "last_heartbeat": self._last_heartbeat,
        }

    def get_logs(self, level: str | None = None, limit: int = 50) -> list[dict]:
        """获取日志"""
        logs = list(self.logs)
        if level:
            logs = [l for l in logs if l["level"] == level]
        return logs[-limit:]

    def set_log_level(self, level: str) -> dict:
        """设置日志级别"""
        valid_levels = ["debug", "info", "warning", "error"]
        if level not in valid_levels:
            return {"success": False, "error": f"无效的日志级别，可选: {valid_levels}"}
        self.log_level = level
        self._log("info", f"日志级别设置为: {level}")
        return {"success": True, "log_level": level}


class BackendManager:
    """后端 MCP 连接管理器"""

    def __init__(self, store: JsonStore):
        self.store = store
        self.clients: dict[str, BackendClient] = {}

    def get_client(self, mcp_tool_id: str) -> BackendClient | None:
        """获取指定 MCP 工具的后端客户端"""
        if mcp_tool_id in self.clients:
            return self.clients[mcp_tool_id]

        tool_config = self.store.load_tool(mcp_tool_id)
        if not tool_config:
            return None

        # 只有 mcp 类型的工具才有后端连接
        if tool_config.get("kind") != "mcp":
            return None

        connection = tool_config.get("connection")
        if not connection:
            return None

        client = BackendClient(mcp_tool_id, connection)
        self.clients[mcp_tool_id] = client
        return client

    def list_mcp_tools(self) -> list[str]:
        """列出所有已配置的 MCP 类工具 ID"""
        tools = self.store.list_tools()
        return [t["id"] for t in tools if t.get("kind") == "mcp"]

    async def start_backend(self, mcp_tool_id: str) -> dict:
        """启动指定后端，成功后自动同步工具目录"""
        client = self.get_client(mcp_tool_id)
        if not client:
            return {"success": False, "error": f"启动器 '{mcp_tool_id}' 未配置后端 MCP 连接"}

        result = await client.start()
        if result.get("success"):
            # 自动同步工具目录
            sync_result = await self.sync_tools_catalog(mcp_tool_id)
            if sync_result.get("success"):
                result["message"] += f"\n工具目录已自动同步：{sync_result.get('count', 0)} 个工具"
            else:
                result["message"] += f"\n工具目录同步失败：{sync_result.get('error', '')}"
        return result

    async def sync_tools_catalog(self, mcp_tool_id: str) -> dict:
        """同步后端工具目录到 backend-tools/"""
        client = self.get_client(mcp_tool_id)
        if not client:
            return {"success": False, "error": f"MCP 工具 '{mcp_tool_id}' 未配置"}

        if client.status != BackendClient.STATUS_RUNNING:
            return {"success": False, "error": "后端未运行，请先启动"}

        try:
            tools = await client.list_tools(force_refresh=True)

            # 保存到 backend-tools/ 目录
            self.store.save_backend_tools(mcp_tool_id, tools)

            return {"success": True, "count": len(tools)}
        except Exception as e:
            return {"success": False, "error": f"同步失败: {type(e).__name__}: {e}"}

    async def stop_backend(self, mcp_tool_id: str) -> dict:
        """停止指定后端"""
        client = self.get_client(mcp_tool_id)
        if not client:
            return {"success": False, "error": f"启动器 '{mcp_tool_id}' 未配置后端 MCP 连接"}
        return await client.stop()

    def get_backend_status(self, mcp_tool_id: str | None = None) -> dict:
        """获取后端状态"""
        if mcp_tool_id:
            client = self.get_client(mcp_tool_id)
            if not client:
                return {"success": False, "error": f"MCP 工具 '{mcp_tool_id}' 未配置"}
            return {"success": True, "status": client.get_status()}

        # 返回所有后端状态
        all_status = []
        for mid in self.list_mcp_tools():
            client = self.get_client(mid)
            if client:
                all_status.append(client.get_status())
        return {"success": True, "backends": all_status}

    def get_backend_logs(self, mcp_tool_id: str, level: str | None = None, limit: int = 50) -> dict:
        """获取后端日志"""
        client = self.get_client(mcp_tool_id)
        if not client:
            return {"success": False, "error": f"启动器 '{mcp_tool_id}' 未配置"}
        return {"success": True, "logs": client.get_logs(level, limit)}

    def set_backend_log_level(self, mcp_tool_id: str, level: str) -> dict:
        """设置后端日志级别"""
        client = self.get_client(mcp_tool_id)
        if not client:
            return {"success": False, "error": f"启动器 '{mcp_tool_id}' 未配置"}
        return client.set_log_level(level)

    async def call_backend_tool(self, mcp_tool_id: str, tool_name: str, arguments: dict) -> dict:
        """路由调用：转发到后端 MCP"""
        client = self.get_client(mcp_tool_id)
        if not client:
            return {
                "success": False,
                "errorType": "config_error",
                "error": f"启动器 '{mcp_tool_id}' 未配置后端 MCP 连接"
            }

        try:
            result = await client.call_tool(tool_name, arguments)
            return {"success": True, "result": result}
        except ConnectionError as e:
            return {
                "success": False,
                "errorType": "connection_error",
                "error": f"无法连接后端 MCP: {e}"
            }
        except RuntimeError as e:
            return {
                "success": False,
                "errorType": "runtime_error",
                "error": str(e)
            }
        except Exception as e:
            return {
                "success": False,
                "errorType": "unknown_error",
                "error": f"未知错误 ({type(e).__name__}): {e}"
            }

    async def list_backend_tools(self, mcp_tool_id: str, force_refresh: bool = False) -> dict:
        """列出后端 MCP 提供的工具（带完整参数 schema）"""
        client = self.get_client(mcp_tool_id)
        if not client:
            return {"success": False, "error": f"启动器 {mcp_tool_id} 未配置后端 MCP 连接"}

        try:
            tools = await client.list_tools(force_refresh=force_refresh)
            return {"success": True, "tools": tools}
        except Exception as e:
            return {"success": False, "error": f"获取后端工具列表失败: {str(e)}"}

    async def close_all(self):
        """关闭所有连接"""
        for client in self.clients.values():
            await client.stop()
        self.clients.clear()
