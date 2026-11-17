"""运行时目录解析与旧路径迁移

背景（2026-10-16）：此前人写的声明（data/tools）和程序生成的数据（后端子工具
缓存、动作清单快照、后端期望状态）都混在 `data/` 下，而后者被活实例反复改写
`lastSynced`，工作区永远脏。现在按「谁写的」分家。

这里守住两条最容易出错的：
1. 迁移**绝不覆盖**新位置已有的数据——迁移期间可能还有旧实例在往新位置写。
2. 测试环境必须能靠 `AWAM_RUNTIME_DIR` 完全隔离，否则会用真实用户数据跑测试
   （且因为不在仓库里，git status 看不出来）。
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import runtime_paths  # noqa: E402
from runtime_paths import (  # noqa: E402
    APP_DIR_NAME,
    ENV_VAR,
    default_runtime_dir,
    get_runtime_dir,
    migrate_legacy_runtime,
)
from store import JsonStore  # noqa: E402


def test_env_var_overrides_os_default(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "custom"))
    assert get_runtime_dir() == tmp_path / "custom"


def test_os_default_uses_localappdata_on_windows(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    if os.name != "nt":
        pytest.skip("只在 Windows 上验证 LOCALAPPDATA 分支")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\fakehome\AppData\Local")
    assert default_runtime_dir() == Path(r"C:\fakehome\AppData\Local") / APP_DIR_NAME


def test_os_default_falls_back_to_home(monkeypatch):
    """LOCALAPPDATA 缺失（或被清理）时不能抛异常"""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_runtime_dir() == Path.home() / APP_DIR_NAME


def test_tests_are_isolated_from_real_runtime_dir():
    """conftest 必须在 import src 之前把运行时目录指到临时目录"""
    assert os.environ.get(ENV_VAR), "conftest 未设置 AWAM_RUNTIME_DIR，测试会碰真实用户数据"
    runtime = Path(os.environ[ENV_VAR])
    assert runtime.exists()
    # 绝不能是真实的 OS 惯例位置
    assert runtime != default_runtime_dir()


# ---- 迁移 ----


def _build_legacy_repo(tmp_path: Path) -> tuple[Path, Path]:
    """搭一个「旧版布局」的仓库：data/ 下混着声明和运行时数据"""
    repo = tmp_path / "repo"
    data = repo / "data"
    (data / "tools").mkdir(parents=True)
    (data / "backend-tools").mkdir(parents=True)
    (data / "launcher-actions").mkdir(parents=True)
    (data / "tools" / "app.json").write_text('{"id": "app"}', encoding="utf-8")
    (data / "backend-tools" / "b.json").write_text('{"parent": "b"}', encoding="utf-8")
    (data / "launcher-actions" / "q.json").write_text('{"parent": "q"}', encoding="utf-8")
    # 旧版把期望状态放在仓库根目录
    (repo / "backend_desired_state.json").write_text('{"b": true}', encoding="utf-8")
    return repo, data


def test_migrate_moves_runtime_data_only(tmp_path):
    repo, data = _build_legacy_repo(tmp_path)
    runtime = tmp_path / "runtime"

    moved = migrate_legacy_runtime(data, runtime)

    assert (runtime / "backend-tools" / "b.json").exists()
    assert (runtime / "launcher-actions" / "q.json").exists()
    assert (runtime / "backend_desired_state.json").exists()
    # 人写的声明必须原地不动
    assert (data / "tools" / "app.json").exists()
    assert sorted(moved) == [
        "backend-tools/b.json",
        "backend_desired_state.json",
        "launcher-actions/q.json",
    ]
    # 旧位置清空
    assert not (data / "backend-tools").exists()
    assert not (repo / "backend_desired_state.json").exists()


def test_migrate_never_overwrites_existing(tmp_path):
    """新位置已有数据时不覆盖——迁移期间可能有旧实例正在往新位置写"""
    repo, data = _build_legacy_repo(tmp_path)
    runtime = tmp_path / "runtime"
    (runtime / "backend-tools").mkdir(parents=True)
    (runtime / "backend-tools" / "b.json").write_text('{"parent": "NEW"}', encoding="utf-8")

    migrate_legacy_runtime(data, runtime)

    kept = json.loads((runtime / "backend-tools" / "b.json").read_text(encoding="utf-8"))
    assert kept == {"parent": "NEW"}, "覆盖了新位置的数据，会丢运行中的状态"
    # 旧文件留着不动，等下次再迁（不丢数据优先于整洁）
    assert (data / "backend-tools" / "b.json").exists()


def test_migrate_is_idempotent(tmp_path):
    repo, data = _build_legacy_repo(tmp_path)
    runtime = tmp_path / "runtime"
    migrate_legacy_runtime(data, runtime)
    assert migrate_legacy_runtime(data, runtime) == []


def test_migrate_survives_missing_legacy_dirs(tmp_path):
    """新装（没有旧数据）不该报错"""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    assert migrate_legacy_runtime(data, tmp_path / "runtime") == []


def test_store_keeps_declarations_in_base_dir(tmp_path):
    """向后兼容：不传 runtime_dir 时子目录仍在 base_dir 下（测试全靠这个）"""
    store = JsonStore(tmp_path / "data")
    assert store.backend_tools_dir == tmp_path / "data" / "backend-tools"
    assert store.launcher_actions_dir == tmp_path / "data" / "launcher-actions"


def test_store_splits_runtime_when_given(tmp_path):
    store = JsonStore(tmp_path / "data", runtime_dir=tmp_path / "runtime")
    assert store.tools_dir == tmp_path / "data" / "tools"
    assert store.backend_tools_dir == tmp_path / "runtime" / "backend-tools"
    assert store.launcher_actions_dir == tmp_path / "runtime" / "launcher-actions"
