"""包管理适配层（`src/pkgman.py`）的守卫（P2-07）。

真机勘察给出的每一个坑都在这里钉一条：

- 只读 `current/manifest.json` 会漏掉 current 断链的安装 → `current_broken` 回落；
- manifest 的 `bin` 写成字符串时按字符迭代会产出垃圾路径（真机上表现为
  「bw.exe 有 6 个 bin」）→ `_as_entries`；
- manifest 不声明 `bin` 但 `shims/<包名>.exe` 能跑（真机 `bayes`）→ shim 候选；
- 扩展名决定能不能直接 spawn（真机实测：`.cmd` 可、`.ps1` 抛 WinError 193）→ `runnable`；
- `launchable` 与 `launch_target` 必须由同一批候选算出，否则一行数据两种答案。

磁盘布局全部用 `tmp_path` 里的合成 scoop 树，不碰真机 scoop。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pkgman
from errors import ErrorType
from pkgman import PackageError, launch_target, list_installed


# ---- 合成布局 ----


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_app(
    root: Path,
    name: str,
    version: str = "1.0.0",
    *,
    current: bool = True,
    manifest: dict | None = None,
    install: dict | None = None,
    exe_names: tuple[str, ...] = (),
) -> Path:
    """造一个 app 目录。`current=False` 时把 manifest 留在版本目录并留一个空的
    `current`，正是真机 6 个断链包的形状。"""
    app = root / "apps" / name
    target = app / ("current" if current else version)
    target.mkdir(parents=True)
    if manifest is not None:
        write_json(target / "manifest.json", manifest)
    if install is not None:
        write_json(target / "install.json", install)
    for exe in exe_names:
        (target / exe).write_text("fake binary", encoding="utf-8")
    if not current:
        (app / "current").mkdir()
    return target


def make_shim(root: Path, filename: str) -> Path:
    p = root / "shims" / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("fake shim", encoding="utf-8")
    return p


@pytest.fixture()
def scoop(tmp_path: Path) -> Path:
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
        manifest={
            "version": "2.0.0",
            "bin": ["a.exe", "b.exe"],
            "shortcuts": [["a.exe", "界面 A"]],
        },
        install={"bucket": "extras"},
        exe_names=("a.exe", "b.exe"),
    )
    make_app(root, "nomanifest", manifest=None)  # 只有目录，没有 manifest
    make_shim(root, "nomanifest.exe")
    make_shim(root, "nomanifest.shim")      # 描述符文件，不是入口
    make_shim(root, "alias-of-other.cmd")   # 别人的别名，不算这个包的
    make_shim(root, "goodpkg.ps1")
    return root


def run(coro):
    return asyncio.run(coro)


# ---- resolve_root ----


def test_resolve_root_uses_process_env(scoop, monkeypatch):
    monkeypatch.setenv("SCOOP", str(scoop))
    root, reason = pkgman.resolve_root("scoop")
    assert root == str(scoop)
    assert "进程环境" in reason


def test_resolve_root_falls_back_to_registry(scoop, monkeypatch):
    monkeypatch.delenv("SCOOP", raising=False)
    monkeypatch.setattr(pkgman, "registry_env_value",
                        lambda k: str(scoop) if k == "SCOOP" else None)
    root, reason = pkgman.resolve_root()
    assert root == str(scoop)
    assert "注册表" in reason


def test_resolve_root_reports_missing_dir_instead_of_guessing(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOOP", str(tmp_path / "nope"))
    root, reason = pkgman.resolve_root()
    assert root is None
    assert "不存在" in reason


def test_resolve_root_uses_userprofile_default(scoop, monkeypatch):
    monkeypatch.delenv("SCOOP", raising=False)
    monkeypatch.setattr(pkgman, "registry_env_value", lambda k: None)
    monkeypatch.setenv("USERPROFILE", str(scoop.parent))
    root, reason = pkgman.resolve_root()
    assert root == str(scoop)
    assert "USERPROFILE" in reason


def test_resolve_root_refuses_empty_environment(scoop, monkeypatch):
    monkeypatch.delenv("SCOOP", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(pkgman, "registry_env_value", lambda k: None)
    root, reason = pkgman.resolve_root()
    assert root is None
    assert "USERPROFILE" in reason


def test_unsupported_manager_is_invalid_params():
    with pytest.raises(PackageError) as exc:
        pkgman.resolve_root("winget")
    assert exc.value.error_type == "invalid_params"
    assert "winget" in str(exc.value)


# ---- 包名校验 ----


@pytest.mark.parametrize(
    "bad", ["../evil", "a/b", "a\\b", "", "   ", "a;rm -rf /", "x" * 101, "a b|c"]
)
def test_bad_names_rejected(bad):
    with pytest.raises(PackageError) as exc:
        pkgman.validate_name(bad)
    assert exc.value.error_type == "invalid_params"


@pytest.mark.parametrize("good", ["7zip", "nodejs-lts", "ffmpeg.shared", "a_b", "c++", "yt-dlp"])
def test_good_names_accepted(good):
    assert pkgman.validate_name(good) == good


# ---- 已装视图 ----


def test_list_installed_covers_all_three_states(scoop):
    rows = {r["name"]: r for r in list_installed(str(scoop))}
    assert set(rows) == {"goodpkg", "brokenpkg", "nomanifest"}
    assert rows["goodpkg"]["state"] == pkgman.STATE_OK
    assert rows["goodpkg"]["currentAlive"] is True
    assert rows["brokenpkg"]["state"] == pkgman.STATE_CURRENT_BROKEN
    assert rows["brokenpkg"]["currentAlive"] is False
    assert rows["nomanifest"]["state"] == pkgman.STATE_NO_MANIFEST


def test_row_key_set_is_identical_across_states(scoop):
    """字段集必须一致：调用方不该按「有没有这个键」分支（no_manifest 曾漏 currentAlive）"""
    keysets = {frozenset(r) for r in list_installed(str(scoop))}
    assert len(keysets) == 1, keysets


def test_bucket_and_arch_come_from_install_json(scoop):
    row = pkgman.find_installed(str(scoop), "goodpkg")
    assert row["bucket"] == "main"
    assert row["architecture"] == "64bit"
    assert row["version"] == "1.2.3"
    assert row["description"] == "好包"


def test_string_bin_yields_one_entry_not_one_per_char(scoop):
    """真机 bug 回归：manifest 写 `bin: "bw.exe"` 时按字符迭代会产出 6 个假入口"""
    row = pkgman.find_installed(str(scoop), "goodpkg")
    assert row["bin"] == [str(scoop / "apps" / "goodpkg" / "current" / "good.exe")]


def test_manifest_relative_escape_is_dropped(tmp_path):
    root = tmp_path / "scoop"
    make_app(root, "runaway", manifest={"version": "1.0", "bin": ["../../../../evil.exe"]})
    assert pkgman.find_installed(str(root), "runaway")["bin"] == []


def test_no_manifest_row_still_finds_its_shim(scoop):
    """没 manifest 但有同名 shim（真机 scoop / bayes 同形）：行必须承认它可启动"""
    row = pkgman.find_installed(str(scoop), "nomanifest")
    assert row["shim"] == [str(scoop / "shims" / "nomanifest.exe")]
    assert row["launchable"] is True


def test_installed_one_without_root_skips_shim_layer(scoop):
    """不传 root 就是纯 manifest 视图：shim 为空、launchable 跟着变假"""
    row = pkgman.installed_one(scoop / "apps" / "nomanifest", "nomanifest")
    assert row["shim"] == []
    assert row["launchable"] is False


def test_shim_lookup_is_exact_stem_only(scoop):
    """只认同名 shim：`.shim` 描述符与别人的别名都不算这个包的入口"""
    assert [Path(p).name for p in pkgman.shim_paths(str(scoop), "nomanifest")] == ["nomanifest.exe"]
    assert pkgman.shim_paths(str(scoop), "goodpkg") == [str(scoop / "shims" / "goodpkg.ps1")]
    assert pkgman.shim_paths(str(scoop), "ghost") == []


def test_broken_current_falls_back_to_newest_version_dir(tmp_path):
    root = tmp_path / "scoop"
    app = root / "apps" / "multi"
    for v in ("2.3.1", "19.8.3"):
        d = app / v
        d.mkdir(parents=True)
        write_json(d / "manifest.json", {"version": v})
    (app / "current").mkdir()
    row = pkgman.find_installed(str(root), "multi")
    assert row["version"] == "19.8.3", "字符串排序会把 19.8.3 排在 2.3.1 前面"
    assert row["state"] == pkgman.STATE_CURRENT_BROKEN


def test_list_installed_on_missing_apps_dir(tmp_path):
    assert list_installed(str(tmp_path)) == []


# ---- launch_target ----


def test_precedence_shortcut_then_bin_then_shim(scoop):
    broken = pkgman.find_installed(str(scoop), "brokenpkg")
    tgt = launch_target(broken)
    assert tgt["kind"] == "shortcut"
    assert tgt["path"] == str(scoop / "apps" / "brokenpkg" / "2.0.0" / "a.exe")
    assert launch_target(pkgman.find_installed(str(scoop), "nomanifest"))["kind"] == "shim"


def test_ps1_shim_is_never_the_default_pick(scoop):
    """`.ps1` 在盘上但 CreateProcess 起不来：默认挑选必须跳过它，并给出结构化原因"""
    row = {"name": "goodpkg", "shortcuts": [], "bin": [],
           "shim": [str(scoop / "shims" / "goodpkg.ps1")]}
    tgt = launch_target(row)
    assert tgt["ok"] is False
    assert tgt["code"] == pkgman.NO_LIVE_ENTRY
    assert tgt["candidates"][0]["exists"] is True
    assert tgt["candidates"][0]["runnable"] is False
    assert launch_target(row, "goodpkg.ps1")["code"] == pkgman.NOT_SPAWNABLE


def test_launchable_and_launch_target_agree(scoop):
    """同一批候选、同一个谓词：两处口径不许分叉"""
    for row in list_installed(str(scoop)):
        assert row["launchable"] == launch_target(row)["ok"], row["name"]


def test_want_matches_alias_name_and_filename_case_insensitively(scoop):
    row = pkgman.find_installed(str(scoop), "brokenpkg")
    assert launch_target(row, "界面 a")["ok"] is True
    assert launch_target(row, "B.EXE")["path"].endswith("b.exe")


def test_failure_codes_are_structured(scoop):
    row = pkgman.find_installed(str(scoop), "brokenpkg")
    assert launch_target(row, "nope.exe")["code"] == pkgman.NO_SUCH_ENTRY
    assert launch_target({"name": "x", "shortcuts": [], "bin": [], "shim": []})["code"] == pkgman.NO_ENTRIES
    dead = {"name": "x", "shortcuts": [], "bin": [str(scoop / "gone.exe")], "shim": []}
    assert launch_target(dead)["code"] == pkgman.NO_LIVE_ENTRY


def test_candidates_carry_exists_flag(scoop):
    """断链安装的 manifest 会声明已不在盘上的路径：不标 exists 就是把死路径当好路径"""
    row = pkgman.find_installed(str(scoop), "brokenpkg")
    row["bin"] = row["bin"] + [str(scoop / "apps" / "brokenpkg" / "2.0.0" / "ghost.exe")]
    ghosts = [c for c in launch_target(row)["candidates"] if not c["exists"]]
    assert len(ghosts) == 1


# ---- 搜索 ----


def test_search_ranks_exact_prefix_contains(scoop):
    for name in ("git", "github", "my-git-tools", "zen"):
        write_json(scoop / "buckets" / "main" / "bucket" / f"{name}.json",
                   {"description": f"{name} 的说明", "version": "9.9.9", "homepage": "https://x"})
    res = pkgman.search_packages(str(scoop), "git", limit=10)
    assert [r["name"] for r in res["rows"]] == ["git", "github", "my-git-tools"]
    assert res["rows"][0]["bucket"] == "main"
    assert res["scannedManifests"] == 4
    assert res["scannedBuckets"] == 1


def test_search_is_case_insensitive_on_bucket_names(scoop):
    write_json(scoop / "buckets" / "main" / "bucket" / "Git.json", {"description": "d"})
    assert [r["name"] for r in pkgman.search_packages(str(scoop), "GIT")["rows"]] == ["Git"]


def test_search_empty_keyword_and_missing_buckets(scoop):
    with pytest.raises(PackageError) as exc:
        pkgman.search_packages(str(scoop), "   ")
    assert exc.value.error_type == "invalid_params"
    missing = pkgman.search_packages(str(scoop / "nowhere"), "git")
    assert missing["rows"] == [] and missing["scannedBuckets"] == 0


def test_search_limit_is_clamped(scoop):
    write_json(scoop / "buckets" / "main" / "bucket" / "git.json", {"description": "d"})
    assert pkgman.search_packages(str(scoop), "git", limit=9999)["limit"] == pkgman.MAX_SEARCH_LIMIT
    # 传了就夹取边界，不许悄悄换回默认值（`0 or DEFAULT` 那种写法会把 0 当「没传」）
    assert pkgman.search_packages(str(scoop), "git", limit=0)["limit"] == 1
    assert pkgman.search_packages(str(scoop), "git")["limit"] == pkgman.DEFAULT_SEARCH_LIMIT
    assert pkgman.search_packages(str(scoop), "git", limit="3")["limit"] == 3
    with pytest.raises(PackageError) as exc:
        pkgman.search_packages(str(scoop), "git", limit="abc")
    assert exc.value.error_type == "invalid_params"


# ---- 安装命令 ----


def test_install_argv_points_at_this_roots_shim_not_path(scoop):
    make_shim(scoop, "scoop.cmd")
    assert pkgman.install_argv(str(scoop), "yt-dlp") == [
        str(scoop / "shims" / "scoop.cmd"), "install", "yt-dlp"
    ]


def test_install_without_scoop_entry_is_install_required(scoop):
    with pytest.raises(PackageError) as exc:
        pkgman.install_argv(str(scoop), "yt-dlp")
    assert exc.value.error_type == "install_required"


def test_install_argv_rejects_traversal_name(scoop):
    make_shim(scoop, "scoop.cmd")
    with pytest.raises(PackageError):
        pkgman.install_argv(str(scoop), "../evil")


# ---- 执行 ----


def test_run_argv_does_not_inherit_stdin(scoop):
    """缺陷②的回归钉：子进程继承 server 的 stdio 管道会偷走 JSON-RPC 字节。
    stdin=DEVNULL 时读 stdin 立刻拿到空串；继承了就会挂住或读到父进程的数据。"""
    res = run(pkgman.run_argv([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]))
    assert res["ok"] is True, res
    assert "''" in res["stdout"]


def test_run_argv_reports_exit_and_output(scoop):
    res = run(pkgman.run_argv([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]))
    assert res["ok"] is False
    assert res["exitCode"] == 3
    assert "boom" in res["stderr"]
    assert res["timedOut"] is False


def test_run_argv_timeout_kills_and_flags(scoop):
    res = run(pkgman.run_argv([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1))
    assert res["timedOut"] is True
    assert res["ok"] is False


def test_run_argv_missing_command_is_config_error(scoop):
    with pytest.raises(PackageError) as exc:
        run(pkgman.run_argv([str(scoop / "no-such-binary.exe")]))
    assert exc.value.error_type == "config_error"


def test_run_argv_output_is_capped(scoop):
    res = run(pkgman.run_argv([sys.executable, "-c", "print('y' * 100 + 'x' * 200000)"]))
    assert len(res["stdout"]) == pkgman.OUTPUT_MAX_CHARS
    # 截的是尾部（有用那段在结尾），行尾换行属于尾部内容本身
    assert res["stdout"].rstrip().endswith("x")
    assert "y" not in res["stdout"]


def test_run_argv_reports_truncation_and_honours_a_larger_cap(scoop):
    """留尾对日志是对的，对 JSON 是拦腰砍：调用方要能抬上限，并且知道到底截没截"""
    argv = [sys.executable, "-c",
            "import sys; sys.stdout.write('x' * %d)" % (pkgman.OUTPUT_MAX_CHARS + 1000)]
    narrow = run(pkgman.run_argv(argv))
    assert len(narrow["stdout"]) == pkgman.OUTPUT_MAX_CHARS
    assert narrow["stdoutTruncated"] is True
    wide = run(pkgman.run_argv(argv, max_output=pkgman.UNIGET_OUTPUT_MAX_CHARS))
    assert wide["stdoutTruncated"] is False
    assert len(wide["stdout"]) == pkgman.OUTPUT_MAX_CHARS + 1000


def test_run_argv_timeout_is_capped(scoop):
    res = run(pkgman.run_argv([sys.executable, "-c", "print('ok')"], timeout=99999))
    assert res["timeout"] == pkgman.MAX_TIMEOUT


def test_run_argv_timeout_zero_is_not_default(scoop):
    """0 是「1 秒都不许跑」，不是「没传」——回落到默认 120 会把拒绝变成静默等待"""
    res = run(pkgman.run_argv([sys.executable, "-c", "print('ok')"], timeout=0))
    assert res["timeout"] == 1
    with pytest.raises(PackageError) as exc:
        run(pkgman.run_argv([sys.executable, "-c", "print('ok')"], timeout="soon"))
    assert exc.value.error_type == "invalid_params"


# ---- 列表瘦身视图 ----


def test_brief_shape(scoop):
    b = pkgman.brief(list_installed(str(scoop))[0])
    assert set(b) == {
        "name", "state", "version", "bucket", "description", "homepage",
        "launchable", "entryCount",
    }
    # 瘦身视图里不许出现绝对路径：那正是它要省掉的体积
    assert not any(isinstance(v, str) and ("apps" in v or "shims" in v) for v in b.values())


# ---- 版本比较键 ----


@pytest.mark.parametrize(
    "newer,older",
    [("19.8.3", "2.3.1"), ("25.01", "1.3.1"), ("2.66.1", "0.50"), ("1.10.0", "1.9.9")],
)
def test_version_key_compares_numeric_segments(newer, older):
    """字符串排序会把 19.8.3 排在 2.3.1 前面，差集就会给出反的答案"""
    assert pkgman.version_key(newer) > pkgman.version_key(older)


def test_version_key_empty_is_lowest_but_caller_must_guard():
    """空值返回 [] —— 任何版本都比它大，所以「读不到已装版本」必须在差集里先挡掉"""
    assert pkgman.version_key(None) == []
    assert pkgman.version_key("") < pkgman.version_key("0.0.1")


# ---- 本地 bucket 索引差集（纯读盘）----


def make_bucket(root: Path, bucket: str, name: str, version: str | None) -> None:
    write_json(root / "buckets" / bucket / "bucket" / f"{name}.json", {"version": version})


def test_bucket_version_reads_layout_used_by_search_packages(scoop):
    make_bucket(scoop, "main", "goodpkg", "1.3.0")
    assert pkgman.bucket_version(str(scoop), "main", "goodpkg") == "1.3.0"


def test_bucket_version_accepts_flat_bucket_dir(scoop):
    """少数 bucket 把清单平铺在 `buckets/<bucket>/`，与 search_packages 同一条回落"""
    write_json(scoop / "buckets" / "flat" / "goodpkg.json", {"version": "9.9.9"})
    assert pkgman.bucket_version(str(scoop), "flat", "goodpkg") == "9.9.9"


@pytest.mark.parametrize("bucket", ["../escape", "..\\windows", "/abs"])
def test_bucket_version_refuses_traversal(scoop, bucket):
    """bucket 名来自盘上的 install.json，但它参与路径拼接，越界一律不读"""
    make_bucket(scoop, "main", "goodpkg", "1.3.0")
    assert pkgman.bucket_version(str(scoop), bucket, "goodpkg") is None


def test_bucket_version_missing_manifest_is_none(scoop):
    assert pkgman.bucket_version(str(scoop), "main", "nothere") is None


def test_read_disk_updates_lists_only_strictly_newer(scoop):
    make_bucket(scoop, "main", "goodpkg", "1.3.0")      # 1.2.3 → 1.3.0：有更新
    make_bucket(scoop, "extras", "brokenpkg", "2.0.0")  # 已装就是 2.0.0：不动
    d = pkgman.read_disk_updates(str(scoop))
    assert [(r["name"], r["installedVersion"], r["availableVersion"]) for r in d["rows"]] == [
        ("goodpkg", "1.2.3", "1.3.0")
    ]
    assert d["rows"][0]["bucket"] == "main"
    assert d["rows"][0]["state"] == "ok"
    assert d["scanned"] == 3


def test_read_disk_updates_keeps_broken_current_visible(scoop):
    """current 断链的包照样能比版本，但 state 必须带出去，别让调用方当成正常安装"""
    make_bucket(scoop, "extras", "brokenpkg", "3.1.0")
    row = pkgman.read_disk_updates(str(scoop))["rows"][0]
    assert row["state"] == "current_broken"


def test_read_disk_updates_reasons_are_distinguished(scoop):
    """三种比不了的成因分开说：没有 install.json / 直链安装 / 索引里没这个包"""
    make_app(scoop, "urlpkg", manifest={"version": "1.0.0"},
             install={"url": "https://x/y.json"})
    d = pkgman.read_disk_updates(str(scoop))
    reasons = {u["name"]: u["reason"] for u in d["unknown"]}
    assert "install.json 读不到" in reasons["nomanifest"]
    assert "直链安装" in reasons["urlpkg"]
    assert "urlpkg" not in [r["name"] for r in d["rows"]]


def test_read_disk_updates_missing_bucket_manifest_is_unknown(scoop):
    """有 bucket 但索引里没有这个包（例如包已从 bucket 移除）→ 说「比不了」，不说「已最新」"""
    d = pkgman.read_disk_updates(str(scoop))
    assert {u["name"] for u in d["unknown"]} == {"goodpkg", "brokenpkg", "nomanifest"}
    assert any("索引里没有 goodpkg.json" in u["reason"] for u in d["unknown"])


def test_read_disk_updates_truncates_rows(monkeypatch, scoop):
    make_bucket(scoop, "main", "goodpkg", "9.9.9")
    make_bucket(scoop, "extras", "brokenpkg", "9.9.9")
    monkeypatch.setattr(pkgman, "MAX_UPDATE_ROWS", 1)
    d = pkgman.read_disk_updates(str(scoop))
    assert len(d["rows"]) == 1 and d["truncated"] is True


def test_read_disk_updates_on_missing_buckets_dir(scoop):
    """整个 buckets 目录都不存在：不抛，全部进 unknown，差集为空"""
    import shutil

    shutil.rmtree(scoop / "buckets", ignore_errors=True)
    d = pkgman.read_disk_updates(str(scoop))
    assert d["rows"] == [] and d["scanned"] == 3 and d["unknown"]


# ---- UniGetUI 入口定位与命令构造 ----


def test_locate_uniget_cli_prefers_env_override(tmp_path, monkeypatch):
    exe = tmp_path / "somewhere" / "uniget.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("fake", encoding="utf-8")
    monkeypatch.setenv(pkgman.UNIGET_CLI_ENV_VAR, str(exe))
    found, source = pkgman.locate_uniget_cli()
    assert found == str(exe) and "AWAM_UNIGET_CLI" in source


def test_locate_uniget_cli_env_override_missing_file_does_not_fall_through(tmp_path, monkeypatch):
    """显式指了路径却指错：说清是覆盖失效，而不是偷偷去找默认位置"""
    monkeypatch.setenv(pkgman.UNIGET_CLI_ENV_VAR, str(tmp_path / "nope.exe"))
    found, reason = pkgman.locate_uniget_cli()
    assert found is None and "AWAM_UNIGET_CLI" in reason and "不存在" in reason


def test_locate_uniget_cli_finds_default_install_dir(tmp_path, monkeypatch):
    r"""安装器 per-user 模式写在 %LOCALAPPDATA%\Programs\UniGetUI（本机实测位置）"""
    exe = tmp_path / "Programs" / "UniGetUI" / "uniget.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("fake", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    found, source = pkgman.locate_uniget_cli()
    assert found == str(exe) and "官方安装位置" in source


def test_locate_uniget_cli_reports_every_path_it_tried(tmp_path, monkeypatch):
    monkeypatch.delenv(pkgman.UNIGET_CLI_ENV_VAR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    found, reason = pkgman.locate_uniget_cli()
    assert found is None and "没找到 uniget.exe" in reason
    assert str(tmp_path / "la" / "Programs" / "UniGetUI" / "uniget.exe") in reason
    assert str(tmp_path / "pf" / "UniGetUI" / "uniget.exe") in reason


def test_locate_uniget_cli_without_any_env(monkeypatch):
    monkeypatch.delenv(pkgman.UNIGET_CLI_ENV_VAR, raising=False)
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramW6432"):
        monkeypatch.delenv(var, raising=False)
    found, reason = pkgman.locate_uniget_cli()
    assert found is None and "无法定位" in reason


def test_uniget_status_argv_is_bare():
    assert pkgman.uniget_status_argv("C:\\x\\uniget.exe") == ["C:\\x\\uniget.exe", "status"]


def test_uniget_updates_argv_passes_manager_as_whitelisted_value():
    argv = pkgman.uniget_updates_argv("uniget.exe", "Winget")
    assert argv == ["uniget.exe", "package", "updates", "--manager", "winget"]


def test_uniget_updates_argv_without_manager_has_no_flag():
    assert pkgman.uniget_updates_argv("uniget.exe") == ["uniget.exe", "package", "updates"]


@pytest.mark.parametrize("bad", ["--elevated", "--wait", "scoop; calc", "../x", "not-a-manager"])
def test_uniget_manager_rejects_option_smuggling(bad):
    """参数位一旦开放就会被选项字符串走私，所以只认 docs/CLI.md 那批稳定 id"""
    with pytest.raises(PackageError) as exc:
        pkgman.uniget_updates_argv("uniget.exe", bad)
    assert exc.value.error_type == "invalid_params"


@pytest.mark.parametrize("good", list(pkgman.UNIGET_MANAGERS))
def test_every_documented_uniget_manager_is_accepted(good):
    assert pkgman.validate_uniget_manager(good) == good.lower()


# ---- UniGetUI 输出解析（载荷形状取自 2026-10-24 本机 v2026.3.2 实测）----


def test_parse_uniget_running_true():
    assert pkgman.parse_uniget_running('{"running": true}') is True


@pytest.mark.parametrize("text", ['{"running": false}', "{}", '{"running": "true"}'])
def test_parse_uniget_running_is_false_unless_boolean_true(text):
    """非 true 一律按「不可用」处理：字符串 "true" 不算数，否则会把无会话当有会话"""
    assert pkgman.parse_uniget_running(text) is False


def test_parse_uniget_updates_maps_id_to_name_and_holds_display_name():
    text = json.dumps({
        "status": "success",
        "updates": [
            {"name": "7-Zip", "id": "7zip.7zip", "version": "25.01",
             "newVersion": "26.05", "source": "WinGet: winget", "manager": "winget",
             "isUpgradable": True},
            {"name": "scoop", "id": "scoop", "version": "0.99", "newVersion": "1.0",
             "source": "Scoop: main", "manager": "scoop", "isUpgradable": True},
        ],
    })
    rows = pkgman.parse_uniget_updates(text)["rows"]
    by_name = {r["name"]: r for r in rows}
    assert [r["name"] for r in rows] == ["scoop", "7zip.7zip"], "按 (manager, name) 排序"
    winget = by_name["7zip.7zip"]
    assert winget["installedVersion"] == "25.01"
    assert winget["availableVersion"] == "26.05"
    assert winget["source"] == "WinGet: winget"
    assert winget["displayName"] == "7-Zip"
    assert "displayName" not in by_name["scoop"], "id 与显示名相同时不重复给字段"


def test_parse_uniget_updates_drops_rows_without_a_new_version():
    text = json.dumps({"status": "success", "updates": [
        {"name": "x", "id": "x", "version": "1.0", "newVersion": "",
         "manager": "scoop", "isUpgradable": False},
    ]})
    assert pkgman.parse_uniget_updates(text)["rows"] == []


def test_parse_uniget_updates_empty_array_means_up_to_date():
    """`updates: []` 是真的「都最新」，与下面缺键的假成功不是一回事"""
    res = pkgman.parse_uniget_updates('{"status": "success", "updates": []}')
    assert res == {"rows": [], "truncated": False}


def test_parse_uniget_updates_requires_the_updates_array():
    """数组键按命令不同：`package installed` 是 packages，`package updates` 是 updates

    读错键会得到 status=success + 空清单，被下游当成「没有可用更新」——假阴性，
    所以缺这个键必须报错，而不是返回空。
    """
    with pytest.raises(PackageError) as exc:
        pkgman.parse_uniget_updates(
            json.dumps({"status": "success", "packages": [{"id": "x", "isUpgradable": True}]})
        )
    assert "updates 数组" in str(exc.value)


def test_parse_uniget_updates_surfaces_uniget_error_message():
    """UniGetUI 失败时也是 JSON，只是 status=error（实测的无会话消息）"""
    text = json.dumps({
        "status": "error",
        "command": "",
        "message": "The IPC API token is not available. Start UniGetUI and try again.",
    })
    with pytest.raises(PackageError) as exc:
        pkgman.parse_uniget_updates(text)
    assert exc.value.error_type == "runtime_error"
    assert "IPC API token" in str(exc.value)


@pytest.mark.parametrize("text", ["", "not json", "[1,2]", '{"packages": "oops"}'])
def test_parse_uniget_updates_rejects_unexpected_shapes(text):
    """status 缺失、顶层不是对象、只有别的键——都不能被读成「没有更新」"""
    with pytest.raises(PackageError):
        pkgman.parse_uniget_updates(text)


@pytest.mark.parametrize("text", ["", "not json", "[1,2]"])
def test_parse_uniget_running_rejects_non_object_json(text):
    """status 输出的形状不对时按失败处理，而不是默认「会话在跑」"""
    with pytest.raises(PackageError):
        pkgman.parse_uniget_running(text)


# ---- UniGetUI 只读面（2026-10-31）：取值校验 / 命令构造 / 载荷解析 ----


@pytest.mark.parametrize("bad", ["-x", "--elevated"])
def test_validate_name_rejects_leading_dash(bad):
    """scoop 包名的字符集允许短横，但开头的短横会被 argv 当成选项位"""
    with pytest.raises(PackageError) as exc:
        pkgman.validate_name(bad)
    assert exc.value.error_type == "invalid_params"
    assert "不能以 - 开头" in str(exc.value)


@pytest.mark.parametrize("good", [
    "7zip.7zip", "Microsoft.VisualStudioCode", "requests",
    "some package", "a+b_c-d.e",
])
def test_validate_package_id_accepts_real_world_ids(good):
    assert pkgman.validate_package_id(good) == good
    assert pkgman.validate_query(good) == good


@pytest.mark.parametrize("bad", ["", "  ", "-x", "a\x00b", "a\nb", "x" * 201])
def test_validate_package_id_and_query_reject_argv_hazards(bad):
    for fn in (pkgman.validate_package_id, pkgman.validate_query):
        with pytest.raises(PackageError) as exc:
            fn(bad)
        assert exc.value.error_type == "invalid_params"


def test_uniget_readonly_argv_shapes():
    exe = "uniget.exe"
    assert pkgman.uniget_managers_argv(exe) == [exe, "manager", "list"]
    assert pkgman.uniget_sources_argv(exe) == [exe, "source", "list"]
    assert pkgman.uniget_sources_argv(exe, "Scoop") == [
        exe, "source", "list", "--manager", "scoop",
    ]
    assert pkgman.uniget_installed_argv(exe, "scoop") == [
        exe, "package", "installed", "--manager", "scoop",
    ]
    assert pkgman.uniget_search_argv(exe, "winget", "7zip") == [
        exe, "package", "search", "--manager", "winget", "--query", "7zip",
    ]
    assert pkgman.uniget_details_argv(exe, "winget", "7zip.7zip") == [
        exe, "package", "details", "--manager", "winget", "--id", "7zip.7zip",
    ]
    assert pkgman.uniget_versions_argv(exe, "winget", "7zip.7zip") == [
        exe, "package", "versions", "--manager", "winget", "--id", "7zip.7zip",
    ]


def test_uniget_search_requires_a_whitelisted_manager_and_query():
    with pytest.raises(PackageError) as exc:
        pkgman.uniget_search_argv("uniget.exe", "--elevated", "7zip")
    assert exc.value.error_type == "invalid_params"
    with pytest.raises(PackageError) as exc:
        pkgman.uniget_search_argv("uniget.exe", "scoop", "--query")
    assert exc.value.error_type == "invalid_params"


def test_parse_uniget_packages_keeps_cli_order_and_maps_id_to_name():
    """`search` 的顺序是相关性，不能按字母重排；`name` 取 id，显示名不同才带"""
    text = json.dumps({"status": "success", "packages": [
        {"name": "7-Zip", "id": "7zip.7zip", "version": "26.05", "newVersion": "",
         "source": "WinGet: winget", "manager": "winget", "isUpgradable": False},
        {"name": "7-Zip Beta", "id": "7zip-beta", "version": "26.00", "newVersion": "",
         "source": "Scoop: versions", "manager": "scoop", "isUpgradable": True},
        {"name": "", "id": ""},
    ]})
    rows = pkgman.parse_uniget_packages(text, "uniget package search")["rows"]
    assert [r["name"] for r in rows] == ["7zip.7zip", "7zip-beta"]
    assert rows[0]["displayName"] == "7-Zip"
    assert rows[1]["displayName"] == "7-Zip Beta"
    assert rows[1]["upgradable"] is True
    assert rows[0]["availableVersion"] is None


def test_parse_uniget_packages_requires_packages_array():
    with pytest.raises(PackageError) as exc:
        pkgman.parse_uniget_packages(
            json.dumps({"status": "success", "updates": []}),
            "uniget package installed",
        )
    assert "packages 数组" in str(exc.value)


def test_parse_uniget_packages_surfaces_error_status():
    """错误应答退出码也是 0：只能靠 status 字段识别"""
    with pytest.raises(PackageError) as exc:
        pkgman.parse_uniget_packages(json.dumps({
            "status": "error",
            "message": 'No ready package manager matching "winget"',
        }), "uniget package installed")
    assert exc.value.error_type == "runtime_error"
    assert "No ready package manager" in str(exc.value)


def test_parse_uniget_managers_keeps_readiness():
    text = json.dumps({"status": "success", "managers": [
        {"name": "scoop", "displayName": "Scoop", "enabled": True, "ready": True,
         "executablePath": "S:\\Libs\\Scoop\\shims\\scoop.cmd"},
        {"name": "winget", "displayName": "WinGet", "enabled": True, "ready": False,
         "executablePath": ""},
        {"displayName": "缺 name"},
    ]})
    rows = pkgman.parse_uniget_managers(text)["rows"]
    assert [r["name"] for r in rows] == ["scoop", "winget"]
    assert rows[0]["ready"] is True and rows[0]["executablePath"].endswith("scoop.cmd")
    assert rows[1]["ready"] is False and "executablePath" not in rows[1]


def test_parse_uniget_managers_requires_managers_array():
    with pytest.raises(PackageError):
        pkgman.parse_uniget_managers('{"status": "success", "sources": []}')


def test_parse_uniget_sources_keeps_configuration_state():
    text = json.dumps({"status": "success", "sources": [
        {"manager": "scoop", "name": "extras", "displayName": "Scoop: extras",
         "url": "https://github.com/ScoopInstaller/Extras", "packageCount": 2449,
         "updateDate": "2026-10-31 16:24:39", "isKnown": True, "isConfigured": True},
        {"manager": "scoop", "name": "games", "displayName": "Scoop: games",
         "url": "", "packageCount": 0, "updateDate": "", "isKnown": True,
         "isConfigured": False},
        {"manager": "", "name": "orphan"},
    ]})
    rows = pkgman.parse_uniget_sources(text)["rows"]
    assert [r["name"] for r in rows] == ["extras", "games"]
    assert rows[0]["packageCount"] == 2449 and rows[0]["isConfigured"] is True
    assert rows[1]["url"] is None and rows[1]["updateDate"] is None
    assert rows[1]["isConfigured"] is False


def test_parse_uniget_details_allowlists_fields_and_keeps_lists():
    text = json.dumps({"status": "success", "package": {
        "name": "7zip", "id": "7zip", "version": "25.01", "newVersion": "",
        "source": "Scoop: main", "manager": "scoop",
        "description": "A multi-format file archiver", "publisher": "",
        "author": "7-zip", "homepageUrl": "https://www.7-zip.org/",
        "license": "LGPL-2.1-or-later", "installerType": "Scoop 软件包",
        "installerSize": 1996800,
        "installLocation": "S:\\Libs\\Scoop\\apps\\7zip\\current",
        "tags": [], "screenshots": ["https://example.com/1.png"],
        "dependencies": [], "versions": [], "unknownField": "should not leak",
    }})
    pkg = pkgman.parse_uniget_details(text)["package"]
    assert pkg["name"] == "7zip"
    assert pkg["installerSize"] == 1996800
    assert pkg["screenshots"] == ["https://example.com/1.png"]
    assert pkg["tags"] == []
    assert "unknownField" not in pkg
    assert pkg["publisher"] is None


def test_parse_uniget_details_requires_package_object():
    with pytest.raises(PackageError) as exc:
        pkgman.parse_uniget_details('{"status": "success", "packages": []}')
    assert "package 对象" in str(exc.value)


def test_parse_uniget_versions_caps_and_counts(monkeypatch):
    text = json.dumps({"status": "success", "versions": ["1.0", "1.1", "", "2.0"]})
    res = pkgman.parse_uniget_versions(text)
    assert res == {"versions": ["1.0", "1.1", "2.0"], "count": 3, "truncated": False}

    monkeypatch.setattr(pkgman, "MAX_VERSION_ROWS", 1)
    brief = pkgman.parse_uniget_versions(
        json.dumps({"status": "success", "versions": ["1.0", "1.1"]})
    )
    assert brief == {"versions": ["1.0"], "count": 2, "truncated": True}


def test_parse_uniget_versions_requires_versions_array():
    with pytest.raises(PackageError) as exc:
        pkgman.parse_uniget_versions('{"status": "success", "versions": "1.0"}')
    assert "versions 数组" in str(exc.value)


# ---- run_uniget_read：先探会话，再发数据命令 ----


def _scripted_uniget(monkeypatch, *, exe="C:\\Fake\\uniget.exe", running=True,
                     query_ok=True, exit_code=0, stdout='{"status": "success"}',
                     truncated=False):
    calls: list[dict] = []

    async def _run(argv, timeout=None, max_output=pkgman.OUTPUT_MAX_CHARS):
        calls.append({"argv": list(argv), "timeout": timeout, "maxOutput": max_output})
        if argv[-1] == "status":
            return {"ok": True, "exitCode": 0, "timedOut": False,
                    "stdoutTruncated": False,
                    "stdout": json.dumps({"running": running}), "stderr": ""}
        return {"ok": query_ok, "exitCode": exit_code, "timedOut": False,
                "stdoutTruncated": truncated, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(pkgman, "run_argv", _run)
    monkeypatch.setattr(pkgman, "locate_uniget_cli", lambda: (exe, "test: 单测入口"))
    return calls


def test_run_uniget_read_probes_status_then_uses_the_json_cap(monkeypatch):
    calls = _scripted_uniget(
        monkeypatch, stdout=json.dumps({"status": "success", "packages": []}),
    )
    res = run(pkgman.run_uniget_read(
        lambda exe: pkgman.uniget_installed_argv(exe, "scoop"),
        timeout=45,
        parse=pkgman.parse_uniget_packages,
    ))
    assert res["available"] is True and res["phase"] is None
    assert [c["argv"][-1] for c in calls] == ["status", "scoop"]
    assert [c["timeout"] for c in calls] == [45, 45], "timeout 要传给两条命令"
    assert calls[1]["maxOutput"] == pkgman.UNIGET_OUTPUT_MAX_CHARS
    assert res["data"] == {"rows": []}


def test_run_uniget_read_stops_after_status_when_no_session(monkeypatch):
    calls = _scripted_uniget(monkeypatch, running=False)
    res = run(pkgman.run_uniget_read(pkgman.uniget_managers_argv))
    assert [c["argv"][-1] for c in calls] == ["status"], "无会话时不许再发第二条"
    assert res["available"] is False and res["phase"] == "status"
    assert "running=false" in res["reason"] and "不代为冷启动" in res["reason"]
    assert res["exe"] == "C:\\Fake\\uniget.exe"


def test_run_uniget_read_missing_cli_is_locate_phase(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("定位失败时不该 spawn")

    monkeypatch.setattr(pkgman, "run_argv", _boom)
    monkeypatch.setattr(pkgman, "locate_uniget_cli", lambda: (None, "没找到 uniget.exe"))
    res = run(pkgman.run_uniget_read(pkgman.uniget_managers_argv))
    assert res["available"] is False and res["phase"] == "locate"
    assert "没找到 uniget.exe" in res["reason"] and res["exe"] is None


def test_run_uniget_read_status_failure_reports_exit_code(monkeypatch):
    async def _run(argv, timeout=None, max_output=pkgman.OUTPUT_MAX_CHARS):
        return {"ok": False, "exitCode": 1, "timedOut": False,
                "stdoutTruncated": False, "stdout": "", "stderr": "pipe unavailable"}

    monkeypatch.setattr(pkgman, "run_argv", _run)
    monkeypatch.setattr(pkgman, "locate_uniget_cli",
                        lambda: ("C:\\Fake\\uniget.exe", "test"))
    res = run(pkgman.run_uniget_read(pkgman.uniget_managers_argv))
    assert res["phase"] == "status"
    assert "退出码 1" in res["reason"] and "pipe unavailable" in res["reason"]


def test_run_uniget_read_query_failure_keeps_the_message(monkeypatch):
    _scripted_uniget(
        monkeypatch, query_ok=False, exit_code=2,
        stdout='{"status": "error", "message": "The IPC API token is not available."}',
    )
    res = run(pkgman.run_uniget_read(pkgman.uniget_managers_argv))
    assert res["phase"] == "query"
    assert "退出码 2" in res["reason"] and "IPC API token" in res["reason"]


def test_run_uniget_read_nonzero_exit_still_classifies_json_errors(monkeypatch):
    """真机 not-found：退出码 2 + `status=error` 正文，不能一律判 runtime_error"""
    _scripted_uniget(
        monkeypatch, query_ok=False, exit_code=2,
        stdout=json.dumps({
            "status": "error",
            "message": 'No package matching id "nope" was found.',
        }),
    )
    res = run(pkgman.run_uniget_read(
        lambda exe: pkgman.uniget_details_argv(exe, "scoop", "nope"),
        parse=pkgman.parse_uniget_details,
    ))
    assert res["phase"] == "query"
    assert res["errorType"] == ErrorType.NOT_FOUND
    assert "退出码 2" in res["reason"]


def test_run_uniget_read_truncated_output_degrades_with_a_next_step(monkeypatch):
    _scripted_uniget(
        monkeypatch, truncated=True,
        stdout='{"status": "success", "packages": [{"id": "x"',
    )
    res = run(pkgman.run_uniget_read(
        lambda exe: pkgman.uniget_installed_argv(exe, "scoop"),
        parse=pkgman.parse_uniget_packages,
    ))
    assert res["phase"] == "parse"
    assert "被截断" in res["reason"] and "manager" in res["reason"]


def test_run_uniget_read_parse_failure_is_its_own_phase(monkeypatch):
    _scripted_uniget(monkeypatch, stdout="not json")
    res = run(pkgman.run_uniget_read(
        pkgman.uniget_managers_argv,
        parse=pkgman.parse_uniget_managers,
    ))
    assert res["phase"] == "parse" and "不是 JSON" in res["reason"]
    assert res["available"] is False and res["data"] is None
