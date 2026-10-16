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

# 对外工具名清单。新增/移除工具时必须同步更新这里——用名字集合而不是
# `len(tools) == N`：裸数字变红时看不出是多了还是少了哪个工具
# （2026-10-16 接入 launcher 三个工具时，两处写死 12 的断言同时变红）。
EXPECTED_TOOL_NAMES = {
    "search_tools",
    "list_tools",
    "get_tool_info",
    "launch_tool",
    "call_backend_tool",
    "list_backend_tools",
    "start_backend",
    "stop_backend",
    "get_backend_status",
    "get_backend_logs",
    "set_backend_log_level",
    "sync_backend_tools",
    "sync_launcher_actions",
    "list_launcher_actions",
    "run_launcher_action",
}


@pytest.fixture(autouse=True)
def _isolate_npm_bin_on_path(monkeypatch):
    """默认屏蔽 PATH 自愈的真实执行，并重置其进程级缓存"""

    async def _noop():
        return None

    monkeypatch.setattr(backend_client, "_npm_bin_dir", None)
    monkeypatch.setattr(backend_client, "ensure_npm_bin_on_path", _noop)
    yield
