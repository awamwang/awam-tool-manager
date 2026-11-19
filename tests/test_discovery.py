"""P1 本机工具自动发现：`discovery.py` 单元面 + `discover_tools` 集成面。

**全部探测跑在 tmp_path 合成数据上，不碰用户真机**（本期授权口径：代码先行，
真机结果由客户端调 `discover_tools` 得到）。因此清单里的路径一律用自定义
token `{AWAM_FAKE_ROOT}`，由 fixture 把它 setenv 到 tmp 目录；真机用的
`{ProgramFiles}` 之类只出现在仓库清单文件里，本文件只校验它能通过校验、
不执行探测。

两条最重要的纪律各自有用例钉住：
1. **默认零 spawn**——`run_commands` 不为 true 时 `subprocess.run` 换成
   一调用就 raise 的桩，跑到了就是红；
2. **没检查成 ≠ 没装**——环境变量展开失败必须判 inconclusive/unknown，
   不能降级成 not_installed。
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import discovery  # noqa: E402
import server  # noqa: E402
from errors import ToolError  # noqa: E402


TOKEN = "AWAM_FAKE_ROOT"
TEMPLATE = "{" + TOKEN + "}"
MISSING_TOKEN = "{AWAM_DEFINITELY_UNSET_VAR}"


def run(coro):
    """统一的事件循环入口（本仓库不装 pytest-asyncio）"""
    return asyncio.run(coro)


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """把清单模板的 {AWAM_FAKE_ROOT} 指到 tmp，返回该目录"""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv(TOKEN, str(root))
    return root


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """把 PATH 换成一个空 tmp 目录，返回它（要「命令可用」就在里面造文件）

    Windows 的 `shutil.which` 按 PATHEXT 补后缀试，POSIX 只看裸名，所以
    `make_cli` 同时留裸名和 `.cmd`，两个平台都能命中。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def make_cli(bin_dir: Path, name="fakecli") -> Path:
    exe = bin_dir / name
    exe.write_text("", encoding="utf-8")
    bin_dir.joinpath(name + ".cmd").write_text("", encoding="utf-8")
    return exe


class FakeRegistry:
    """只提供 `list_all_tools()` 的最小对照面"""

    def __init__(self, ids=()):
        self._ids = list(ids)

    def list_all_tools(self):
        return [{"id": i} for i in self._ids]


def cand(tool_id, *, name=None, kind=None, purpose=None, probes=(), command=None):
    data = {"tool_id": tool_id, "name": name or tool_id.title(), "probes": list(probes)}
    if kind is not None:
        data["kind"] = kind
    if purpose is not None:
        data["purpose"] = purpose
    if command is not None:
        data["command"] = command
    return data


def manifest_of(*cands):
    return {"version": 1, "candidates": list(cands)}


def exists_probe(*paths, **kw):
    data = {"probe": "path_exists", "paths": list(paths)}
    data.update(kw)
    return data


# ---------------- 清单加载与校验 ----------------


def test_missing_manifest_file_is_empty_not_error(tmp_path):
    """零配置 = 没有候选，而不是报错：文件不存在是本期的合法状态"""
    m = discovery.load_manifest(tmp_path / "nope.json")
    assert m == {"version": 1, "candidates": []}


def test_env_var_overrides_default_path(tmp_path, monkeypatch):
    path = tmp_path / "custom.json"
    path.write_text(json.dumps(manifest_of(cand("alpha"))), encoding="utf-8")
    monkeypatch.setenv(discovery.DISCOVERY_ENV_VAR, str(path))
    assert discovery.resolve_discovery_path() == path
    assert [c["tool_id"] for c in discovery.load_manifest()["candidates"]] == ["alpha"]


def test_write_error_json_raises_discovery_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(discovery.DiscoveryError, match="不是合法 JSON"):
        discovery.load_manifest(path)


@pytest.mark.parametrize(
    "data, expected",
    [
        ([], "必须是 JSON 对象"),
        ({"candidates": {}}, "candidates 数组"),
        ({"candidates": ["x"]}, "必须是对象"),
        ({"candidates": [{"tool_id": "", "name": "A"}]}, "tool_id 非法"),
        (
            {"candidates": [cand("dup"), cand("dup")]},
            "tool_id 重复: dup",
        ),
        ({"candidates": [{"tool_id": "a", "name": "  "}]}, "缺少 name"),
        ({"candidates": [cand("a", kind="cli")]}, "kind 取值非法"),
        ({"candidates": [cand("a", probes=[{"probe": "registry", "path": "x"}])]},
         "探测类型不支持"),
        ({"candidates": [cand("a", probes=[{"probe": "path_exists"}])]},
         "至少要给一个 path"),
        ({"candidates": [cand("a", probes=["x"])]}, "必须是对象"),
        ({"candidates": [cand("a", command={"argv": []})]}, "command.argv 非法"),
        ({"candidates": [cand("a", command={"argv": ["x", "  "]})]}, "command.argv 非法"),
        ({"candidates": [cand("a", command="x")]}, "command 必须是对象"),
        ({"candidates": [cand("a", command={"argv": ["x"], "version_pattern": "("})]},
         "不是合法正则"),
    ],
)
def test_validate_manifest_rejects(data, expected):
    assert expected in discovery.validate_manifest(data)


def test_validate_manifest_accepts_good_manifest(fake_root):
    m = manifest_of(
        cand("alpha", kind="app", purpose="演示",
             probes=[exists_probe(f"{TEMPLATE}/alpha")],
             command={"argv": ["alpha", "--version"], "version_pattern": r"([0-9.]+)"}),
        cand("beta", probes=[{"probe": "dir_count", "path": f"{TEMPLATE}/b", "limit": 5}]),
    )
    assert discovery.validate_manifest(m) is None


def test_repo_shipped_manifest_validates_without_probing():
    """仓库里人工维护的 data/discovery.json 必须过校验（只读文件，不探测）

    这条是清单拼写错误（重复 id、正则坏了、kind 造词）的 CI 守卫：
    错一个字段，真机上那次 `discover_tools` 就会 config_error。
    """
    data = json.loads((ROOT / "data" / "discovery.json").read_text(encoding="utf-8"))
    assert discovery.validate_manifest(data) is None
    assert data["candidates"], "仓库清单不该是空的"


