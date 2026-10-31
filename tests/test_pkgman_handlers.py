"""包管理四个对外工具的 handler 守卫（P2-07，`src/tool_specs.py` 的 `_handle_*pkg*`）。

钉的是三件事，都不是适配层能自己证明的：

1. **默认零 spawn**：`list/search` 不跑任何命令，`launch_package` / `install_package`
   不给 `execute` 只回预览——把 `launch_app` / `run_argv` 打成「一调就炸」来证明，
   而不是断言返回里没有某个字段；
2. **门禁位置**：`install_package` 的 `execute=true` 必须过 `decide_destructive`
   （默认要 `allowDestructive`，策略 `deny` 时客户端自解无效），且拒绝发生在
   `run_argv` 之前；`launch_package` 在放行名单内，不要求这个参数；
3. **错误分类不靠猜文案**：`install_required`（没有这个安装器 / 没有 scoop 入口）、
   `not_found`（包没装）、`invalid_params`（未接线的 manager、空关键词、非法包名）、
   `runtime_error`（挑不出入口、命令非零退出）各归各位。

直接调 handler 而不走 `server.dispatch_tool`：dispatch 用的是仓库真实 `data/tools/`
与真实运行目录，而这里的「scoop 根」是 `tmp_path` 里的合成树，中间那层只会把
monkeypatch 的靶子遮住（协议层与 dispatch 层的覆盖在 e2e 场景里）。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pkgman  # noqa: E402
import policy as policy_mod  # noqa: E402
import tool_specs  # noqa: E402
from errors import ToolError  # noqa: E402
from registry import ToolRegistry  # noqa: E402
from store import JsonStore  # noqa: E402
from test_pkgman import make_app, make_shim, write_json  # noqa: E402
from tool_specs import ToolContext  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _ctx(tmp_path: Path) -> ToolContext:
    store = JsonStore(tmp_path)
    return ToolContext(
        registry=ToolRegistry(store),
        backend_manager=None,
        launcher_manager=None,
        store=store,
        log=lambda _msg: None,
    )


def _err(excinfo) -> dict:
    return json.loads(str(excinfo.value))


def _spawn_forbidden(monkeypatch, module, name):
    """把执行入口打成「一调就炸」：默认路径一旦 spawn 就是红，而不是靠读返回猜"""

    async def _boom(*_args, **_kwargs):
        raise AssertionError(f"{name} 被调用了：默认路径必须零 spawn")

    monkeypatch.setattr(module, name, _boom)


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch) -> Path:
    """合成一棵 scoop 树并把 `resolve_root` 指过去（handler 经模块属性取值才打得住）"""
    root = tmp_path / "scoop"
    make_app(
        root,
        "goodpkg",
        manifest={"version": "1.2.3", "bin": "good.exe", "description": "好包"},
        install={"bucket": "main", "architecture": "64bit"},
        exe_names=("good.exe",),
    )
    make_app(
        root,
        "brokenpkg",
        version="2.0.0",
        current=False,
        manifest={"version": "2.0.0", "bin": ["a.exe"], "shortcuts": [["a.exe", "界面 A"]]},
        install={"bucket": "extras"},
        exe_names=("a.exe",),
    )
    make_app(root, "nomanifest", manifest=None)
    make_shim(root, "nomanifest.exe")
    make_shim(root, "goodpkg.ps1")
    monkeypatch.setattr(
        pkgman, "resolve_root", lambda manager="scoop": (str(root), "test: 单测指定的根")
    )
    return root


# ---- 只读的两个工具 ----


def test_list_packages_is_read_only(tmp_path, fake_root, monkeypatch):
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    res = run(tool_specs._handle_list_packages({}, _ctx(tmp_path)))
    assert res["success"] is True
    assert res["root"] == str(fake_root)
    assert res["rootSource"] == "test: 单测指定的根"
    assert res["count"] == 3 == res["returned"]
    assert res["states"] == {"ok": 1, "current_broken": 1, "no_manifest": 1}
    # 断链包的 bin 在残留版本目录里、no_manifest 包靠 shim，三种都挑得出可起入口
    assert res["launchable"] == 3
    assert {p["name"] for p in res["packages"]} == {"goodpkg", "brokenpkg", "nomanifest"}


def test_list_packages_state_filter_keeps_full_count(tmp_path, fake_root):
    res = run(tool_specs._handle_list_packages({"state": pkgman.STATE_CURRENT_BROKEN}, _ctx(tmp_path)))
    assert res["count"] == 3, "count 是整份清单的，筛选条件只体现在 returned"
    assert res["returned"] == 1 and res["stateFilter"] == "current_broken"
    assert [p["name"] for p in res["packages"]] == ["brokenpkg"]


def test_list_packages_empty_apps_is_not_a_failure(tmp_path, fake_root):
    """装了 0 个包是正常结果：成功 + 说明，而不是报「读不到」"""
    import shutil

    for name in ("goodpkg", "brokenpkg", "nomanifest"):
        shutil.rmtree(fake_root / "apps" / name)
    res = run(tool_specs._handle_list_packages({}, _ctx(tmp_path)))
    assert res["success"] is True and res["count"] == 0
    assert "还没装东西" in res["note"]


def test_search_packages_reads_bucket(tmp_path, fake_root, monkeypatch):
    write_json(
        fake_root / "buckets" / "main" / "bucket" / "yt-dlp.json",
        {"version": "2025.1.1", "description": "downloader"},
    )
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    res = run(tool_specs._handle_search_packages({"keyword": "yt-dlp", "limit": 5}, _ctx(tmp_path)))
    assert res["count"] == 1 and res["scannedManifests"] == 1
    assert res["packages"][0]["bucket"] == "main"
    assert res["limit"] == 5


def test_search_packages_empty_keyword_is_invalid_params(tmp_path, fake_root):
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_search_packages({"keyword": "  "}, _ctx(tmp_path)))
    assert _err(exc)["errorType"] == "invalid_params"


def test_search_packages_no_match_explains_scope(tmp_path, fake_root):
    write_json(fake_root / "buckets" / "main" / "bucket" / "git.json", {"description": "d"})
    res = run(tool_specs._handle_search_packages({"keyword": "zzznotapackage"}, _ctx(tmp_path)))
    assert res["count"] == 0
    assert "已知 bucket 里没有" in res["note"], "要说「这个 bucket 里没有」，不能说「装不了」"


# ---- 根目录与 manager 的分类 ----


def test_unresolved_root_is_install_required(tmp_path, monkeypatch):
    """定位不到根要说「这台机器没这个安装器」，不许降级成空清单（那是「装了 0 个」）"""
    monkeypatch.setattr(
        pkgman,
        "resolve_root",
        lambda manager="scoop": (None, "环境变量与注册表里都没有 SCOOP"),
    )
    for handler, args in (
        (tool_specs._handle_list_packages, {}),
        (tool_specs._handle_search_packages, {"keyword": "git"}),
        (tool_specs._handle_launch_package, {"package": "goodpkg"}),
        (tool_specs._handle_install_package, {"package": "yt-dlp"}),
    ):
        with pytest.raises(ToolError) as exc:
            run(handler(args, _ctx(tmp_path)))
        payload = _err(exc)
        assert payload["errorType"] == "install_required", payload
        assert "环境变量与注册表里都没有 SCOOP" in payload["error"]


def test_unsupported_manager_is_invalid_params(tmp_path):
    """winget 未接线：走真实 resolve_root，报 invalid_params 而不是「定位不到根」"""
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_list_packages({"manager": "winget"}, _ctx(tmp_path)))
    payload = _err(exc)
    assert payload["errorType"] == "invalid_params"
    assert "winget" in payload["error"]


def test_launch_bad_package_name_is_invalid_params(tmp_path, fake_root):
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_launch_package({"package": "../evil"}, _ctx(tmp_path)))
    assert _err(exc)["errorType"] == "invalid_params"


# ---- launch_package ----


def test_launch_preview_never_spawns(tmp_path, fake_root, monkeypatch):
    _spawn_forbidden(monkeypatch, tool_specs, "launch_app")
    res = run(tool_specs._handle_launch_package({"package": "goodpkg"}, _ctx(tmp_path)))
    assert res["executed"] is False
    assert res["kind"] == "bin"
    assert res["path"] == str(fake_root / "apps" / "goodpkg" / "current" / "good.exe")
    assert "execute=true" in res["note"]


def test_launch_missing_package_is_not_found(tmp_path, fake_root):
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_launch_package({"package": "nope"}, _ctx(tmp_path)))
    payload = _err(exc)
    assert payload["errorType"] == "not_found"
    assert "search_packages" in payload["error"]


def test_launch_unspawnable_entry_carries_code_and_candidates(tmp_path, fake_root, monkeypatch):
    """挑不出入口时把 code / 候选一起回：调用方看到「差在哪」，不是一个光秃秃的失败"""
    _spawn_forbidden(monkeypatch, tool_specs, "launch_app")
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_launch_package(
            {"package": "goodpkg", "entry": "goodpkg.ps1"}, _ctx(tmp_path)
        ))
    payload = _err(exc)
    assert payload["errorType"] == "runtime_error"
    assert payload["code"] == pkgman.NOT_SPAWNABLE
    assert any(
        c["name"] == "goodpkg.ps1" and c["runnable"] is False for c in payload["candidates"]
    )


def test_launch_execute_needs_no_destructive_flag(tmp_path, fake_root, monkeypatch):
    """launch_package 在放行名单内：起的是用户早就装好的程序，与 launch_tool 同级"""
    seen = {}

    async def _fake_launch(launch, tool_id, tool_name, wait):
        seen.update({"launch": launch, "tool_id": tool_id, "name": tool_name, "wait": wait})
        return {"executed": True, "pid": 4321, "alive": True}

    monkeypatch.setattr(tool_specs, "launch_app", _fake_launch)
    res = run(tool_specs._handle_launch_package(
        {"package": "goodpkg", "execute": True, "wait": 1}, _ctx(tmp_path)
    ))
    assert res["success"] is True and res["pid"] == 4321
    assert seen["launch"] == {
        "type": "executable",
        "command": str(fake_root / "apps" / "goodpkg" / "current" / "good.exe"),
    }
    assert seen["tool_id"] == "scoop/goodpkg", "保活注册表的键要能区分是哪个包"
    assert seen["name"] == "goodpkg"
    assert seen["wait"] == 1


# ---- install_package ----


def test_install_preview_never_executes(tmp_path, fake_root, monkeypatch):
    make_shim(fake_root, "scoop.cmd")
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    res = run(tool_specs._handle_install_package({"package": "yt-dlp"}, _ctx(tmp_path)))
    assert res["executed"] is False
    assert res["argv"] == [str(fake_root / "shims" / "scoop.cmd"), "install", "yt-dlp"]
    assert res["alreadyInstalled"] is False
    assert "allowDestructive=true" in res["note"]


def test_install_reports_already_installed(tmp_path, fake_root):
    make_shim(fake_root, "scoop.cmd")
    res = run(tool_specs._handle_install_package({"package": "goodpkg"}, _ctx(tmp_path)))
    assert res["alreadyInstalled"] is True
    assert res["state"] == "ok" and res["version"] == "1.2.3"


def test_install_execute_blocks_without_allow(tmp_path, fake_root, monkeypatch):
    make_shim(fake_root, "scoop.cmd")
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_install_package({"package": "yt-dlp", "execute": True}, _ctx(tmp_path)))
    payload = _err(exc)
    assert payload["errorType"] == "destructive_blocked"
    assert "改动本机已装清单" in payload["error"]


def test_install_deny_mode_wins_over_client(tmp_path, fake_root, monkeypatch):
    make_shim(fake_root, "scoop.cmd")
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    monkeypatch.setattr(
        policy_mod, "_POLICY", {**policy_mod.get_policy(), "destructive": {"mode": "deny"}}
    )
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_install_package(
            {"package": "yt-dlp", "execute": True, "allowDestructive": True}, _ctx(tmp_path)
        ))
    assert _err(exc)["errorType"] == "permission_denied"


def test_install_allowed_runs_command_and_reads_back(tmp_path, fake_root, monkeypatch):
    make_shim(fake_root, "scoop.cmd")
    calls = []

    async def _fake_run(argv, timeout=None):
        calls.append((argv, timeout))
        return {
            "ok": True,
            "exitCode": 0,
            "timedOut": False,
            "stdout": "Installing 'yt-dlp'",
            "stderr": "",
            "argv": argv,
            "timeout": 120,
        }

    monkeypatch.setattr(pkgman, "run_argv", _fake_run)
    res = run(tool_specs._handle_install_package(
        {"package": "yt-dlp", "execute": True, "allowDestructive": True, "timeout": 60},
        _ctx(tmp_path),
    ))
    assert calls == [(res["argv"], 60)]
    assert res["executed"] is True and res["exitCode"] == 0
    assert res["installed"] is None
    # 退出码 0 但 apps 下读不到：明说「不要当成安装成功」，别让调用方自己脑补
    assert "不要当成安装成功" in res["note"]


def test_install_nonzero_exit_is_business_failure_not_tool_error(tmp_path, fake_root, monkeypatch):
    """命令跑完但没装上，是这次执行的结果：载荷里带退出码与输出，不抛协议层错误"""
    make_shim(fake_root, "scoop.cmd")

    async def _fake_run(argv, timeout=None):
        return {
            "ok": False,
            "exitCode": 1,
            "timedOut": False,
            "stdout": "",
            "stderr": "not found",
            "argv": argv,
            "timeout": 120,
        }

    monkeypatch.setattr(pkgman, "run_argv", _fake_run)
    res = run(tool_specs._handle_install_package(
        {"package": "no-such-pkg", "execute": True, "allowDestructive": True}, _ctx(tmp_path)
    ))
    assert res["success"] is False
    assert res["errorType"] == "runtime_error"
    assert res["exitCode"] == 1 and res["stderr"] == "not found"


def test_install_timeout_is_passed_through_unconverted(tmp_path, fake_root, monkeypatch):
    """封顶只在 `run_argv` 一处：handler 再换算一次，两处口径迟早分家"""
    make_shim(fake_root, "scoop.cmd")
    seen = {}

    async def _fake_run(argv, timeout=None):
        seen["timeout"] = timeout
        return {
            "ok": True,
            "exitCode": 0,
            "timedOut": False,
            "stdout": "",
            "stderr": "",
            "argv": argv,
            "timeout": 600,
        }

    monkeypatch.setattr(pkgman, "run_argv", _fake_run)
    run(tool_specs._handle_install_package(
        {"package": "yt-dlp", "execute": True, "allowDestructive": True, "timeout": 99999},
        _ctx(tmp_path),
    ))
    assert seen["timeout"] == 99999


def test_install_without_scoop_entry_is_install_required(tmp_path, fake_root):
    """apps 目录在、shims/scoop.cmd 不在：报 install_required，而不是「包不存在」"""
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_install_package({"package": "yt-dlp"}, _ctx(tmp_path)))
    assert _err(exc)["errorType"] == "install_required"


def test_install_gate_audit_row_labels_package_op(tmp_path, fake_root, monkeypatch):
    """审计 action 记 install_package、sub_op 记包名：记成通用工具名就查不出是谁装的哪个包"""
    log_path = tmp_path / "audit.log"
    monkeypatch.setattr(policy_mod, "AUDIT_LOG", log_path)
    make_shim(fake_root, "scoop.cmd")
    with pytest.raises(ToolError):
        run(tool_specs._handle_install_package(
            {"package": "yt-dlp", "execute": True}, _ctx(tmp_path)
        ))
    rows = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if '"destructive_gate"' in line
    ]
    assert len(rows) == 1, f"应恰好一行门禁审计，实际 {len(rows)} 行"
    assert rows[0]["action"] == "install_package"
    assert rows[0]["sub_op"] == "yt-dlp"
    assert rows[0]["kind"] == "package"
    assert rows[0]["tool"] == "scoop:yt-dlp"
    assert rows[0]["decision"] == "block"


# ---- check_package_updates：两条口径，只有一条是默认值 ----


FAKE_EXE = "C:\\Fake\\UniGetUI\\uniget.exe"


def _fake_uniget(monkeypatch, *, exe=FAKE_EXE, running=True, updates=None,
                 updates_ok=True, updates_exit=0):
    """把两次外部查询打成脚本化的应答，并记下真实收到的 argv 与 timeout。

    第一条命令必须是 `status`：它不需要 token，是「有没有活会话」的探针。探针说没有
    就不许再发第二条——第二条会把参数转给 `UniGetUI.exe` 去冷启动 GUI。
    """
    calls: list[dict] = []
    payload = updates if updates is not None else {
        "status": "success",
        "updates": [
            {"name": "7-Zip", "id": "7zip.7zip", "version": "25.01",
             "newVersion": "26.05", "source": "WinGet: winget", "manager": "winget",
             "isUpgradable": True},
        ],
    }

    async def _run(argv, timeout=None, max_output=pkgman.OUTPUT_MAX_CHARS):
        command = "status" if argv[-1] == "status" else "updates"
        calls.append({"argv": list(argv), "timeout": timeout, "maxOutput": max_output})
        if command == "status":
            stdout = json.dumps({"running": running, "version": "2026.3.2"})
            return {"ok": True, "exitCode": 0, "timedOut": False, "stdoutTruncated": False,
                    "stdout": stdout, "stderr": "", "argv": argv, "timeout": timeout}
        return {"ok": updates_ok, "exitCode": updates_exit, "timedOut": False,
                "stdoutTruncated": False,
                "stdout": json.dumps(payload), "stderr": "", "argv": argv,
                "timeout": timeout}

    monkeypatch.setattr(pkgman, "run_argv", _run)
    monkeypatch.setattr(pkgman, "locate_uniget_cli", lambda: (exe, "test: 单测指定的入口"))
    return calls


def test_check_updates_default_is_read_disk_and_never_spawns(tmp_path, fake_root, monkeypatch):
    """默认口径零 spawn：升级信号来自已装版本 vs 本地 bucket 索引，不碰任何外部进程"""
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    write_json(fake_root / "buckets" / "main" / "bucket" / "goodpkg.json", {"version": "1.3.0"})
    res = run(tool_specs._handle_check_package_updates({}, _ctx(tmp_path)))
    assert res["engine"] == "read_disk"
    assert res["scanned"] == 3
    assert [(r["name"], r["installedVersion"], r["availableVersion"]) for r in res["updates"]] == [
        ("goodpkg", "1.2.3", "1.3.0")
    ]
    assert res["count"] == 1
    assert res["uniget"]["available"] is False
    assert "execute=true" in res["uniget"]["reason"]
    assert res["cli"] is None
    assert "scoop update" in res["note"], "要说清索引新鲜度不等于上游最新发布"


def test_check_updates_default_lists_unknowable_packages(tmp_path, fake_root, monkeypatch):
    """比不了的包进 unknown 并给原因，不许被静默当成「已最新」"""
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    res = run(tool_specs._handle_check_package_updates({}, _ctx(tmp_path)))
    assert {u["name"] for u in res["unknown"]} == {"goodpkg", "brokenpkg", "nomanifest"}
    assert res["updates"] == []


@pytest.mark.parametrize("bad", ["--elevated", "not-a-manager"])
def test_check_updates_bad_manager_is_invalid_params(tmp_path, fake_root, monkeypatch, bad):
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_check_package_updates({"manager": bad}, _ctx(tmp_path)))
    assert _err(exc)["errorType"] == "invalid_params"


def test_check_updates_missing_cli_falls_back_to_read_disk(tmp_path, fake_root, monkeypatch):
    """没装 UniGetUI 不是失败：退回读盘口径，并把「为什么退回来」原样带回"""
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")  # 连 status 都不该发
    monkeypatch.setattr(
        pkgman, "locate_uniget_cli",
        lambda: (None, "没找到 uniget.exe（试过: C:\\x\\uniget.exe）"),
    )
    res = run(tool_specs._handle_check_package_updates({"execute": True}, _ctx(tmp_path)))
    assert res["engine"] == "read_disk"
    assert res["uniget"]["available"] is False
    assert "没找到 uniget.exe" in res["uniget"]["reason"]
    assert res["cli"] is None


def test_check_updates_no_session_stops_after_status_probe(tmp_path, fake_root, monkeypatch):
    """探针说没会话就收手：只发过 status，绝不发第二条（第二条会冷启动 UniGetUI 窗口）"""
    calls = _fake_uniget(monkeypatch, running=False)
    res = run(tool_specs._handle_check_package_updates({"execute": True}, _ctx(tmp_path)))
    assert [c["argv"][-1] for c in calls] == ["status"], "无会话时不许再发 package updates"
    assert res["engine"] == "read_disk"
    assert "running=false" in res["uniget"]["reason"]
    assert "不代为冷启动" in res["uniget"]["reason"]
    assert res["cli"] == FAKE_EXE, "入口找到了，卡点在没有会话——载荷要区分这两件事"


def test_check_updates_execute_uses_uniget_rows_without_touching_disk(tmp_path, monkeypatch):
    """外部口径成立时不读 scoop：连 resolve_root 都不碰，本机没装 scoop 也能查 winget"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("UniGetUI 口径可用时不该再走读盘层")

    monkeypatch.setattr(pkgman, "resolve_root", _boom)
    calls = _fake_uniget(monkeypatch)
    res = run(tool_specs._handle_check_package_updates(
        {"execute": True, "timeout": 45}, _ctx(tmp_path)
    ))
    assert res["engine"] == "uniget"
    assert res["count"] == 1
    assert res["updates"][0]["name"] == "7zip.7zip"
    assert res["updates"][0]["displayName"] == "7-Zip"
    assert res["cli"] == FAKE_EXE
    assert [c["timeout"] for c in calls] == [45, 45], "timeout 要真的传到每条命令"
    assert calls[0]["argv"] == [FAKE_EXE, "status"]
    assert calls[1]["argv"] == [FAKE_EXE, "package", "updates"]


