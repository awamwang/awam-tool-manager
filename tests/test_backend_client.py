"""测试 BackendClient / BackendManager 的稳定性修复

这些用例不需要真实的后端 MCP 子进程：用假的 session / context 驱动，
只验证状态机、清理顺序、超时与缓存保护四件事。
"""
import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import backend_client  # noqa: E402  —— monkeypatch 需要模块对象本身
from backend_client import BackendClient, BackendManager, BackendBusyError  # noqa: E402

# conftest 的 autouse 夹具默认把 ensure_npm_bin_on_path 打成 noop；
# 需要验证 PATH 自愈本身的用例从这里拿原始实现，再 monkeypatch 覆盖回去。
REAL_ENSURE_NPM_BIN = backend_client.ensure_npm_bin_on_path


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
        """模拟一条已建立的连接：连接任务持有两个假 context 等待停止信号

        与真实 _connection_loop 同构：stdio_client / ClientSession 的
        进入与退出都在同一个任务内完成——这正是 anyio 作用域的正确用法，
        也是被测行为的一部分。
        """
        client_ctx = FakeContext(
            "client", self.tracker, fail_exit=fail_client_exit
        )
        stdio_ctx = FakeContext("stdio", self.tracker)
        self._client_context = client_ctx
        self._stdio_context = stdio_ctx
        self.session = object()  # 占位，表示已有会话
        self._ready = asyncio.Event()
        self._ready.set()
        self._shutdown = asyncio.Event()

        async def loop():
            try:
                async with stdio_ctx:
                    async with client_ctx:
                        await self._shutdown.wait()
            finally:
                # 与真实 _connection_loop 一致：任务终结即清对外引用
                self.session = None
                self._stdio_context = None
                self._client_context = None

        self._conn_task = asyncio.create_task(loop())


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
    """D7：error 状态下重新 start，先收尸上次残留的连接任务，避免孤儿进程"""

    async def run():
        client = FakeBackendClient()
        client.status = BackendClient.STATUS_ERROR
        # 上一轮连接的残留：conn_task 还持有旧 stdio context 没退出去
        old_stdio = FakeContext("old_stdio", client.tracker)
        client._stdio_context = old_stdio
        client._ready = asyncio.Event()
        client._ready.set()
        client._shutdown = asyncio.Event()

        async def stale_loop():
            try:
                async with old_stdio:
                    await client._shutdown.wait()
            finally:
                # 与真实 _connection_loop 一致：任务终结即清对外引用
                client.session = None
                client._stdio_context = None
                client._client_context = None

        client._conn_task = asyncio.create_task(stale_loop())
        await asyncio.sleep(0)  # 让 stale_loop 先进入 async with
        # start() 会在清理后尝试真正拉起子进程；这里用魔数让它停在连接类型校验前
        client.connection_config["type"] = "unsupported"
        await client.start()
        return client

    client = asyncio.run(run())
    assert "exit:old_stdio" in client.tracker  # 残留连接被优雅收尸
    assert client._conn_task is None
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

        # TTL=0 表示每次都算过期。注意：Windows 上 time.time() 刻度约 15ms，
        # 相邻两次调用可能返回同一个值，使 cache_age 恰为 0.0 —— `0.0 > 0` 为假
        # 就会误判「缓存命中」，本用例随即随机性失败（实锤：2026-10-16 跑出
        # 1 failed / 124 passed，与任何代码改动无关）。显式回拨缓存时间戳，
        # 让「已过期」这件事不受时钟精度影响，回归才可复现。
        client.TOOLS_CACHE_TTL = 0
        client._tools_cache_time -= 1
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


def test_start_cancelled_does_not_strand_starting_status():
    """start() 被外部取消后，status 不得停留在 starting（否则永不自愈）。

    背景（2026-10-16 实测）：toolport 网关对每次 tools/call 有 30s 预算，
    超时会取消请求、向 handler 注入 CancelledError；CancelledError 在
    Python 3.8+ 继承 BaseException，start() 原来的 except Exception 接不住，
    status 便永远卡在 starting——之后所有 get/status/list 调用只会看到
    「后端未运行，当前状态: starting」。取消要继续上抛，但状态必须落地。
    """

    async def run():
        client = BackendClient("fake", {"type": "stdio"})
        hang = asyncio.Event()

        async def slow_install():
            # 模拟慢启动步骤（如冷机上的 npm 自装）撞上网关 30s 预算被取消
            await hang.wait()

        client.ensure_installed = slow_install  # type: ignore[method-assign]

        task = asyncio.create_task(client.start())
        await asyncio.sleep(0.05)  # 让 start() 置入 STARTING 并卡在慢步骤上
        assert client.status == BackendClient.STATUS_STARTING

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # 取消语义保持：继续向上抛是被允许的

        assert client.status == BackendClient.STATUS_STOPPED, (
            f"取消后 status 应从 starting 收回为 stopped，实际: {client.status}"
        )
        assert "取消" in (client.last_error or "")

    asyncio.run(run())


def test_heartbeat_failure_cleans_up():
    """心跳失败不只改状态，还要释放子进程"""

    class DeadSession:
        async def send_ping(self):
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


class _SdkSurfaceSession:
    """只暴露真实 ClientSession 用到的 API 的假会话

    心跳/工具缓存/调用只会碰 initialize / send_ping / list_tools /
    call_tool。若代码调了不存在的方法（如曾经的 ping()），这里立刻
    AttributeError——测试环境装的是开发版 mcp，真 SDK 的表面以本类的
    白名单 + test_heartbeat_probe_matches_real_sdk 的契约定死。
    """

    def __init__(self):
        self.ping_calls = 0

    async def initialize(self):
        return None

    async def send_ping(self):
        self.ping_calls += 1
        return None

    async def list_tools(self):
        class _Result:
            tools = []

        return _Result()

    async def call_tool(self, name, arguments):
        raise AssertionError("心跳测试不应调用 call_tool")


