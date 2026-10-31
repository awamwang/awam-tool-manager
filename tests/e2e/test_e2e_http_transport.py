"""场景：真实 Streamable HTTP 起服 + 真实 MCP 会话。

覆盖三件事：
1. `--transport http` 真的监听并响应 `/health`；
2. 无 token 的 `/mcp` 请求被 401 拦下；
3. 带 Bearer token 的 MCP 客户端能 initialize 并拿到与 stdio 相同的工具清单。
"""

import asyncio
import socket
import subprocess
import sys
import tempfile

import httpx
import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .helpers import build_env, load_expected_tool_names, wait_until

TOKEN = "e2e-http-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _list_tool_names(url: str) -> list[str]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=10)
            result = await asyncio.wait_for(session.list_tools(), timeout=10)
            return [tool.name for tool in result.tools]


async def _health_ready(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(url)
            return response.status_code == 200
    except httpx.HTTPError:
        # 服务还没开始监听；交给 wait_until 继续轮询。
        return False


@pytest.mark.e2e
def test_e2e_http_transport_session(e2e_repo, e2e_runtime):
    port = _free_port()
    env = build_env(
        e2e_runtime,
        {
            "AWAM_TRANSPORT": "http",
            "AWAM_HTTP_HOST": "127.0.0.1",
            "AWAM_HTTP_PORT": str(port),
            "AWAM_HTTP_TOKEN": TOKEN,
        },
    )
    base = f"http://127.0.0.1:{port}"
    stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, str(e2e_repo / "src" / "server.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        text=True,
    )
    try:
        asyncio.run(
            wait_until(
                lambda: _health_ready(base + "/health"),
                timeout=20,
                description="HTTP /health 未就绪",
            )
        )

        unauthorized = httpx.post(
            base + "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert unauthorized.status_code == 401

        names = asyncio.run(_list_tool_names(base + "/mcp"))
        assert set(names) == load_expected_tool_names()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stderr.seek(0)
        log = stderr.read()
        stderr.close()

    assert "Streamable HTTP" in log, log[-2000:]
    assert "Traceback" not in log, log[-2000:]
