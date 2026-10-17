"""e2e 用例的夹具：每用例一份仓库副本 + 一个独立运行时目录。

**为什么是 function 级而不是 session 级**：设计 §4.4 要求「每用例独立运行时
目录」，避免 audit.log / 期望状态 / 后端缓存在用例之间串味——e2e 的价值就在
于真，串味会让它变成偶发绿。仓库副本才 21 KB，重建成本远低于排查串味。
"""

from pathlib import Path

import pytest

# 相对导入而不是 `import helpers`：本目录是包（见 __init__.py），相对导入
# 不需要把 tests/e2e 塞进 sys.path——塞进去会和 tests/conftest.py 抢名字。
from .helpers import repo_copy  # noqa: E402


@pytest.fixture
def e2e_repo(tmp_path) -> Path:
    """`src/` + `data/` 的 tmp 副本（**写它，绝不写仓库 data/**）。"""
    return repo_copy(tmp_path)


@pytest.fixture
def e2e_runtime(tmp_path) -> Path:
    """独立运行时目录（audit.log、期望状态、后端缓存都落这里）。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime
