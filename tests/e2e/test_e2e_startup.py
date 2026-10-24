"""场景①：真实 stdio 起服 + `tools/list` ≡ golden（覆盖 R1 / R3）。

**为什么这一条价值最高**：`src/server.py:419` 的 `main()` 在此前**没有任何
用例执行过**——启动即崩、stdout 协议污染、迁移/预热异常全都不可见（设计
§1 的 R1）。而 `list_tools` 五字段原本只有 import 级比对（`test_tool_specs.py
:57`），不经 stdio，序列化变形测不出来（R3）。

本场景是**真实进程采集**，与 import 级互为交叉验证：两边都绿才说明「声明」
和「线上真正下发的」是同一份。

预算 ≤4s（设计 §2）。
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

from .helpers import (  # noqa: E402
    has_real_traceback,
    load_expected_tool_names,
    load_golden,
    stdio_session,
    tool_fields,
)


async def _collect(repo, runtime, env_extra=None):
    started = time.monotonic()
    async with stdio_session(repo, runtime, env_extra=env_extra) as (session, log):
        tools = (await session.list_tools()).tools
        names = [t.name for t in tools]
        fields = [tool_fields(t) for t in tools]
    # 进程已退出，此处才能安全读 stderr（fd 偏移曾与子进程共享）
    return names, fields, log, time.monotonic() - started


@pytest.mark.e2e
def test_e2e_list_tools_matches_golden(e2e_repo, e2e_runtime):
    names, fields, log, elapsed = asyncio.run(_collect(e2e_repo, e2e_runtime))
    print(f"\n[场景①] 起服 + list_tools 耗时 {elapsed:.2f}s（预算 4s）")
    expected = load_expected_tool_names()

    # 1) main() 真的走到过：启动日志出现，且全程无未捕获异常。
    #    顺序很重要——服务若崩在启动阶段，后面的字段比对只会得到一句空 diff。
    text = log.text()
    assert "启动中" in text, "stderr 没有启动日志——main() 可能没走到：\n" + log.tail()
    assert not has_real_traceback(text), (
        "stderr 出现真实异常（已剔除 Windows asyncio 关闭噪音）：\n" + log.tail()
    )

    # 2) 工具名集合与手写清单一致（保序：顺序也是对外契约的一部分）
    assert set(names) == expected, (
        f"真实进程下发的工具名与手写清单不一致：\n  多出 {sorted(set(names) - expected)}"
        f"\n  缺少 {sorted(expected - set(names))}"
    )
    assert len(names) == len(expected), (
        f"工具数量不符：实得 {len(names)}，期望 {len(expected)}"
    )

    # 3) 五字段逐位全等——真实通道采集 vs import 级 golden
    golden = load_golden()
    assert fields == golden, (
        "真实 stdio 下发的 tools/list 与 golden 不一致（字段或顺序漂移）。\n"
        f"  实得工具数 {len(fields)}，golden {len(golden)}\n"
        "  stderr 尾部：\n" + log.tail(15)
    )


@pytest.mark.e2e
def test_e2e_default_mode_exposes_front_door(e2e_repo, e2e_runtime):
    """`EXPOSURE_MODE=default`：真实进程下发 29 个工具（含 7 个意图前门）"""
    names, fields, log, elapsed = asyncio.run(
        _collect(e2e_repo, e2e_runtime, {"EXPOSURE_MODE": "default"})
    )
    print(f"\n[场景①-default] 起服 + list_tools 耗时 {elapsed:.2f}s")
    assert "暴露模式: default" in log.text(), log.tail()

    expected = load_expected_tool_names("default")
    assert set(names) == expected, (
        f"default 模式工具名不符：多出 {sorted(set(names) - expected)}，"
        f"缺少 {sorted(expected - set(names))}"
    )
    assert names[-7:] == [
        "search_files",
        "open_path",
        "open_app",
        "search_games",
        "open_game",
        "search_actions",
        "run_action",
    ]
    assert fields == load_golden("default")


@pytest.mark.e2e
def test_e2e_full_mode_exposes_everything(e2e_repo, e2e_runtime):
    """`EXPOSURE_MODE=full`：真实进程下发全部 34 个工具"""
    names, fields, log, elapsed = asyncio.run(
        _collect(e2e_repo, e2e_runtime, {"EXPOSURE_MODE": "full"})
    )
    print(f"\n[场景①-full] 起服 + list_tools 耗时 {elapsed:.2f}s")
    assert "暴露模式: full" in log.text(), log.tail()

    expected = load_expected_tool_names("full")
    assert set(names) == expected, (
        f"full 模式工具名不符：多出 {sorted(set(names) - expected)}，"
        f"缺少 {sorted(expected - set(names))}"
    )
    assert fields == load_golden("full")
