"""HTTP transport 的配置、单实例与鉴权边界。

真会话（initialize / list_tools）在 `tests/e2e/test_e2e_http_transport.py`，
这里只钉不依赖子进程的判定：非法配置必须启动前失败、非回环地址必须显式
放行、token 优先级、第二个监听者必须失败、/health 免鉴权而 /mcp 不可绕过。
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import http_transport as ht  # noqa: E402


# ---- transport / host / port ----


def test_default_transport_is_stdio(monkeypatch):
    monkeypatch.delenv(ht.TRANSPORT_ENV_VAR, raising=False)
    assert ht.normalize_transport(None) == "stdio"
    assert ht.normalize_transport("") == "stdio"


def test_transport_env_fallback_and_cli_override(monkeypatch):
    monkeypatch.setenv(ht.TRANSPORT_ENV_VAR, "http")
    assert ht.normalize_transport(None) == "http"
    # 显式参数优先于环境变量
    assert ht.normalize_transport("stdio") == "stdio"


def test_invalid_transport_is_rejected():
    with pytest.raises(ht.HttpConfigError):
        ht.normalize_transport("websocket")


def test_loopback_hosts_are_accepted(monkeypatch):
    monkeypatch.delenv(ht.ALLOW_REMOTE_ENV_VAR, raising=False)
    for host in ("127.0.0.1", "::1", "localhost"):
        assert ht.resolve_host(host) == host


def test_non_loopback_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv(ht.ALLOW_REMOTE_ENV_VAR, raising=False)
    with pytest.raises(ht.HttpConfigError):
        ht.resolve_host("0.0.0.0")
    with pytest.raises(ht.HttpConfigError):
        ht.resolve_host("192.168.1.10")

    monkeypatch.setenv(ht.ALLOW_REMOTE_ENV_VAR, "1")
    assert ht.resolve_host("0.0.0.0") == "0.0.0.0"


def test_port_defaults_and_validation(monkeypatch):
    monkeypatch.delenv(ht.PORT_ENV_VAR, raising=False)
    assert ht.resolve_port(None) == ht.DEFAULT_PORT
    assert ht.resolve_port("9000") == 9000
    with pytest.raises(ht.HttpConfigError):
        ht.resolve_port(0)
    with pytest.raises(ht.HttpConfigError):
        ht.resolve_port("http")


# ---- token ----


def test_token_env_wins_and_does_not_create_file(monkeypatch, tmp_path):
    monkeypatch.setenv(ht.TOKEN_ENV_VAR, "env-token")
    token, source = ht.resolve_token(tmp_path)
    assert token == "env-token"
    assert ht.TOKEN_ENV_VAR in source
    assert not (tmp_path / ht.TOKEN_FILE_NAME).exists()


def test_token_file_is_created_then_reused(monkeypatch, tmp_path):
    monkeypatch.delenv(ht.TOKEN_ENV_VAR, raising=False)
    first, first_source = ht.resolve_token(tmp_path)
    second, second_source = ht.resolve_token(tmp_path)

    assert first == second
    assert len(first) >= 32
    assert first_source == second_source == str(tmp_path / ht.TOKEN_FILE_NAME)
    assert (tmp_path / ht.TOKEN_FILE_NAME).read_text(encoding="utf-8").strip() == first


def test_empty_token_file_is_reported(monkeypatch, tmp_path):
    monkeypatch.delenv(ht.TOKEN_ENV_VAR, raising=False)
    (tmp_path / ht.TOKEN_FILE_NAME).write_text("", encoding="utf-8")
    with pytest.raises(ht.HttpConfigError):
        ht.resolve_token(tmp_path)


# ---- 单实例 ----


def test_second_listener_on_same_port_fails():
    first = ht.bind_socket(
        ht.HttpConfig(host="127.0.0.1", port=0, token="t", token_source="test")
    )
    try:
        port = first.getsockname()[1]
        with pytest.raises(ht.HttpTransportError):
            ht.bind_socket(
                ht.HttpConfig(host="127.0.0.1", port=port, token="t", token_source="test")
            )
    finally:
        first.close()


# ---- 鉴权 ----


def _run_asgi(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_mcp_path_requires_bearer_token():
    app = ht.BearerAuthMiddleware(_ok_app, "secret")

    missing = _run_asgi(app, {"type": "http", "path": "/mcp", "headers": []})
    assert missing[0]["status"] == 401

    wrong = _run_asgi(
        app,
        {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer nope")],
        },
    )
    assert wrong[0]["status"] == 401

    ok = _run_asgi(
        app,
        {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer secret")],
        },
    )
    assert ok[0]["status"] == 200


def test_health_is_exempt_from_auth():
    app = ht.BearerAuthMiddleware(_ok_app, "secret")
    sent = _run_asgi(app, {"type": "http", "path": "/health", "headers": []})
    assert sent[0]["status"] == 200


def test_health_includes_gateway_version_not_mcp_sdk():
    """/health.version 必须是 Server(version=...)，不能回落到 mcp 包版本。"""
    from mcp.server import Server

    server = Server("awam-tool-manager", version="0.1.0")
    app = ht.build_asgi_app(server, token="secret")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }
    sent = _run_asgi(app, scope)
    assert sent[0]["status"] == 200
    body = next(m["body"] for m in sent if m["type"] == "http.response.body")
    payload = __import__("json").loads(body.decode("utf-8"))
    assert payload["status"] == "ok"
    assert payload["transport"] == "http"
    assert payload["version"] == "0.1.0"
    assert payload["version"] != __import__("importlib.metadata", fromlist=["version"]).version("mcp")
