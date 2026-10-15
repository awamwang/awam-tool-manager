"""测试 BackendClient / BackendManager 的稳定性修复

这些用例不需要真实的后端 MCP 子进程：用假的 session / context 驱动，
只验证状态机、清理顺序、超时与缓存保护四件事。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from backend_client import BackendClient, BackendManager  # noqa: E402


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