def test_repo_manifest_probes_only_declared_env_tokens():
    """仓库清单里出现的 token 必须是 Windows 真实存在的环境变量名

    拼错的 token 不会报错，只会让该位置「没检查成」→ unknown，
    发现面无声缩小。所以扫的是**路径字符串里的 token**，不是整份文本里的
    花括号（后者会把 JSON 对象括号和正则量词也算进去）。
    """
    import re as _re

    data = json.loads((ROOT / "data" / "discovery.json").read_text(encoding="utf-8"))
    templates = []
    for c in data["candidates"]:
        for probe in c.get("probes", []):
            paths = probe.get("paths") or [probe.get("path")]
            templates.extend(p for p in paths if isinstance(p, str))
    assert templates, "仓库清单该用 {ENV} 模板而不是硬编码盘符"

    tokens = set()
    for template in templates:
        tokens.update(_re.findall(r"\{([^{}]*)\}", template))
    allowed = {
        "SCOOP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramFiles",
        "ProgramFiles(X86)", "SystemRoot",
    }
    assert tokens <= allowed, f"未登记的环境变量名: {sorted(tokens - allowed)}"


# ---------------- expand_path ----------------


def test_expand_path_resolves_env_token(fake_root):
    path, unresolved = discovery.expand_path(f"{TEMPLATE}/sub/dir")
    assert unresolved is None
    assert path == fake_root / "sub" / "dir"


def test_expand_path_missing_env_is_not_a_relative_path(monkeypatch):
    """没设环境变量必须报「没设」，不能退化成相对路径

    退化会把「本机没这个变量」伪装成「这个路径不存在」，两者在结论里必须分开。
    """
    monkeypatch.delenv("AWAM_DEFINITELY_UNSET_VAR", raising=False)
    path, unresolved = discovery.expand_path(MISSING_TOKEN + "/apps")
    assert path is None
    assert unresolved == "AWAM_DEFINITELY_UNSET_VAR"


def test_expand_path_rejects_unrecognised_token():
    """花括号里是不合法 token 时同样按「没展开」处理，绝不拿字面量去试存在性"""
    path, unresolved = discovery.expand_path("{not a var!}/apps")
    assert path is None
    assert unresolved is not None and "not a var" in unresolved


def test_expand_path_supports_windows_parenthesised_names():
    """`ProgramFiles(x86)` 是 Windows 真实变量名，括号不能当成字面量"""
    if not sys.platform.startswith("win"):
        pytest.skip("该变量名只存在于 Windows")
    path, unresolved = discovery.expand_path("{ProgramFiles(x86)}")
    assert unresolved is None if path is not None else "ProgramFiles(x86)" in unresolved


# ---------------- 注册表回退（MCP 宿主不给全环境时） ----------------


class FakeHandle:
    def __init__(self, module, hive, key):
        self._module = module
        self.hive = hive
        self.key = key

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """只覆盖 `discovery._read_reg_string` 用到的四个入口

    `raises` 按 `(hive, key, name)` 抛指定异常，用来模拟「键存在但读不到」
    （权限/视图问题）——这条路径必须和「值不存在」分开，见注册表探针用例。
    """

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, values=None, expand_result=None, raises=None):
        self.values = dict(values or {})
        self.expand_result = expand_result
        self.raises = dict(raises or {})
        self.queries: list[tuple] = []

    def OpenKey(self, hive, key):
        return FakeHandle(self, hive, key)

    def QueryValueEx(self, handle, name):
        target = (handle.hive, handle.key, name)
        self.queries.append(target)
        if target in self.raises:
            raise self.raises[target]
        got = self.values.get(target)
        if got is None:
            raise FileNotFoundError(name)  # OSError 的子类，与真实缺值同路径
        return got

    def ExpandEnvironmentStrings(self, raw):
        return raw if self.expand_result is None else self.expand_result


def test_expand_path_falls_back_to_registry_when_process_env_lacks_token(monkeypatch):
    """宿主没给变量 ≠ 本机没这个目录：注册表是第二来源

    真机首跑的 4 个 `unknown` 全因 `mcp` SDK 只把「安全变量」传给子进程，
    `ProgramFiles` 与用户自定义的 `SCOOP` 都不在其中。
    """
    monkeypatch.delenv("AWAM_DEFINITELY_UNSET_VAR", raising=False)
    monkeypatch.setattr(discovery.os, "name", "nt")
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg({
            ("HKCU", "Environment", "AWAM_DEFINITELY_UNSET_VAR"): (
                "S:\\Libs\\Scoop",
                FakeWinreg.REG_SZ,
            )
        }),
    )

    path, unresolved = discovery.expand_path("{AWAM_DEFINITELY_UNSET_VAR}/apps")
    assert unresolved is None
    assert path == Path("S:\\Libs\\Scoop" + "/apps")


def test_expand_path_process_env_wins_over_registry(fake_root, monkeypatch):
    monkeypatch.setattr(discovery.os, "name", "nt")
    fake = FakeWinreg({
        ("HKCU", "Environment", TOKEN): ("D:\\from\\registry", FakeWinreg.REG_SZ)
    })
    monkeypatch.setattr(discovery, "winreg", fake)

    path, unresolved = discovery.expand_path(f"{TEMPLATE}/sub")
    assert unresolved is None and path == fake_root / "sub"
    assert fake.queries == [], "进程环境已有值，不该再去读注册表"


def test_expand_path_refuses_partially_expanded_registry_value(monkeypatch):
    """`REG_EXPAND_SZ` 展开后仍残留 `%…%` 就当取不到，不拿半截变量名当路径"""
    monkeypatch.delenv("AWAM_DEFINITELY_UNSET_VAR", raising=False)
    monkeypatch.setattr(discovery.os, "name", "nt")
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg(
            {
                ("HKCU", "Environment", "AWAM_DEFINITELY_UNSET_VAR"): (
                    "%AWAM_OTHER_UNSET%\\apps",
                    FakeWinreg.REG_EXPAND_SZ,
                )
            }
        ),
    )

    path, unresolved = discovery.expand_path("{AWAM_DEFINITELY_UNSET_VAR}")
    assert path is None and unresolved == "AWAM_DEFINITELY_UNSET_VAR"