def test_check_updates_raises_the_output_cap_for_json_payloads(tmp_path, monkeypatch):
    """`uniget package updates` 是 JSON，不是日志：按 4000 字符留尾会拦腰截断（本机整份 57652 字符）"""
    monkeypatch.setattr(pkgman, "resolve_root", lambda *a, **k: (None, "本机没有 scoop"))
    calls = _fake_uniget(monkeypatch)
    run(tool_specs._handle_check_package_updates({"execute": True}, _ctx(tmp_path)))
    assert calls[0]["maxOutput"] == pkgman.OUTPUT_MAX_CHARS, "status 输出很短，默认口径够用"
    assert calls[1]["maxOutput"] == pkgman.UNIGET_OUTPUT_MAX_CHARS


def test_check_updates_truncated_json_degrades_instead_of_reporting_zero(tmp_path, fake_root, monkeypatch):
    """抬了上限仍被截：要说「输出被截断」，不能把半个 JSON 读成 0 条更新"""

    async def _run(argv, timeout=None, max_output=pkgman.OUTPUT_MAX_CHARS):
        is_status = argv[-1] == "status"
        return {
            "ok": True, "exitCode": 0, "timedOut": False,
            "stdoutTruncated": not is_status,
            "stdout": json.dumps({"running": True}) if is_status
            else '{"status": "success", "updates": [{"id": "x", "isUpgradable": true}',
            "stderr": "",
        }

    monkeypatch.setattr(pkgman, "run_argv", _run)
    monkeypatch.setattr(pkgman, "locate_uniget_cli", lambda: (FAKE_EXE, "test"))
    res = run(tool_specs._handle_check_package_updates({"execute": True}, _ctx(tmp_path)))
    assert res["engine"] == "read_disk"
    assert "被截断" in res["uniget"]["reason"]
    assert "manager" in res["uniget"]["reason"], "要给出可自救的那一步：按安装器分开查"