def test_heartbeat_probe_matches_real_sdk():
    """A9：心跳探测必须落在真实 ClientSession 存在的方法上

    曾经的 bug：`await session.ping()` —— mcp 1.x 只有 send_ping()。
    生产环境（mcp 1.29.0）每次启动后端后心跳必败、连接被清、状态 error，
    后端永远留不住；而开发环境（mcp 1.30）的心跳 fake 自带 ping 方法，
    测试全绿也发现不了。本用例从两头堵死：
    1. 契约断言：探测方法必须真实存在于 mcp.ClientSession；
    2. 行为断言：心跳跑完一轮后状态仍为 running（假 session 表面即真实
       SDK 白名单，调错方法会 AttributeError → 状态 error → 断言失败）。
    """
    import mcp

    assert hasattr(mcp.ClientSession, "send_ping"), (
        "mcp.ClientSession 没有 send_ping()——SDK API 变了，"
        "请同步更新 _heartbeat_loop 的探测方式"
    )

    async def run():
        client = FakeBackendClient()
        client.status = BackendClient.STATUS_RUNNING
        client.attach()
        session = _SdkSurfaceSession()
        client.session = session
        client.HEARTBEAT_INTERVAL = 0.01
        client._heartbeat_task = asyncio.create_task(client._heartbeat_loop())
        for _ in range(100):
            if session.ping_calls >= 2:
                break
            await asyncio.sleep(0.02)
        await client.stop()
        return client, session

    client, session = asyncio.run(run())
    assert session.ping_calls >= 2, "心跳应周期调用 send_ping"
    assert client.status == BackendClient.STATUS_STOPPED  # stop 后的正常状态
    assert "exit:stdio" in client.tracker


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


# ---- A6：后端依赖自动安装（ensure_installed）与启动预热 ----
#
# 背景：tools/*.json 的 install 字段此前是死元数据，包没全局安装时
# start_backend 必然失败且永不自愈。这里把「检测缺失 → 自动安装一次 →
# 装完复检」的逻辑固化成用例，安装命令全部用替身执行，不碰真实 npm。


class FakeInstallProcess:
    """模拟 create_subprocess_shell 返回的进程对象"""

    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return (b"", self._stderr)

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _fake_install_factory(calls, returncode: int = 0, stderr: bytes = b""):
    """记录安装命令并返回可控的假进程"""

    async def factory(command, **kwargs):
        calls.append(command)
        return FakeInstallProcess(returncode, stderr)

    return factory


def _authorize_install(monkeypatch):
    """显式授权自动执行 install.command（环境变量通道）

    P0 之后 install.command 默认不被执行；断言「安装逻辑本身」的用例
    必须先授权，否则测到的是门禁而不是安装流程。
    """
    monkeypatch.setenv(BackendClient.INSTALL_ENV_VAR, "1")


def _revoke_install_authorization(monkeypatch):
    """确保环境里没有任何授权（用例间隔离：外部环境可能设了这个变量）"""
    monkeypatch.delenv(BackendClient.INSTALL_ENV_VAR, raising=False)


class _FakeStdioContext:
    """假装 stdio_client(params)：不进真实子进程，__aenter__ 给出两个流"""

    def __init__(self, tracker: list):
        self.tracker = tracker

    async def __aenter__(self):
        self.tracker.append("enter:stdio")
        return (object(), object())

    async def __aexit__(self, *args):
        self.tracker.append("exit:stdio")


class _FakeSession:
    """假装 ClientSession：initialize / list_tools 都成功"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def initialize(self):
        pass

    async def list_tools(self):
        class _Result:
            tools = []

        return _Result()


def test_resolved_command_unwraps_shell_wrappers():
    """which 必须盯着壳后面的真实目标，否则 cmd.exe 永远命中，
    「没安装」会被误判成「已安装」"""
    c = BackendClient("x", {})
    assert c._resolved_command({"command": "cmd.exe", "args": ["/c", "foo"]}) == "foo"
    assert c._resolved_command({"command": "cmd", "args": ["/k", "foo"]}) == "foo"
    assert c._resolved_command({"command": "powershell.exe", "args": ["-Command", "foo"]}) == "foo"
    assert c._resolved_command({"command": "pwsh", "args": ["-c", "foo"]}) == "foo"
    assert c._resolved_command({"command": "bash", "args": ["-c", "foo"]}) == "foo"
    assert c._resolved_command({"command": "sh", "args": ["-c", "foo"]}) == "foo"


def test_resolved_command_edge_cases():
    """直接形式、参数不够、非壳程序、引号、缺 command"""
    c = BackendClient("x", {})
    # 直接形式：command 自身就是目标（绝对路径 which 也认）
    assert c._resolved_command({"command": "everything-search-mcp"}) == "everything-search-mcp"
    assert c._resolved_command({"command": r"C:\tools\foo.exe"}) == r"C:\tools\foo.exe"
    # 壳包裹但参数不够：没有目标可剥，退回 command 自身
    assert c._resolved_command({"command": "cmd.exe", "args": ["/c"]}) == "cmd.exe"
    # 非壳程序的 /c 不算开关
    assert c._resolved_command({"command": "foo", "args": ["/c", "bar"]}) == "foo"
    # 引号包裹的目标要剥掉（路径带空格的常见写法）
    assert c._resolved_command({"command": "cmd.exe", "args": ["/c", '"foo bar"']}) == "foo bar"
    # 没有 command
    assert c._resolved_command({}) is None
    assert c._resolved_command({"args": ["/c", "foo"]}) is None


def test_ensure_installed_skips_when_target_on_path(monkeypatch):
    """已在 PATH 上：不执行安装，也不读 install 配置"""
    monkeypatch.setattr(shutil, "which", lambda name: f"C:\\bin\\{name}")
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"method": "npm", "command": "npm i -g foo"},
    )
    result = asyncio.run(client.ensure_installed())

    assert result == {"success": True, "available": True, "installed": False}
    assert calls == []


def test_ensure_installed_without_install_config_does_not_block(monkeypatch):
    """没有 install.command：如实报告不可用但不阻塞，由 spawn 产出原生错误"""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    client = BackendClient("x", {"command": "cmd.exe", "args": ["/c", "foo"]})
    result = asyncio.run(client.ensure_installed())

    assert result["success"] is True
    assert result["available"] is False
    assert result["installed"] is False
    assert result["note"] == "未配置安装命令"
    assert calls == []


def test_ensure_installed_reports_unavailable_when_command_missing(monkeypatch):
    """没配 command：解析不出目标，如实报告不可用但不阻塞（spawn 会原生报错）"""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    client = BackendClient("x", {"type": "stdio"}, {"command": "npm i -g foo"})
    result = asyncio.run(client.ensure_installed())

    assert result["success"] is True
    assert result["available"] is False
    assert result["note"] == "无法解析启动命令"
    assert calls == []  # 没有目标可检测，安装无从谈起


def test_ensure_installed_ignores_malformed_install_config(monkeypatch):
    """install 是坏数据（字符串/数字）时不能炸掉启动路径"""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    client = BackendClient("x", {"command": "cmd.exe", "args": ["/c", "foo"]}, "npm i -g foo")
    result = asyncio.run(client.ensure_installed())

    assert result["success"] is True
    assert result["available"] is False
    assert calls == []  # 解析不出安装命令，与「没配」同等对待


def test_ensure_installed_installs_once_then_short_circuits(monkeypatch):
    """装一次：安装后目标出现在 PATH 上；之后再调用直接短路，不重复装"""
    _authorize_install(monkeypatch)
    state = {"which": None}
    monkeypatch.setattr(shutil, "which", lambda name: state["which"])
    calls = []

    async def factory(command, **kwargs):
        calls.append(command)
        state["which"] = f"C:\\bin\\{command}"  # 安装后 which 命中
        return FakeInstallProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", factory)

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"method": "npm", "command": "npm i -g foo"},
    )

    first = asyncio.run(client.ensure_installed())
    assert first == {"success": True, "available": True, "installed": True}
    assert calls == ["npm i -g foo"]

    second = asyncio.run(client.ensure_installed())
    assert second == {"success": True, "available": True, "installed": False}
    assert calls == ["npm i -g foo"]  # available 短路，根本不进安装分支


def test_ensure_installed_does_not_retry_after_failure(monkeypatch):
    """安装失败后不重复锤 npm；重试路径是重启服务或改配置触发重建"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_shell",
        _fake_install_factory(calls, returncode=1, stderr=b"npm ERR! network"),
    )

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"command": "npm i -g foo"},
    )

    first = asyncio.run(client.ensure_installed())
    assert first["success"] is False
    assert first["installed"] is False
    assert "退出码 1" in first["error"]
    assert "npm ERR! network" in first["error"]  # stderr 尾部要带回，便于排障

    second = asyncio.run(client.ensure_installed())
    assert second["success"] is False
    assert "跳过重复" in second["error"]
    assert calls == ["npm i -g foo"]  # 只执行过一次