def test_registry_reads_machine_env_after_user_env(monkeypatch):
    monkeypatch.setattr(discovery.os, "name", "nt")
    fake = FakeWinreg({
        ("HKLM", discovery._MACHINE_ENV_KEY, "SCOOP_GLOBAL"): (
            "S:\\Libs\\Scoop",
            FakeWinreg.REG_SZ,
        )
    })
    monkeypatch.setattr(discovery, "winreg", fake)

    assert discovery.registry_env_value("SCOOP_GLOBAL") == "S:\\Libs\\Scoop"
    assert (fake.HKEY_CURRENT_USER, "Environment", "SCOOP_GLOBAL") in fake.queries


def test_program_files_x86_derived_when_official_value_absent(monkeypatch):
    """x86 目录值缺失时按系统惯例从 64 位目录推导（32 位系统上就长这样）"""
    monkeypatch.setattr(discovery.os, "name", "nt")
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg({
            (
                "HKLM",
                discovery._CURRENT_VERSION_KEY,
                "ProgramFilesDir",
            ): ("C:\\Program Files", FakeWinreg.REG_SZ)
        }),
    )

    assert discovery.registry_env_value("ProgramFiles") == "C:\\Program Files"
    assert discovery.registry_env_value("ProgramFiles(x86)") == "C:\\Program Files (x86)"


def test_registry_not_consulted_off_windows(monkeypatch):
    monkeypatch.setattr(discovery.os, "name", "posix")
    monkeypatch.setattr(discovery, "winreg", FakeWinreg())

    assert discovery.registry_env_value("ProgramFiles") is None


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="注册表只在 Windows 上")
def test_program_files_resolves_on_windows_without_the_env_var(monkeypatch):
    """摘掉进程环境变量后仍能展开成真目录（本机的实然结论，不是替身）"""
    monkeypatch.delenv("ProgramFiles", raising=False)
    path, unresolved = discovery.expand_path("{ProgramFiles}")
    assert unresolved is None, f"注册表回退未生效：{unresolved}"
    assert path.name.lower() == "program files"


# ---------------- registry_value 探针（不依赖盘符的权威登记） ----------------


def reg_probe(**kw):
    data = {"probe": "registry_value"}
    data.update(kw)
    return data


STEAM_KEY = "Software\\Valve\\Steam"


def test_registry_value_hit_uses_the_registered_path(fake_root, monkeypatch):
    """登记值本身就是安装位置：读它，不用穷举「装在哪个盘」

    值按真机 Steam 的形态给——正斜杠、盘符小写（`s:/program files (x86)/steam/steam.exe`），
    所以探针必须先归一分隔符再试存在性。
    """
    exe = fake_root / "steam.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg({("HKCU", STEAM_KEY, "SteamExe"): (str(exe).replace("\\", "/"), FakeWinreg.REG_SZ)}),
    )

    evidence, verdict = discovery.run_probes(
        cand("steam", probes=[reg_probe(key=STEAM_KEY, value="SteamExe")])
    )
    assert verdict == discovery.VERDICT_HIT
    row = evidence[0]
    assert row["ok"] is True
    assert Path(row["path"]).name == "steam.exe"
    assert row["key"] == f"HKCU\\{STEAM_KEY}"


def test_registry_value_absent_is_miss(fake_root, monkeypatch):
    """键/值不存在是事实信号（这个登记记录没有），不是「没检查成」"""
    monkeypatch.setattr(discovery, "winreg", FakeWinreg())
    evidence, verdict = discovery.run_probes(
        cand("steam", probes=[reg_probe(key=STEAM_KEY, value="SteamExe")])
    )
    assert verdict == discovery.VERDICT_MISS
    assert "不存在" in evidence[0]["reason"]


def test_registry_value_stale_pointer_is_miss(fake_root, monkeypatch):
    """卸载/搬家后留下的僵尸登记不该被读成「装着」"""
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg({("HKCU", STEAM_KEY, "SteamExe"): ("Q:\\gone\\steam.exe", FakeWinreg.REG_SZ)}),
    )
    evidence, verdict = discovery.run_probes(
        cand("steam", probes=[reg_probe(key=STEAM_KEY, value="SteamExe")])
    )
    assert verdict == discovery.VERDICT_MISS
    assert "失效" in evidence[0]["reason"]


def test_registry_value_unreadable_is_inconclusive(fake_root, monkeypatch):
    """权限/视图问题 ≠ 没装（与「环境变量取不到判 inconclusive」同一条纪律）"""
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg(raises={("HKCU", STEAM_KEY, "SteamExe"): PermissionError("拒绝访问")}),
    )
    _evidence, verdict = discovery.run_probes(
        cand("steam", probes=[reg_probe(key=STEAM_KEY, value="SteamExe")])
    )
    assert verdict == discovery.VERDICT_INCONCLUSIVE


def test_registry_value_unexpanded_is_inconclusive(fake_root, monkeypatch):
    """`REG_EXPAND_SZ` 展开后仍残留 %…% → 没检查成，绝不拿字面量去试存在性"""
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg(
            {("HKCU", STEAM_KEY, "SteamExe"): ("%NOSUCHVAR%\\steam.exe", FakeWinreg.REG_EXPAND_SZ)},
            expand_result="%NOSUCHVAR%\\steam.exe",
        ),
    )
    evidence, verdict = discovery.run_probes(
        cand("steam", probes=[reg_probe(key=STEAM_KEY, value="SteamExe")])
    )
    assert verdict == discovery.VERDICT_INCONCLUSIVE
    assert "未能展开" in evidence[0]["reason"]


def test_registry_value_can_skip_the_path_check(fake_root, monkeypatch):
    """`require_exists=false` 时只问「登记在不在」（版本号这类非路径值）"""
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg({("HKLM", "SOFTWARE\\Valve\\Steam", "Version"): ("1.2.3", FakeWinreg.REG_SZ)}),
    )
    evidence, verdict = discovery.run_probes(
        cand(
            "steam",
            probes=[reg_probe(hive="HKLM", key="SOFTWARE\\Valve\\Steam", value="Version",
                              require_exists=False)],
        )
    )
    assert verdict == discovery.VERDICT_HIT
    assert evidence[0]["data"] == "1.2.3"


def test_registry_value_off_windows_is_inconclusive(monkeypatch):
    monkeypatch.setattr(discovery, "winreg", None)
    evidence, verdict = discovery.run_probes(
        cand("steam", probes=[reg_probe(key=STEAM_KEY, value="SteamExe")])
    )
    assert verdict == discovery.VERDICT_INCONCLUSIVE
    assert "非 Windows" in evidence[0]["reason"]


def test_registry_hit_rescues_a_missed_declared_path(fake_root, monkeypatch):
    """假阴性回归：装在非常规盘时，声明位置全 miss，登记值必须把它救回 installed

    真机第一版清单对 Steam 判的是 `not_installed`（中信心）——它实际装在
    `S:\\Program Files (x86)\\Steam`，而清单只声明了 `C:\\Program Files` 两处。
    """
    exe = fake_root / "steam.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv(TOKEN, str(fake_root / "Steam"))
    monkeypatch.setattr(
        discovery,
        "winreg",
        FakeWinreg({("HKCU", STEAM_KEY, "SteamExe"): (str(exe), FakeWinreg.REG_SZ)}),
    )
    result = discovery.discover(
        manifest_of(
            cand(
                "steam",
                kind="app",
                probes=[
                    reg_probe(key=STEAM_KEY, value="SteamExe"),
                    exists_probe(f"{TEMPLATE}\\Steam\\steam.exe"),
                ],
            )
        ),
        FakeRegistry(),
    )
    row = result["candidates"][0]
    assert row["status"] == "installed"
    assert row["confidence"] == "high"
    assert len(row["evidence"]) == 2
    # 逐行看证据：登记值命中、声明位置落空（不是「没检查成」）
    assert row["evidence"][0]["probe"] == "registry_value"
    assert row["evidence"][0]["key"] == f"HKCU\\{STEAM_KEY}"
    assert row["evidence"][0]["ok"] is True
    assert row["evidence"][1]["probe"] == "path_exists"
    assert row["evidence"][1]["ok"] is False


def test_unwired_probe_kind_never_becomes_a_false_hit(fake_root):
    """没接线的探针类型只能判「没检查成」

    它本来会掉进 path 流程，而 `probe.get("path", "")` 给出的空串经 `Path("")`
    就是当前目录——存在。于是「这个类型压根没实现」会被说成「装着的」。
    """
    evidence, verdict = discovery.run_probes(
        cand("alpha", probes=[{"probe": "registry_value_but_not_wired"}])
    )
    assert verdict == discovery.VERDICT_INCONCLUSIVE
    assert evidence[0]["ok"] is False
    assert "未实现" in evidence[0]["reason"]


@pytest.mark.parametrize(
    "probe, expect",
    [
        (reg_probe(hive="HKEY_CURRENT_USER", key=STEAM_KEY, value="SteamExe"), "hive 取值非法"),
        (reg_probe(key=STEAM_KEY, value="SteamExe"), None),
        (reg_probe(value="SteamExe"), "key 必须是非空字符串"),
        (reg_probe(key=STEAM_KEY), "value 必须是非空字符串"),
        (reg_probe(key=STEAM_KEY, value="SteamExe", require_exists="yes"), "require_exists 必须是布尔"),
        (reg_probe(key=STEAM_KEY, value="  "), "value 必须是非空字符串"),
    ],
)
def test_registry_probe_manifest_validation(probe, expect):
    error = discovery.validate_manifest(manifest_of(cand("steam", probes=[probe])))
    if expect is None:
        assert error is None
    else:
        assert expect in error


def test_repo_steam_candidate_has_a_drive_independent_probe():
    """仓库清单里的 Steam 必须带登记值探针

    只有声明路径的候选会随安装盘符变卦——这条守卫防止「加个路径就完事」的
    修法回到老位置。
    """
    data = json.loads((ROOT / "data" / "discovery.json").read_text(encoding="utf-8"))
    steam = next(c for c in data["candidates"] if c["tool_id"] == "steam")
    kinds = [p["probe"] for p in steam["probes"]]
    assert "registry_value" in kinds
    registry = next(p for p in steam["probes"] if p["probe"] == "registry_value")
    assert (registry["hive"], registry["key"], registry["value"]) == (
        "HKCU",
        STEAM_KEY,
        "SteamExe",
    )


# ---------------- run_probes ----------------


def test_path_exists_hit_reports_path(fake_root):
    (fake_root / "alpha.exe").write_text("", encoding="utf-8")
    evidence, verdict = discovery.run_probes(
        cand("alpha", probes=[exists_probe(f"{TEMPLATE}/alpha.exe")])
    )
    assert verdict == discovery.VERDICT_HIT
    assert evidence[0]["ok"] is True
    assert evidence[0]["path"].endswith("alpha.exe")


def test_path_exists_miss_lists_checked_paths(fake_root):
    evidence, verdict = discovery.run_probes(
        cand("alpha", probes=[exists_probe(f"{TEMPLATE}/nope.exe")])
    )
    assert verdict == discovery.VERDICT_MISS
    assert evidence[0]["ok"] is False
    assert evidence[0]["checked"] == [str(fake_root / "nope.exe")]


def test_second_declared_location_can_hit(fake_root):
    """声明多个候选位置：第一个不存在、第二个存在 → 命中"""
    (fake_root / "b" / "x.exe").parent.mkdir()
    (fake_root / "b" / "x.exe").write_text("", encoding="utf-8")
    _, verdict = discovery.run_probes(
        cand("x", probes=[exists_probe(f"{TEMPLATE}/a/x.exe", f"{TEMPLATE}/b/x.exe")])
    )
    assert verdict == discovery.VERDICT_HIT


