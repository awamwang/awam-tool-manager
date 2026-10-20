"""测试共用夹具。

ensure_npm_bin_on_path 在真实环境里会跑 `npm prefix -g` 并修改
os.environ["PATH"]——对单测来说是杂质：慢（秒级）、依赖 npm 存在、
污染进程环境。这里默认把它打成 no-op，保证既有用例的 hermetic；
需要验证 PATH 自愈本身的用例在自己的 monkeypatch 里覆盖即可
（monkeypatch 是可堆叠的，用例内的 patch 会盖住这里的默认 patch）。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 运行时目录必须在 import 任何 src 模块**之前**定死：
# `backend_client.DESIRED_STATE_DIR`（及其旧单文件迁移源路径）是 import 期
# 求值的模块常量，晚一步设置就会指向真实的 %LOCALAPPDATA%\.awam-tool-manager
# ——那会让测试读写用户真实运行时数据，而且因为不在仓库里，git status 根本
# 看不出来。conftest 由 pytest 最先加载，这里设置对后续所有 import 生效。
_RUNTIME_ISOLATION_DIR = tempfile.mkdtemp(prefix="awam-runtime-")
os.environ.setdefault("AWAM_RUNTIME_DIR", _RUNTIME_ISOLATION_DIR)

# 暴露模式必须 hermetic：宿主环境变量或仓库 data/exposure.json 会改变
# tools/list 的切片。单元测试以 lazy 为基线（协议出口的历史默认），需要
# 其他模式的用例自己 monkeypatch `server.EXPOSURE_MODE` 或直接传参。
os.environ.pop("AWAM_EXPOSURE_MODE", None)
os.environ["AWAM_EXPOSURE_FILE"] = str(
    Path(_RUNTIME_ISOLATION_DIR) / "no-exposure.json"
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import backend_client  # noqa: E402

# 对外工具名清单（按暴露模式分层）。新增/移除工具时必须同步更新这里——用
# 名字集合而不是 `len(tools) == N`：裸数字变红时看不出是多了还是少了哪个
# 工具（2026-10-16 接入 launcher 三个工具时，两处写死 12 的断言同时变红）。
EXPECTED_TOOL_NAMES_LAZY = {
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
    "get_tool_help",
    "get_usage_stats",
    "discover_tools",
    "list_packages",
    "search_packages",
    "launch_package",
    "install_package",
}

EXPECTED_TOOL_NAMES_DEFAULT = EXPECTED_TOOL_NAMES_LAZY | {
    "search_files",
    "open_path",
    "open_app",
    "search_games",
    "open_game",
    "search_actions",
    "run_action",
}

EXPECTED_TOOL_NAMES_FULL = EXPECTED_TOOL_NAMES_DEFAULT | {
    "get_file_info",
    "get_everything_status",
    "search_winget_packages",
    "install_winget_package",
}

# 既有引用的口径：协议出口的默认模式是 lazy。新用例请直接用带模式后缀的常量。
EXPECTED_TOOL_NAMES = EXPECTED_TOOL_NAMES_LAZY


@pytest.fixture(autouse=True)
def _isolate_npm_bin_on_path(monkeypatch):
    """默认屏蔽 PATH 自愈的真实执行，并重置其进程级缓存"""

    async def _noop():
        return None

    monkeypatch.setattr(backend_client, "_npm_bin_dir", None)
    monkeypatch.setattr(backend_client, "ensure_npm_bin_on_path", _noop)
    yield