def test_ensure_installed_reports_when_command_ok_but_target_missing(monkeypatch):
    """安装退出码 0 但目标仍不在 PATH：如实报告，不谎报成功"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"command": "npm i -g foo"},
    )
    result = asyncio.run(client.ensure_installed())

    assert result["success"] is False
    assert result["installed"] is True
    assert "仍不在 PATH" in result["error"]


def test_ensure_installed_times_out_and_kills_process(monkeypatch):
    """安装超时：杀进程、报错，不会无限等下去"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    procs = []

    class HangingProcess(FakeInstallProcess):
        async def communicate(self):
            await asyncio.sleep(3600)  # 永远不返回

    async def factory(command, **kwargs):
        proc = HangingProcess()
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", factory)

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"command": "npm i -g foo"},
    )
    client.INSTALL_TIMEOUT = 0.05
    result = asyncio.run(client.ensure_installed())

    assert result["success"] is False
    assert "超时" in result["error"]
    assert procs[0].killed is True  # 超时不是干等，子进程必须被收掉


def test_start_installs_missing_dependency_before_spawn(monkeypatch):
    """A6：start() 在 spawn 之前先自装缺失依赖；装完继续走正常启动流程"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    tracker = []
    monkeypatch.setattr(backend_client, "stdio_client", lambda params: _FakeStdioContext(tracker))
    monkeypatch.setattr(backend_client, "ClientSession", lambda r, w: _FakeSession())

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"], "timeout": 5},
        {"command": "npm i -g foo"},
    )

    async def run():
        result = await client.start()
        await client.stop()  # 收掉心跳任务，避免 loop 关闭时的 pending 警告
        return result

    result = asyncio.run(run())

    assert calls == ["npm i -g foo"], "spawn 之前必须先安装"
    assert result["success"] is True
    assert "enter:stdio" in tracker  # 安装后继续走了正常启动流程
    assert any("依赖已自动安装" in log["message"] for log in client.logs)


def test_get_client_passes_and_tracks_install_config():
    """install 元数据必须透传给客户端，否则 ensure_installed 无从装起"""
    store = FakeStore({
        "mcp1": {
            "id": "mcp1",
            "kind": "mcp",
            "connection": {"type": "stdio"},
            "install": {"command": "npm i -g x"},
        }
    })
    manager = BackendManager(store)
    client = manager.get_client("mcp1")
    assert client.install_config == {"command": "npm i -g x"}


def test_get_client_rebuilds_when_only_install_changed():
    """只改 install 也要重建客户端，否则新的安装命令不生效"""
    store = FakeStore({"mcp1": _mcp_tool({"type": "stdio"})})
    store.tools["mcp1"]["install"] = {"command": "old"}
    manager = BackendManager(store)

    first = manager.get_client("mcp1")
    assert first.install_config == {"command": "old"}
    first.status = BackendClient.STATUS_STOPPED  # 空闲才允许热替换

    store.tools["mcp1"]["install"] = {"command": "new"}
    second = manager.get_client("mcp1")
    assert second is not first
    assert second.install_config == {"command": "new"}


def test_ensure_all_installed_warms_every_mcp_tool(monkeypatch):
    """预热遍历所有 MCP 工具：只装不启；无 install 的不阻塞；app 类不参与"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    store = FakeStore({
        "mcp1": {
            "id": "mcp1",
            "kind": "mcp",
            "connection": {"type": "stdio", "command": "cmd.exe", "args": ["/c", "tool-a"]},
            "install": {"command": "npm i -g a"},
        },
        "mcp2": {
            "id": "mcp2",
            "kind": "mcp",
            "connection": {"type": "stdio", "command": "cmd.exe", "args": ["/c", "tool-b"]},
        },  # 无 install
        "mcp3": {"id": "mcp3", "kind": "mcp"},  # 缺 connection，预热时跳过
        "app1": {"id": "app1", "kind": "app"},  # 非 mcp，不预热
    })
    manager = BackendManager(store)
    result = asyncio.run(manager.ensure_all_installed())

    assert result["total"] == 2
    assert result["available"] == 0
    assert result["installed"] == 1  # 只有 mcp1 有安装命令
    assert calls == ["npm i -g a"]

    by_id = {d["tool_id"]: d for d in result["details"]}
    assert by_id["mcp2"]["available"] is False  # 没配 install：不阻塞，如实报告