def test_unexpanded_location_overrides_miss(fake_root, monkeypatch):
    """有一处没检查成 → 整体 inconclusive，不是 miss

    这是发现类接口最坏的一种错：把「没检查成的位置」当成「那儿没装」。
    """
    monkeypatch.delenv("AWAM_DEFINITELY_UNSET_VAR", raising=False)
    evidence, verdict = discovery.run_probes(
        cand("x", probes=[exists_probe(f"{TEMPLATE}/nope.exe", MISSING_TOKEN + "/real.exe")])
    )
    assert verdict == discovery.VERDICT_INCONCLUSIVE
    assert "环境变量未设置" in evidence[0]["reason"]
    assert discovery.status_of(verdict) == ("unknown", "none")


def test_hit_wins_over_unexpanded_location(fake_root, monkeypatch):
    monkeypatch.delenv("AWAM_DEFINITELY_UNSET_VAR", raising=False)
    (fake_root / "here.exe").write_text("", encoding="utf-8")
    _, verdict = discovery.run_probes(
        cand("x", probes=[exists_probe(f"{TEMPLATE}/here.exe", MISSING_TOKEN + "/y.exe")])
    )
    assert verdict == discovery.VERDICT_HIT


def test_dir_count_reports_entries_sorted_and_skips_dotdirs(fake_root):
    for name in ["zlib", "alpha", ".cache", "mid"]:
        (fake_root / "pkgs" / name).mkdir(parents=True)
    evidence, verdict = discovery.run_probes(
        cand("p", probes=[{"probe": "dir_count", "path": f"{TEMPLATE}/pkgs"}])
    )
    row = evidence[0]
    assert row["ok"] is True and row["count"] == 3
    assert row["entries"] == ["alpha", "mid", "zlib"]
    # 目录里有什么不决定装没装：dir_count 不下结论
    assert verdict == discovery.VERDICT_INCONCLUSIVE


def test_dir_count_limit_is_clamped(fake_root):
    for i in range(5):
        (fake_root / f"d{i}").mkdir()
    row = discovery.run_probes(
        cand("p", probes=[{"probe": "dir_count", "path": TEMPLATE, "limit": 99999}])
    )[0][0]
    assert row["count"] == 5
    negative = discovery.run_probes(
        cand("q", probes=[{"probe": "dir_count", "path": TEMPLATE, "limit": -3}])
    )[0][0]
    assert negative["count"] == 0 and negative["entries"] == []


def test_dir_count_missing_dir_is_ok_false(fake_root):
    evidence, verdict = discovery.run_probes(
        cand("p", probes=[{"probe": "dir_count", "path": f"{TEMPLATE}/absent"}])
    )
    assert evidence[0]["ok"] is False and evidence[0]["reason"] == "目录不存在"
    # 目录不存在同样不决定装没装（清单里 python 就是这种补充探测）
    assert verdict == discovery.VERDICT_INCONCLUSIVE


def test_no_probes_gives_no_verdict(fake_root):
    _, verdict = discovery.run_probes(cand("bare"))
    assert verdict == discovery.VERDICT_INCONCLUSIVE


# ---------------- 结论合成 ----------------


@pytest.mark.parametrize(
    "verdicts, expected",
    [
        ([], discovery.VERDICT_INCONCLUSIVE),
        ([discovery.VERDICT_MISS], discovery.VERDICT_MISS),
        ([discovery.VERDICT_MISS, discovery.VERDICT_HIT], discovery.VERDICT_HIT),
        ([discovery.VERDICT_MISS, discovery.VERDICT_INCONCLUSIVE],
         discovery.VERDICT_INCONCLUSIVE),
        ([discovery.VERDICT_HIT, discovery.VERDICT_INCONCLUSIVE], discovery.VERDICT_HIT),
    ],
)
def test_fold_verdicts(verdicts, expected):
    assert discovery.fold_verdicts(verdicts) == expected


@pytest.mark.parametrize(
    "verdict, status, confidence",
    [
        (discovery.VERDICT_HIT, "installed", "high"),
        (discovery.VERDICT_MISS, "not_installed", "medium"),
        (discovery.VERDICT_INCONCLUSIVE, "unknown", "none"),
    ],
)
def test_status_of_table(verdict, status, confidence):
    assert discovery.status_of(verdict) == (status, confidence)


# ---------------- resolve_executable（真机 WinError 193 回归） ----------------


def make_exec(path: Path) -> Path:
    """造一个「看起来可执行」的文件（POSIX 上 `which` 还要看 X_OK 位）"""
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def test_resolve_replaces_extensionless_shim_with_real_target(fake_bin, monkeypatch):
    """裸名垫片先命中 → 改取同目录带可执行后缀的真身

    真机现象：Scoop 的 shims 目录里同时有给 Git Bash 用的裸名 `scoop` 和
    `scoop.cmd`，`shutil.which` 按裸名先返回前者，实跑报
    `[WinError 193] %1 不是有效的 Win32 应用程序`（pyenv 的 `python` 同因）。
    """
    make_exec(fake_bin / "alpha")
    make_exec(fake_bin / "alpha.cmd")
    monkeypatch.setattr(discovery.os, "name", "nt")
    # 「裸名先命中」这个前提随 Python 版本变：3.13 的 `which(mode=X_OK)`
    # 只按 PATHEXT 补后缀，不再优先返回裸名（真机 `where.exe` 会）。这里
    # 直接把前提桩出来，让断言只测 resolve_executable 的二次解析。
    monkeypatch.setattr(
        discovery.shutil, "which", lambda name: str(fake_bin / "alpha")
    )

    assert Path(discovery.resolve_executable("alpha")).name.lower() == "alpha.cmd"


def test_resolve_off_windows_keeps_which_result(fake_bin, monkeypatch):
    """POSIX 上裸名本来就是正常形态，不做二次解析"""
    make_exec(fake_bin / "alpha")
    make_exec(fake_bin / "alpha.cmd")
    monkeypatch.setattr(discovery.os, "name", "posix")

    assert discovery.resolve_executable("alpha") == discovery.shutil.which("alpha")


def test_resolve_missing_command_is_none(fake_bin):
    assert discovery.resolve_executable("nope") is None


