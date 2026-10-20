"""意图级前门工具（default / full）的行为守卫。

这些工具只做「解析目标 → 门禁 → 调用既有能力」，所以测试也按这三段钉：

- 解析：名称/appid/动作 ID 的精确优先与多候选不自动选择；
- 门禁：默认预览不产生副作用，`execute=true` 才执行，破坏性目标另需
  `allowDestructive=true`；
- 既有能力：后端参数映射、Steam/启动器调用、winget 路由都走原 manager，
  不复制实现。

所有外部进程/后端都在这里打成 fake：真机启动 GUI/游戏需要用户在场，
不属于单测范围（真机只读检查见 e2e 与手工记录）。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402
import tool_specs  # noqa: E402
from errors import ErrorType, ToolError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def call(name: str, arguments: dict) -> dict:
    return json.loads(run(server.dispatch_tool(name, arguments)))


def payload_of(exc_info) -> dict:
    return json.loads(str(exc_info.value))


@pytest.fixture
def default_mode(monkeypatch):
    monkeypatch.setattr(server, "EXPOSURE_MODE", "default")


@pytest.fixture
def full_mode(monkeypatch):
    monkeypatch.setattr(server, "EXPOSURE_MODE", "full")


class _FakeBackend:
    def __init__(self, result: dict | None = None, status: str = "running"):
        self.result = result or {"success": True}
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


# ---- 文件搜索 / 元信息（Everything 后端的参数映射）----


def test_search_files_maps_friendly_args_to_backend(default_mode, fake_backend):
    fake_backend.result = {"success": True, "count": 1, "results": [{"name": "a.txt"}]}

    payload = call(
        "search_files",
        {
            "keyword": "a",
            "limit": 999,
            "match_path": True,
            "match_case": True,
            "regex": True,
        },
    )

    assert payload["success"] is True
    assert payload["keyword"] == "a"
    assert payload["count"] == 1
    assert fake_backend.calls == [
        (
            "everything-search-mcp",
            "search_files",
            {
                "query": "a",
                "maxResults": 200,
                "matchPath": True,
                "matchCase": True,
                "regex": True,
            },
        )
    ]


def test_get_file_info_routes_to_everything(full_mode, fake_backend):
    fake_backend.result = {"success": True, "info": {"name": "a.txt", "isFile": True}}

    payload = call("get_file_info", {"path": r"C:\tmp\a.txt"})

    assert payload["info"]["name"] == "a.txt"
    assert fake_backend.calls == [
        ("everything-search-mcp", "get_file_info", {"path": r"C:\tmp\a.txt"})
    ]


# ---- open_path：默认预览、可执行文件要授权、reveal 只定位 ----


def test_open_path_preview_never_executes(default_mode, tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("x", encoding="utf-8")
    opened: list[tuple[Path, bool]] = []

    async def fake_open(path, reveal=False):
        opened.append((path, reveal))
        return {"opened": str(path)}

    monkeypatch.setattr(tool_specs.file_ops, "open_target", fake_open)

    payload = call("open_path", {"path": str(target)})

    assert payload["exists"] is True
    assert payload["executed"] is False
    assert opened == []


def test_open_path_executable_needs_allow_destructive(
    default_mode, tmp_path, monkeypatch
):
    target = tmp_path / "setup.exe"
    target.write_bytes(b"")
    opened: list[tuple[Path, bool]] = []

    async def fake_open(path, reveal=False):
        opened.append((path, reveal))
        return {"opened": str(path)}

    monkeypatch.setattr(tool_specs.file_ops, "open_target", fake_open)

    with pytest.raises(ToolError) as exc:
        call("open_path", {"path": str(target), "execute": True})
    assert payload_of(exc)["errorType"] == ErrorType.DESTRUCTIVE_BLOCKED
    assert opened == []

    payload = call(
        "open_path",
        {"path": str(target), "execute": True, "allowDestructive": True},
    )
    assert payload["executed"] is True
    assert opened == [(target, False)]


def test_open_path_reveal_is_not_execution(default_mode, tmp_path, monkeypatch):
    target = tmp_path / "setup.exe"
    target.write_bytes(b"")
    opened: list[tuple[Path, bool]] = []

    async def fake_open(path, reveal=False):
        opened.append((path, reveal))
        return {"opened": str(path)}

    monkeypatch.setattr(tool_specs.file_ops, "open_target", fake_open)

    payload = call("open_path", {"path": str(target), "execute": True, "reveal": True})

    assert payload["executed"] is True
    assert opened == [(target, True)], "reveal 不应要求 allowDestructive"


def test_open_path_missing_file_fails_only_on_execute(default_mode):
    payload = call("open_path", {"path": r"C:\definitely-missing\a.txt"})
    assert payload["exists"] is False
    assert payload["executed"] is False
    assert "未执行" in payload["note"]

    with pytest.raises(ToolError) as exc:
        call("open_path", {"path": r"C:\definitely-missing\a.txt", "execute": True})
    assert payload_of(exc)["errorType"] == ErrorType.NOT_FOUND


# ---- open_app：多候选不自动选；单候选默认预览 ----


def _patch_app_candidates(monkeypatch, candidates, destructive=False):
    launched: list[tuple[dict, str, str, object]] = []
    monkeypatch.setattr(tool_specs, "_app_candidates", lambda ctx, name: candidates)
    monkeypatch.setattr(
        server.registry,
        "get_tool",
        lambda tool_id: {
            "id": tool_id,
            "name": "Demo",
            "launch": {"type": "executable", "command": "demo.exe"},
            "destructive": destructive,
        },
    )

    async def fake_launch(launch, tool_id, tool_name="", wait=None):
        launched.append((launch, tool_id, tool_name, wait))
        return {"success": True, "pid": 1}

    monkeypatch.setattr(tool_specs, "launch_app", fake_launch)
    return launched


def test_open_app_multiple_candidates_never_auto_selects(default_mode, monkeypatch):
    candidates = [
        {"source": "app", "toolId": "demo1", "name": "Demo 1"},
        {"source": "app", "toolId": "demo2", "name": "Demo 2"},
    ]
    launched = _patch_app_candidates(monkeypatch, candidates)

    preview = call("open_app", {"name": "demo"})
    assert preview["resolved"] is False
    assert preview["count"] == 2
    assert preview["executed"] is False
    assert launched == []

    with pytest.raises(ToolError) as exc:
        call("open_app", {"name": "demo", "execute": True})
    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.INVALID_PARAMS
    assert [c["toolId"] for c in payload["candidates"]] == ["demo1", "demo2"]
    assert launched == [], "多候选时绝不能先启动再报错"


def test_open_app_single_candidate_previews_then_executes(default_mode, monkeypatch):
    candidate = {"source": "app", "toolId": "demo", "name": "Demo"}
    launched = _patch_app_candidates(monkeypatch, [candidate])

    preview = call("open_app", {"name": "demo"})
    assert preview["resolved"] is True
    assert preview["executed"] is False
    assert launched == []

    done = call("open_app", {"name": "demo", "execute": True, "wait": 0})
    assert done["executed"] is True
    assert launched == [
        ({"type": "executable", "command": "demo.exe"}, "demo", "Demo", 0)
    ]


# ---- Steam：搜索与启动 ----


STEAM_ACTIONS = [
    {
        "id": "app:10",
        "appid": "10",
        "name": "Alpha",
        "installdir": "Alpha",
        "library": "S:\\SteamLibrary",
        "sizeOnDisk": 100,
        "lastPlayed": 123,
    },
    {
        "id": "app:20",
        "appid": "20",
        "name": "Beta",
        "installdir": "Beta",
        "library": "D:\\Games",
        "sizeOnDisk": 200,
        "lastPlayed": 456,
    },
]


def _patch_launcher(monkeypatch, actions_by_tool):
    calls: list[dict] = []

    def fake_list(tool_id, keyword=None, limit=None, refresh=False):
        calls.append({"tool_id": tool_id, "keyword": keyword, "limit": limit})
        rows = list(actions_by_tool.get(tool_id, []))
        if keyword:
            needle = keyword.lower()
            rows = [row for row in rows if needle in str(row.get("name") or "").lower()]
        return rows[:limit] if limit else rows

    monkeypatch.setattr(server.launcher_manager, "list_actions", fake_list)
    return calls


def test_search_games_returns_rows(default_mode, monkeypatch):
    _patch_launcher(monkeypatch, {"steam": STEAM_ACTIONS})

    payload = call("search_games", {"keyword": "alpha"})

    assert payload["count"] == 1
    assert payload["games"] == [
        {
            "appid": "10",
            "actionId": "app:10",
            "name": "Alpha",
            "installedDir": "Alpha",
            "library": "S:\\SteamLibrary",
            "sizeOnDisk": 100,
            "lastPlayed": 123,
        }
    ]


def test_open_game_preview_does_not_run(default_mode, monkeypatch):
    _patch_launcher(monkeypatch, {"steam": STEAM_ACTIONS})

    payload = call("open_game", {"game": "10"})

    assert payload["resolved"] is True
    assert payload["game"]["appid"] == "10"
    assert payload["executed"] is False
    assert "execute=true" in payload["note"]


def test_open_game_multiple_matches_need_a_precise_name(default_mode, monkeypatch):
    _patch_launcher(monkeypatch, {"steam": STEAM_ACTIONS})

    single = call("open_game", {"game": "alph"})
    assert single["resolved"] is True

    renamed = [
        {**STEAM_ACTIONS[0], "name": "Game Alpha"},
        {**STEAM_ACTIONS[1], "name": "Game Beta"},
    ]
    _patch_launcher(monkeypatch, {"steam": renamed})
    multi = call("open_game", {"game": "game"})
    assert multi["resolved"] is False
    assert multi["count"] == 2

    with pytest.raises(ToolError) as exc:
        call("open_game", {"game": "game", "execute": True})
    assert payload_of(exc)["errorType"] == ErrorType.INVALID_PARAMS


def test_open_game_execute_requires_confirmation_then_runs(default_mode, monkeypatch):
    _patch_launcher(monkeypatch, {"steam": STEAM_ACTIONS})
    ran: list[dict] = []

    async def fake_run(tool_id, action_id=None, action_name=None, param=None, wait=None):
        ran.append(
            {
                "tool_id": tool_id,
                "action_id": action_id,
                "action_name": action_name,
                "wait": wait,
            }
        )
        return {"success": True, "actionId": action_id}

    monkeypatch.setattr(server.launcher_manager, "run_action", fake_run)

    with pytest.raises(ToolError) as exc:
        call("open_game", {"game": "10", "execute": True})
    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.DESTRUCTIVE_BLOCKED
    assert ran == []

    done = call("open_game", {"game": "10", "execute": True, "allowDestructive": True})
    assert done["executed"] is True
    assert ran == [
        {"tool_id": "steam", "action_id": "app:10", "action_name": None, "wait": None}
    ]


# ---- Quicker / uTools：搜索与执行 ----


ACTION_ROWS = {
    "quicker": [
        {
            "id": "act-1",
            "name": "Open Browser",
            "description": "打开浏览器",
            "type": "action",
            "keywords": ["browser"],
        }
    ],
    "utools": [
        {
            "id": "plugin-1",
            "name": "Translate",
            "description": "翻译",
            "type": "plugin",
            "keywords": ["翻译"],
        }
    ],
}


def test_search_actions_searches_all_runners(default_mode, monkeypatch):
    _patch_launcher(monkeypatch, ACTION_ROWS)

    payload = call("search_actions", {"keyword": "browser"})

    assert payload["count"] == 1
    assert payload["searched"] == ["quicker", "utools"]
    assert payload["actions"][0]["source"] == "quicker"
    assert payload["actions"][0]["actionId"] == "act-1"


def test_run_action_preview_then_confirmed_execution(default_mode, monkeypatch):
    _patch_launcher(monkeypatch, ACTION_ROWS)
    ran: list[dict] = []

    async def fake_run(tool_id, action_id=None, action_name=None, param=None, wait=None):
        ran.append(
            {
                "tool_id": tool_id,
                "action_id": action_id,
                "action_name": action_name,
                "param": param,
                "wait": wait,
            }
        )
        return {"success": True, "actionId": action_id}

    monkeypatch.setattr(server.launcher_manager, "run_action", fake_run)

    preview = call("run_action", {"action": "Open Browser"})
    assert preview["resolved"] is True
    assert preview["target"]["toolId"] == "quicker"
    assert ran == []

    with pytest.raises(ToolError) as exc:
        call("run_action", {"action": "Open Browser", "execute": True})
    assert payload_of(exc)["errorType"] == ErrorType.DESTRUCTIVE_BLOCKED
    assert ran == []

    done = call(
        "run_action",
        {
            "action": "Open Browser",
            "execute": True,
            "allowDestructive": True,
            "param": "https://example.com",
        },
    )
    assert done["executed"] is True
    assert ran == [
        {
            "tool_id": "quicker",
            "action_id": "act-1",
            "action_name": "Open Browser",
            "param": "https://example.com",
            "wait": None,
        }
    ]


# ---- winget：搜索与安装 ----


def test_search_winget_packages_maps_args(full_mode, fake_backend):
    fake_backend.result = {"success": True, "count": 0, "results": []}

    payload = call(
        "search_winget_packages", {"keyword": "7zip", "upgradeable": True}
    )

    assert payload["upgradeable"] is True
    assert fake_backend.calls == [
        (
            "winget-mcp",
            "find-winget-packages",
            {"upgradeable": True, "query": "7zip"},
        )
    ]


def test_install_winget_package_previews_without_backend(full_mode, fake_backend):
    payload = call(
        "install_winget_package",
        {"identifier": "7zip.7zip", "upgrade_only": True},
    )

    assert payload["executed"] is False
    assert payload["arguments"] == {
        "identifier": "7zip.7zip",
        "upgradeOnly": True,
    }
    assert fake_backend.calls == []
    assert fake_backend.started == []


def test_install_winget_package_requires_confirmation(full_mode, fake_backend):
    fake_backend.result = {"success": True, "exitCode": 0}

    with pytest.raises(ToolError) as exc:
        call(
            "install_winget_package",
            {"identifier": "7zip.7zip", "execute": True},
        )
    payload = payload_of(exc)
    assert payload["errorType"] == ErrorType.DESTRUCTIVE_BLOCKED
    assert fake_backend.calls == []

    done = call(
        "install_winget_package",
        {"identifier": "7zip.7zip", "execute": True, "allowDestructive": True},
    )
    assert done["executed"] is True
    assert fake_backend.calls == [
        (
            "winget-mcp",
            "install-winget-package",
            {"identifier": "7zip.7zip", "upgradeOnly": False},
        )
    ]