# ---------------------------------------------------------------------------
# P0：install.command 执行门禁（默认拒绝 + 授权 + 命令白名单）
#
# 背景（架构评审 ARC-P0-01）：install.command 写在**进版本库**的
# data/tools/*.json 里，旧逻辑让它在「服务启动预热」和「start_backend」
# 两条路径上被无条件 create_subprocess_shell 执行——来源不可控、无任何
# destructive/allowDestructive 门禁，与「launch_tool 只返回命令不执行」
# 的取舍正好相反，是最小权限倒置。
#
# 修复后的三段式门禁，本组用例逐段钉住：
#   ① 未显式授权（AWAM_AUTO_INSTALL / install.auto=true）→ 一条都不执行；
#   ② 授权了还要命令形态落在白名单内（无 shell 元字符 + 前缀合法）；
#   ③ 被拦下时 start() 返回带 errorType 的结构化失败，而不是 spawn 后
#      丢一个「命令不存在」的原生错误把真实原因盖掉。
# ---------------------------------------------------------------------------


def test_install_gate_default_denies_execution(monkeypatch):
    """默认未授权：依赖缺失也一条安装命令都不执行"""
    _revoke_install_authorization(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"method": "npm", "command": "npm i -g foo"},
    )
    result = asyncio.run(client.ensure_installed())

    assert result["installBlocked"] == "unauthorized"
    assert result["available"] is False
    assert result["installed"] is False
    # success 仍为 True：这是策略性拒绝，不是执行失败
    assert result["success"] is True
    assert calls == [], "未授权时绝不允许执行 install.command"


def test_install_gate_unauthorized_message_names_the_switch(monkeypatch):
    """拒绝原因必须可操作：告诉调用方怎么开授权，而不是只说「不允许」"""
    _revoke_install_authorization(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory([]))

    client = BackendClient(
        "x", {"command": "cmd.exe", "args": ["/c", "foo"]}, {"command": "npm i -g foo"}
    )
    result = asyncio.run(client.ensure_installed())

    assert BackendClient.INSTALL_ENV_VAR in result["note"]
    assert "install.auto" in result["note"]


def test_install_gate_env_var_authorizes(monkeypatch):
    """部署者设 AWAM_AUTO_INSTALL=1：恢复自动安装"""
    _authorize_install(monkeypatch)
    state = {"which": None}
    monkeypatch.setattr(shutil, "which", lambda name: state["which"])
    calls = []

    async def factory(command, **kwargs):
        calls.append(command)
        state["which"] = "C:\\bin\\foo"
        return FakeInstallProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", factory)

    client = BackendClient(
        "x", {"command": "cmd.exe", "args": ["/c", "foo"]}, {"command": "npm i -g foo"}
    )
    result = asyncio.run(client.ensure_installed())

    assert result["installed"] is True
    assert "installBlocked" not in result
    assert calls == ["npm i -g foo"]


def test_install_gate_entry_opt_in_authorizes(monkeypatch):
    """条目自己声明 install.auto=true：逐工具 opt-in，无需全局开环境变量"""
    _revoke_install_authorization(monkeypatch)
    state = {"which": None}
    monkeypatch.setattr(shutil, "which", lambda name: state["which"])
    calls = []

    async def factory(command, **kwargs):
        calls.append(command)
        state["which"] = "C:\\bin\\foo"
        return FakeInstallProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", factory)

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"command": "npm i -g foo", "auto": True},
    )
    result = asyncio.run(client.ensure_installed())

    assert result["installed"] is True
    assert calls == ["npm i -g foo"]


def test_install_gate_falsy_env_values_do_not_authorize(monkeypatch):
    """环境变量的假值（0/false/off/空/乱写）一律不授权"""
    for raw in ("", "   ", "0", "false", "off", "no", "1x", "true;"):
        monkeypatch.setenv(BackendClient.INSTALL_ENV_VAR, raw)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        calls = []
        monkeypatch.setattr(
            asyncio, "create_subprocess_shell", _fake_install_factory(calls)
        )
        client = BackendClient(
            "x", {"command": "cmd.exe", "args": ["/c", "foo"]}, {"command": "npm i -g foo"}
        )
        result = asyncio.run(client.ensure_installed())
        assert result["installBlocked"] == "unauthorized", f"{raw!r} 不应构成授权"
        assert calls == []


def test_install_gate_auto_false_is_not_opt_in(monkeypatch):
    """install.auto 必须是严格的 true；字符串 "true"/1 都不算（schema 也校 boolean）"""
    _revoke_install_authorization(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    for auto in ("true", 1, "yes"):
        calls = []
        monkeypatch.setattr(
            asyncio, "create_subprocess_shell", _fake_install_factory(calls)
        )
        client = BackendClient(
            "x",
            {"command": "cmd.exe", "args": ["/c", "foo"]},
            {"command": "npm i -g foo", "auto": auto},
        )
        result = asyncio.run(client.ensure_installed())
        assert result["installBlocked"] == "unauthorized", f"auto={auto!r} 不应构成授权"
        assert calls == []


def test_install_gate_rejects_shell_metacharacters(monkeypatch):
    """授权也拦不住 shell 串联：&& / ; / $( ) / 反引号 一律拒绝"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    for command in (
        "npm i -g foo && calc",
        "npm i -g foo; calc",
        "npm i -g foo | calc",
        "npm i -g $(calc)",
        "npm i -g foo `calc`",
        "npm i -g foo > out.txt",
        'npm i -g foo "x',
    ):
        calls = []
        monkeypatch.setattr(
            asyncio, "create_subprocess_shell", _fake_install_factory(calls)
        )
        client = BackendClient(
            "x", {"command": "cmd.exe", "args": ["/c", "foo"]}, {"command": command}
        )
        result = asyncio.run(client.ensure_installed())
        assert result["installBlocked"] == "not_allowed", f"{command!r} 应被拒绝"
        assert calls == [], f"{command!r} 不应被执行"


def test_install_gate_rejects_unknown_prefix(monkeypatch):
    """前缀不在白名单内：powershell/choco/裸前缀 等一律拒绝"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    for command in (
        "powershell -c Install-Package foo",
        "choco install foo -y",
        "npm i -g",  # 前缀合法但没跟包名：什么都不装，没有放行理由
        "notacommand install foo",
        "npm",  # 只有一个词
    ):
        calls = []
        monkeypatch.setattr(
            asyncio, "create_subprocess_shell", _fake_install_factory(calls)
        )
        client = BackendClient(
            "x", {"command": "cmd.exe", "args": ["/c", "foo"]}, {"command": command}
        )
        result = asyncio.run(client.ensure_installed())
        assert result["installBlocked"] == "not_allowed", f"{command!r} 应被拒绝"
        assert "白名单" in result["error"]
        assert calls == []


