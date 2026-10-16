"""运行时数据目录：程序生成、因运行而异的数据都放这里。

与 `data/` 的分工（2026-10-16 定的，此前两者混在 `data/` 下导致工作区永远脏）：

- `data/` —— **人写的声明**（工具条目）。进版本库：值得备份、能跨机器同步、
  改了要 review。
- **运行时目录** —— **程序生成的**：后端子工具缓存、启动器动作清单快照、
  后端期望状态。它们因运行而异，且多实例并发改写（`lastSynced` 被活实例
  反复改写就是症状），进版本库没有意义。

默认位置按 OS 惯例：Windows 用 `%LOCALAPPDATA%`，其他用 home 下的同名隐藏
目录。可用 `AWAM_RUNTIME_DIR` 覆盖——测试隔离与便携部署都靠它。

之所以不做成 import 期求值的模块常量：那样测试就没法用 monkeypatch 改
环境变量来切换目录了。这里一律走函数，调用方自行决定是否缓存。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = ".awam-tool-manager"
ENV_VAR = "AWAM_RUNTIME_DIR"

# 旧位置 → 新位置的相对子路径（迁移用）
LEGACY_SUBDIRS = ("backend-tools", "launcher-actions")
LEGACY_ROOT_FILE = "backend_desired_state.json"


def default_runtime_dir() -> Path:
    """按 OS 惯例的默认位置"""
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / APP_DIR_NAME
    return Path.home() / APP_DIR_NAME


def get_runtime_dir() -> Path:
    """运行时目录；`AWAM_RUNTIME_DIR` 优先于 OS 惯例位置"""
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return Path(override)
    return default_runtime_dir()


def migrate_legacy_runtime(data_dir: Path, runtime_dir: Path, log=None) -> list[str]:
    """把旧位置（仓库内）的运行时数据搬到新位置。

    只在**目标不存在**时搬，绝不覆盖新位置已有的数据——迁移期间可能还有旧
    实例在往新位置写（三个宿主各拉起一个 server 是常态），覆盖会丢数据。

    全程静默：迁移只是锦上添花，失败不阻断启动。返回搬走的文件清单（测试用）。
    """
    import shutil  # noqa: PLC0415

    moved: list[str] = []

    def _note(message: str) -> None:
        if log:
            log(message)

    for sub in LEGACY_SUBDIRS:
        src_dir = data_dir / sub
        dst_dir = runtime_dir / sub
        if not src_dir.exists():
            continue
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            for src in src_dir.glob("*.json"):
                dst = dst_dir / src.name
                if dst.exists():
                    continue
                shutil.move(str(src), str(dst))
                moved.append(f"{sub}/{src.name}")
            # 目录空了就顺手清掉，避免仓库里留个空壳
            if not any(src_dir.iterdir()):
                src_dir.rmdir()
        except OSError as exc:
            _note(f"迁移 {sub} 失败（沿用旧位置）: {exc}")

    legacy_root = data_dir.parent / LEGACY_ROOT_FILE
    if legacy_root.exists():
        dst = runtime_dir / LEGACY_ROOT_FILE
        if not dst.exists():
            try:
                runtime_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy_root), str(dst))
                moved.append(LEGACY_ROOT_FILE)
            except OSError as exc:
                _note(f"迁移 {LEGACY_ROOT_FILE} 失败（沿用旧位置）: {exc}")
    return moved
