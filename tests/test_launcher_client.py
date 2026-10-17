"""启动器（非 MCP）适配器测试：以 Quicker 为实现

重点覆盖两类容易悄悄失效的地方：
1. 动作清单解析——Quicker 的动作列表是「JSON 数组的字符串」，扫原文永远
   匹配不到，必须先解析再取字符串值（2026-10-16 实测踩到，名称全丢）。
2. 调用判定——QuickerStarter 失败时退出码仍是 0，只能靠输出文本判断，
   且输出是 GBK（按 UTF-8 解会得乱码）。
"""

import asyncio
import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import launcher_client  # noqa: E402
from launcher_client import LauncherClient, LauncherError, LauncherManager  # noqa: E402
from schema import validate_launcher_catalog  # noqa: E402
from store import JsonStore  # noqa: E402


QUICKER_TOOL = {
    "id": "quicker",
    "name": "Quicker",
    "kind": "launcher",
    "launcher": {
        "type": "quicker",
        "command": r"C:\Program Files\Quicker\QuickerStarter.exe",
        "dataDir": str(Path(__file__).parent / "fake_quicker"),
        "timeout": 20,
    },
}

# 真实 Quicker state 的形状：动作列表变量的值是「JSON 数组的字符串」。
# 注意这里**不能再 json.dumps 一次**——真实数据里它只被转义一层，解析回来后
# 内容就是 `("描述")`；多套一层 json.dumps 会造出 `(\"描述\")`，正则就匹配不到了
# （2026-10-16 写本用例时真的多套了一层，6 个用例因此全红）。
ACTION_LIST_PAYLOAD = (
    '["'
    '[url:XAction]列表宿主动作<_global>("宿主动作自己的条目")|11111111-1111-1111-1111-111111111111","'
    '[url:XAction]文件/全文搜索<_global>("用Everything搜索文件")|92c14d1b-0479-40f0-8952-1359a8a765fb","'
    '[url:SendKeys]->右<common>("")|658d60a3-5a20-44af-a270-fa581cbf240d"'
    '"]'
)


def build_fake_quicker(tmp_path: Path, with_state_file: bool = True) -> Path:
    """搭一个最小的 Quicker 数据目录"""
    data_dir = tmp_path / "fake_quicker"
    states = data_dir / "states"
    states.mkdir(parents=True, exist_ok=True)

    # 某个动作缓存了全量动作列表（值里带转义引号，正是踩坑点）
    (states / "state_11111111-1111-1111-1111-111111111111.json").write_text(
        json.dumps(
            {
                "ActionId": "11111111-1111-1111-1111-111111111111",
                "States": {"$var:拼接列表": ACTION_LIST_PAYLOAD},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if with_state_file:
        # 一个只有 ID、没有名称的动作
        (states / "state_22222222-2222-2222-2222-222222222222.json").write_text(
            json.dumps({"ActionId": "22222222-2222-2222-2222-222222222222", "States": {}}),
            encoding="utf-8",
        )
    return data_dir


def make_client(tmp_path: Path, with_state_file: bool = True):
    data_dir = build_fake_quicker(tmp_path, with_state_file)
    tool = json.loads(json.dumps(QUICKER_TOOL))
    tool["launcher"]["dataDir"] = str(data_dir)
    return LauncherClient(tool, JsonStore(tmp_path / "data"))


# ---- 动作清单解析 ----


def test_scan_reads_names_from_json_string_values(tmp_path):
    """名称藏在「JSON 数组的字符串」里：扫原文拿不到，必须先解析"""
    client = make_client(tmp_path)
    actions, _ = client._scan_quicker()

    by_id = {a["id"]: a for a in actions}
    assert "92c14d1b-0479-40f0-8952-1359a8a765fb" in by_id
    named = by_id["92c14d1b-0479-40f0-8952-1359a8a765fb"]
    assert named["name"] == "文件/全文搜索"
    assert named["app"] == "_global"
    assert "Everything" in named["description"]


def test_scan_collects_ids_from_state_filenames(tmp_path):
    """没有名称的动作也要在清单里（只有 ID 照样能调用）"""
    client = make_client(tmp_path)
    actions, _ = client._scan_quicker()
    assert "22222222-2222-2222-2222-222222222222" in {a["id"] for a in actions}


def test_scan_marks_partial_when_names_missing(tmp_path):
    client = make_client(tmp_path)
    actions, source = client._scan_quicker()
    # 3 个有名称（列表里 3 项）+ 1 个只有 ID（2222... 的状态文件）→ partial
    assert len(actions) == 4
    assert sum(1 for a in actions if a["name"]) == 3
    assert source == "partial"


def test_scan_reports_auto_when_all_named(tmp_path):
    """没有「只有 ID」的动作时，source 才是 auto"""
    client = make_client(tmp_path, with_state_file=False)
    actions, source = client._scan_quicker()
    assert len(actions) == 3
    assert all(a["name"] for a in actions)
    assert source == "auto"


def test_sync_persists_catalog(tmp_path):
    client = make_client(tmp_path)
    result = client.sync_actions()
    assert result["success"] is True
    assert result["count"] >= 2

    persisted = JsonStore(tmp_path / "data").load_launcher_actions("quicker")
    assert len(persisted) == result["count"]


def test_sync_requires_data_dir(tmp_path):
    tool = {"id": "quicker", "kind": "launcher", "launcher": {"type": "quicker", "command": "x"}}
    with pytest.raises(LauncherError, match="dataDir"):
        LauncherClient(tool, JsonStore(tmp_path / "data")).sync_actions()


# ---- 查询 ----


def test_list_actions_filters_by_keyword(tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()

    hits = client.list_actions(keyword="搜索")
    assert [a["name"] for a in hits] == ["文件/全文搜索"]

    # 关键词也能命中描述与 ID
    assert client.list_actions(keyword="Everything")
    assert client.list_actions(keyword="92c14d1b")


def test_list_actions_respects_limit(tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()
    assert len(client.list_actions(limit=1)) == 1


def test_find_action_exact_then_fuzzy(tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()

    assert client.find_action(action_id="658D60A3-5A20-44AF-A270-FA581CBF240D")["name"] == "->右"
    assert client.find_action(action_name="文件/全文搜索")["id"] == "92c14d1b-0479-40f0-8952-1359a8a765fb"
    assert client.find_action(action_name="文件")["name"] == "文件/全文搜索"
    assert client.find_action(action_name="不存在的动作") is None


# ---- 调用 ----


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


def patch_process(monkeypatch, process: FakeProcess, capture: dict):
    async def fake_exec(*args, **kwargs):
        capture["args"] = args
        capture["kwargs"] = kwargs
        return process

    monkeypatch.setattr(launcher_client.asyncio, "create_subprocess_exec", fake_exec)


def test_run_action_builds_quicker_command(monkeypatch, tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()
    capture: dict = {}
    patch_process(monkeypatch, FakeProcess(stdout="ok".encode("gbk")), capture)

    result = asyncio.run(client.run_action(action_id="92c14d1b-0479-40f0-8952-1359a8a765fb"))

    assert result["success"] is True
    assert result["output"] == "ok"
    args = capture["args"]
    assert args[0].endswith("QuickerStarter.exe")
    assert args[1] == "-c20"  # 默认取 launcher.timeout
    assert args[2] == "runaction:92c14d1b-0479-40f0-8952-1359a8a765fb"
    # A8：绝不继承 server 的 stdin（否则子进程会阻塞在读管道上）
    assert capture["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL


def test_run_action_appends_param_with_question_mark(monkeypatch, tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()
    capture: dict = {}
    patch_process(monkeypatch, FakeProcess(stdout=b""), capture)

    asyncio.run(client.run_action(action_name="文件/全文搜索", param="hello", wait=5))

    assert capture["args"][1] == "-c5"
    assert capture["args"][2] == "runaction:92c14d1b-0479-40f0-8952-1359a8a765fb?hello"


def test_run_action_detects_failure_from_output_text(monkeypatch, tmp_path):
    """退出码是 0 但输出说失败了 —— 必须判失败（Quicker 的真实行为）"""
    client = make_client(tmp_path)
    capture: dict = {}
    message = "Error：未能成功启动动作 abc (Extern)：未能找到该动作 abc"
    patch_process(monkeypatch, FakeProcess(stdout=message.encode("gbk"), returncode=0), capture)

    result = asyncio.run(client.run_action(action_id="abc"))

    assert result["success"] is False
    assert result["exitCode"] == 0, "退出码确实仍是 0，正是要靠文本判定的原因"
    assert "未能找到该动作" in result["error"]


def test_run_action_requires_target(tmp_path):
    client = make_client(tmp_path)
    with pytest.raises(LauncherError, match="action_id"):
        asyncio.run(client.run_action())


def test_run_action_requires_command(tmp_path):
    tool = {
        "id": "quicker",
        "kind": "launcher",
        "launcher": {"type": "quicker", "dataDir": str(tmp_path)},
    }
    client = LauncherClient(tool, JsonStore(tmp_path / "data"))
    with pytest.raises(LauncherError, match="launcher.command"):
        asyncio.run(client.run_action(action_id="abc"))


# ---- manager ----


class FakeRegistry:
    def __init__(self, tool: dict | None):
        self.tool = tool

    def get_tool(self, tool_id: str):
        return self.tool if self.tool and self.tool["id"] == tool_id else None


def test_manager_rejects_non_launcher(tmp_path):
    registry = FakeRegistry({"id": "everything", "kind": "app"})
    manager = LauncherManager(registry, JsonStore(tmp_path / "data"))
    with pytest.raises(LauncherError, match="不是启动器"):
        manager.sync("everything")


def test_manager_rejects_unknown_tool(tmp_path):
    manager = LauncherManager(FakeRegistry(None), JsonStore(tmp_path / "data"))
    with pytest.raises(LauncherError, match="不存在"):
        manager.sync("nope")


# ---- 数据校验 ----


def test_validate_launcher_catalog_accepts_generated(tmp_path):
    client = make_client(tmp_path)
    client.sync_actions()
    data = json.loads(
        (tmp_path / "data" / "launcher-actions" / "quicker.json").read_text(encoding="utf-8")
    )
    assert validate_launcher_catalog(data) is None


def test_validate_launcher_catalog_rejects_broken():
    assert validate_launcher_catalog({"parent": "quicker"}) is not None  # 缺 actions
    assert validate_launcher_catalog({"actions": "not-a-list"}) is not None
    assert validate_launcher_catalog({"parent": "quicker", "actions": [{}]}) is not None  # 缺 id


# ---- 对外工具：执行动作的确认门禁 ----


def test_run_action_blocked_without_confirmation():
    """Quicker 动作可以是任何操作，条目标 destructive → 未确认不得执行"""
    import server  # noqa: PLC0415

    with pytest.raises(server.ToolError) as exc:
        asyncio.run(
            server.dispatch_tool(
                "run_launcher_action", {"tool_id": "quicker", "action_id": "whatever"}
            )
        )
    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "destructive_blocked"


def _patch_launcher_gate(monkeypatch, **entry_extra):
    """把 registry.get_tool 打成一个带指定门禁字段的启动器条目"""
    import server  # noqa: PLC0415

    monkeypatch.setattr(
        server.registry,
        "get_tool",
        lambda tool_id: {
            "id": tool_id,
            "kind": "launcher",
            "destructive": True,
            **entry_extra,
        },
    )
    return server


def test_run_action_with_destructive_actions_allows_unlisted(monkeypatch):
    """ARC-P1-05：配了 destructiveActions 后，未命中的动作直接执行"""
    server = _patch_launcher_gate(monkeypatch, destructiveActions=["*卸载*"])
    calls = []

    async def fake_run(tool_id, action_id=None, action_name=None, param=None, wait=None):
        calls.append((tool_id, action_id))
        return {"success": True, "message": "已执行"}

    monkeypatch.setattr(server.launcher_manager, "run_action", fake_run)

    text = asyncio.run(
        server.dispatch_tool(
            "run_launcher_action", {"tool_id": "quicker", "action_id": "open-browser"}
        )
    )
    payload = json.loads(text)
    assert payload["success"] is True
    assert calls == [("quicker", "open-browser")]


def test_run_action_with_destructive_actions_blocks_match(monkeypatch):
    """ARC-P1-05：配了 destructiveActions 后，命中动作仍拦；确认后才执行"""
    server = _patch_launcher_gate(monkeypatch, destructiveActions=["*卸载*"])
    calls = []

    async def fake_run(tool_id, action_id=None, action_name=None, param=None, wait=None):
        calls.append((tool_id, action_id))
        return {"success": True, "message": "已执行"}

    monkeypatch.setattr(server.launcher_manager, "run_action", fake_run)

    # 动作名命中 glob（大小写/中文通配）→ 未确认拒绝
    with pytest.raises(server.ToolError) as exc:
        asyncio.run(
            server.dispatch_tool(
                "run_launcher_action",
                {"tool_id": "quicker", "action_id": "x", "action_name": "卸载微信"},
            )
        )
    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "destructive_blocked"
    assert "卸载微信" in payload["error"], "拒绝消息应带上命中的具体动作"
    assert calls == [], "未确认的命中动作不得执行"

    # 显式确认后可执行
    text = asyncio.run(
        server.dispatch_tool(
            "run_launcher_action",
            {
                "tool_id": "quicker",
                "action_id": "x",
                "action_name": "卸载微信",
                "allowDestructive": True,
            },
        )
    )
    assert json.loads(text)["success"] is True
    assert calls == [("quicker", "x")]


def test_list_actions_needs_no_confirmation():
    """列/搜是只读的，不该被 destructive 门禁拦"""
    import server  # noqa: PLC0415

    text = asyncio.run(
        server.dispatch_tool("list_launcher_actions", {"tool_id": "quicker", "limit": 2})
    )
    payload = json.loads(text)
    assert payload["success"] is True
    assert payload["count"] == 2


# ---- A13：动作清单不再是死列表 ----
#
# 背景：`list_actions` 原先只读 launcher-actions/*.json，而写盘只发生在
# 手动 sync 时——Quicker 里新增/删除动作后，清单要等到有人想起调 sync 才变，
# 事实上的死列表。现在按 lastSynced 判断新鲜度，过期自动重扫。
#
# 下面一律用「改写 lastSynced」而不是「等待 TTL 到期」来制造陈旧：
# 依赖时钟刻度会让用例变成 flaky（Windows 上 time.time() 约 15ms 一跳，
# 2026-10-16 已因此挂过一次）。


def _set_synced(store, parent_id: str, when: datetime.datetime) -> None:
    path = store.get_launcher_actions_path(parent_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["lastSynced"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make_stale(client, store, hours: float = 2) -> None:
    _set_synced(store, "quicker", datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=hours))


def _add_action_file(data_dir: Path, action_id: str) -> None:
    (data_dir / "states" / f"state_{action_id}.json").write_text(
        json.dumps({"ActionId": action_id, "States": {}}), encoding="utf-8"
    )


NEW_ACTION_ID = "33333333-3333-3333-3333-333333333333"


def test_scan_supplements_names_from_run_log(tmp_path):
    """日志是名称的第二个来源：只补空，不覆盖 state 缓存里的完整信息"""
    data_dir = build_fake_quicker(tmp_path)
    logs = data_dir / "logs"
    logs.mkdir()
    (logs / "quicker.log").write_text(
        "2026-10-16 22:11:06,290 [action:识别二维码] INFO - "
        "执行动作：识别二维码 id=22222222-2222-2222-2222-222222222222\n"
        "2026-10-16 22:12:00,000 [action:文件搜索] INFO - "
        "执行动作：文件/全文搜索 id=92c14d1b-0479-40f0-8952-1359a8a765fb\n",
        encoding="utf-8",
    )
    tool = json.loads(json.dumps(QUICKER_TOOL))
    tool["launcher"]["dataDir"] = str(data_dir)
    client = LauncherClient(tool, JsonStore(tmp_path / "data"))

    by_id = {a["id"]: a for a in client._scan_quicker()[0]}
    # 缺名称的被补上了
    assert by_id["22222222-2222-2222-2222-222222222222"]["name"] == "识别二维码"
    # 已有完整信息的未被覆盖（description/type 仍在）
    rich = by_id["92c14d1b-0479-40f0-8952-1359a8a765fb"]
    assert rich["name"] == "文件/全文搜索"
    assert rich["app"] == "_global"


def test_list_actions_uses_cache_when_fresh(tmp_path):
    """缓存新鲜 → 直接读盘，不重新扫描（省一次全量扫盘）"""
    client = make_client(tmp_path)
    store = client.store
    client.sync_actions()
    _add_action_file(Path(client.data_dir), NEW_ACTION_ID)
    _set_synced(store, "quicker", datetime.datetime.now(datetime.timezone.utc))

    ids = {a["id"] for a in client.list_actions()}
    assert NEW_ACTION_ID not in ids, "缓存新鲜时不该看到扫描后才有的新动作"


def test_list_actions_refreshes_when_stale(tmp_path):
    """缓存过期 → 自动重扫，新动作出现在结果里"""
    client = make_client(tmp_path)
    store = client.store
    client.sync_actions()
    _add_action_file(Path(client.data_dir), NEW_ACTION_ID)
    _make_stale(client, store)

    ids = {a["id"] for a in client.list_actions()}
    assert NEW_ACTION_ID in ids, "缓存陈旧后应自动重扫，拿到新动作"


def test_list_actions_refresh_flag_forces_rescan(tmp_path):
    """refresh=true 即便缓存新鲜也重扫"""
    client = make_client(tmp_path)
    store = client.store
    client.sync_actions()
    _add_action_file(Path(client.data_dir), NEW_ACTION_ID)
    _set_synced(store, "quicker", datetime.datetime.now(datetime.timezone.utc))

    ids = {a["id"] for a in client.list_actions(refresh=True)}
    assert NEW_ACTION_ID in ids


def test_find_action_rescans_for_newly_added_action(tmp_path):
    """刚在 Quicker 里新建的动作，首次调用就该找得到（不该要求先手动 sync）"""
    client = make_client(tmp_path)
    store = client.store
    client.sync_actions()
    _add_action_file(Path(client.data_dir), NEW_ACTION_ID)
    _make_stale(client, store)

    found = client.find_action(action_id=NEW_ACTION_ID)
    assert found is not None and found["id"] == NEW_ACTION_ID


def test_refresh_failure_keeps_serving_old_cache(tmp_path):
    """刷新失败（启动器数据目录没了）不能让「列出动作」整个炸掉"""
    client = make_client(tmp_path)
    store = client.store
    client.sync_actions()
    before = client.list_actions()
    _make_stale(client, store)
    # 把数据目录整个移走，模拟 Quicker 卸载 / 配置改错
    import shutil  # noqa: PLC0415

    shutil.rmtree(client.data_dir)

    after = client.list_actions()
    assert after == before, "刷新失败应静默沿用旧缓存，而不是抛异常或清空"


def test_list_launcher_actions_reports_freshness():
    """对外工具要暴露 lastSynced / source，否则调用方不知道清单有多旧"""
    import server  # noqa: PLC0415

    payload = json.loads(
        asyncio.run(
            server.dispatch_tool("list_launcher_actions", {"tool_id": "quicker", "limit": 1})
        )
    )
    assert "lastSynced" in payload
    assert "source" in payload
    assert payload["count"] == 1