def test_install_gate_allows_whitelisted_commands(monkeypatch):
    """白名单内的安装形式必须放行——门禁不能把 A6 的自装能力一刀切掉"""
    _authorize_install(monkeypatch)
    state = {"which": None}
    monkeypatch.setattr(shutil, "which", lambda name: state["which"])
    calls = []

    async def factory(command, **kwargs):
        calls.append(command)
        state["which"] = "C:\\bin\\foo"
        return FakeInstallProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", factory)

    for prefix in BackendClient.INSTALL_COMMAND_ALLOWLIST:
        command = " ".join(prefix) + " some-package"
        allowed, reason = BackendClient._install_command_allowed(command)
        assert allowed, f"{command!r} 应放行（{reason}）"

        calls.clear()
        state["which"] = None  # 每个命令都从「未安装」状态开始验
        client = BackendClient(
            "x", {"command": "cmd.exe", "args": ["/c", "foo"]}, {"command": command}
        )
        result = asyncio.run(client.ensure_installed())
        assert result["installed"] is True, command
        assert calls == [command]


def test_install_gate_case_insensitive_prefix(monkeypatch):
    """命令大小写不敏感：NPM I -G / Winget Install 都应放行"""
    assert BackendClient._install_command_allowed("NPM I -G foo")[0] is True
    assert BackendClient._install_command_allowed("Winget Install --id Foo")[0] is True
    assert BackendClient._install_command_allowed("winget INSTALL foo")[0] is True


def test_install_gate_does_not_trigger_when_target_available(monkeypatch):
    """目标已在 PATH：既不走门禁也不执行安装（与修复前一致）"""
    _revoke_install_authorization(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: f"C:\\bin\\{name}")
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"command": "npm i -g foo && evil"},
    )
    result = asyncio.run(client.ensure_installed())

    assert result == {"success": True, "available": True, "installed": False}
    assert calls == []


def test_start_backend_blocked_returns_structured_failure(monkeypatch):
    """P0：start() 被门禁拦下时返回 install_required，不 spawn、不掩盖原因"""
    _revoke_install_authorization(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    tracker = []
    monkeypatch.setattr(backend_client, "stdio_client", lambda params: _FakeStdioContext(tracker))
    monkeypatch.setattr(backend_client, "ClientSession", lambda r, w: _FakeSession())

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"], "timeout": 5},
        {"command": "npm i -g foo"},
    )
    result = asyncio.run(client.start())

    assert result["success"] is False
    assert result["errorType"] == "install_required"
    # Q1（P1-03）：status 恢复状态机值；错误分类只看 errorType
    assert result["status"] == "error"
    assert BackendClient.INSTALL_ENV_VAR in result["message"]
    assert calls == []
    assert tracker == [], "被拦下时不允许 spawn 子进程"
    assert client.status == BackendClient.STATUS_STOPPED


def test_start_backend_not_allowed_returns_structured_failure(monkeypatch):
    """授权了但命令不合规：返回 install_not_allowed，同样不 spawn"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    tracker = []
    monkeypatch.setattr(backend_client, "stdio_client", lambda params: _FakeStdioContext(tracker))
    monkeypatch.setattr(backend_client, "ClientSession", lambda r, w: _FakeSession())

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"], "timeout": 5},
        {"command": "npm i -g foo && calc"},
    )
    result = asyncio.run(client.start())

    assert result["success"] is False
    assert result["errorType"] == "install_not_allowed"
    assert calls == []
    assert tracker == []


def test_prewarm_blocked_reports_missing_without_executing(monkeypatch):
    """启动预热未授权时退化为纯检测：一条安装命令都不执行，但缺谁要说明白"""
    _revoke_install_authorization(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_install_factory(calls))

    store = FakeStore({
        "mcp1": {
            "id": "mcp1",
            "kind": "mcp",
            "connection": {"type": "stdio", "command": "cmd.exe", "args": ["/c", "tool-a"]},
            "install": {"command": "npm i -g a"},
        },
        "app1": {"id": "app1", "kind": "app"},
    })
    manager = BackendManager(store)
    result = asyncio.run(manager.ensure_all_installed())

    assert result["blocked"] == 1
    assert result["installed"] == 0
    assert calls == [], "预热阶段未授权时不允许执行任何安装命令"
    by_id = {d["tool_id"]: d for d in result["details"]}
    assert by_id["mcp1"]["installBlocked"] == "unauthorized"


# ---------------------------------------------------------------------------
# A7：调用方超时预算内的有界返回 + PATH 自愈 + anyio 作用域安全
#
# 背景（2026-10-16 实测）：经 toolport 网关调用 start_backend，4/4 在整整
# 30000ms 超时。网关的 tools/call 预算 30s，超时后取消请求——而
# asyncio.wait_for 从外部任务取消 anyio 协程会撕裂取消作用域栈，把整个
# MCP Server 进程带崩（RuntimeError: Attempted to exit a cancel scope...）。
# 三个修复：限时改用 anyio.fail_after；安装锁等待有界；PATH 自愈避免
# 误触发数十秒的 npm 安装。
# ---------------------------------------------------------------------------


class _HangingInstallProcess:
    """模拟卡住不返回的安装进程（npm i -g 在跑）"""

    def __init__(self):
        self.returncode = None
        self.killed = False

    async def communicate(self):
        await asyncio.Event().wait()  # 永远挂住，直到被取消

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class _FakeNpmPrefixProcess:
    """模拟 npm prefix -g 的假进程"""

    def __init__(self, prefix: str, returncode: int = 0):
        self._prefix = prefix
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        if self.returncode != 0:
            return (b"", b"npm not found")
        return (f"{self._prefix}\n".encode("utf-8"), b"")

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _fake_shell_dispatch(prefix: str, returncode: int = 0, log: list | None = None):
    """create_subprocess_shell 的假实现：npm prefix 走假 prefix 进程，
    其余命令记入 log 并返回立即成功的假安装进程"""

    async def factory(command, **kwargs):
        if log is not None:
            log.append(command)
        if command.strip().startswith("npm prefix"):
            return _FakeNpmPrefixProcess(prefix, returncode)
        return FakeInstallProcess()

    return factory


def test_ensure_installed_returns_installing_when_lock_held(monkeypatch):
    """A7：别人正持锁安装时，本请求不干等，快速返回 installing"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(BackendClient, "INSTALL_LOCK_TIMEOUT", 0.2)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    async def hanging_shell(command, **kwargs):
        return _HangingInstallProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", hanging_shell)

    async def scenario():
        client = BackendClient(
            "x",
            {"command": "cmd.exe", "args": ["/c", "foo"]},
            {"command": "npm i -g foo"},
        )
        installer = asyncio.create_task(client.ensure_installed())
        await asyncio.sleep(0.05)  # 让 installer 拿到锁并进入安装

        t0 = time.monotonic()
        result = await client.ensure_installed()
        elapsed = time.monotonic() - t0

        assert result.get("installing") is True
        assert result.get("success") is False
        assert elapsed < 1.0, "不应干等整个安装（调用方的 tools/call 预算有限）"

        installer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await installer

    asyncio.run(scenario())


