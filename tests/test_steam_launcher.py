"""Steam 启动器适配器（`launcher_client` 的第三个实现）的守卫。

钉三件事，都是这个适配器独有的：

1. **清单来源是 acf、不是 vdf**。动作只能由盘上真实存在的 `appmanifest_*.acf`
   生成；失踪库的 appid 一个都不能出现。这条一破，用户就会看到点不动的假游戏。
2. **调用形态是 `steam.exe -applaunch <appid>`**，执行一律经
   `app_launcher.launch_app`（`stdin=DEVNULL`、进程保活、观察窗口都在那边）。
   这里用 patch 掉 `launch_app` 的方式断言参数，绝不真起 Steam。
3. **退出码不代表游戏启动了**。Steam 是单实例客户端，`-applaunch` 投递完就退，
   所以载荷必须带 `deliveryOnly`；`param` 没有通道，必须回显成 `ignoredParam`
   而不是悄悄吞掉。

另外把「schema 的 `launcher.type` enum 与 `SUPPORTED_ADAPTERS` 必须逐字一致」
钉成一条守卫：清单里合法、跑起来报不支持，是最容易被漏掉的一类分家。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import launcher_client
import steam_library
from launcher_client import LauncherClient, LauncherError
from schema import TOOL_ENTRY_SCHEMA
from store import JsonStore

# 与 test_steam_library 共用合成树生成器（同一份 VDF 形态，两处各写一遍迟早分家）
from test_steam_library import acf_text, vdf_text


# ---- 合成 Steam ----


def build_steam(tmp_path: Path, *, missing_library: bool = True) -> Path:
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True, exist_ok=True)
    (root / "steam.exe").write_text("fake", encoding="utf-8")
    (root / "steamapps" / "appmanifest_400040.acf").write_text(
        acf_text("400040", "ShareX"), encoding="utf-8"
    )
    second = tmp_path / "SteamLibrary"
    (second / "steamapps").mkdir(parents=True, exist_ok=True)
    (second / "steamapps" / "appmanifest_1509960.acf").write_text(
        acf_text("1509960", "PICO PARK", size="80009044"), encoding="utf-8"
    )
    libs = [
        ("0", str(root).replace("\\", "\\\\"), ["400040"]),
        ("1", str(second).replace("\\", "\\\\"), ["1509960"]),
    ]
    if missing_library:
        gone = tmp_path / "Games" / "SteamLibrary"  # 不创建：真机 E 盘就是这个形状
        libs.append(("2", str(gone).replace("\\", "\\\\"), ["730", "1245620"]))
    (root / "steamapps" / "libraryfolders.vdf").write_text(vdf_text(libs), encoding="utf-8")
    return root


def make_client(tmp_path: Path, *, missing_library: bool = True, **launcher_extra) -> LauncherClient:
    root = build_steam(tmp_path, missing_library=missing_library)
    cfg = {
        "type": "steam",
        "command": str(root / "steam.exe"),
        "dataDir": str(root),
        **launcher_extra,
    }
    tool = {
        "id": "steam",
        "name": "Steam",
        "kind": "launcher",
        "destructive": True,
        "launcher": cfg,
    }
    store = JsonStore(tmp_path / "data", tmp_path / "runtime")
    return LauncherClient(tool, store)


def record_launch_app(monkeypatch, *, exit_code: int = 0) -> dict:
    """把执行层换成记录器：本模块不关心 launch_app 内部，只关心传了什么"""
    seen: dict = {}

    async def fake(launch, tool_id, tool_name, wait):
        seen["launch"] = launch
        seen["tool_id"] = tool_id
        seen["tool_name"] = tool_name
        seen["wait"] = wait
        return {
            "success": exit_code == 0,
            "toolId": tool_id,
            "toolName": tool_name,
            "executed": True,
            "pid": 4242,
            "exitCode": exit_code,
            "running": False,
        }

    monkeypatch.setattr(launcher_client, "launch_app", fake)
    return seen


# ---- 同步 ----


def test_sync_lists_only_manifests_on_disk(tmp_path):
    client = make_client(tmp_path)
    result = client.sync_actions()
    assert result["count"] == 2
    assert [a["id"] for a in result["actions"]] == ["app:1509960", "app:400040"]
    # 失踪库的 appid 一个都不能出现在清单里
    assert all("730" not in a["id"] and "1245620" not in a["id"] for a in result["actions"])


def test_source_is_partial_only_when_a_library_is_missing(tmp_path):
    assert make_client(tmp_path).sync_actions()["source"] == "partial"
    assert make_client(tmp_path, missing_library=False).sync_actions()["source"] == "auto"


def test_action_rows_carry_launch_fields(tmp_path):
    action = next(a for a in make_client(tmp_path).sync_actions()["actions"]
                  if a["id"] == "app:1509960")
    assert action["name"] == "PICO PARK"
    assert action["type"] == "game"
    assert action["appid"] == "1509960"
    assert "76 MB" in action["description"], "体积进描述：搜索与人工核对都用得上"
    assert action["library"].endswith("SteamLibrary")


def test_search_matches_name_library_and_size(tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()
    assert [a["id"] for a in client.list_actions(keyword="pico")] == ["app:1509960"]
    assert [a["id"] for a in client.list_actions(keyword="SteamLibrary")] == ["app:1509960"]
    assert client.list_actions(keyword="不存在的游戏") == []


def test_find_action_accepts_name_then_fuzzy(tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()
    assert client.find_action(action_name="PICO PARK")["id"] == "app:1509960"
    assert client.find_action(action_name="pico")["id"] == "app:1509960"


def test_sync_works_without_data_dir_by_using_registry(tmp_path, monkeypatch):
    """条目没写 dataDir 时按 Steam 自己登记的 SteamPath 定位（真机那条路径不在 C 盘）"""
    root = build_steam(tmp_path)
    monkeypatch.setattr(steam_library, "registry_steam_values",
                        lambda: {"SteamPath": str(root).replace("\\", "/")})
    client = make_client(tmp_path)
    client.data_dir = None
    assert client.sync_actions()["count"] == 2


def test_sync_raises_when_steam_cannot_be_located(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_library, "registry_steam_values", lambda: {})
    monkeypatch.setattr(steam_library, "FALLBACK_DIRS", (str(tmp_path / "nope"),))
    client = make_client(tmp_path)
    client.data_dir = None
    with pytest.raises(LauncherError, match="定位不到 Steam 根目录"):
        client.sync_actions()


def test_steam_does_not_require_data_dir_config(tmp_path, monkeypatch):
    """`dataDir` 对 steam 是可选的：这条把「和其它适配器一样必填」的分家挡住"""
    root = build_steam(tmp_path)
    monkeypatch.setattr(steam_library, "registry_steam_values", lambda: {"SteamPath": str(root)})
    tool = {"id": "steam", "kind": "launcher", "launcher": {"type": "steam"}}
    client = LauncherClient(tool, JsonStore(tmp_path / "data", tmp_path / "runtime"))
    assert client.sync_actions()["count"] == 2


# ---- 调用 ----


def test_run_builds_applaunch_argv(monkeypatch, tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()
    seen = record_launch_app(monkeypatch)

    result = asyncio.run(client.run_action(action_id="app:400040"))

    assert seen["launch"] == {
        "type": "executable",
        "command": str(Path(client.data_dir) / "steam.exe"),
        "args": ["-applaunch", "400040"],
    }
    assert seen["tool_id"] == "steam"
    assert seen["tool_name"] == "ShareX", "保活注册表用启动器 ID，显示名取游戏名"
    assert result["success"] is True
    assert result["actionId"] == "app:400040"
    assert result["command"].endswith("steam.exe -applaunch 400040")
    assert result["deliveryOnly"] is True, "退出码只代表指令已投递，不能报成「游戏已启动」"


def test_run_accepts_bare_appid_and_unsynced_target(monkeypatch, tmp_path):
    """没同步过也能调：`-applaunch` 收的就是 appid，清单只是便利层"""
    client = make_client(tmp_path)
    seen = record_launch_app(monkeypatch)
    result = asyncio.run(client.run_action(action_id="1509960"))
    assert seen["launch"]["args"] == ["-applaunch", "1509960"]
    assert result["actionName"] == ""


def test_run_rejects_non_numeric_target_without_spawning(monkeypatch, tmp_path):
    """ID 形态不对就必须在 spawn 之前拦住——否则是把脏字符串交给 steam.exe"""

    async def boom(*_args, **_kwargs):
        raise AssertionError("launch_app 被调用了：ID 校验必须在执行之前")

    monkeypatch.setattr(launcher_client, "launch_app", boom)
    client = make_client(tmp_path)
    with pytest.raises(LauncherError, match="app:<appid>"):
        asyncio.run(client.run_action(action_id="app:not-a-number"))


def test_run_resolves_command_from_registry_when_absent(monkeypatch, tmp_path):
    root = build_steam(tmp_path)
    monkeypatch.setattr(steam_library, "registry_steam_values", lambda: {"SteamPath": str(root)})
    tool = {"id": "steam", "kind": "launcher", "launcher": {"type": "steam", "dataDir": str(root)}}
    client = LauncherClient(tool, JsonStore(tmp_path / "data", tmp_path / "runtime"))
    client.command = ""
    seen = record_launch_app(monkeypatch)
    asyncio.run(client.run_action(action_id="app:400040"))
    assert seen["launch"]["command"] == str(root / "steam.exe")


def test_run_fails_when_command_and_root_both_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_library, "registry_steam_values", lambda: {})
    monkeypatch.setattr(steam_library, "FALLBACK_DIRS", (str(tmp_path / "nope"),))
    client = make_client(tmp_path)
    client.command = ""
    client.data_dir = None
    with pytest.raises(LauncherError, match="定位不到 Steam 根目录"):
        asyncio.run(client.run_action(action_id="app:400040"))


def test_run_echoes_ignored_param(monkeypatch, tmp_path):
    """Steam 没有动作传参通道：传了要看得见，不能悄悄丢了"""
    client = make_client(tmp_path)
    client.sync_actions()
    record_launch_app(monkeypatch)
    result = asyncio.run(client.run_action(action_id="app:400040", param="-noverify"))
    assert result["ignoredParam"] == "-noverify"
    assert "param 已被忽略" in result["note"]


def test_run_passes_wait_through_to_launch_app(monkeypatch, tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()
    seen = record_launch_app(monkeypatch)
    asyncio.run(client.run_action(action_id="app:400040", wait=3))
    assert seen["wait"] == 3


def test_run_reports_failure_payload_not_exception(monkeypatch, tmp_path):
    """退出码非 0 走返回体（与 launch_tool / 其它启动器分支同构），不抛裸异常"""
    client = make_client(tmp_path)
    client.sync_actions()
    record_launch_app(monkeypatch, exit_code=1)
    result = asyncio.run(client.run_action(action_id="app:400040"))
    assert result["success"] is False
    assert result["exitCode"] == 1


# ---- 适配器接线的一致性 ----


def test_supported_adapters_match_schema_enum():
    """schema 认但代码不支持 = 清单里合法、跑起来报错，必须逐字一致"""
    enum = TOOL_ENTRY_SCHEMA["properties"]["launcher"]["properties"]["type"]["enum"]
    assert enum == list(launcher_client.SUPPORTED_ADAPTERS)


def test_registered_steam_entry_is_syncable():
    """仓库里的 `data/tools/steam.json` 必须是可同步的形状：适配器类型受支持"""
    entry = json.loads(
        (Path(__file__).parent.parent / "data" / "tools" / "steam.json").read_text(
            encoding="utf-8"
        )
    )
    assert entry["kind"] == "launcher"
    assert entry["launcher"]["type"] in launcher_client.SUPPORTED_ADAPTERS
    # destructive=true：Steam 动作没有 dry-run，execute=true 就是一次真启动
    assert entry["destructive"] is True


# ---- 门禁：执行启动需要显式授权 ----


def test_run_steam_game_blocked_without_allow_destructive(monkeypatch):
    """真条目 + 真策略：不给 allowDestructive 就走不到执行"""
    import server  # noqa: PLC0415

    monkeypatch.setattr(
        server.launcher_manager,
        "list_actions",
        lambda tool_id: [{"id": "app:400040", "name": "Steam 游戏", "type": "game"}],
    )

    with pytest.raises(server.ToolError) as exc:
        asyncio.run(
            server.dispatch_tool(
                "call_capability",
                {"tool_id": "steam", "capability_id": "app:400040", "execute": True},
            )
        )
    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "destructive_blocked"
    assert "allowDestructive" in payload["error"]
