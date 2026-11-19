"""可选 Streamable HTTP transport（默认仍是 stdio）。

MCP 客户端按 ``python src/server.py --transport http`` 使用本模块；不传
``--transport`` 时 server.py 继续走 stdio，行为与历史版本完全一致。

设计取舍：

- **单实例靠端口预绑定**，不靠锁文件。Windows 上 ``SO_REUSEADDR`` 允许
  两个进程绑同一端口，因此这里**不设**该选项；第二个实例会拿到明确的
  ``HttpTransportError``，而不是悄悄共享端口。
- **默认只监听回环地址**。非回环地址必须显式设
  ``AWAM_HTTP_ALLOW_REMOTE=1``，否则启动即失败——本服务能启动本机程序、
  调 MCP 后端，误暴露到局域网比配置麻烦得多。
- **默认要求 Bearer token**。token 优先取 ``AWAM_HTTP_TOKEN``，否则在
  运行时目录生成 ``http-token``（0o600，能设则设）。``/health`` 不鉴权，
  只返回存活状态，方便探活。
- 暴露模式仍是**进程级**：一个 HTTP 实例只服务一种 ``EXPOSURE_MODE``。
  需要不同工具面就起不同端口的实例，或重启服务。
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import os
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

TRANSPORTS = ("stdio", "http")
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18778

TRANSPORT_ENV_VAR = "AWAM_TRANSPORT"
HOST_ENV_VAR = "AWAM_HTTP_HOST"
PORT_ENV_VAR = "AWAM_HTTP_PORT"
TOKEN_ENV_VAR = "AWAM_HTTP_TOKEN"
ALLOW_REMOTE_ENV_VAR = "AWAM_HTTP_ALLOW_REMOTE"
TOKEN_FILE_NAME = "http-token"

MCP_PATH = "/mcp"
HEALTH_PATH = "/health"

_TRUTHY = {"1", "true", "yes", "on"}


class HttpConfigError(RuntimeError):
    """HTTP transport 配置非法：启动前就失败，不带病运行。"""


class HttpTransportError(RuntimeError):
    """HTTP transport 运行时失败（典型是端口已被占用）。"""


@dataclass(frozen=True)
class HttpConfig:
    host: str
    port: int
    token: str
    token_source: str


def normalize_transport(value: object = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = os.environ.get(TRANSPORT_ENV_VAR, "").strip()
    transport = raw.lower() or DEFAULT_TRANSPORT
    if transport not in TRANSPORTS:
        raise HttpConfigError(
            f"transport 取值非法: {raw!r}（可选值: {', '.join(TRANSPORTS)}）"
        )
    return transport


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_host(value: object = None) -> str:
    host = str(value or os.environ.get(HOST_ENV_VAR, "").strip() or DEFAULT_HOST).strip()
    if not host:
        raise HttpConfigError("HTTP host 不能为空")
    if not _is_loopback(host) and not _env_flag(ALLOW_REMOTE_ENV_VAR):
        raise HttpConfigError(
            f"拒绝监听非回环地址 {host!r}：本服务会启动本机程序。"
            f"确需远程访问请显式设置 {ALLOW_REMOTE_ENV_VAR}=1，并自行做好防火墙与 token 管理"
        )
    return host


def resolve_port(value: object = None) -> int:
    raw = value if value is not None else os.environ.get(PORT_ENV_VAR, "").strip()
    if raw in (None, ""):
        return DEFAULT_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise HttpConfigError(f"HTTP port 必须是整数（收到: {raw!r}）") from None
    if not 1 <= port <= 65535:
        raise HttpConfigError(f"HTTP port 必须在 1..65535（收到: {port}）")
    return port


def token_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / TOKEN_FILE_NAME


def load_or_create_token(path: Path) -> tuple[str, str]:
    """读 token 文件；不存在则生成并原子落盘。返回 ``(token, 来源说明)``。"""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    except OSError as exc:
        raise HttpConfigError(f"读取 HTTP token 文件失败（{path}）: {exc}") from exc

    if token:
        return token, str(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # 另一个实例在读取之后、创建之前赢了竞态：用它的 token，别覆盖。
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HttpConfigError(f"读取 HTTP token 文件失败（{path}）: {exc}") from exc
        if not token:
            raise HttpConfigError(
                f"HTTP token 文件为空（{path}）；请删除后重新启动以生成新 token"
            )
        return token, str(path)
    except OSError as exc:
        raise HttpConfigError(f"创建 HTTP token 文件失败（{path}）: {exc}") from exc

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token, str(path)


def resolve_token(
    runtime_dir: str | Path,
    value: object = None,
) -> tuple[str, str]:
    explicit = str(value or "").strip()
    if explicit:
        return explicit, "参数"
    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token, f"环境变量 {TOKEN_ENV_VAR}"
    return load_or_create_token(token_path(runtime_dir))


def resolve_http_config(
    runtime_dir: str | Path,
    *,
    host: object = None,
    port: object = None,
    token: object = None,
) -> HttpConfig:
    token_value, token_source = resolve_token(runtime_dir, token)
    return HttpConfig(
        host=resolve_host(host),
        port=resolve_port(port),
        token=token_value,
        token_source=token_source,
    )


def bind_socket(config: HttpConfig) -> socket.socket:
    """预绑定监听端口。第二个实例在同一端口上会在这里失败。"""
    host = "127.0.0.1" if config.host.lower() == "localhost" else config.host
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # Windows 上 SO_REUSEADDR 会允许两个进程绑同一端口，故只在 Unix 上开；
        # 它只影响 TIME_WAIT 的重绑，不会放行第二个活跃监听者。
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, config.port))
    except OSError as exc:
        sock.close()
        raise HttpTransportError(
            f"无法监听 {config.host}:{config.port}（可能已有一个 awam-tool-manager 实例在运行）: {exc}"
        ) from exc
    return sock


def _header(scope: dict, name: bytes) -> bytes | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value
    return None


class BearerAuthMiddleware:
    """只保护 HTTP 请求；``/health`` 与 lifespan 直接放行。"""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        supplied = _header(scope, b"authorization")
        if not supplied or not hmac.compare_digest(supplied, self._expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"unauthorized"}',
                }
            )
            return
        await self.app(scope, receive, send)


class _MCPEndpoint:
    """把 StreamableHTTPSessionManager 的原始 ASGI 入口挂到路由上。"""

    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


def build_asgi_app(server, *, token: str):
    """装配 Starlette 应用：``/mcp`` 走 MCP，``/health`` 探活，其余 401/404。"""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    manager = StreamableHTTPSessionManager(server)
    # 与 MCP serverInfo.version 对齐，供 a-file-manager gatewayCompat 使用
    gateway_version = getattr(server, "version", None)
    if isinstance(gateway_version, str):
        gateway_version = gateway_version.strip() or None
    else:
        gateway_version = None

    async def health(_request):
        body = {"status": "ok", "transport": "http"}
        if gateway_version:
            body["version"] = gateway_version
        return JSONResponse(body)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    app = Starlette(
        routes=[
            Route(HEALTH_PATH, health, methods=["GET"]),
            Mount(MCP_PATH, app=_MCPEndpoint(manager)),
        ],
        lifespan=lifespan,
    )
    return BearerAuthMiddleware(app, token)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


async def serve_http(server, config: HttpConfig, *, log) -> None:
    import uvicorn

    asgi_app = build_asgi_app(server, token=config.token)
    sock = bind_socket(config)
    uv_server = uvicorn.Server(
        uvicorn.Config(
            asgi_app,
            host=config.host,
            port=config.port,
            lifespan="on",
            access_log=False,
        )
    )
    log(f"HTTP MCP 端点: http://{_url_host(config.host)}:{config.port}{MCP_PATH}")
    log(
        "HTTP 鉴权: Authorization: Bearer <token>"
        f"（token 来自 {config.token_source}）"
    )
    try:
        await uv_server.serve(sockets=[sock])
    finally:
        with contextlib.suppress(OSError):
            sock.close()