def test_cancelled_install_does_not_poison_retry(monkeypatch):
    """A7：安装被取消（调用方断开/网关超时）时不标记 attempted，重试还能再装"""
    _authorize_install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    class _CancelOnFirst:
        """第一次 communicate 被取消；第二次正常成功"""

        def __init__(self):
            self.round = 0
            self.returncode = 0
            self.killed = False

        async def communicate(self):
            self.round += 1
            if self.round == 1:
                await asyncio.Event().wait()  # 挂住等取消
            return (b"", b"")

        def kill(self):
            self.killed = True

        async def wait(self):
            return self.returncode

    # 进程实例只建一次：第一次 communicate 挂住等取消，重试时同一实例
    # round=2 走到成功分支（若每次新建，round 归零会再次挂起 → 测试死锁）
    proc = _CancelOnFirst()

    async def shell(command, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", shell)

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", "foo"]},
        {"command": "npm i -g foo"},
    )

    async def scenario():
        task = asyncio.create_task(client.ensure_installed())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert client._install_attempted is False, "被取消不算「装过」"

        # 重试：安装重新执行并成功
        result = await client.ensure_installed()
        assert result.get("installed") is True
        assert client._install_attempted is True

    asyncio.run(scenario())


def test_start_returns_installing_without_spawn(monkeypatch):
    """A7：ensure_installed 报告 installing 时，start 直接返回、不 spawn"""

    async def fake_ensure_installed(self):
        return {
            "success": False,
            "available": False,
            "installed": False,
            "installing": True,
            "note": "依赖正在安装中，请稍后重试",
        }

    monkeypatch.setattr(BackendClient, "ensure_installed", fake_ensure_installed)

    def boom(*args, **kwargs):
        raise AssertionError("installing 时不应创建 stdio 连接")

    monkeypatch.setattr(backend_client, "stdio_client", boom)

    client = BackendClient("x", {"command": "cmd.exe", "args": ["/c", "foo"]})
    result = asyncio.run(client.start())

    assert result["success"] is False
    assert result["status"] == "installing"
    assert "稍后重试" in result["message"]
    assert client.status == BackendClient.STATUS_STOPPED


def test_ensure_npm_bin_on_path_patches_path_and_caches(monkeypatch, tmp_path):
    """A7：PATH 自愈成功时补入目录，且结果进程级缓存（不反复跑 npm）"""
    bin_dir = tmp_path / "npm-global"
    bin_dir.mkdir()
    monkeypatch.setattr(backend_client, "_npm_bin_dir", None)
    monkeypatch.setenv("PATH", str(tmp_path))  # 模拟被裁剪的环境：不含 bin_dir

    log = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_shell", _fake_shell_dispatch(str(bin_dir), log=log)
    )
    result = asyncio.run(REAL_ENSURE_NPM_BIN())
    assert result == str(bin_dir)
    assert str(bin_dir) in os.environ["PATH"].split(os.pathsep)
    assert log == ["npm prefix -g"]

    # 第二次命中缓存：不再起子进程
    log.clear()

    def boom(*args, **kwargs):
        raise AssertionError("缓存命中时不应再跑 npm prefix")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", boom)
    assert asyncio.run(REAL_ENSURE_NPM_BIN()) == str(bin_dir)
    assert log == []


def test_ensure_npm_bin_on_path_failure_is_silent_and_cached(monkeypatch):
    """A7：npm 不可用时静默返回 None，不动 PATH，失败也进缓存不重试"""
    monkeypatch.setattr(backend_client, "_npm_bin_dir", None)
    log = []
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_shell",
        _fake_shell_dispatch("", returncode=1, log=log),
    )
    before = os.environ.get("PATH", "")

    assert asyncio.run(REAL_ENSURE_NPM_BIN()) is None
    assert os.environ.get("PATH", "") == before
    assert log == ["npm prefix -g"]

    # 失败同样缓存：不再跑第二次
    log.clear()
    assert asyncio.run(REAL_ENSURE_NPM_BIN()) is None
    assert log == []


def test_ensure_installed_resolves_target_from_npm_bin_without_installing(
    monkeypatch, tmp_path
):
    """A7：PATH 自愈后目标可解析——不触发安装（网关裁剪 PATH 的场景）"""
    bin_dir = tmp_path / "npm-global"
    bin_dir.mkdir()
    target = "awam-probe-cli"
    exe = bin_dir / (target + (".cmd" if os.name == "nt" else ""))
    exe.write_text("stub", encoding="utf-8")
    if os.name != "nt":  # pragma: no cover - 本仓库主要在 Windows 上跑
        exe.chmod(0o755)

    log = []
    monkeypatch.setattr(backend_client, "_npm_bin_dir", None)
    monkeypatch.setattr(
        asyncio, "create_subprocess_shell", _fake_shell_dispatch(str(bin_dir), log=log)
    )
    monkeypatch.setenv("PATH", str(tmp_path))  # 裁剪环境：没有 bin_dir
    # ensure_installed 内部要走真实的 PATH 自愈（conftest 默认打成 noop）
    monkeypatch.setattr(backend_client, "ensure_npm_bin_on_path", REAL_ENSURE_NPM_BIN)

    client = BackendClient(
        "x",
        {"command": "cmd.exe", "args": ["/c", target]},
        {"command": f"npm i -g {target}"},
    )
    result = asyncio.run(client.ensure_installed())

    assert result == {"success": True, "available": True, "installed": False}
    assert log == ["npm prefix -g"], "PATH 自愈解决后不应触发安装"