def test_suffix_order_follows_pathext_and_skips_non_executables(fake_bin, monkeypatch):
    """顺序以 PATHEXT 为准；`.py` 这类 CreateProcess 起不来的后缀不参与尝试"""
    for name in ("alpha.exe", "alpha.cmd", "alpha.py"):
        make_exec(fake_bin / name)
    monkeypatch.setattr(discovery.os, "name", "nt")

    monkeypatch.setenv("PATHEXT", ".PY;.CMD;.EXE")
    assert Path(discovery.resolve_executable("alpha")).name.lower() == "alpha.cmd"
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    assert Path(discovery.resolve_executable("alpha")).name.lower() == "alpha.exe"


def test_path_order_beats_suffix_order(tmp_path, monkeypatch):
    """先目录、后后缀（与 cmd.exe 一致）：PATH 靠前的 `.bat` 要赢过靠后的 `.exe`

    真机反例：pyenv 的 `python.bat` 在第 53 个 PATH 项，商店占位 `python.exe` 在
    第 66 项；按后缀外层解析会跳过前者、选中那个 rc=9009 的占位 stub。
    """
    early = tmp_path / "early"
    late = tmp_path / "late"
    early.mkdir()
    late.mkdir()
    make_exec(early / "python")
    make_exec(early / "python.bat")
    make_exec(late / "python.exe")
    monkeypatch.setenv("PATH", os.pathsep.join([str(early), str(late)]))
    monkeypatch.setenv("PATHEXT", ".EXE;.BAT;.CMD")
    monkeypatch.setattr(discovery.os, "name", "nt")

    assert Path(discovery.resolve_executable("python")).parent == early


def test_resolve_keeps_which_hit_when_no_executable_sibling(fake_bin, monkeypatch):
    """补不到真身就照旧交回裸名：`available` 不降级，失败由实跑如实报出"""
    make_exec(fake_bin / "alpha")
    monkeypatch.setattr(discovery.os, "name", "nt")
    # 同前：which 命中裸名这一前提显式桩出，避免依赖 Python/PATHEXT 的选择顺序
    monkeypatch.setattr(
        discovery.shutil, "which", lambda name: str(fake_bin / "alpha")
    )

    assert Path(discovery.resolve_executable("alpha")).name == "alpha"


# ---------------- 命令面 ----------------


def test_command_availability_does_not_spawn(fake_bin, monkeypatch):
    """只查 PATH 不 spawn：available 是默认路径就能给出的证据

    用桩记录调用次数，而不是让桩 raise——断言「一次都没被调用」才是本用例
    要证的性质（raise 型桩会把「根本没调用」和「调用后炸」混成一个红法）。
    """
    calls = []
    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **k: calls.append(a))
    make_cli(fake_bin, "alpha")

    row = discovery.probe_command(cand("a", command={"argv": ["alpha", "--version"]}))
    assert calls == []
    assert row["preview"] == "alpha --version"
    assert row["run"] is False and row["available"] is True
    # 不钉 which 选中的具体垫片名（Windows 按 PATHEXT 补后缀，裸名与 .cmd 都算命中）
    assert Path(row["path"]).parent == fake_bin


def test_command_availability_resolves_absolute_path(fake_bin):
    make_cli(fake_bin, "alpha")
    row = discovery.probe_command(cand("a", command={"argv": ["alpha", "--version"]}))
    assert Path(row["path"]).is_absolute()


def test_command_absent_from_path_without_run(fake_bin):
    row = discovery.probe_command(cand("a", command={"argv": ["nope", "--version"]}))
    assert row["available"] is False
    assert "reason" not in row  # 没跑就不编造失败原因，可用与否已是结论


def test_run_commands_executes_and_parses_version(fake_bin, monkeypatch):
    make_cli(fake_bin, "alpha")
    captured = {}

    class Done:
        returncode = 0
        stdout = "Alpha 3.2.1 (release)\n"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return Done()

    monkeypatch.setattr(discovery.subprocess, "run", fake_run)
    row = discovery.probe_command(
        cand("a", command={
            "argv": ["alpha", "--version"],
            "version_pattern": r"Alpha ([0-9]+\.[0-9]+\.[0-9]+)",
        }),
        run=True,
    )
    assert row["run"] is True and row["ok"] is True
    assert row["version"] == "3.2.1"
    # 无 shell + 用 which 解析出的绝对路径（.cmd 垫片在 Windows 上必须绝对路径）
    assert Path(captured["argv"][0]).parent == fake_bin
    assert captured["argv"][1:] == ["--version"]
    assert captured["kw"]["shell"] is False
    # stdin 必须 DEVNULL：子进程继承网关的 stdio 协议管道会把入站 JSON-RPC 读走
    # （真机实测：`.cmd` 垫片经 cmd.exe 执行后整个 server 直接退出）
    assert captured["kw"]["stdin"] is subprocess.DEVNULL


def test_run_executes_the_real_target_not_the_bare_shim(fake_bin, monkeypatch):
    """实跑用的是补出来的真身，不是裸名垫片（WinError 193 的用户可见面）"""
    make_exec(fake_bin / "alpha")
    make_exec(fake_bin / "alpha.cmd")
    monkeypatch.setattr(discovery.os, "name", "nt")
    captured = {}

    class Done:
        returncode = 0
        stdout = "alpha 1.0"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return Done()

    monkeypatch.setattr(discovery.subprocess, "run", fake_run)
    row = discovery.probe_command(cand("a", command={"argv": ["alpha"]}), run=True)

    assert Path(row["path"]).name.lower() == "alpha.cmd"
    assert Path(captured["argv"][0]).name.lower() == "alpha.cmd"


def test_run_without_version_pattern_keeps_output(fake_bin, monkeypatch):
    make_cli(fake_bin, "alpha")

    class Done:
        returncode = 0
        stdout = "x" * 900
        stderr = ""

    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **k: Done())
    row = discovery.probe_command(cand("a", command={"argv": ["alpha"]}), run=True)
    assert "version" not in row
    assert len(row["output"]) == 400  # 输出截断，不把大文本塞进协议载荷


