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


class ErrorType:
    """`errorType` 契约取值常量枚举（P1-03 建立，SSOT）。

    客户端可达的失败载荷必须携带其中之一。合法集 = `ErrorType.all()`，
    由 `tests/test_error_type_contract.py` 的守卫钉住两件事：

    1. 枚举值集合与 `SKILL.md`「错误处理约定」声明的集合一致
       （文档 vs 代码漂移即显红，而不是静默）；
    2. A 层（经 `ensure_success()` 可达客户端）的每个失败站点都带合法值。

    取值是普通 str 常量而非 `enum.Enum`：载荷要过 `json.dumps`，既有测试
    也按字符串比对（`== "config_error"`）——与 `BackendClient.STATUS_*`
    的状态机常量同一体例。

    16 种取值的语义 / 触发条件 / 客户端动作见 SKILL.md「错误处理约定」
    与清点单 `docs/architecture_incremental_p1-03_errorType_inventory.md` §2。
    """

    NOT_FOUND = "not_found"
    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_RESOURCE = "unknown_resource"
    UNKNOWN_PROMPT = "unknown_prompt"
    # A1（set_backend_log_level 的 level 非法值）落地后首次可达；
    # 参数校验议题上线后将扩大启用面
    INVALID_PARAMS = "invalid_params"
    CONNECTION_ERROR = "connection_error"
    RUNTIME_ERROR = "runtime_error"
    CONFIG_ERROR = "config_error"
    DESTRUCTIVE_BLOCKED = "destructive_blocked"
    BACKEND_BUSY = "backend_busy"
    # Q2 新增（P1-03）：依赖安装中是分钟级忙态，与 backend_busy 的秒级
    # 并发冲突分工不同；契约卡要求「可重试」
    BACKEND_INSTALLING = "backend_installing"
    INTERNAL_ERROR = "internal_error"
    LAUNCHER_ERROR = "launcher_error"
    INSTALL_REQUIRED = "install_required"
    INSTALL_NOT_ALLOWED = "install_not_allowed"
    # 兜底值：后端抛了既非 BackendBusyError/ConnectionError/RuntimeError
    # 的未分类异常
    UNKNOWN_ERROR = "unknown_error"

    @classmethod
    def all(cls) -> frozenset:
        """合法取值集合（守卫测试与文档比对用）"""
        return frozenset(
            value
            for key, value in vars(cls).items()
            if not key.startswith("_") and isinstance(value, str)
        )


def fail(error: str, error_type: str = ErrorType.INVALID_PARAMS, **extra) -> None:
    """抛出携带 JSON 载荷的业务错误"""
    payload = {"success": False, "errorType": error_type, "error": error}
    payload.update(extra)
    raise ToolError(json.dumps(payload, ensure_ascii=False))


def ensure_success(res: dict) -> dict:
    """后端 / 注册中心返回的失败结果一律转成 ToolError"""
    if not res.get("success"):
        raise ToolError(json.dumps(res, ensure_ascii=False, default=str))
    return res