def test_check_updates_manager_reaches_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(pkgman, "resolve_root", lambda *a, **k: (None, "本机没有 scoop"))
    calls = _fake_uniget(monkeypatch)
    res = run(tool_specs._handle_check_package_updates(
        {"execute": True, "manager": "Winget"}, _ctx(tmp_path)
    ))
    assert calls[1]["argv"] == [FAKE_EXE, "package", "updates", "--manager", "winget"]
    assert res["manager"] == "winget"


def test_check_updates_failed_command_degrades_with_exit_code(tmp_path, fake_root, monkeypatch):
    """第二条命令非零退出（实测无 token 时就是退出码 2）：退回读盘并带上退出码"""
    _fake_uniget(monkeypatch, updates_ok=False, updates_exit=2,
                 updates={"status": "error", "message": "The IPC API token is not available."})
    res = run(tool_specs._handle_check_package_updates({"execute": True}, _ctx(tmp_path)))
    assert res["engine"] == "read_disk"
    assert "退出码 2" in res["uniget"]["reason"]
    assert "IPC API token" in res["uniget"]["reason"]


def test_check_updates_other_manager_without_uniget_is_install_required(tmp_path, fake_root, monkeypatch):
    """读盘层覆盖不到 winget：返回空清单会被读成「没有更新」，必须显式失败"""
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    monkeypatch.setattr(pkgman, "locate_uniget_cli", lambda: (None, "没找到 uniget.exe"))
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_check_package_updates(
            {"manager": "winget", "execute": True}, _ctx(tmp_path)
        ))
    err = _err(exc)
    assert err["errorType"] == "install_required"
    assert "只能由 UniGetUI 提供" in err["error"]
    assert err["manager"] == "winget"


