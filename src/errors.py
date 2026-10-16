"""业务错误类型与失败出口（P1-04 从 server.py 原样搬出）。

`tool_specs.py` 的 handlers 需要 `fail(...)` / `ensure_success(...)` 和
`ToolError`；若留在 `server.py`，`tool_specs` import `server` 会形成
`server → tool_specs → server` 循环。`server.py` 改为
`from errors import ...` 并在其命名空间 re-export，外部引用
（如 `from server import ToolError`）拿到的仍是同一个类对象。
"""

import json


class ToolError(Exception):
    """业务级失败。

    mcp 1.x 的低层 Server 会把 @app.call_tool() 的**正常返回值**恒包装成
    CallToolResult(isError=False)，因此「让客户端知道这次调用失败了」的唯一
    途径就是抛出异常 —— SDK 捕获后会自动转成 isError=True 的结果。

    异常正文统一为 JSON，确保客户端既能感知失败，也能读到可读的原因。
    """


def fail(error: str, error_type: str = "invalid_params", **extra) -> None:
    """抛出携带 JSON 载荷的业务错误"""
    payload = {"success": False, "errorType": error_type, "error": error}
    payload.update(extra)
    raise ToolError(json.dumps(payload, ensure_ascii=False))


def ensure_success(res: dict) -> dict:
    """后端 / 注册中心返回的失败结果一律转成 ToolError"""
    if not res.get("success"):
        raise ToolError(json.dumps(res, ensure_ascii=False, default=str))
    return res