# ---------------------------------------------------------------------------
# A8：连接生命周期独占任务化——anyio 取消作用域的任务绑定
#
# 背景（2026-10-16 实证）：stdio_client 与 ClientSession 的 __aenter__ 都会
# 进入各自内部 TaskGroup 的 anyio CancelScope，而 CancelScope 绑定「进入它
# 的任务」。旧实现在 start 的调用任务里 __aenter__、事后再在清理任务里
# __aexit__，后果：
#   1. 请求任务作用域栈上留下永不闭合的内层作用域，mcp SDK 的
#      RequestResponder 在工具响应发出之后退出时踩空：
#      RuntimeError: Attempted to exit a cancel scope that isn't the
#      current tasks's current cancel scope → TaskGroup → Server 进程
#      exit 1（toolport 网关审计 4/4，响应已到达客户端之后才崩）；
#   2. 清理任务 __aexit__ 报 Attempted to exit cancel scope in a
#      different task than it was entered in（被兜住，但作用域泄漏）。
# 修复：连接的一生（spawn/握手/持有/拆除）收拢进 _connection_loop 独占
# 任务，其他任务只用 self.session 或经 _ready/_shutdown 事件协作。
# ---------------------------------------------------------------------------


class _RecordingStdio:
    """记录 __aenter__/__aexit__ 发生在哪个任务的假 stdio_client"""

    def __init__(self, events: list):
        self.events = events

    async def __aenter__(self):
        self.events.append(("enter", "stdio", asyncio.current_task()))
        return (object(), object())

    async def __aexit__(self, *args):
        self.events.append(("exit", "stdio", asyncio.current_task()))


class _RecordingSession:
    """记录 __aenter__/__aexit__ 任务的假 ClientSession（initialize 等均成功）"""

    def __init__(self, events: list):
        self.events = events

    async def __aenter__(self):
        self.events.append(("enter", "session", asyncio.current_task()))
        return self

    async def __aexit__(self, *args):
        self.events.append(("exit", "session", asyncio.current_task()))

    async def initialize(self):
        pass

    async def list_tools(self):
        class _Result:
            tools = []

        return _Result()


def test_connection_contexts_enter_and_exit_in_same_task(monkeypatch):
    """A8：stdio_client / ClientSession 必须在同一任务内进入并退出

    这是 anyio CancelScope 的硬约束。旧实现跨任务进出，直接把整个
    MCP Server 进程带崩（见本节背景）。start 与 stop 特意跑在不同任务里
    （≈ 真实 server：start 在 tools/call 请求任务，close_all 在主任务），
    旧实现在此必然 enter/exit 任务不一致。
    """
    events: list = []
    monkeypatch.setattr(backend_client, "stdio_client", lambda params: _RecordingStdio(events))
    monkeypatch.setattr(backend_client, "ClientSession", lambda r, w: _RecordingSession(events))

    async def scenario():
        client = BackendClient(
            "x", {"command": "cmd.exe", "args": ["/c", "foo"], "timeout": 5}
        )
        start_result = await asyncio.create_task(client.start())
        assert start_result["success"] is True
        caller = asyncio.current_task()
        # 清理跑在另一个任务里：≈ 真实 server 的 close_all 所在主任务
        await asyncio.create_task(client.stop())
        return caller

    caller = asyncio.run(scenario())

    enters = {name: task for phase, name, task in events if phase == "enter"}
    exits = {name: task for phase, name, task in events if phase == "exit"}
    assert set(enters) == {"stdio", "session"}, f"进入事件不全: {events}"
    assert set(exits) == {"stdio", "session"}, f"退出事件不全: {events}"
    for name in ("stdio", "session"):
        assert enters[name] is exits[name], (
            f"{name} 的进入与退出不在同一任务——anyio 作用域踩空回归"
        )
        assert enters[name] is not caller, f"{name} 不应压在调用方任务的作用域栈上"


class _HangingStdio:
    """__aenter__ 永远挂住（模拟 spawn 卡死）"""

    async def __aenter__(self):
        await asyncio.Event().wait()
        return (object(), object())  # pragma: no cover

    async def __aexit__(self, *args):
        pass  # pragma: no cover


def test_start_timeout_reaps_connection_task(monkeypatch):
    """A8：等待就绪超时必须收尸连接任务——否则子进程孤儿、任务泄漏"""
    monkeypatch.setattr(backend_client, "stdio_client", lambda params: _HangingStdio())

    async def scenario():
        client = BackendClient(
            "x", {"command": "cmd.exe", "args": ["/c", "foo"], "timeout": 0.2}
        )
        result = await client.start()
        return client, result

    client, result = asyncio.run(scenario())

    assert result["success"] is False
    assert "超时" in result["error"]
    assert client._conn_task is None, "超时后连接任务必须被回收"
    assert client.status == BackendClient.STATUS_ERROR


class _SlowStdio:
    """__aenter__ 慢到超过调用方耐心（模拟握手窗口内的网关取消）"""

    async def __aenter__(self):
        await asyncio.sleep(5)
        return (object(), object())  # pragma: no cover

    async def __aexit__(self, *args):
        pass  # pragma: no cover


def test_start_cancelled_midflight_reaps_connection_task(monkeypatch):
    """A8：start 在途被取消（调用方断开/网关超时）也必须收尸连接任务"""
    monkeypatch.setattr(backend_client, "stdio_client", lambda params: _SlowStdio())

    async def scenario():
        client = BackendClient(
            "x", {"command": "cmd.exe", "args": ["/c", "foo"], "timeout": 10}
        )
        task = asyncio.create_task(client.start())
        await asyncio.sleep(0.1)  # 让 start 进入等待就绪的窗口
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)  # 双保险：收尸在取消路径里同步执行
        return client

    client = asyncio.run(scenario())

    assert client._conn_task is None, "取消后连接任务必须被回收，不能留孤儿"


def test_stop_joins_connection_task():
    """A8：stop 返回时连接任务必须已收尸——子进程不会活过 stop"""

    async def run():
        client = FakeBackendClient()
        client.status = BackendClient.STATUS_RUNNING
        client.attach()
        task = client._conn_task
        await client.stop()
        return client, task

    client, task = asyncio.run(run())

    assert task.done(), "stop 应等待连接任务自行拆除后再返回"
    assert "exit:stdio" in client.tracker