def test_check_updates_without_either_source_is_install_required(tmp_path, monkeypatch):
    """两条口径都没了：说清读盘为什么定位不到、外部为什么不可用"""
    monkeypatch.setattr(
        pkgman, "resolve_root", lambda manager="scoop": (None, "环境变量与注册表里都没有 SCOOP")
    )
    monkeypatch.setattr(pkgman, "locate_uniget_cli", lambda: (None, "没找到 uniget.exe"))
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_check_package_updates({"execute": True}, _ctx(tmp_path)))
    err = _err(exc)
    assert err["errorType"] == "install_required"
    assert "SCOOP" in err["error"] and "没找到 uniget.exe" in err["error"]


# ---- UniGetUI 只读意图工具（2026-10-31）----

_DEFAULT_UNIGET_RESPONSES = {
    "manager list": {
        "status": "success",
        "managers": [
            {"name": "scoop", "displayName": "Scoop", "enabled": True, "ready": True,
             "executablePath": "S:\\Libs\\Scoop\\shims\\scoop.cmd"},
            {"name": "winget", "displayName": "WinGet", "enabled": True, "ready": False,
             "executablePath": ""},
            {"name": "dotnet-tool", "displayName": ".NET Tool", "enabled": True,
             "ready": False, "executablePath": ""},
        ],
    },
    "source list": {
        "status": "success",
        "sources": [
            {"manager": "scoop", "name": "extras", "displayName": "Scoop: extras",
             "url": "https://github.com/ScoopInstaller/Extras", "packageCount": 2449,
             "updateDate": "2026-10-31 16:24:39", "isKnown": True, "isConfigured": True},
        ],
    },
    "package search": {
        "status": "success",
        "packages": [
            {"name": "7-Zip", "id": "7zip.7zip", "version": "26.05", "newVersion": "",
             "source": "WinGet: winget", "manager": "winget", "isUpgradable": False},
            {"name": "7-Zip Beta", "id": "7zip-beta", "version": "26.00", "newVersion": "",
             "source": "Scoop: versions", "manager": "winget", "isUpgradable": False},
        ],
    },
    "package installed": {
        "status": "success",
        "packages": [
            {"name": "7zip", "id": "7zip", "version": "25.01", "newVersion": "",
             "source": "Scoop: main", "manager": "scoop", "isUpgradable": False},
            {"name": "Age", "id": "age", "version": "1.3.2", "newVersion": "",
             "source": "Scoop: extras", "manager": "scoop", "isUpgradable": False},
        ],
    },
    "package details": {
        "status": "success",
        "package": {
            "name": "7zip", "id": "7zip", "version": "25.01", "newVersion": "",
            "source": "Scoop: main", "manager": "scoop",
            "description": "A multi-format file archiver", "author": "7-zip",
            "homepageUrl": "https://www.7-zip.org/", "license": "LGPL-2.1-or-later",
            "installerType": "Scoop 软件包", "installerSize": 1996800,
            "installLocation": "S:\\Libs\\Scoop\\apps\\7zip\\current",
            "tags": [], "screenshots": [], "dependencies": [], "versions": [],
        },
    },
    "package versions": {
        "status": "success",
        "versions": ["26.05", "25.01"],
    },
}


