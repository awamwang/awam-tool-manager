"""后端 MCP 客户端：管理到各个后端 MCP Server 的连接和调用转发

支持：启动/停止/状态/日志/心跳检测/工具列表缓存
"""

import asyncio
import contextlib
import sys
import time
from collections import deque

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from store import JsonStore


class BackendBusyError(Exception):
    """后端正在处理其他调用，并发请求被直接拒绝（见 A3 方案 2：单后端串行 + 超限报 busy）

    与 ConnectionError / RuntimeError 区分：它不表示「坏了」，而是「现在忙」，
    调用方可以稍后重试。manager.call_backend_tool 会把它映射成 errorType=backend_busy。
    """


class BackendClient:
    """单个后端 MCP Server 的客户端（常驻连接，支持管理操作）"""

    STATUS_STOPPED = "stopped"
    STATUS_STARTING = "starting"
    STATUS_RUNNING = "running"
    STATUS_ERROR = "error"

    # 心跳间隔 / 心跳探测超时 / 各类后端操作的默认超时（秒）
    HEARTBEAT_INTERVAL = 30
    HEARTBEAT_TIMEOUT = 10
    DEFAULT_TIMEOUT = 30.0
    # 工具列表缓存有效期（秒）
    TOOLS_CACHE_TTL = 300

    # A3 方案 2：单后端串行，任意时刻只允许一个 call_tool 在飞；
    # 并发调用直接报 backend_busy（不排队）。要改造成「信号量上限 + 等待队列」，
    # 只需把这里调大并配合 asyncio.Semaphore + 排队超时，其余逻辑不变。
    MAX_CONCURRENT_CALLS = 1

    VALID_LOG_LEVELS = ["debug", "info", "warning", "error"]
    # 级别优先级：低于当前 log_level 的消息不入队
    LOG_LEVEL_PRIORITY = {"debug": 10, "info": 20, "warning": 30, "error": 40}

    def __init__(self, tool_id: str, connection_config: dict):
        self.tool_id = tool_id
        self.connection_config = connection_config
        self.session: ClientSession | None = None
        self.status = self.STATUS_STOPPED
        self.last_error: str | None = None
        self.started_at: float | None = None

        # 后端调用超时，可在 connection 配置里用 timeout 覆盖
        self.timeout = float(connection_config.get("timeout") or self.DEFAULT_TIMEOUT)

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
        # 调用串行锁（A3 方案 2）：同一后端任意时刻只允许一个 call_tool 在飞，
        # 避免对同一个 stdio 会话并发发 JSON-RPC 造成的竞态。与 _lock（start/stop
        # 串行）分离，二者职责不同，不能共用同一把锁，否则会互相阻塞。
        self._call_lock = asyncio.Lock()
        # cleanup 独立的锁：心跳失败、stop()、start() 的失败回滚都可能触发清理，
        # 用一把专用锁保证清理串行执行且幂等，避免两个协程同时 __aexit__ 同一个 context
        self._cleanup_lock = asyncio.Lock()
        self._stdio_context = None
        self._client_context = None

    def _log(self, level: str, message: str):
        """记录日志

        过滤必须发生在**入队之前**。self.logs 是定长 500 的 deque：心跳每 30s
        一条 debug，不在这一层拦掉的话，约 4 小时就能把队列填满并把真正的
        error 挤出去 —— 而排查故障时最需要的恰恰就是被挤掉的那部分。
        只在 get_logs() 里按 level 筛是没用的，那时数据已经丢了。
        """
        if self.LOG_LEVEL_PRIORITY.get(level, 20) < self.LOG_LEVEL_PRIORITY.get(self.log_level, 20):
            return

        timestamp = time.strftime("%H:%M:%S")
        self.logs.append({"time": timestamp, "level": level, "message": message})
        # 打印到 stderr，不能污染 stdout 协议通道
        print(f"[awam:{self.tool_id}][{level}] {message}", file=sys.stderr, flush=True)

    async def start(self) -> dict:
        """启动后端 MCP Server 并建立连接"""
        if self.status == self.STATUS_RUNNING:
            return {"success": True, "message": "已在运行中"}

        async with self._lock:
            if self.status == self.STATUS_RUNNING:
                return {"success": True, "message": "已在运行中"}

            # 上一次连接可能残留（例如心跳失败后置为 error 但没清理），
            # 必须先清理干净，否则旧的子进程会变成孤儿进程
            if self._stdio_context or self._client_context or self.session:
                self._log("warning", "检测到上次连接的残留资源，先清理")
                await self._cleanup()

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

                # args 里可能是数字/布尔，拼接前统一转字符串
                self._log("info", f"启动命令: {command} {' '.join(str(a) for a in args)}")

                params = StdioServerParameters(command=command, args=args, env=env)

                # 启动子进程并连接（整体限时，避免子进程卡住拖死整个服务）
                self._stdio_context = stdio_client(params)
                read_stream, write_stream = await asyncio.wait_for(
                    self._stdio_context.__aenter__(), timeout=self.timeout
                )

                self._client_context = ClientSession(read_stream, write_stream)
                await self._client_context.__aenter__()
                self.session = self._client_context

                await asyncio.wait_for(self.session.initialize(), timeout=self.timeout)
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
        """停止后端 MCP Server

        与 start() 共用同一把锁：并发调用时不会有「start 刚建好连接、stop 已把它
        标记为 stopped」这类状态与真实连接不一致的情况。
        """
        if self.status == self.STATUS_STOPPED:
            return {"success": True, "message": "已停止"}

        # 取消心跳任务
        task, self._heartbeat_task = self._heartbeat_task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        async with self._lock:
            if self.status == self.STATUS_STOPPED:
                return {"success": True, "message": "已停止"}

            self._log("info", "正在停止...")
            await self._cleanup()

        self.status = self.STATUS_STOPPED
        self._cached_tools = None
        self._tools_cache_time = 0
        self.started_at = None
        self._log("info", "已停止")
        return {"success": True, "message": "已停止"}

    async def _cleanup(self):
        """清理连接资源（串行且幂等）

        - client 与 stdio 两个 context 必须各自独立 try：
          共用 try 时，若第一个 __aexit__ 抛异常，第二个就不会执行，
          而 stdio context 持有子进程，不 exit 会留下孤儿进程。
        - 并发调用时被 _cleanup_lock 串行化，重复调用直接返回。
        """
        async with self._cleanup_lock:
            if not (self._client_context or self._stdio_context or self.session):
                return

            try:
                if self._client_context:
                    await self._client_context.__aexit__(None, None, None)
            except Exception as e:
                self._log("warning", f"清理会话时出错: {e}")
            finally:
                self._client_context = None

            try:
                if self._stdio_context:
                    await self._stdio_context.__aexit__(None, None, None)
            except Exception as e:
                self._log("warning", f"清理 stdio 连接时出错: {e}")
            finally:
                self._stdio_context = None

            self.session = None

    def _mark_broken(self, reason: str):
        """连接已不可用时调用：置为 error 并在后台清理，避免继续复用坏连接"""
        self.status = self.STATUS_ERROR
        self.last_error = reason
        self._log("error", f"连接不可用: {reason}")
        if self._stdio_context or self._client_context:
            asyncio.create_task(self._safe_cleanup())

    async def _safe_cleanup(self):
        """后台清理，吞掉异常避免在 task 里抛出无人接收的异常"""
        try:
            await self._cleanup()
        except Exception as e:
            self._log("warning", f"自动清理失败: {e}")

    async def _heartbeat_loop(self):
        """心跳检测循环"""
        while self.status == self.STATUS_RUNNING:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                if self.session:
                    await asyncio.wait_for(
                        self.session.ping(), timeout=self.HEARTBEAT_TIMEOUT
                    )
                    self._last_heartbeat = time.time()
                    self._log("debug", "心跳正常")
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 心跳失败说明连接已断：标记不可用并主动清理，
                # 否则子进程会一直残留，且状态会停在虚假的 running
                self.status = self.STATUS_ERROR
                self.last_error = f"心跳失败: {e}"
                self._log("warning", f"心跳失败，连接标记为不可用: {e}")
                await self._cleanup()
                break

    async def _refresh_tools_cache(self):
        """刷新工具列表缓存"""
        if not self.session:
            return
        try:
            tools = await asyncio.wait_for(
                self.session.list_tools(), timeout=self.timeout
            )
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
        except asyncio.TimeoutError:
            self._mark_broken(f"刷新工具列表超时（{self.timeout}s）")
        except Exception as e:
            self._log("warning", f"刷新工具列表失败: {e}")

    async def list_tools(self, force_refresh: bool = False) -> list[dict]:
        """列出后端 MCP 提供的所有工具（带缓存）"""
        if self.status != self.STATUS_RUNNING:
            raise ConnectionError(f"后端未运行，当前状态: {self.status}")

        # 缓存有效期见 TOOLS_CACHE_TTL
        cache_age = time.time() - self._tools_cache_time
        if force_refresh or not self._cached_tools or cache_age > self.TOOLS_CACHE_TTL:
            await self._refresh_tools_cache()

        return self._cached_tools or []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用后端 MCP 的工具

        A3 方案 2（单后端串行 + 超限报 busy）：任意时刻只允许一个 call_tool 在飞。
        若已有调用在执行，立即抛 BackendBusyError 由上层转成 backend_busy，
        不排队、不积压。这样既能避免对同一个 stdio 会话并发发 JSON-RPC 的竞态，
        又不会因无界排队把请求闷在服务内部。
        """
        if self.status != self.STATUS_RUNNING:
            raise ConnectionError(f"后端未运行，当前状态: {self.status}")

        # 单后端串行：已有调用在飞则直接拒绝（只判断不阻塞，避免此处自己 await 造成误判）
        if self._call_lock.locked():
            raise BackendBusyError(
                f"后端工具 '{self.tool_id}' 正在处理其他调用，"
                f"请稍后重试（单后端串行，并发调用会直接拒绝）"
            )

        self._log("debug", f"调用工具: {tool_name}")

        async with self._call_lock:
            try:
                result = await asyncio.wait_for(
                    self.session.call_tool(tool_name, arguments), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                # 超时通常意味着连接已经废了，标记后让后续请求快速失败而不是继续卡
                reason = f"调用工具 '{tool_name}' 超时（{self.timeout}s）"
                self._mark_broken(reason)
                raise TimeoutError(reason) from None
            except Exception as e:
                self._log("error", f"调用工具失败: {e}")
                raise RuntimeError(
                    f"后端 MCP 调用工具 '{tool_name}' 失败: {type(e).__name__}: {e}"
                ) from e

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
            "tool_id": self.tool_id,
            "status": self.status,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "uptime_seconds": uptime,
            "log_level": self.log_level,
            "tools_count": len(self._cached_tools) if self._cached_tools else 0,
            "last_heartbeat": self._last_heartbeat,
            "timeout": self.timeout,
        }

    def get_logs(self, level: str | None = None, limit: int = 50) -> list[dict]:
        """获取最近的日志

        limit<=0 明确返回空列表。这里不能用 logs[-limit:] 兜底：Python 里
        -0 == 0，切片 [0:] 返回的是**全部**日志 —— 传 0 本意是「不要」，
        结果变成「全量」，语义正好相反。
        """
        logs = list(self.logs)
        if level:
            logs = [l for l in logs if l["level"] == level]

        if limit is None or limit <= 0:
            return []
        return logs[-limit:]

    def set_log_level(self, level: str) -> dict:
        """设置日志级别"""
        valid_levels = self.VALID_LOG_LEVELS
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

    def get_client(self, tool_id: str) -> BackendClient | None:
        """获取（或按需创建）指定 MCP 工具的后端客户端

        缓存会被复用，但创建时快照了一份 connection 配置用于比对：

        - 工具已从磁盘删除 / 不再是 mcp 类型 / 没有 connection → 不再返回旧
          缓存对象，避免出现「配置项已删但还能启后端」的幽灵连接；
        - 空闲（stopped/error，即没有活子进程）且磁盘配置已变 → 就地重建，
          改完 tools/*.json 不必重启 MCP Server 就能生效；
        - 正在运行/启动中时**绝不重建**：那会把正在跑的子进程丢成孤儿，
          且调用方手里的旧引用与被替换的新对象从此不是同一个。
        """
        # 运行中的连接先返回：热替换会让调用方手里的引用与新对象分家，
        # 也会把活着的子进程丢成孤儿。改配置请先 stop 再 start。
        client = self.clients.get(tool_id)
        if client is not None and client.status not in (
            BackendClient.STATUS_STOPPED,
            BackendClient.STATUS_ERROR,
        ):
            return client

        # 空闲（stopped/error）或从未连接：一律以磁盘上的当前配置为准
        tool_config = self.store.load_tool(tool_id)
        if not tool_config:
            return None

        # 只有 mcp 类型的工具才有后端连接
        if tool_config.get("kind") != "mcp":
            return None

        connection = tool_config.get("connection")
        if not connection:
            return None

        # 配置没变就继续复用旧对象，避免平白丢掉日志历史
        if client is not None and client.connection_config == connection:
            return client

        previous = self.clients.pop(tool_id, None)
        client = BackendClient(tool_id, connection)
        if previous is not None:
            # 继承日志与日志级别：重建不应让运维刚查过的历史日志凭空消失
            client.logs = previous.logs
            client.log_level = previous.log_level
        self.clients[tool_id] = client
        return client

    def list_mcp_tools(self) -> list[str]:
        """列出所有已配置的 MCP 类工具 ID"""
        tools = self.store.list_tools()
        return [t["id"] for t in tools if t.get("kind") == "mcp"]

    async def start_backend(self, tool_id: str) -> dict:
        """启动指定后端，成功后自动同步工具目录"""
        client = self.get_client(tool_id)
        if not client:
            return {"success": False, "error": f"工具 '{tool_id}' 未配置 MCP 后端连接"}

        result = await client.start()
        if result.get("success"):
            # 自动同步工具目录
            sync_result = await self.sync_tools_catalog(tool_id)
            if sync_result.get("success"):
                result["message"] += f"\n工具目录已自动同步：{sync_result.get('count', 0)} 个工具"
            else:
                result["message"] += f"\n工具目录同步失败：{sync_result.get('error', '')}"
        return result

    async def sync_tools_catalog(self, tool_id: str) -> dict:
        """同步后端工具目录到 backend-tools/"""
        client = self.get_client(tool_id)
        if not client:
            return {"success": False, "error": f"MCP 工具 '{tool_id}' 未配置"}

        if client.status != BackendClient.STATUS_RUNNING:
            return {"success": False, "error": "后端未运行，请先启动"}

        try:
            tools = await client.list_tools(force_refresh=True)

            # 后端异常时 list_tools 可能返回空列表，此时写盘会用空结果覆盖上一次
            # 成功同步的缓存，导致已缓存的工具目录永久丢失。宁可不同步。
            if not tools:
                return {
                    "success": False,
                    "error": "后端返回空工具列表，保留原缓存未覆盖"
                }

            # 保存到 backend-tools/ 目录
            self.store.save_backend_tools(tool_id, tools)

            return {"success": True, "count": len(tools)}
        except Exception as e:
            return {"success": False, "error": f"同步失败: {type(e).__name__}: {e}"}

    async def stop_backend(self, tool_id: str) -> dict:
        """停止指定后端"""
        client = self.get_client(tool_id)
        if not client:
            return {"success": False, "error": f"工具 '{tool_id}' 未配置 MCP 后端连接"}
        return await client.stop()

    def get_backend_status(self, tool_id: str | None = None) -> dict:
        """获取后端状态"""
        if tool_id:
            client = self.get_client(tool_id)
            if not client:
                return {"success": False, "error": f"MCP 工具 '{tool_id}' 未配置"}
            return {"success": True, "status": client.get_status()}

        # 返回所有后端状态
        all_status = []
        for mid in self.list_mcp_tools():
            client = self.get_client(mid)
            if client:
                all_status.append(client.get_status())
        return {"success": True, "backends": all_status}

    def get_backend_logs(self, tool_id: str, level: str | None = None, limit: int = 50) -> dict:
        """获取后端日志"""
        client = self.get_client(tool_id)
        if not client:
            return {"success": False, "error": f"工具 '{tool_id}' 未配置"}
        return {"success": True, "logs": client.get_logs(level, limit)}

    def set_backend_log_level(self, tool_id: str, level: str) -> dict:
        """设置后端日志级别"""
        client = self.get_client(tool_id)
        if not client:
            return {"success": False, "error": f"工具 '{tool_id}' 未配置"}
        return client.set_log_level(level)

    async def call_backend_tool(self, tool_id: str, tool_name: str, arguments: dict) -> dict:
        """路由调用：转发到后端 MCP"""
        client = self.get_client(tool_id)
        if not client:
            return {
                "success": False,
                "errorType": "config_error",
                "error": f"工具 '{tool_id}' 未配置 MCP 后端连接"
            }

        try:
            result = await client.call_tool(tool_name, arguments)
            return {"success": True, "result": result}
        except BackendBusyError as e:
            # A3 方案 2：后端正忙，直接透传 busy 类型化错误，调用方可重试
            return {
                "success": False,
                "errorType": "backend_busy",
                "error": str(e)
            }
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

    async def list_backend_tools(self, tool_id: str, force_refresh: bool = False) -> dict:
        """列出后端 MCP 提供的工具（带完整参数 schema）"""
        client = self.get_client(tool_id)
        if not client:
            return {"success": False, "error": f"工具 {tool_id} 未配置 MCP 后端连接"}

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
