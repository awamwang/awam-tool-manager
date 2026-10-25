"""场景④：resources / prompts 在**真实 stdio 通道**上的往返（覆盖 R6）。

**为什么值得单列一个场景**：D23（`read_resource` 返回值类型错误）与 D26
（`get_prompt` 返回类型不符合协议）两个 P0 的共同成因是「能力存在但从未被
真正调用过」——它们当时只被 import 层 / 内存通道的用例覆盖，协议层 handshake
一过就没人再碰。本场景把它们钉在真实通道上：

- D23 回归保护：`read_resource("awam://tools")` 必须真能读出 JSON。
  `server.py:214` 的 `str(uri)` 归一化是关键——pydantic 的 `AnyUrl` 与 str
  比较恒为 False，漏掉这行就是「能列出来但读不出来」。
- D26 回归保护：`get_prompt()` 必须返回 `GetPromptResult`（带 `.messages`）。
  老 SDK 接受裸 `list[PromptMessage]`，新 SDK 直接拒——返回值类型一变，
  prompts/get **全部**失败。

单进程复用一个会话（Q5 裁决），预算 ≤4s。
"""

import asyncio
import json
import time

import pytest

from .helpers import has_real_traceback, stdio_session  # noqa: E402

EXPECTED_RESOURCE_URIS = {
    "awam://mcp-servers",
    "awam://tools",
    "awam://backends/status",
}
EXPECTED_PROMPTS = {"getting_started", "how_to_search", "how_to_call_capability"}
# data/tools/ 里真实存在的条目（tmp 副本自带），用它证明读出来的是真数据
KNOWN_TOOL_ID = "everything-search-mcp"


async def _run(repo, runtime):
    started = time.monotonic()
    obs: dict = {}
    async with stdio_session(repo, runtime) as (session, log):
        obs["uris"] = [str(r.uri) for r in (await session.list_resources()).resources]

        result = await session.read_resource("awam://tools")
        obs["tools_text"] = result.contents[0].text

        obs["prompt_names"] = [p.name for p in (await session.list_prompts()).prompts]
        obs["prompts"] = {}
        for name in obs["prompt_names"]:
            prompt = await session.get_prompt(name, {})
            messages = getattr(prompt, "messages", None)
            obs["prompts"][name] = {
                # D26：老 SDK 返回裸列表时没有 .messages，这条会红
                "has_messages": messages is not None,
                "description": getattr(prompt, "description", None),
                "text": messages[0].content.text if messages else "",
            }
    obs["elapsed"] = time.monotonic() - started
    return obs, log


@pytest.mark.e2e
def test_e2e_resources_and_prompts_roundtrip(e2e_repo, e2e_runtime):
    obs, log = asyncio.run(_run(e2e_repo, e2e_runtime))
    tail = log.tail(30)

    print(f"\n[场景④] resources/prompts 往返耗时 {obs['elapsed']:.2f}s（预算 4s）")

    # 1) 资源清单：三个 uri 都在（真实通道下 pydantic 序列化没变形）
    assert set(obs["uris"]) == EXPECTED_RESOURCE_URIS, (
        f"资源清单不符：{sorted(obs['uris'])}\n{tail}"
    )

    # 2) D23 回归保护：awam://tools 真能读出 JSON（AnyUrl 归一化有效）
    try:
        payload = json.loads(obs["tools_text"])
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"read_resource('awam://tools') 返回的不是合法 JSON（D23 回归）：{exc}\n"
            f"  原文前 200 字符: {obs['tools_text'][:200]!r}\n{tail}"
        ) from exc
    assert isinstance(payload, list) and payload, f"资源内容不是非空列表：{payload!r}"
    ids = {t.get("id") for t in payload if isinstance(t, dict)}
    assert KNOWN_TOOL_ID in ids, f"资源内容里没有真实条目 {KNOWN_TOOL_ID}：{sorted(ids)}"

    # 3) 提示清单：三个模板都在，不多不少
    assert set(obs["prompt_names"]) == EXPECTED_PROMPTS, (
        f"提示模板清单不符：{sorted(obs['prompt_names'])}"
    )

    # 4) D26 回归保护：prompts/get 必须给出 GetPromptResult（有 .messages）
    for name, got in obs["prompts"].items():
        assert got["has_messages"] is True, (
            f"get_prompt('{name}') 没有 .messages——返回类型退化了（D26 回归）"
        )
        assert got["text"].strip(), f"get_prompt('{name}') 正文为空"
    # 正文内容抽查：模板讲的是本服务该讲的事（不是空壳）
    assert "search_tools" in obs["prompts"]["how_to_search"]["text"]

    # 5) 全程无未捕获异常
    assert not has_real_traceback(log.text()), (
        "stderr 出现真实异常（已剔除 Windows asyncio 关闭噪音）：\n" + tail
    )