def _fake_uniget_surface(monkeypatch, *, exe=FAKE_EXE, running=True,
                         responses=None, error_commands=()):
    """按子命令（`arg 1-2`）脚本化应答；`status` 永远先走会话探针。"""
    calls: list[dict] = []
    payloads = {**_DEFAULT_UNIGET_RESPONSES, **(responses or {})}

    async def _run(argv, timeout=None, max_output=pkgman.OUTPUT_MAX_CHARS):
        command = "status" if argv[-1] == "status" else " ".join(argv[1:3])
        calls.append({"argv": list(argv), "timeout": timeout,
                      "maxOutput": max_output, "command": command})
        if command == "status":
            stdout = json.dumps({"running": running})
        elif command in error_commands:
            stdout = json.dumps(
                {"status": "error", "message": "The operation was canceled."}
            )
        else:
            stdout = json.dumps(payloads[command])
        return {"ok": True, "exitCode": 0, "timedOut": False,
                "stdoutTruncated": False, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(pkgman, "run_argv", _run)
    monkeypatch.setattr(pkgman, "locate_uniget_cli", lambda: (exe, "test: 单测入口"))
    return calls


def test_list_package_managers_returns_readiness_and_optional_sources(tmp_path, monkeypatch):
    calls = _fake_uniget_surface(monkeypatch)
    res = run(tool_specs._handle_list_package_managers({}, _ctx(tmp_path)))
    assert res["source"] == "uniget" and res["count"] == 3
    assert res["readyCount"] == 1
    assert [m["name"] for m in res["managers"]] == ["scoop", "winget", "dotnet-tool"]
    assert "sources" not in res, "默认不要软件源"
    assert [c["command"] for c in calls] == ["status", "manager list"]

    res = run(tool_specs._handle_list_package_managers(
        {"include_sources": True}, _ctx(tmp_path)
    ))
    assert res["sourceCount"] == 1
    assert res["sources"][0]["name"] == "extras"


def test_list_package_managers_sources_failure_does_not_fail_the_call(tmp_path, monkeypatch):
    _fake_uniget_surface(monkeypatch, error_commands={"source list"})
    res = run(tool_specs._handle_list_package_managers(
        {"include_sources": True}, _ctx(tmp_path)
    ))
    assert res["count"] == 3, "安装器清单已经拿到了"
    assert "The operation was canceled." in res["sourcesUnavailable"]
    assert "sources" not in res


@pytest.mark.parametrize("args", [
    {"keyword": "7zip"},
    {"manager": "--elevated", "keyword": "7zip"},
    {"manager": "winget", "keyword": "--query"},
])
def test_search_uniget_rejects_bad_params_without_spawning(tmp_path, monkeypatch, args):
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_search_uniget_packages(args, _ctx(tmp_path)))
    assert _err(exc)["errorType"] == "invalid_params"