def test_nonzero_exit_marks_not_ok(fake_bin, monkeypatch):
    make_cli(fake_bin, "alpha")

    class Done:
        returncode = 7
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **k: Done())
    row = discovery.probe_command(cand("a", command={"argv": ["alpha", "-v"]}), run=True)
    assert row["ok"] is False and row["exitCode"] == 7
    assert row["output"] == "boom"


def test_timeout_is_reported_not_raised(fake_bin, monkeypatch):
    make_cli(fake_bin, "alpha")

    def boom(*_a, **_k):
        raise discovery.subprocess.TimeoutExpired("alpha", 1)

    monkeypatch.setattr(discovery.subprocess, "run", boom)
    row = discovery.probe_command(cand("a", command={"argv": ["alpha"]}), run=True)
    assert row["ok"] is False and "超时" in row["reason"]


def test_oserror_is_reported_not_raised(fake_bin, monkeypatch):
    make_cli(fake_bin, "alpha")

    def boom(*_a, **_k):
        raise PermissionError("被拦了")

    monkeypatch.setattr(discovery.subprocess, "run", boom)
    row = discovery.probe_command(cand("a", command={"argv": ["alpha"]}), run=True)
    assert row["ok"] is False and "无法执行" in row["reason"]


def test_command_unavailable_when_run_for_reports_reason(fake_bin):
    row = discovery.probe_command(cand("a", command={"argv": ["nope"]}), run=True)
    assert row["available"] is False and row["ok"] is False
    assert "不在 PATH" in row["reason"]


def test_preview_falls_back_to_argv_join(fake_bin):
    row = discovery.probe_command(
        cand("a", command={"argv": ["nope", "--version"], "preview": "显式预览"})
    )
    assert row["preview"] == "显式预览"


def test_preview_of_joins_argv_when_absent(fake_bin):
    assert discovery._preview_of(cand("a", command={"argv": ["nope", "-v"]})) == "nope -v"
    assert discovery._preview_of(cand("a", command={})) == ""


def test_invalid_argv_reasons_without_available(fake_bin):
    """没 argv 就没有可用与否的结论——不能让发现面自己造出一维 miss"""
    row = discovery.probe_command(cand("a", command={}))
    assert "available" not in row and row["run"] is False
    assert "命令面" in row["reason"]


# ---------------- discover 编排 ----------------


def test_discover_rows_and_summary(fake_root):
    (fake_root / "here").mkdir()
    m = manifest_of(
        cand("here", kind="app", purpose="在的", probes=[exists_probe(f"{TEMPLATE}/here")]),
        cand("gone", probes=[exists_probe(f"{TEMPLATE}/gone")]),
    )
    res = discovery.discover(m, FakeRegistry(["here"]))
    by_id = {c["toolId"]: c for c in res["candidates"]}
    assert by_id["here"]["status"] == "installed"
    assert by_id["here"]["registered"] is True
    assert by_id["here"]["purpose"] == "在的" and by_id["here"]["kind"] == "app"
    assert by_id["gone"]["status"] == "not_installed"
    assert by_id["gone"]["registered"] is False
    assert res["summary"] == {
        "installed": 1, "not_installed": 1, "unknown": 0,
        "registered": 1, "unregistered": 1,
    }


def test_discover_defaults_kind_and_purpose(fake_root):
    row = discovery.discover(
        manifest_of(cand("x", probes=[exists_probe(f"{TEMPLATE}/x")])),
        FakeRegistry(),
    )["candidates"][0]
    assert row["kind"] == "app" and row["purpose"] == ""
    assert "command" not in row


def test_discover_forwards_run_commands(fake_root, fake_bin, monkeypatch):
    """run_commands 逐层传到 probe_command：只有 true 才真的调 subprocess

    桩返回假 CompletedProcess 形状的对象，所以「调了一次」是计数真话，
    本机没有任何子进程被起过。命令**在 PATH 上**是关键：换成不在 PATH 的命令，
    「没 spawn」就只是「没找到命令」，忽略门禁的变异杀不动（第一版踩过）。
    """
    make_cli(fake_bin, "herecli")
    calls = []

    class Done:
        returncode = 0
        stdout = "herecli 1.0"
        stderr = ""

    def recorder(*_a, **_k):
        calls.append(1)
        return Done()

    monkeypatch.setattr(discovery.subprocess, "run", recorder)
    m = manifest_of(cand("x", probes=[exists_probe(TEMPLATE)],
                        command={"argv": ["herecli", "--version"],
                                 "version_pattern": r"([0-9][0-9.]*)"}))

    off = discovery.discover(m, FakeRegistry())["candidates"][0]["command"]
    assert (off["run"], off["available"], off.get("version")) == (False, True, None)
    assert calls == []

    on = discovery.discover(m, FakeRegistry(), run_commands=True)["candidates"][0]["command"]
    assert (on["run"], on["available"], on["version"]) == (True, True, "1.0")
    assert len(calls) == 1


def test_command_availability_can_decide_status_alone(fake_bin):
    """PATH 上找得到就判 installed——这是「只有命令面」的 CLI 工具的唯一出路"""
    make_cli(fake_bin, "alpha")
    m = manifest_of(cand("alpha", command={"argv": ["alpha", "--version"]}))
    row = discovery.discover(m, FakeRegistry())["candidates"][0]
    assert (row["status"], row["confidence"]) == ("installed", "high")


def test_path_hit_and_command_miss_still_installed(fake_root, fake_bin):
    """多维度取或：声明位置命中就够了，命令不在 PATH 不推翻结论"""
    (fake_root / "alpha.exe").write_text("", encoding="utf-8")
    m = manifest_of(cand("alpha", probes=[exists_probe(f"{TEMPLATE}/alpha.exe")],
                        command={"argv": ["alpha-not-on-path"]}))
    row = discovery.discover(m, FakeRegistry())["candidates"][0]
    assert row["status"] == "installed"
    assert row["command"]["available"] is False


def test_command_available_and_path_miss_is_installed(fake_root, fake_bin):
    """反过来：装在别处（声明路径没中）但命令在 PATH 上，仍判 installed"""
    make_cli(fake_bin, "alpha")
    m = manifest_of(cand("alpha", probes=[exists_probe(f"{TEMPLATE}/gone")],
                        command={"argv": ["alpha", "--version"]}))
    row = discovery.discover(m, FakeRegistry())["candidates"][0]
    assert (row["status"], row["confidence"]) == ("installed", "high")


