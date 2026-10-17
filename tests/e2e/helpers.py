"""真实 stdio 端到端（e2e）测试的公共基建（ARC-P3-02）。

设计来源：`docs/architecture_e2e_design_2026-10-17.md`。三条硬规则在本模块
里落到具体函数上，实施新场景时请复用而不是重写：

1. **绝不写仓库 `data/`**——`repo_copy()` 把 `src/` + `data/` 复制到 tmp。
   复制而不是给生产代码加 `AWAM_DATA_DIR` 开关，是因为 `DATA_DIR` 在
   `server.py:63` 硬编码为 `<src 父目录>/data`；加 env 覆盖等于为测试改产品
   代码，复制可零改动绕开。
2. **禁止固定 `sleep`**——一律用 `wait_until()`（轮询 + 超时 + 失败带现场）。
   固定 sleep 要么在快的机器上浪费时间，要么在慢的机器上 flaky。
3. **stderr 必留存**——`StderrLog.tail()` 给断言消息用。e2e 失败若只剩一句
   timeout 而拿不到子进程日志，等于没测。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# tests/e2e/helpers.py → 仓库根
ROOT = Path(__file__).resolve().parent.parent.parent

# 复制时排除的噪音（体积大或与运行无关）
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache")

# e2e 超时上限（设计 §4.6）：握手 5s / 状态轮询 10s / 单场景 8s
INIT_TIMEOUT = 10.0  # 起进程 + initialize 握手，Windows 上解释器冷启动偏慢
CALL_TIMEOUT = 10.0  # 单次 tools/call


class StderrLog:
    """子进程 stderr 的读取口。

    两个坑，都在本类里封住：
    1. **会话进行中不要读**——`errlog` 的 fd 与子进程共享读写偏移，中途
       seek(0) 会打乱子进程自己的写入位置；
    2. **临时文件关闭后要能读**——所以会话结束时先 `snapshot()` 把全文捞进
       内存，再关文件。否则断言消息里拿到的就是一个已关闭的文件（第一次
       实现就是这个 bug：`ValueError: I/O operation on closed file`）。
    """

    def __init__(self, fp):
        self._fp = fp
        self._text: str | None = None

    def snapshot(self) -> str:
        """把 stderr 全文读进内存（只能调一次有意义，内部做了幂等）。"""
        if self._text is None:
            self._fp.seek(0)
            self._text = self._fp.read()
        return self._text

    def text(self) -> str:
        if self._text is None:
            return self.snapshot()
        return self._text

    def tail(self, n: int = 30) -> str:
        """尾部 n 行——塞进断言消息用（全文可能上千行，断言里放不下）。"""
        lines = self.text().splitlines()
        return "\n".join(lines[-n:])


def repo_copy(tmp_path: Path) -> Path:
    """把 `src/` 与 `data/` 复制到 tmp，返回副本根（其下有 src/ 与 data/）。

    只复制这两处：server 运行时只需要它们（DATA_DIR 由 src 的位置推导，
    见 `server.py:63`），带上 tests/docs 只会拖慢且不提供任何隔离。
    """
    dest = tmp_path / "repo"
    shutil.copytree(ROOT / "src", dest / "src", ignore=COPY_IGNORE)
    shutil.copytree(ROOT / "data", dest / "data", ignore=COPY_IGNORE)
    return dest


def write_tool_entry(repo_dir: Path, entry: dict) -> Path:
    """往副本的 `data/tools/` 写一个条目（**绝不写仓库真实 data/**）。"""
    path = repo_dir / "data" / "tools" / f"{entry['id']}.json"
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_env(runtime_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：运行时目录隔离 + 清掉会干扰判定的开关。

    - `AWAM_RUNTIME_DIR`：不设会落到用户真实的 %LOCALAPPDATA%（见
      `runtime_paths.py:24`），既污染用户机器又因为不在仓库里而看不出来。
    - `AWAM_AUTO_INSTALL` / `AWAM_POLICY_FILE`：清掉宿主环境残留，保证用例
      的判定只取决于自己在 `extra` 里设了什么。
    """
    env = dict(os.environ)
    env["AWAM_RUNTIME_DIR"] = str(runtime_dir)
    env.pop("AWAM_AUTO_INSTALL", None)
    env.pop("AWAM_POLICY_FILE", None)
    if extra:
        env.update(extra)
    return env


@asynccontextmanager
async def stdio_session(
    repo_dir: Path,
    runtime_dir: Path,
    env_extra: dict[str, str] | None = None,
):
    """真实子进程 + 真实 MCP 会话。yield `(session, stderr_log)`。

    用 `sys.executable` 而不是硬编码解释器路径：跑测试的解释器就是服务该
    用的解释器，换机器/换 venv 都不会出现「跑得起来的进程和我测的不是同一个」。

    超时保护：`initialize()` 握手挂死会让用例永远卡住，这里用 wait_for 兜住
    （超时即失败，不重试——重试会把真 flaky 掩盖成偶发绿）。
    """
    env = build_env(runtime_dir, env_extra)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(repo_dir / "src" / "server.py")],
        env=env,
    )
    fp = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    log = StderrLog(fp)
    try:
        async with stdio_client(params, errlog=fp) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=INIT_TIMEOUT)
                yield session, log
    finally:
        # stdio_client 的 __aexit__ 会等子进程结束，此处读 stderr 是安全的。
        # 先快照再关闭——顺序反了断言就拿不到任何日志。
        log.snapshot()
        fp.close()


async def call_tool(session, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    """调一次工具，返回 `(isError, payload)`。

    payload 是解析后的 JSON dict；解析不了就原样退化为 `{"_raw": 文本}`，
    让断言能报出「服务到底吐了什么」而不是一个 JSONDecodeError。
    """
    result = await asyncio.wait_for(
        session.call_tool(name, arguments), timeout=CALL_TIMEOUT
    )
    text = result.content[0].text if result.content else ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"_raw": text}
    return bool(result.isError), payload


async def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 5.0,
    interval: float = 0.1,
    description: str = "",
) -> Any:
    """轮询直到 predicate 取到真值，超时抛 AssertionError 并带上最后一次取值。

    predicate 可以是同步函数也可以是协程函数。取代 `time.sleep(...)`：
    固定等待在慢机器上不够、在快机器上白等，而且失败时拿不到现场。
    """
    deadline = time.monotonic() + timeout
    last = None
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        last = value
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"wait_until 超时（{timeout}s）{description}；最后一次取值={last!r}"
            )
        await asyncio.sleep(interval)


def tool_fields(tool) -> dict[str, Any]:
    """抽取 golden 比对的五字段，序列化规则与 `tests/test_tool_specs.py` 一致。

    annotations 走 `exclude_none=True`（MCP 线上序列化同款），否则 pydantic
    会把全部 hint 键连同 null 一起输出，与 golden 逐位比对会假红。
    """
    annotations = None
    if getattr(tool, "annotations", None) is not None:
        annotations = tool.annotations.model_dump(exclude_none=True)
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.inputSchema,
        "annotations": annotations,
        "outputSchema": tool.outputSchema,
    }


def has_real_traceback(text: str) -> bool:
    """stderr 里有没有**真**异常（剔除 Windows asyncio 的关闭噪音）。

    噪音来源：子进程被 terminate 时，它的 asyncio proactor 来不及收尾，
    `__del__` 里抛 `ResourceWarning: unclosed transport`，连带打印一段
    Traceback。这是关闭阶段的副作用，不是产品缺陷——但它会让「stderr 无
    Traceback」这条断言**永远为红**，所以必须精确剔除。

    剔除是**按段**做的（只丢掉含噪音的那一段），不是按全文关键字放行：
    服务真崩时的 Traceback 不含这些标记，照旧让断言红。
    """
    if "Traceback (most recent call last):" not in text:
        return False
    noise = ("unclosed transport", "ResourceWarning", "Exception ignored in")
    for segment in text.split("Traceback (most recent call last):")[1:]:
        if not any(marker in segment for marker in noise):
            return True
    return False


def load_expected_tool_names() -> set[str]:
    """按**文件路径**加载 `tests/conftest.py` 里手写的工具名清单。

    不能用 `from conftest import EXPECTED_TOOL_NAMES`：tests/ 与 tests/e2e/
    都没有 `__init__.py`，两个 conftest 在 sys.modules 里同名，谁后加载谁
    生效——实测在 e2e 用例里 import 拿到的是 tests/e2e/conftest.py（它根本
    没有这个常量）。按路径加载可绕开这个命名冲突。
    """
    path = ROOT / "tests" / "conftest.py"
    spec = importlib.util.spec_from_file_location("_tests_root_conftest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EXPECTED_TOOL_NAMES


def load_golden() -> list[dict[str, Any]]:
    """`tests/golden/tools_list.json`（import 级采集的历史基线）。"""
    return json.loads(
        (ROOT / "tests" / "golden" / "tools_list.json").read_text(encoding="utf-8")
    )