def test_search_uniget_passes_manager_query_and_limit(tmp_path, monkeypatch):
    calls = _fake_uniget_surface(monkeypatch)
    res = run(tool_specs._handle_search_uniget_packages(
        {"manager": "Winget", "keyword": "7zip", "limit": 1}, _ctx(tmp_path)
    ))
    assert calls[1]["argv"] == [
        FAKE_EXE, "package", "search", "--manager", "winget", "--query", "7zip",
    ]
    assert res["manager"] == "winget"
    assert res["keyword"] == "7zip"
    assert res["count"] == 2 and res["returned"] == 1 and res["truncated"] is True
    assert res["packages"][0]["name"] == "7zip.7zip"
    assert res["packages"][0]["displayName"] == "7-Zip"


def test_list_uniget_packages_sorts_by_id_and_limits(tmp_path, monkeypatch):
    _fake_uniget_surface(monkeypatch)
    res = run(tool_specs._handle_list_uniget_packages(
        {"manager": "scoop", "limit": 1}, _ctx(tmp_path)
    ))
    assert res["count"] == 2
    assert [p["name"] for p in res["packages"]] == ["7zip"]
    assert res["truncated"] is True


def test_get_package_details_includes_versions_by_default(tmp_path, monkeypatch):
    calls = _fake_uniget_surface(monkeypatch)
    res = run(tool_specs._handle_get_package_details(
        {"manager": "scoop", "package": "7zip"}, _ctx(tmp_path)
    ))
    assert res["package"] == "7zip"
    assert res["details"]["installerSize"] == 1996800
    assert res["versions"] == ["26.05", "25.01"]
    assert res["versionCount"] == 2 and res["versionsTruncated"] is False
    assert [c["command"] for c in calls] == [
        "status", "package details", "status", "package versions",
    ]