def test_note_for_declares_probe_surface_is_not_exhaustive(fake_root):
    m = manifest_of(cand("x", probes=[exists_probe(f"{TEMPLATE}/gone")]))
    res = discovery.discover(m, FakeRegistry())
    note = discovery.note_for(res)
    assert "不是" in note and "本机没有" in note
    assert "尚未登记" in note


# ---------------- discover_tools（dispatch 集成面）----------------


@pytest.fixture
def shipped_manifest(fake_root, fake_bin, tmp_path, monkeypatch):
    """把 AWAM_DISCOVERY_FILE 指到 tmp 里的一份合成清单

    三个覆盖面各一个：`here` 路径命中**且命令在 PATH 上**（门禁用例要靠它
    证明「可用 ≠ 会跑」）、`gone` 两处都没有、`unknownvar` 有一处位置没检查成。
    """
    (fake_root / "here").mkdir()
    make_cli(fake_bin, "herecli")
    path = tmp_path / "discovery.json"
    path.write_text(
        json.dumps(
            manifest_of(
                cand("here", purpose="命中项",
                     probes=[exists_probe(f"{TEMPLATE}/here")],
                     command={"argv": ["herecli", "--version"]}),
                cand("gone", probes=[exists_probe(f"{TEMPLATE}/gone")],
                     command={"argv": ["gonecli", "--version"]}),
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(discovery.DISCOVERY_ENV_VAR, str(path))
    return path


def test_dispatch_discover_tools_payload(shipped_manifest, fake_bin, monkeypatch):
    monkeypatch.setattr(
        server._TOOL_CONTEXT.registry, "list_all_tools", lambda: [{"id": "here"}]
    )
    payload = json.loads(run(server.dispatch_tool("discover_tools", {})))
    assert payload["success"] is True
    assert payload["manifestPath"] == str(shipped_manifest)
    assert payload["runCommands"] is False
    assert payload["summary"] == {
        "installed": 1, "not_installed": 1, "unknown": 0,
        "registered": 1, "unregistered": 1,
    }
    assert payload["returned"] == 2
    by_id = {c["toolId"]: c for c in payload["candidates"]}
    assert by_id["here"]["purpose"] == "命中项"
    assert by_id["here"]["status"] == "installed"
    assert by_id["gone"]["status"] == "not_installed"
    # here 的命令**在 PATH 上**但默认不跑：available True / run False 同时成立
    here_cmd = by_id["here"]["command"]
    assert here_cmd["available"] is True and here_cmd["run"] is False
    assert Path(here_cmd["path"]).parent == fake_bin
    assert by_id["gone"]["command"] == {
        "preview": "gonecli --version", "run": False, "available": False,
    }
    assert "data/tools/" in payload["note"]


def test_dispatch_discover_tools_never_spawns_by_default(shipped_manifest, monkeypatch):
    """门禁的正证：默认路径下 subprocess.run 一次都不该被调用

    清单里刻意放一个**在 PATH 上**的命令（herecli）：否则「没 spawn」只是因为
    「没找到命令」，M3 那类「忽略 run 门禁」的变异就杀不动（第一版踩过）。
    用记录型桩而不是 raise 型桩——「一次都没调」才是本用例要证的性质。
    """
    calls = []
    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **k: calls.append(a))
    payload = json.loads(run(server.dispatch_tool("discover_tools", {})))
    assert calls == []
    assert payload["runCommands"] is False
    runs = [c["command"]["run"] for c in payload["candidates"] if "command" in c]
    assert runs == [False, False]


def test_dispatch_discover_tools_status_filter_keeps_full_summary(shipped_manifest):
    payload = json.loads(run(server.dispatch_tool("discover_tools", {"status": "installed"})))
    assert payload["statusFilter"] == "installed"
    assert payload["returned"] == 1
    assert [c["toolId"] for c in payload["candidates"]] == ["here"]
    # summary 不被筛选裁剪：否则「summary.installed=1 / 返回 1 条」会看不出
    # 到底是筛过还是本来就只有这些
    assert payload["summary"]["not_installed"] == 1
    assert "statusFilter" not in json.loads(run(server.dispatch_tool("discover_tools", {})))


def test_dispatch_discover_tools_empty_manifest_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(discovery.DISCOVERY_ENV_VAR, str(tmp_path / "absent.json"))
    payload = json.loads(run(server.dispatch_tool("discover_tools", {})))
    assert payload["success"] is True and payload["candidates"] == []
    assert "没有候选可探测" in payload["note"]
    assert "还没声明" in payload["note"]


def test_dispatch_discover_tools_bad_manifest_is_config_error(tmp_path, monkeypatch):
    path = tmp_path / "discovery.json"
    path.write_text(
        json.dumps(manifest_of(cand("dup", name="A"), cand("dup", name="B"))),
        encoding="utf-8",
    )
    monkeypatch.setenv(discovery.DISCOVERY_ENV_VAR, str(path))

    with pytest.raises(ToolError) as exc:
        run(server.dispatch_tool("discover_tools", {}))

    payload = json.loads(str(exc.value))
    assert payload["success"] is False
    assert payload["errorType"] == "config_error"
    assert "tool_id 重复" in payload["error"]


def test_discover_tools_spec_is_open_world_not_read_only():
    """注解：会 spawn 的能力面不声明只读（与 get_tool_help 同判例）"""
    from tool_specs import build_mcp_tools, get_spec

    spec = get_spec("discover_tools")
    assert spec.annotations == {"openWorldHint": True, "destructiveHint": False}
    assert "readOnlyHint" not in spec.annotations
    assert set(spec.input_schema["properties"]) == {"run_commands", "status"}
    assert spec.input_schema["properties"]["status"]["enum"] == [
        "installed", "not_installed", "unknown",
    ]
    assert "required" not in spec.input_schema
    built = {t.name: t for t in build_mcp_tools()}["discover_tools"]
    assert built.annotations.model_dump(exclude_none=True, by_alias=True)["openWorldHint"] is True
