"""errorType 契约守卫（P1-03 S6）。

清点单 `docs/architecture_incremental_p1-03_errorType_inventory.md` §5
建议的三类测试落地：

1. **枚举完整性**：`ErrorType` 常量枚举的值集合 == `SKILL.md`「错误处理
   约定」与 `docs/format.md`「错误返回约定」两份文档各自声明的集合
   （各 17 种，任何一份漂移即红）——把「文档 vs 代码」的漂移变成显红，
   而非静默。format.md 的枚举段是手写区（不在生成区块，
   `test_docs_sync` 的生成块校验管不到），必须由本守卫单独看住；
2. **A 层站点守卫**：清点单 §3.1 的 14 个客户端可达失败站点（经
   `ensure_success()` 转 `ToolError` 进协议）逐个真实触发，断言载荷带
   合法 `errorType`（合法集 = `ErrorType.all()`，含 `backend_installing`）
   且具体取值与 §3.2 补齐表逐站一致——哪个站点漏带 / 带错，这里就红；
3. **覆盖台账**：17 种取值每种至少一条触发用例。台账记录「哪个测试
   触发了哪个值」并校验被引用的测试函数真实存在——删了触发用例不补，
   台账就红。

两个此前的覆盖缺口由本文件补上：`invalid_params` 在 A1 落地前是死值
（`fail()` 默认参数从未被用上）；`launcher_error` 是当时 16 种里唯一没有
触发用例的非 A 层值（`list_launcher_actions` 的 handler 把
`LauncherError` 转成带 errorType 的失败，而不是落进内部异常兜底）。
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402
from backend_client import BackendClient, BackendManager  # noqa: E402
from errors import ErrorType, ToolError  # noqa: E402
from launcher_client import LauncherError  # noqa: E402

SKILL_PATH = ROOT / "SKILL.md"
FORMAT_PATH = ROOT / "docs" / "format.md"
TESTS_DIR = Path(__file__).parent


def run(coro):
    """统一的事件循环入口"""
    return asyncio.run(coro)


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


class _ListToolsClient(BackendClient):
    """list_tools 行为可注入的假客户端（返回固定列表 / 抛异常）"""

    def __init__(self, tool_id: str = "mcp1", *, tools=None, exc=None):
        super().__init__(tool_id, {"type": "stdio"})
        self._tools = tools
        self._exc = exc

    async def list_tools(self, force_refresh: bool = False):
        if self._exc is not None:
            raise self._exc
        return self._tools if self._tools is not None else [{"name": "a"}]


def _manager_with_client(client: BackendClient) -> BackendManager:
    """装着一个 RUNNING 假 client 的 manager（get_client 原样返回它）"""
    store = FakeStore({client.tool_id: _mcp_tool({"type": "stdio"})})
    manager = BackendManager(store)
    client.status = BackendClient.STATUS_RUNNING
    manager.clients[client.tool_id] = client
    return manager


# ---- 1. 枚举完整性：代码 == SKILL.md + docs/format.md 声明 ----


def _declared_error_types(doc_path: Path) -> set:
    """解析文档中 errorType 枚举段（反引号包裹、`/` 分隔、可跨行）。

    两份文档的锚点体例相同：以「`errorType`（取值）：「开头，列表以
    句号（SKILL.md）或空行（format.md）收尾。
    """
    text = doc_path.read_text(encoding="utf-8")
    head = text.split("`errorType`", 1)[1].split("：", 1)[1]
    segment = re.split(r"。|\n\s*\n", head, maxsplit=1)[0]
    return set(re.findall(r"`([a-z_]+)`", segment))


def _assert_doc_enum_matches(doc_path: Path) -> None:
    """单份文档的枚举 == ErrorType.all()；报错指明是哪份文档漂移"""
    declared = _declared_error_types(doc_path)
    legal = set(ErrorType.all())
    label = doc_path.name
    assert declared, f"{label} 解析不到任何 errorType 取值，解析器可能错了"
    assert declared == legal, (
        f"{label} 的 errorType 枚举与 ErrorType.all() 不一致："
        f"{label} 多出 {sorted(declared - legal)}，"
        f"缺少 {sorted(legal - declared)}"
    )


def test_error_type_enum_matches_docs():
    """代码枚举必须与两份文档（SKILL.md 契约 + format.md 规范）各自一致"""
    _assert_doc_enum_matches(SKILL_PATH)
    _assert_doc_enum_matches(FORMAT_PATH)


# ---- 2. A 层 14 站点守卫：失败必带合法 errorType ----


def _a_layer_cases() -> list:
    """[(站点号, 期望 errorType, 零参触发器)]。

    每个触发器真实走一遍对应失败分支，返回 payload dict。期望值与清点单
    §3.2 补齐表逐站一致——站点漏带 / 带错 errorType，守卫即红。
    """
    cases = []

    # A1（客户端级）：日志级别取值非法 → invalid_params（该值的首个发射点）
    level_client = BackendClient("fake", {"type": "stdio"})
    cases.append((
        "A1", ErrorType.INVALID_PARAMS,
        lambda: level_client.set_log_level("trace"),
    ))

    # A2/A3/A7/A8/A9/A10/A12（manager 级）：store 里没有该工具 →
    # get_client 返回 None 的各「未配置」分支
    missing = BackendManager(FakeStore())
    cases += [
        ("A2", ErrorType.CONFIG_ERROR,
         lambda: run(missing.start_backend("__no_such__"))),
        ("A3", ErrorType.CONFIG_ERROR,
         lambda: run(missing.sync_tools_catalog("__no_such__"))),
        ("A7", ErrorType.CONFIG_ERROR,
         lambda: run(missing.stop_backend("__no_such__"))),
        ("A8", ErrorType.CONFIG_ERROR,
         lambda: missing.get_backend_status("__no_such__")),
        ("A9", ErrorType.CONFIG_ERROR,
         lambda: missing.get_backend_logs("__no_such__")),
        ("A10", ErrorType.CONFIG_ERROR,
         lambda: missing.set_backend_log_level("__no_such__", "info")),
        ("A12", ErrorType.CONFIG_ERROR,
         lambda: run(missing.list_backend_tools("__no_such__"))),
    ]

    # A4：配了连接但未运行（get_client 新建的 client 默认 stopped）
    idle = BackendManager(FakeStore({"mcp1": _mcp_tool({"type": "stdio"})}))
    cases.append((
        "A4", ErrorType.CONNECTION_ERROR,
        lambda: run(idle.sync_tools_catalog("mcp1")),
    ))

    # A5：RUNNING 但后端返回空工具列表（Q3：runtime_error + warning）
    empty_sync = _manager_with_client(_ListToolsClient(tools=[]))
    cases.append((
        "A5", ErrorType.RUNTIME_ERROR,
        lambda: run(empty_sync.sync_tools_catalog("mcp1")),
    ))

    # A6：同步过程抛异常
    boom_sync = _manager_with_client(_ListToolsClient(exc=RuntimeError("boom")))
    cases.append((
        "A6", ErrorType.RUNTIME_ERROR,
        lambda: run(boom_sync.sync_tools_catalog("mcp1")),
    ))

    # A13：拉取工具列表抛异常
    boom_list = _manager_with_client(_ListToolsClient(exc=RuntimeError("boom")))
    cases.append((
        "A13", ErrorType.RUNTIME_ERROR,
        lambda: run(boom_list.list_backend_tools("mcp1")),
    ))

    # A11：启动过程异常。用「不支持的连接类型」天然触发——start() 入口
    # 的 conn_type 检查直接 raise，走失败收 rollback 分支；不 patch 任何
    # 方法，避免与 A14 的 patch 产生顺序耦合（lambda 是惰性执行的）。
    bad_type_client = BackendClient("x", {"type": "sse"})
    cases.append((
        "A11", ErrorType.RUNTIME_ERROR,
        lambda: run(bad_type_client.start()),
    ))

    # A14：依赖安装中（Q2：backend_installing，枚举第 16 种）。
    # 实例级 patch：只影响这一个 client，且先于任何 class 级 patch 求值。
    async def _installing():
        return {
            "success": False,
            "installing": True,
            "note": "依赖正在安装中，请稍后重试",
        }

    installing_client = BackendClient(
        "x", {"command": "cmd.exe", "args": ["/c", "foo"]}
    )
    installing_client.ensure_installed = _installing
    cases.append((
        "A14", ErrorType.BACKEND_INSTALLING,
        lambda: run(installing_client.start()),
    ))

    return cases


def test_a_layer_failures_all_carry_valid_error_type():
    """14 个客户端可达失败站点逐个触发：必带合法 errorType 且取值正确"""
    for site, expected, trigger in _a_layer_cases():
        payload = trigger()
        assert payload["success"] is False, f"{site} 触发器没走到失败分支"
        assert "errorType" in payload, f"{site} 漏带 errorType"
        assert payload["errorType"] in ErrorType.all(), (
            f"{site} 的 errorType={payload['errorType']!r} 不在合法集内"
        )
        assert payload["errorType"] == expected, (
            f"{site} 的 errorType 与清点单 §3.2 补齐表不符"
        )


# ---- 3. 覆盖台账：17 种取值各有触发用例 ----


def test_launcher_error_is_reachable(monkeypatch):
    """launcher_error 触发用例（P1-03 补：此前唯一没有触发用例的非 A 层值）"""
    def boom(*args, **kwargs):
        raise LauncherError("启动器数据目录不可用")

    monkeypatch.setattr(server.launcher_manager, "list_actions", boom)

    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("list_launcher_actions", {"tool_id": "x"}))

    payload = json.loads(str(exc.value))
    assert payload["errorType"] == ErrorType.LAUNCHER_ERROR


# 值 → 触发它的测试（file, function）。删了某个触发用例不补，台账就红。
_TRIGGER_LEDGER = {
    ErrorType.NOT_FOUND: (
        "test_server_dispatch.py", "test_missing_tool_id_raises_not_found"),
    ErrorType.UNKNOWN_TOOL: (
        "test_server_dispatch.py", "test_unknown_tool_raises_typed_error"),
    ErrorType.UNKNOWN_RESOURCE: (
        "test_server_dispatch.py", "test_read_unknown_resource_raises"),
    ErrorType.UNKNOWN_PROMPT: (
        "test_server_dispatch.py", "test_get_unknown_prompt_raises"),
    ErrorType.INVALID_PARAMS: (
        "test_error_type_contract.py",
        "test_a_layer_failures_all_carry_valid_error_type"),  # A1
    ErrorType.CONNECTION_ERROR: (
        "test_backend_client.py", "test_call_backend_tool_error_classification"),
    ErrorType.RUNTIME_ERROR: (
        "test_backend_client.py", "test_call_backend_tool_error_classification"),
    ErrorType.CONFIG_ERROR: (
        "test_backend_client.py", "test_call_backend_tool_on_unknown_tool"),
    ErrorType.DESTRUCTIVE_BLOCKED: (
        "test_registry_policy.py",
        "test_destructive_call_is_blocked_without_confirmation"),
    # Phase 2（P1-05 方案 C）：策略拒绝的真实触发（deny 名单 → dispatch 拒绝）
    ErrorType.PERMISSION_DENIED: (
        "test_policy_config.py", "test_deny_list_blocks_dispatch_call"),
    ErrorType.BACKEND_BUSY: (
        "test_server_dispatch.py", "test_backend_busy_maps_to_typed_error"),
    ErrorType.BACKEND_INSTALLING: (
        "test_backend_client.py", "test_start_returns_installing_without_spawn"),
    ErrorType.INTERNAL_ERROR: (
        "test_server_dispatch.py", "test_internal_exception_becomes_typed_error"),
    ErrorType.LAUNCHER_ERROR: (
        "test_error_type_contract.py", "test_launcher_error_is_reachable"),
    ErrorType.INSTALL_REQUIRED: (
        "test_backend_client.py",
        "test_start_backend_blocked_returns_structured_failure"),
    ErrorType.INSTALL_NOT_ALLOWED: (
        "test_backend_client.py",
        "test_start_backend_not_allowed_returns_structured_failure"),
    ErrorType.UNKNOWN_ERROR: (
        "test_backend_client.py", "test_call_backend_tool_error_classification"),
}


def test_every_error_type_has_a_trigger_case():
    """台账覆盖全部 17 种取值，且被引用的测试函数真实存在"""
    assert set(_TRIGGER_LEDGER) == set(ErrorType.all()), (
        "台账与枚举不一致：补齐缺失条目或删除多余条目"
    )
    for value, (file_name, func_name) in _TRIGGER_LEDGER.items():
        path = TESTS_DIR / file_name
        assert path.exists(), f"{value} 的台账引用了不存在的文件 {file_name}"
        assert f"def {func_name}(" in path.read_text(encoding="utf-8"), (
            f"{value} 的触发用例 {file_name}::{func_name} 不见了——"
            "请补回触发用例或更新台账"
        )