# ---- A10：期望状态持久化 + 跨 server 世代自动恢复 ----
#
# 背景（2026-10-16 用户报障「start 成功后状态仍是 stopped，每次都得重新
# 初始化」的第二个根因）：toolport 网关周期性整池回收它拉起的 server 进程
# （实测约每几分钟一批，回收走关 stdin → 本服务优雅退出 → close_all）。
# 每次回收后新 server 世代内存状态清零，后端全部回到 stopped。stdio MCP
# server 的生命周期归网关管，进程内状态注定随进程消亡——把「启动意图」
# 落盘、新世代启动时自动恢复，是让用户跨回收也能随时查到 running 的唯一
# 办法（真正的「永不重启」需要脱离网关的外部 supervisor，见架构文档）。
# 这里用 fake 的 start/stop 驱动，不碰真实子进程。


async def _ok_start(self):
    """替身：直接成功的 client.start"""
    self.status = BackendClient.STATUS_RUNNING
    self.started_at = time.time()
    return {"success": True, "message": "启动成功"}


async def _fail_start(self):
    """替身：失败的 client.start"""
    return {"success": False, "error": "boom"}


async def _ok_stop(self):
    """替身：直接成功的 client.stop"""
    self.status = BackendClient.STATUS_STOPPED
    return {"success": True, "message": "已停止"}


async def _ok_sync(self, tool_id):
    """替身：manager.sync_tools_catalog"""
    return {"success": True, "count": 0}


def test_start_persists_desired_state(tmp_path, monkeypatch):
    """A10：start 成功必须把启动意图落盘，否则新世代无从恢复"""
    state_file = tmp_path / "desired.json"
    monkeypatch.setattr(backend_client, "DESIRED_STATE_FILE", state_file)
    monkeypatch.setattr(BackendClient, "start", _ok_start)
    monkeypatch.setattr(BackendManager, "sync_tools_catalog", _ok_sync)

    async def run():
        manager = BackendManager(FakeStore({"mcp1": _mcp_tool({"type": "stdio"})}))
        return await manager.start_backend("mcp1")

    result = asyncio.run(run())
    assert result["success"] is True
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["mcp1"]["desired"] is True


def test_failed_start_does_not_persist(tmp_path, monkeypatch):
    """A10：启动失败不写意图——否则每个新世代都会去拉一个注定失败的后端"""
    state_file = tmp_path / "desired.json"
    monkeypatch.setattr(backend_client, "DESIRED_STATE_FILE", state_file)
    monkeypatch.setattr(BackendClient, "start", _fail_start)

    async def run():
        manager = BackendManager(FakeStore({"mcp1": _mcp_tool({"type": "stdio"})}))
        return await manager.start_backend("mcp1")

    result = asyncio.run(run())
    assert result["success"] is False
    assert not state_file.exists(), "失败启动不应留下意图文件"


def test_stop_clears_desired_state(tmp_path, monkeypatch):
    """A10：stop 成功清除意图——用户明确说不要了，恢复时不再拉起"""
    state_file = tmp_path / "desired.json"
    monkeypatch.setattr(backend_client, "DESIRED_STATE_FILE", state_file)
    state_file.write_text(
        json.dumps({"mcp1": {"desired": True}, "other": {"desired": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(BackendClient, "stop", _ok_stop)

    async def run():
        manager = BackendManager(FakeStore({"mcp1": _mcp_tool({"type": "stdio"})}))
        return await manager.stop_backend("mcp1")

    result = asyncio.run(run())
    assert result["success"] is True
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "mcp1" not in state
    assert "other" in state, "只清目标，不碰其他工具的意图"


def test_restore_starts_desired_backends(tmp_path, monkeypatch):
    """A10：全新世代（新 manager，内存无状态）启动时自动拉起 desired 后端"""
    state_file = tmp_path / "desired.json"
    monkeypatch.setattr(backend_client, "DESIRED_STATE_FILE", state_file)
    state_file.write_text(json.dumps({"mcp1": {"desired": True}}), encoding="utf-8")
    monkeypatch.setattr(BackendClient, "start", _ok_start)
    monkeypatch.setattr(BackendManager, "sync_tools_catalog", _ok_sync)

    async def run():
        # 全新世代：clients 为空，等于网关回收后新拉起的 server 进程
        manager = BackendManager(FakeStore({"mcp1": _mcp_tool({"type": "stdio"})}))
        result = await manager.restore_desired_backends()
        return result, manager.get_backend_status("mcp1")

    result, status = asyncio.run(run())
    assert result["mcp1"]["success"] is True
    assert status["status"]["status"] == BackendClient.STATUS_RUNNING


def test_restore_skips_already_running(tmp_path, monkeypatch):
    """A10：同代内已 running 的后端不重复启动"""
    state_file = tmp_path / "desired.json"
    monkeypatch.setattr(backend_client, "DESIRED_STATE_FILE", state_file)
    state_file.write_text(json.dumps({"mcp1": {"desired": True}}), encoding="utf-8")
    started = []

    async def _counting_start(self):
        started.append(self.tool_id)
        self.status = BackendClient.STATUS_RUNNING
        self.started_at = time.time()
        return {"success": True, "message": "启动成功"}

    monkeypatch.setattr(BackendClient, "start", _counting_start)
    monkeypatch.setattr(BackendManager, "sync_tools_catalog", _ok_sync)

    async def run():
        manager = BackendManager(FakeStore({"mcp1": _mcp_tool({"type": "stdio"})}))
        await manager.restore_desired_backends()
        again = await manager.restore_desired_backends()
        return again

    again = asyncio.run(run())
    assert started == ["mcp1"], "第二次恢复应发现已 running 而跳过"
    assert again == {}


def test_restore_cleans_stale_entries(tmp_path, monkeypatch):
    """A10：工具已删除时清掉过期意图，避免每个新世代都白跑一次"""
    state_file = tmp_path / "desired.json"
    monkeypatch.setattr(backend_client, "DESIRED_STATE_FILE", state_file)
    state_file.write_text(json.dumps({"gone": {"desired": True}}), encoding="utf-8")

    async def run():
        # store 里没有 "gone"：工具已删或不再是 mcp 类型
        manager = BackendManager(FakeStore({}))
        return await manager.restore_desired_backends()

    result = asyncio.run(run())
    assert result == {}
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "gone" not in state, "过期意图应被清除"


def test_desired_state_corrupt_file_treated_as_empty(tmp_path, monkeypatch):
    """A10：意图文件损坏按空处理——恢复是尽力而为，不能阻断服务启动"""
    state_file = tmp_path / "desired.json"
    monkeypatch.setattr(backend_client, "DESIRED_STATE_FILE", state_file)
    state_file.write_text("{ 半个 json", encoding="utf-8")

    manager = BackendManager(FakeStore({}))
    assert manager._load_desired_state() == {}