def test_get_package_details_can_skip_versions(tmp_path, monkeypatch):
    calls = _fake_uniget_surface(monkeypatch)
    res = run(tool_specs._handle_get_package_details(
        {"manager": "scoop", "package": "7zip", "include_versions": False},
        _ctx(tmp_path),
    ))
    assert "versions" not in res and "versionCount" not in res
    assert [c["command"] for c in calls] == ["status", "package details"]


def test_get_package_details_versions_failure_is_nonfatal(tmp_path, monkeypatch):
    _fake_uniget_surface(monkeypatch, error_commands={"package versions"})
    res = run(tool_specs._handle_get_package_details(
        {"manager": "scoop", "package": "7zip"}, _ctx(tmp_path)
    ))
    assert res["details"]["name"] == "7zip", "详情已拿到，版本历史失败只降级"
    assert "The operation was canceled." in res["versionsUnavailable"]
    assert "versions" not in res


def test_get_package_details_unknown_id_maps_to_not_found(tmp_path, monkeypatch):
    payload = {"package details": {
        "status": "error",
        "message": 'No package matching id "nope" was found.',
    }}
    _fake_uniget_surface(monkeypatch, responses=payload)
    with pytest.raises(ToolError) as exc:
        run(tool_specs._handle_get_package_details(
            {"manager": "scoop", "package": "nope"}, _ctx(tmp_path)
        ))
    err = _err(exc)
    assert err["errorType"] == "not_found"
    assert err["phase"] == "parse"


