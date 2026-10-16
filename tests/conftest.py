"""测试共用夹具。

ensure_npm_bin_on_path 在真实环境里会跑 `npm prefix -g` 并修改
os.environ["PATH"]——对单测来说是杂质：慢（秒级）、依赖 npm 存在、
污染进程环境。这里默认把它打成 no-op，保证既有用例的 hermetic；
需要验证 PATH 自愈本身的用例在自己的 monkeypatch 里覆盖即可
（monkeypatch 是可堆叠的，用例内的 patch 会盖住这里的默认 patch）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import backend_client  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_npm_bin_on_path(monkeypatch):
    """默认屏蔽 PATH 自愈的真实执行，并重置其进程级缓存"""

    async def _noop():
        return None

    monkeypatch.setattr(backend_client, "_npm_bin_dir", None)
    monkeypatch.setattr(backend_client, "ensure_npm_bin_on_path", _noop)
    yield
