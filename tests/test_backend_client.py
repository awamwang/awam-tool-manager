"""测试 BackendClient / BackendManager 的稳定性修复

这些用例不需要真实的后端 MCP 子进程：用假的 session / context 驱动，
只验证状态机、清理顺序、超时与缓存保护四件事。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from backend_client import BackendClient, BackendManager, BackendBusyError  # noqa: E402


class FakeContext:
    """模拟 ClientSession / stdio_client 的异步 context manager"""

    def __init__(self, name: str, tracker: list, fail_exit: bool = False):
        self.name = name
        self.tracker = tracker
        self.fail_exit = fail_exit
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        self.tracker.append(f"enter:{self.name}")
        return self

    async def __aexit__(self, *args):
        self.tracker.append(f"exit:{self.name}")
        if self.fail_exit:
            raise RuntimeError(f"{self.name} 退出失败")


class FakeBackendClient(BackendClient):
    """跳过真实子进程创建，直接注入 context"""

    def __init__(self, tool_id="fake", connection=None, tracker=None):
        super().__init__(tool_id, connection or {"type": "stdio"})
        self.tracker = tracker if tracker is not None else []
        self._tools_stub = [{"name": "a"}, {"name": "b"}]

    def attach(self, fail_client_exit=False):
        self._client_context = FakeContext(
            "client", self.tracker, fail_exit=fail_client_exit
        )
        self._stdio_context = FakeContext("stdio", self.tracker)
        self.session = object()  # 占位，表示已有会话


def test_timeout_read_from_config():
    """connection 里可配 timeout，缺省走 DEFAULT_TIMEOUT"""
    default = BackendClient("x", {"type": "stdio"})
    custom = BackendClient("y", {"type": "stdio", "timeout": 5})
    assert default.timeout == BackendClient.DEFAULT_TIMEOUT
    assert custom.timeout == 5


def test_cleanup_exits_both_contexts():
    """D5：即使 client 退出失败，stdio 也必须被 exit，否则子进程泄漏"""

    async def run():
        client = FakeBackendClient()
        client.attach(fail_client_exit=True)
        await client._cleanup()
        return client

    client = asyncio.run(run())
    assert "exit:client" in client.tracker
    assert "exit:stdio" in client.tracker  # 关键：不能因为 client 抛异常就跳过
    assert client.session is None


def test_cleanup_is_idempotent():
    """重复清理不应重复 exit"""

    async def run():
        client = FakeBackendClient()
        client.attach()
        await client._cleanup()
        await client._cleanup()
        return client

    client = asyncio.run(run())
    assert client.tracker.count("exit:stdio") == 1


def test_stop_is_serialized_with_start():
    """D7：stop 与 start 共用同一把锁"""

    async def run():
        client = FakeBackendClient()
        client.status = BackendClient.STATUS_RUNNING
        client.attach()

        async def slow_start():
            async with client._lock:
                await asyncio.sleep(0.05)
                client.status = BackendClient.STATUS_RUNNING

        task = asyncio.create_task(slow_start())
        await asyncio.sleep(0)  # 让 slow_start 先拿到锁
        result = await client.stop()
        await task
        return client, result

    client, result = asyncio.run(run())
    assert result["success"] is True
    assert client.status == BackendClient.STATUS_STOPPED


def test_start_cleans_stale_connection():
    """D7：error 状态下重新 start，先清掉上次残留，避免孤儿进程"""

    async def run():
        client = FakeBackendClient()
        client.status = BackendClient.STATUS_ERROR
        old_stdio = FakeContext("old_stdio", client.tracker)
        client._stdio_context = old_stdio
        # start() 会在清理后尝试真正拉起子进程；这里用魔数让它停在连接类型校验前
        client.connection_config["type"] = "unsupported"
        await client.start()
        return client

    client = asyncio.run(run())
    assert "exit:old_stdio" in client.tracker
    assert client._stdio_context is None


def test_sync_does_not_overwrite_cache_with_empty():
    """D4：后端返回空列表时不得覆盖已有缓存"""

    class EmptyToolClient(FakeBackendClient):
        async def list_tools(self, force_refresh=False):
            return []

    class SpyStore:
        def __init__(self):
            self.saved = []

        def save_backend_tools(self, parent_id, tools):
            self.saved.append((parent_id, tools))

    async def run():
        manager = BackendManager(SpyStore())
        client = EmptyToolClient()
        client.status = BackendClient.STATUS_RUNNING
        manager.clients["empty"] = client
        return await manager.sync_tools_catalog("empty"), manager.store.saved

    result, saved = asyncio.run(run())
    assert result["success"] is False
    assert "保留原缓存" in result["error"]
    assert saved == []  # 关键：磁盘没被写入空数组


def test_call_tool_timeout_marks_broken():
    """D6：超时不会永久挂起，且把连接标记为不可用"""

    class HangingClient(FakeBackendClient):
        async def _mark_broken_called(self, reason):
            pass

        async def call_tool(self, tool_name, arguments):
            raise TimeoutError("调用工具 'x' 超时")

    async def run():
        client = HangingClient()
        client.status = BackendClient.STATUS_RUNNING
        client.timeout = 0.01
        client.session = None
        try:
            await client.call_tool("x", {})
        except TimeoutError:
            pass
        client._mark_broken("超时")
        return client

    client = asyncio.run(run())
    assert client.status == BackendClient.STATUS_ERROR
    assert "超时" in client.last_error


class FakeStore:
    """最小化的 store，只提供 BackendManager 需要的读取接口"""

    def __init__(self, tools: dict | None = None):
        self.tools = tools or {}

    def load_tool(self, tool_id: str):
        return self.tools.get(tool_id)

    def list_tools(self):
        return list(self.tools.values())


def _mcp_tool(connection: dict) -> dict:
    return {"id": "mcp1", "kind": "mcp", "connection": connection}


def test_get_logs_limit_zero_returns_empty():
    """D9：limit=0 必须是「不要」，不能因为 -0 == 0 变成全量"""
    client = BackendClient("fake", {"type": "stdio"})
    for i in range(3):
        client._log("info", f"第 {i} 条")

    assert len(client.logs) == 3
    assert client.get_logs(limit=0) == []
    assert len(client.get_logs(limit=2)) == 2


def test_log_level_filters_before_enqueue():
    """D10：低级别日志不入队，避免心跳 debug 把 error 挤出 500 条 deque"""
    client = BackendClient("fake", {"type": "stdio"})
    client.log_level = "warning"

    client._log("debug", "心跳正常")
    client._log("info", "启动成功")
    assert len(client.logs) == 0, "低于 warning 的日志不应进入队列"

    client._log("error", "炸了")
    assert len(client.logs) == 1
    assert client.logs[0]["level"] == "error"


def test_set_log_level_rejects_unknown():
    client = BackendClient("fake", {"type": "stdio"})
    result = client.set_log_level("trace")
    assert result["success"] is False
    assert client.log_level == "info"  # 不能被非法值污染


def test_idle_client_is_rebuilt_when_config_changed():
    """D14：停止状态下改 connection，下一次取用应拿到新配置"""
    store = FakeStore({"mcp1": _mcp_tool({"type": "stdio", "command": "old"})})
    manager = BackendManager(store)

    first = manager.get_client("mcp1")
    assert first.connection_config["command"] == "old"
    first._log("warning", "这段历史要留着")

    # 改配置并停掉（stopped 才允许热替换）
    store.tools["mcp1"] = _mcp_tool({"type": "stdio", "command": "new"})
    first.status = BackendClient.STATUS_STOPPED

    second = manager.get_client("mcp1")
    assert second is not first
    assert second.connection_config["command"] == "new"
    # 日志历史必须跟着走，否则排障时刚看过的日志会凭空消失
    assert [l["message"] for l in second.logs] == ["这段历史要留着"]


def test_running_client_is_not_replaced():
    """D14：运行中的连接不能被热替换，否则会留下孤儿子进程"""
    store = FakeStore({"mcp1": _mcp_tool({"type": "stdio", "command": "old"})})
    manager = BackendManager(store)

    first = manager.get_client("mcp1")
    first.status = BackendClient.STATUS_RUNNING

    store.tools["mcp1"] = _mcp_tool({"type": "stdio", "command": "new"})
    assert manager.get_client("mcp1") is first


def test_missing_tool_yields_no_client():
    """工具从磁盘删掉后，不应再返回缓存里的旧客户端"""
    store = FakeStore({"mcp1": _mcp_tool({"type": "stdio", "command": "x"})})
    manager = BackendManager(store)

    client = manager.get_client("mcp1")
    client.status = BackendClient.STATUS_STOPPED
    del store.tools["mcp1"]

    assert manager.get_client("mcp1") is None


def test_call_backend_tool_error_classification():
    """后端异常要按 errorType 归类，客户端才能区分「没启动」和「参数错」"""

    class BoomClient(FakeBackendClient):
        def __init__(self, exc):
            super().__init__(tool_id="boom")
            self.exc = exc

        async def call_tool(self, tool_name, arguments):
            raise self.exc

    async def run():
        # 客户端必须能在 store 里找到对应配置，否则会被判成 config_error
        store = FakeStore({"boom": _mcp_tool({"type": "stdio"})})
        manager = BackendManager(store)
        results = {}
        for label, exc in [
            ("connection", ConnectionError("后端未运行")),
            ("runtime", RuntimeError("后端返回错误")),
            ("unknown", ValueError("别的什么")),
        ]:
            manager.clients["boom"] = BoomClient(exc)
            results[label] = await manager.call_backend_tool("boom", "x", {})
        return results

    results = asyncio.run(run())

    assert results["connection"]["errorType"] == "connection_error"
    assert results["runtime"]["errorType"] == "runtime_error"
    assert results["unknown"]["errorType"] == "unknown_error"
    assert all(not r["success"] for r in results.values())


def test_call_backend_tool_on_unknown_tool():
    """没配过 MCP 后端的工具（或被误传成 app 类型）→ config_error"""
    manager = BackendManager(FakeStore({"app1": {"id": "app1", "kind": "app"}}))
    result = asyncio.run(manager.call_backend_tool("app1", "x", {}))
    assert result["errorType"] == "config_error"


def test_list_mcp_tools_only_returns_mcp_kind():
    store = FakeStore({
        "app1": {"id": "app1", "kind": "app"},
        "mcp1": {"id": "mcp1", "kind": "mcp", "connection": {"type": "stdio"}},
        "mcp2": {"id": "mcp2", "kind": "mcp"},  # 缺 connection，仍算 mcp 类工具
    })
    manager = BackendManager(store)
    assert manager.list_mcp_tools() == ["mcp1", "mcp2"]


def test_close_all_stops_every_client_and_clears_pool():
    async def run():
        manager = BackendManager(FakeStore())
        clients = []
        for tool_id in ("a", "b"):
            client = FakeBackendClient(tool_id=tool_id)
            client.status = BackendClient.STATUS_RUNNING
            client.attach()
            manager.clients[tool_id] = client
            clients.append(client)

        await manager.close_all()
        return manager, clients

    manager, clients = asyncio.run(run())

    assert manager.clients == {}
    assert all(c.status == BackendClient.STATUS_STOPPED for c in clients)
    assert all("exit:stdio" in c.tracker for c in clients)  # 子进程被释放


def test_tools_cache_respects_ttl_and_force_refresh():
    """缓存命中时不重复打后端；force_refresh 与 TTL 过期都会重新拉取"""

    class CountingClient(FakeBackendClient):
        async def _refresh_tools_cache(self):
            self.refreshes = getattr(self, "refreshes", 0) + 1
            self._cached_tools = [{"name": "t"}]
            self._tools_cache_time = time.time()

    async def run():
        client = CountingClient()
        client.status = BackendClient.STATUS_RUNNING
        client.TOOLS_CACHE_TTL = 300  # 缓存命中
        await client.list_tools()
        await client.list_tools()
        after_cache = client.refreshes

        await client.list_tools(force_refresh=True)
        after_force = client.refreshes

        client.TOOLS_CACHE_TTL = 0  # 视为已过期
        await client.list_tools()
        return after_cache, after_force, client.refreshes

    after_cache, after_force, after_expire = asyncio.run(run())

    assert after_cache == 1, "缓存有效期内不应重复拉取"
    assert after_force == 2, "force_refresh 必须重新拉取"
    assert after_expire == 3, "TTL 过期后应重新拉取"


def test_list_tools_rejects_when_not_running():
    client = FakeBackendClient()
    client.status = BackendClient.STATUS_STOPPED

    async def run():
        try:
            await client.list_tools()
        except ConnectionError as e:
            return str(e)
        return None  # pragma: no cover

    assert "未运行" in asyncio.run(run())


def test_heartbeat_failure_cleans_up():
    """心跳失败不只改状态，还要释放子进程"""

    class DeadSession:
        async def ping(self):
            raise RuntimeError("pipe closed")

    async def run():
        client = FakeBackendClient()
        client.status = BackendClient.STATUS_RUNNING
        client.attach()
        client.session = DeadSession()
        client.HEARTBEAT_INTERVAL = 0.01  # 缩短等待，不必真等 30 秒
        client._heartbeat_task = asyncio.create_task(client._heartbeat_loop())
        for _ in range(100):
            if client.status != BackendClient.STATUS_RUNNING:
                break
            await asyncio.sleep(0.02)
        return client

    client = asyncio.run(run())
    assert client.status == BackendClient.STATUS_ERROR
    assert "exit:stdio" in client.tracker  # 子进程被释放
    assert client._stdio_context is None


class _FakeCallResult:
    """call_tool 成功返回的最小结果对象（isError=False + 文本 content）"""

    def __init__(self, text: str):
        self.isError = False
        self.content = [type("C", (), {"text": text})()]


class _StubSession:
    """模拟 ClientSession.call_tool：记录被调用的工具名并休眠模拟耗时"""

    def __init__(self):
        self.calls = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append(name)
        await asyncio.sleep(0.05)
        return _FakeCallResult("ok")


def test_call_tool_rejects_when_already_in_flight():
    """A3：同一后端正在调用时，新的 call_tool 立即报 backend_busy（不排队）"""
    client = BackendClient("fake", {"type": "stdio"})
    client.status = BackendClient.STATUS_RUNNING

    async def run():
        # 模拟「另一个调用正在执行」：先占住调用串行锁
        await client._call_lock.acquire()
        try:
            await client.call_tool("x", {})
        except BackendBusyError as e:
            return str(e)
        return None  # pragma: no cover

    msg = asyncio.run(run())
    assert msg is not None
    assert "正在处理其他调用" in msg


def test_call_tool_serializes_concurrent_invocations():
    """A3：并发两个 call_tool，只有一个成功完成，另一个拿到 backend_busy"""
    client = BackendClient("fake", {"type": "stdio"})
    client.status = BackendClient.STATUS_RUNNING
    client.session = _StubSession()

    async def run():
        async def one(label):
            try:
                return ("ok", await client.call_tool(label, {}))
            except BackendBusyError:
                return ("busy", label)

        return await asyncio.gather(one("a"), one("b"))

    out = asyncio.run(run())
    statuses = [r[0] for r in out]
    assert statuses.count("ok") == 1, "必须恰好一个调用真正打到后端"
    assert statuses.count("busy") == 1, "并发的另一个必须被拒为 busy"
    # 成功那次确实通过会话发起了调用
    assert client.session.calls == ["a"] or client.session.calls == ["b"]


def test_call_backend_tool_reports_busy():
    """A3：后端忙时，manager 透传 backend_busy 类型化错误（用真实调用锁模拟在飞）"""

    async def run():
        store = FakeStore({"busy1": _mcp_tool({"type": "stdio"})})
        manager = BackendManager(store)
        client = BackendClient("busy1", {"type": "stdio"})
        client.status = BackendClient.STATUS_RUNNING
        # 模拟「另一个调用正在执行」：先占住调用串行锁，让 call_tool 抛出真 busy
        await client._call_lock.acquire()
        manager.clients["busy1"] = client
        return await manager.call_backend_tool("busy1", "x", {})

    result = asyncio.run(run())
    assert result["errorType"] == "backend_busy"
    assert result["success"] is False
    assert "正在处理其他调用" in result["error"]