def test_uniget_readonly_tools_do_not_need_scoop_on_disk(tmp_path, monkeypatch):
    """外部口径成立时不碰 scoop 根目录：本机没装 scoop 也能查 winget"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("UniGetUI 口径可用时不该走读盘层")

    monkeypatch.setattr(pkgman, "resolve_root", _boom)
    _fake_uniget_surface(monkeypatch)
    ctx = _ctx(tmp_path)
    for handler, args in (
        (tool_specs._handle_list_package_managers, {}),
        (tool_specs._handle_search_uniget_packages,
         {"manager": "winget", "keyword": "7zip"}),
        (tool_specs._handle_list_uniget_packages, {"manager": "scoop"}),
        (tool_specs._handle_get_package_details,
         {"manager": "scoop", "package": "7zip"}),
    ):
        res = run(handler(args, ctx))
        assert res["success"] is True


@pytest.mark.parametrize("handler, args", [
    (tool_specs._handle_list_package_managers, {}),
    (tool_specs._handle_search_uniget_packages,
     {"manager": "winget", "keyword": "7zip"}),
    (tool_specs._handle_list_uniget_packages, {"manager": "scoop"}),
    (tool_specs._handle_get_package_details,
     {"manager": "scoop", "package": "7zip"}),
])
def test_uniget_readonly_tools_require_a_live_session(tmp_path, monkeypatch, handler, args):
    calls = _fake_uniget_surface(monkeypatch, running=False)
    with pytest.raises(ToolError) as exc:
        run(handler(args, _ctx(tmp_path)))
    err = _err(exc)
    assert err["errorType"] == "connection_error"
    assert err["phase"] == "status"
    assert "running=false" in err["error"]
    assert [c["command"] for c in calls] == ["status"], "无会话时不许再发第二条"


@pytest.mark.parametrize("handler, args", [
    (tool_specs._handle_list_package_managers, {}),
    (tool_specs._handle_list_uniget_packages, {"manager": "scoop"}),
])
def test_uniget_readonly_tools_missing_cli_is_install_required(tmp_path, monkeypatch,
                                                               handler, args):
    _spawn_forbidden(monkeypatch, pkgman, "run_argv")
    monkeypatch.setattr(pkgman, "locate_uniget_cli", lambda: (None, "没找到 uniget.exe"))
    with pytest.raises(ToolError) as exc:
        run(handler(args, _ctx(tmp_path)))
    err = _err(exc)
    assert err["errorType"] == "install_required"
    assert err["phase"] == "locate"
    assert "没找到 uniget.exe" in err["error"]
