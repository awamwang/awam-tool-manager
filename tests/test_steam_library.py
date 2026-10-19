"""Steam 库解析（`src/steam_library.py`）的守卫。

真机上只有两类事实值得钉：

1. **VDF 的形态**。acf 与 vdf 都是 Valve 自己的嵌套键值格式，制表符缩进、
   键值同行、块用 `{}`，标准库没有解析器。解析器一坏，整份游戏清单就空了，
   而「空清单」在调用方看来与「没装游戏」完全一样——所以畸形输入必须抛
   `SteamError`，不能返回半份结果。
2. **「vdf 声明了」不等于「盘上装着」**。真机实测 3 个库 / 27 个声明 appid，
   其中 `E:\\Games\\SteamLibrary` 目录已不存在，盘上只有 16 份 acf。清单以
   acf 为准、缺口单独报，这条口径一旦被改回去，就会出现 11 条点不动的假游戏。

全部用 `tmp_path` 里的合成 Steam 树，不碰真机 Steam。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import steam_library
from steam_library import SteamError, installed_games, parse_vdf, read_manifest, resolve_root


# ---- 合成布局 ----


def acf_text(appid: str, name: str, *, size: str = "1048576", last_played: str = "0",
             extra: dict[str, str] | None = None) -> str:
    """按真机 acf 的缩进形态生成（键前一个制表符、值再两个制表符，块另起两行）"""
    fields = {
        "appid": appid,
        "Universe": "1",
        "name": name,
        "StateFlags": "4",
        "installdir": name.replace(" ", ""),
        "LastUpdated": "1790923050",
        "LastPlayed": last_played,
        "SizeOnDisk": size,
        "buildid": "24098430",
    }
    fields.update(extra or {})
    lines = ['"AppState"', "{"]
    lines += [f'\t"{k}"\t\t"{v}"' for k, v in fields.items()]
    # 真机 acf 里还有 InstalledDepots / UserConfig 这类嵌套块：解析器必须走得出块，
    # 否则后面的键会全被当成一个值吞掉
    lines += [
        '\t"InstalledDepots"',
        "\t{",
        '\t\t"100"',
        "\t\t{",
        '\t\t\t"manifest"\t\t"3966345552745568756"',
        '\t\t\t"size"\t\t"13699237"',
        "\t\t}",
        "\t}",
        "}",
    ]
    return "\n".join(lines) + "\n"


def vdf_text(libraries: list[tuple[str, str, list[str]]]) -> str:
    """libraries: [(index, path, appids)]；路径按真机写法带双反斜杠"""
    lines = ['"libraryfolders"', "{"]
    for index, path, appids in libraries:
        lines += [f'\t"{index}"', "\t{", f'\t\t"path"\t\t"{path}"', '\t\t"label"\t\t""']
        if appids:
            lines += ["\t\t\"apps\"", "\t\t{"]
            lines += [f'\t\t\t"{appid}"\t\t"1"' for appid in appids]
            lines += ["\t\t}"]
        lines += ["\t}"]
    lines += ["}"]
    return "\n".join(lines) + "\n"


@pytest.fixture()
def steam(tmp_path: Path) -> Path:
    """两库在盘、一库失踪的合成 Steam——正是真机 3/2 的形状"""
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "steam.exe").write_text("fake", encoding="utf-8")
    (root / "steamapps" / "appmanifest_400040.acf").write_text(
        acf_text("400040", "ShareX"), encoding="utf-8"
    )
    (root / "steamapps" / "appmanifest_477160.acf").write_text(
        acf_text("477160", "Human Fall Flat", size="6845503833"), encoding="utf-8"
    )
    second = tmp_path / "SteamLibrary"
    (second / "steamapps").mkdir(parents=True)
    (second / "steamapps" / "appmanifest_1509960.acf").write_text(
        acf_text("1509960", "PICO PARK"), encoding="utf-8"
    )
    gone = tmp_path / "Games" / "SteamLibrary"  # 故意不创建：真机 E 盘那个就是这个状态
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        vdf_text([
            ("0", str(root).replace("\\", "\\\\"), ["400040", "477160"]),
            ("1", str(second).replace("\\", "\\\\"), ["1509960"]),
            ("2", str(gone).replace("\\", "\\\\"), ["570", "730", "1245620"]),
        ]),
        encoding="utf-8",
    )
    return root


# ---- VDF 解析 ----


def test_parse_vdf_reads_nested_blocks():
    doc = parse_vdf(acf_text("400040", "ShareX"))
    state = doc["AppState"]
    assert state["appid"] == "400040"
    assert state["name"] == "ShareX"
    # 嵌套块解析成 dict，不会把块后的键吞掉
    assert state["InstalledDepots"]["100"]["manifest"] == "3966345552745568756"


def test_parse_vdf_unescapes_windows_paths():
    doc = parse_vdf('"x"\n{\n\t"path"\t\t"S:\\\\Program Files (x86)\\\\Steam"\n}')
    assert doc["x"]["path"] == r"S:\Program Files (x86)\Steam"


def test_parse_vdf_keeps_values_as_strings():
    """appid / size 在源文件里就是字符串，本模块不做数值转换（口径与 pkgman 一致）"""
    doc = parse_vdf('"AppState"\n{\n\t"SizeOnDisk"\t\t"318658802"\n}')
    assert doc["AppState"]["SizeOnDisk"] == "318658802"


def test_parse_vdf_rejects_truncated_document():
    with pytest.raises(SteamError):
        parse_vdf('"AppState"\n{\n\t"appid"\t\t"1"\n')  # 缺右括号


def test_parse_vdf_rejects_valueless_key():
    with pytest.raises(SteamError):
        parse_vdf('"AppState"\n{\n\t"name"\n}')


def test_parse_vdf_rejects_block_without_key():
    with pytest.raises(SteamError):
        parse_vdf('{\n\t"a"\t\t"b"\n}')


def test_parse_vdf_accepts_explicit_empty_block():
    """真机没见过 `"key" {}`，但它语法无歧义：括号成对写全了，就是空对象。

    与「键后面没有值」「块缺右括号」的区别是那两个只能来自截断或格式变更，
    必须失败；这条不会与任何损坏形态混淆。
    """
    assert parse_vdf('"x"\n{\n\t"y"\t\t{}\n}') == {"x": {"y": {}}}


# ---- 定位根目录 ----


def test_resolve_root_prefers_entry_config(steam, monkeypatch):
    monkeypatch.setattr(steam_library, "registry_steam_values",
                        lambda: {"SteamPath": str(steam)})
    other = steam.parent / "Other"
    other.mkdir()
    root, source = resolve_root(str(other))
    assert root == other
    assert source == "条目 launcher.dataDir"


def test_resolve_root_normalizes_forward_slashes(monkeypatch, tmp_path):
    """真机登记值写作 `s:/program files (x86)/steam`，正斜杠也得能用"""
    steam = tmp_path / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    monkeypatch.setattr(steam_library, "registry_steam_values",
                        lambda: {"SteamPath": str(steam).replace("\\", "/")})
    root, source = resolve_root()
    assert root == steam
    assert source == "注册表 SteamPath"


def test_resolve_root_falls_back_when_registry_unreadable(monkeypatch, steam):
    monkeypatch.setattr(steam_library, "registry_steam_values", lambda: {})
    monkeypatch.setattr(steam_library, "FALLBACK_DIRS", (str(steam),))
    root, source = resolve_root()
    assert root == steam
    # 兜底来源必须在说明里写明是猜的，不能伪装成登记值
    assert "未经登记值确认" in source


def test_resolve_root_reports_every_attempt_when_nothing_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(steam_library, "registry_steam_values", lambda: {})
    monkeypatch.setattr(steam_library, "FALLBACK_DIRS", (str(tmp_path / "nope"),))
    root, source = resolve_root("Z:/missing")
    assert root is None
    assert "条目 launcher.dataDir" in source and "穷举兜底" in source


# ---- 已装清单 ----


def test_installed_games_uses_manifests_not_vdf(steam):
    data = installed_games(steam)
    assert data["count"] == 3, "两库在盘：acf 一共 3 份"
    assert sorted(g["appid"] for g in data["games"]) == ["1509960", "400040", "477160"]


def test_missing_library_is_reported_but_never_becomes_games(steam):
    """失踪库的 3 个 appid 只进缺口统计，绝不进清单——那是 11 条点不动的假游戏"""
    data = installed_games(steam)
    assert data["declaredAppTotal"] == 6  # 2 + 1 + 失踪库的 3
    assert all(g["appid"] not in {"570", "730", "1245620"} for g in data["games"])
    missing = data["missingLibraries"]
    assert len(missing) == 1
    assert missing[0]["declaredApps"] == 3
    assert "不计入 games" in data["note"]


def test_libraries_expose_exists_per_entry(steam):
    data = installed_games(steam)
    flags = {lib["index"]: lib["exists"] for lib in data["libraries"]}
    assert flags == {"0": True, "1": True, "2": False}


def test_game_rows_carry_launch_and_auditing_fields(steam):
    sharex = next(g for g in installed_games(steam)["games"] if g["appid"] == "400040")
    assert sharex["name"] == "ShareX"
    assert sharex["library"] == str(steam)
    assert sharex["manifest"] == "appmanifest_400040.acf"
    assert sharex["sizeOnDisk"] == "1048576"


def test_unreadable_manifest_is_skipped_with_reason(tmp_path):
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "appmanifest_1.acf").write_text("not vdf at all", encoding="utf-8")
    (root / "steamapps" / "appmanifest_2.acf").write_text(acf_text("2", "Good"), encoding="utf-8")
    data = installed_games(root)
    assert data["count"] == 1
    assert data["games"][0]["name"] == "Good"
    assert any("结构不认识" in row["reason"] for row in data["missingLibraries"])


def test_manifest_without_appstate_is_none():
    assert read_manifest(Path("does-not-exist.acf")) is None


def test_missing_libraryfolders_falls_back_to_main_only(tmp_path):
    """老 Steam / 从未加过库：没有 vdf 也要能出主库清单，并说明只扫了主库"""
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "appmanifest_400040.acf").write_text(
        acf_text("400040", "ShareX"), encoding="utf-8"
    )
    data = installed_games(root)
    assert data["count"] == 1
    assert data["libraryFolders"] == "missing"
    assert "只扫主库" in data["note"]


def test_vdf_present_but_unrecognized_raises(tmp_path):
    """vdf 在、却一个库都没解析出来：格式变了就明确失败。

    静默退回「只扫主库」会把「多盘游戏突然全搜不到」伪装成正常结果，
    那种缺失看起来完全合理，最难查。
    """
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n}', encoding="utf-8"
    )
    with pytest.raises(SteamError):
        installed_games(root)


def test_installed_games_sorts_by_name_then_appid(steam):
    names = [g["name"] for g in installed_games(steam)["games"]]
    assert names == sorted(names, key=str.lower)
