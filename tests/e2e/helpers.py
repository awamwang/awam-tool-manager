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
import signal
import subprocess
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


def copy_fixture(dest_dir: Path, name: str) -> Path:
    """把 `tests/e2e/` 下的**非测试源文件**（假后端等）复制到 tmp。

    为什么复制而不是把仓库路径直接写进条目：被测进程（网关及其拉起的后端）
    应该只接触 tmp 里的世界。仓库路径一旦进被测进程，任何写回/缓存都会落到
    仓库里，而 e2e 的价值恰恰是「被测进程真的做过什么」。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    shutil.copy2(Path(__file__).parent / name, target)
    return target


def pid_alive(pid: int) -> bool:
    """进程是否还活着（**无残留子进程**断言的判定手段）。

    平台实现见 `_pid_alive_*`：Windows 走 `OpenProcess`，**不要换回
    `tasklist`**（单次 0.7~1.5s，轮询时测量比被测事件还慢，会把「进程已退出」
    的判定凭空推后 2s——第一版就是这么把场景③ 拖到 11s 的）。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _win_pid_alive(pid: int) -> bool:
    """Windows 上的存活判定：`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`。

    两条被否掉的路，都是实测否决的：

    - `os.kill(pid, 0)`：Windows 没有「0 号信号」语义，CPython 落到
      TerminateProcess 或直接 ValueError，用作探测是错的；
    - `tasklist /FI "PID eq <pid>"`：**单次实测 0.7~1.5s**。而「无残留子进程」
      是要**轮询**的——测量手段比被测事件还慢，会把判定凭空推后 2s（首次
      实现就是这个坑，场景③ 因此跑到 11s）。

    `ctypes` 是标准库，不引入新依赖；句柄拿到即关，不留泄漏。
    """
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


DEFAULT_FAKE_TOOL_ID = "_e2e_fake_backend"


def fake_backend_entry(
    fixtures_dir: Path,
    pid_file: Path,
    calls_file: Path,
    tool_id: str = DEFAULT_FAKE_TOOL_ID,
) -> dict:
    """假后端条目（写进 **tmp 副本** 的 `data/tools/`，绝不写仓库 `data/`）。

    场景③（全链路）与场景⑤（policy deny）共用同一份条目——同一个被测后端，
    两条场景才有可比性。

    `command` 用 `sys.executable`（绝对路径）是**防竞态的关键**：
    `_target_available()` 为真 → `ensure_installed()` 在 `backend_client.py:494`
    直接短路，根本不进安装 / 持锁路径（设计 §4.2）。
    """
    return {
        "id": tool_id,
        "name": "AwamE2EFakeBackend",
        "displayName": "E2E 假后端",
        "kind": "mcp",
        "status": "installed",
        "description": "e2e 探针：仓内最小 FastMCP 假后端，用于验证后端子进程全生命周期",
        "tags": ["探针", "e2e"],
        "connection": {
            "type": "stdio",
            "command": sys.executable,
            # launch_detached 把假后端拉成孙进程：无残留断言因此覆盖整棵进程树
            "args": [
                str(fixtures_dir / "launch_detached.py"),
                str(fixtures_dir / "fake_backend.py"),
                str(pid_file),
                str(calls_file),
            ],
            # mcp 会把它与自己的默认环境合并（PATH 等照旧继承）；
            # PYTHONUTF8 是防御性的：子进程 stdout 编码不被 locale 带歪
            # （无它时 Windows 子进程的 sys.stdout 是 cp936，见场景③ 文档）
            "env": {"PYTHONUTF8": "1"},
            # 显式超时：真失败时快速红，而不是等默认的 30s
            "timeout": 20,
        },
    }


def env_without_npm() -> dict[str, str]:
    """给网关一个**不含 npm** 的 PATH（e2e 提速用，只影响 npm 解析这一件事）。

    为什么需要：`ensure_installed()` 在判定启动目标是否缺失**之前**会无条件
    跑一次 `npm prefix -g`（`backend_client.py:493`）。本机实测这条命令
    **6.5s**，而服务启动预热（`server.py:369`）与 `start_backend` 会各跑一次
    ——`_npm_bin_dir` 是没有锁的进程级缓存，预热那次还没回来时 start 会再起
    一个 npm。场景③ 因此实测到 12s，远超 8s 预算。

    裁掉 npm 后这条命令秒级失败（cmd 报「不是内部或外部命令」，rc=1），
    `ensure_npm_bin_on_path` 静默返回 None——这正是它为「上游用裁剪过的环境
    启动本服务」准备的路径。被测的**后端生命周期**不受影响：后端以
    `sys.executable` 绝对路径拉起，不依赖 PATH。
    """
    if os.name != "nt":
        return {}  # POSIX 上 npm prefix 是毫秒级，无需裁剪
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return {
        "PATH": os.pathsep.join(
            [str(Path(system_root) / "System32"), str(Path(system_root))]
        )
    }


def force_kill_tree(pid: int) -> None:
    """兜底强杀整棵进程树——**只在用例收尾用，不作为「无残留」的断言手段**。

    断言必须走 `pid_alive()`：先用强杀把现场清干净再来断言，等于自己证明
    自己。这里的用途只有一个：用例因故提前失败（断言红、超时）时，不让后端
    进程变成孤儿留在机器上。
    """
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                check=False,  # 进程可能已经没了：强杀是尽力而为，失败不抛
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


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
