"""暴露模式（lazy / default / full）与 initialize.instructions 的守卫。

钉住五件事：
1. 模式解析优先级（环境变量 > 文件 > lazy）与坏配置快速失败；
2. 三种模式各自下发的工具名集合与数量；
3. instructions 只列当前模式可见的工具，且覆盖声明表全部条目；
4. dispatch 兜底：未暴露的工具在协议面等同 `unknown_tool`（不泄露存在性）；
5. 意图工具的 `_usageTarget` 只进统计、不进对外载荷。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import exposure  # noqa: E402
import server  # noqa: E402
import usage  # noqa: E402
from errors import ErrorType, ToolError  # noqa: E402
from tool_specs import SERVER_TOOL_SPECS, build_mcp_tools  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def payload_of(exc_info) -> dict:
    return json.loads(str(exc_info.value))


# ---- 1. 模式解析 ----


def test_env_var_names_have_no_awam_prefix():
    """对外配置名就是 EXPOSURE_MODE / EXPOSURE_FILE，不带项目私有前缀"""
    assert exposure.MODE_ENV_VAR == "EXPOSURE_MODE"
    assert exposure.EXPOSURE_FILE_ENV_VAR == "EXPOSURE_FILE"


def test_missing_file_defaults_to_lazy(tmp_path):
    assert exposure.load_exposure_config(tmp_path / "absent.json")["mode"] == "lazy"


def test_file_mode_is_read(tmp_path):
    path = tmp_path / "exposure.json"
    path.write_text('{"mode": "default"}', encoding="utf-8")

    assert exposure.resolve_exposure_mode(path) == "default"


def test_env_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "exposure.json"
    path.write_text('{"mode": "full"}', encoding="utf-8")
    monkeypatch.setenv(exposure.MODE_ENV_VAR, "default")

    assert exposure.resolve_exposure_mode(path) == "default"


def test_env_mode_is_normalized(tmp_path, monkeypatch):
    monkeypatch.setenv(exposure.MODE_ENV_VAR, "  FULL  ")

    assert exposure.resolve_exposure_mode(tmp_path / "absent.json") == "full"


@pytest.mark.parametrize("value", ["turbo", "", "lazyx", 1])
def test_invalid_mode_raises(value):
    with pytest.raises(exposure.ExposureError):
        exposure.normalize_mode(value)


def test_invalid_file_mode_raises(tmp_path):
    path = tmp_path / "exposure.json"
    path.write_text('{"mode": "turbo"}', encoding="utf-8")

    with pytest.raises(exposure.ExposureError, match="非法"):
        exposure.load_exposure_config(path)


def test_broken_json_raises(tmp_path):
    path = tmp_path / "exposure.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(exposure.ExposureError, match="JSON"):
        exposure.load_exposure_config(path)


def test_non_object_file_raises(tmp_path):
    path = tmp_path / "exposure.json"
    path.write_text('["default"]', encoding="utf-8")

    with pytest.raises(exposure.ExposureError, match="JSON 对象"):
        exposure.load_exposure_config(path)


# ---- 2. 三模式切片 ----


def test_lazy_keeps_the_legacy_surface():
    """lazy 是兼容基线：与改造前的 22 个管理工具逐名一致。"""
    from conftest import EXPECTED_TOOL_NAMES_LAZY

    assert {t.name for t in build_mcp_tools("lazy")} == EXPECTED_TOOL_NAMES_LAZY
    assert len(build_mcp_tools("lazy")) == 22


def test_default_adds_exactly_the_seven_intent_tools():
    from conftest import EXPECTED_TOOL_NAMES_DEFAULT

    names = [t.name for t in build_mcp_tools("default")]
    assert set(names) == EXPECTED_TOOL_NAMES_DEFAULT
    assert len(names) == 29
    assert names[-7:] == [
        "search_files",
        "open_path",
        "open_app",
        "search_games",
        "open_game",
        "search_actions",
        "run_action",
    ]


def test_full_adds_the_five_low_frequency_tools():
    from conftest import EXPECTED_TOOL_NAMES_FULL

    names = [t.name for t in build_mcp_tools("full")]
    assert set(names) == EXPECTED_TOOL_NAMES_FULL
    assert len(names) == 34
    assert names[-5:] == [
        "get_file_info",
        "get_everything_status",
        "search_winget_packages",
        "install_winget_package",
        "check_package_updates",
    ]


def test_unknown_mode_cannot_build_tools():
    with pytest.raises(exposure.ExposureError):
        build_mcp_tools("turbo")


# ---- 3. instructions ----


@pytest.mark.parametrize("mode", exposure.MODES)
def test_instructions_only_list_visible_tools(mode):
    text = exposure.build_instructions(mode)
    visible = exposure.exposed_tool_names(mode)

    assert f"当前暴露模式：{mode}" in text
    for spec in SERVER_TOOL_SPECS:
        if spec.name in visible:
            assert f"- {spec.name}：" in text, f"{mode}: 可见工具 {spec.name} 没进 instructions"
        else:
            assert spec.name not in text, f"{mode}: 不可见工具 {spec.name} 泄漏进 instructions"
    assert "execute=true" in text
    assert "allowDestructive=true" in text


def test_instruction_names_cover_every_spec():
    exposure.validate_instruction_names()


def test_server_instructions_match_its_mode():
    assert server.app.instructions == exposure.build_instructions(server.EXPOSURE_MODE)


# ---- 4. dispatch 兜底 ----


def test_unexposed_tool_behaves_like_unknown(monkeypatch):
    """lazy 下调用 default 工具：与未知工具同一条错误，不泄露「存在但没开」"""
    monkeypatch.setattr(server, "EXPOSURE_MODE", "lazy")

    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("search_files", {"keyword": "x"}))

    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.UNKNOWN_TOOL
    assert payload["error"] == "未知工具: search_files"
    assert payload["toolName"] == "search_files"


def test_default_exposes_front_door_but_not_full_only_tools(monkeypatch):
    monkeypatch.setattr(server, "EXPOSURE_MODE", "default")

    # 已暴露：归一化错误（invalid_params）说明请求进了本服务的门
    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("search_files", {}))
    assert payload_of(exc)["errorType"] == ErrorType.INVALID_PARAMS

    # 未暴露：full 专属工具仍是 unknown_tool
    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("get_file_info", {"path": "x"}))
    assert payload_of(exc)["errorType"] == ErrorType.UNKNOWN_TOOL


def test_full_exposes_low_frequency_tools(monkeypatch):
    monkeypatch.setattr(server, "EXPOSURE_MODE", "full")

    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("get_file_info", {}))
    assert payload_of(exc)["errorType"] == ErrorType.INVALID_PARAMS


# ---- 5. 使用统计的真实目标 ----


class _FakeBackend:
    def __init__(self, result: dict | None = None, status: str = "running"):
        self.result = result or {"success": True, "count": 0, "results": []}
        self.status = status
        self.started: list[str] = []
        self.calls: list[tuple[str, str, dict]] = []

    def get_backend_status(self, tool_id):
        return {"success": True, "status": {"status": self.status}}

    async def start_backend(self, tool_id):
        self.started.append(tool_id)
        self.status = "running"
        return {"success": True}

    async def call_backend_tool(self, tool_id, tool_name, arguments):
        self.calls.append((tool_id, tool_name, arguments))
        return {"success": True, "result": json.dumps(self.result)}


@pytest.fixture
def fake_backend(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(server.backend_manager, "get_backend_status", backend.get_backend_status)
    monkeypatch.setattr(server.backend_manager, "start_backend", backend.start_backend)
    monkeypatch.setattr(server.backend_manager, "call_backend_tool", backend.call_backend_tool)
    return backend


def test_front_door_records_resolved_target_and_strips_marker(
    monkeypatch, tmp_path, fake_backend
):
    monkeypatch.setattr(server, "EXPOSURE_MODE", "default")
    monkeypatch.setattr(usage, "USAGE_LOG", tmp_path / "usage.log")

    payload = json.loads(run(server.dispatch_tool("search_files", {"keyword": "文件"})))

    assert payload["success"] is True
    assert "_usageTarget" not in payload, "内部统计标记泄漏到对外载荷"
    rows = usage.read_rows()
    assert [row["target"] for row in rows] == ["everything-search-mcp"]
    assert fake_backend.calls == [
        ("everything-search-mcp", "search_files", {"query": "文件"})
    ]


def test_front_door_auto_starts_backend(monkeypatch, fake_backend):
    monkeypatch.setattr(server, "EXPOSURE_MODE", "default")
    fake_backend.status = "stopped"

    run(server.dispatch_tool("search_files", {"keyword": "x"}))

    assert fake_backend.started == ["everything-search-mcp"]


def test_status_tool_does_not_auto_start(monkeypatch, fake_backend):
    monkeypatch.setattr(server, "EXPOSURE_MODE", "full")
    fake_backend.status = "stopped"

    payload = json.loads(run(server.dispatch_tool("get_everything_status", {})))

    assert payload["success"] is True
    assert fake_backend.started == [], "查状态本身不能改变后端状态"
    assert "未运行" in payload["note"]
