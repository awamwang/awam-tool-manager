"""后端 MCP 客户端：管理到各个后端 MCP Server 的连接和调用转发

支持：启动/停止/状态/日志/心跳检测/工具列表缓存

连接生命周期采用「独占任务」模式（_connection_loop）：stdio_client 与
ClientSession 的 __aenter__/__aexit__ 都在同一个任务内完成。原因是
anyio 的 CancelScope 绑定进入它的任务——跨任务进入/退出会直接崩溃，
详见 _connection_loop 文档。
"""

import asyncio
import contextlib
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from errors import ErrorType
from runtime_paths import get_runtime_dir
from store import JsonStore


# 期望状态持久化文件：记录「用户明确 start_backend 成功的后端」。
#
# 为什么需要它（2026-10-16 实测）：toolport 网关会**周期性整池回收**它拉起的
# 所有 server 进程（实测约每几分钟一批，回收时关 stdin → 本服务优雅退出 →
# close_all 停掉全部后端）。每次回收后新 server 世代内存状态清零，所有后端
# 状态回到 stopped——用户视角就是「启动后保不住，每次都得重新初始化」。
# stdio MCP server 的生命周期归客户端（网关）管，进程内状态注定随进程消亡；
# 把「启动意图」落盘后，每个新世代启动时自动恢复，用户任何时刻查状态都是
# running。stop_backend 会清除意图——那是用户明确说「不要了」。
# 位置在运行时目录（`%LOCALAPPDATA%\.awam-tool-manager`）而不是仓库根目录：
# 这是**运行时状态**（谁被明确 start 过），不是源码，放仓库里只能靠
# .gitignore 兜着，还是散落在根目录。见 runtime_paths.py 的分工说明。
#
# 仍是模块级常量：测试靠 monkeypatch.setattr 换掉它来隔离，改成函数会让
# 7 处用例都要改；而它只在 import 期求值一次，生产环境够用。
DESIRED_STATE_FILE = get_runtime_dir() / "backend_desired_state.json"


def _warn(msg: str) -> None:
    """manager 层警告：走 stderr（MCP 协议stdout 通道不能被污染）"""
    print(f"[awam:x][warning] {msg}", file=sys.stderr, flush=True)


def _utc_now_iso() -> str:
    """UTC ISO 时间戳（与 data/ 里其他时间字段同风格，Z 结尾）"""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def install_auto_authorized_by_env() -> bool:
    """部署者是否通过环境变量显式授权「自动执行 install.command」

    授权语义是「我知道这会执行 shell 命令、我同意」——它必须由**人**在 MCP
    客户端配置里显式打开（例如 WorkBuddy 的 mcp.json env 块），不能由程序
    自己默认打开。默认关闭。
    """
    raw = os.environ.get(BackendClient.INSTALL_ENV_VAR, "")
    return raw.strip().lower() in BackendClient.INSTALL_ENV_TRUTHY


# npm 全局 bin 目录的进程级解析缓存：
#   None  = 尚未解析；
#   ""    = 解析失败（npm 不可用或超时，不再重试，避免每次调用都干等）；
#   路径  = 已解析到的 npm 全局 bin 目录。
_npm_bin_dir: str | None = None


def _patch_path(directory: str) -> bool:
    """把 directory 追加进 os.environ["PATH"]；为空或已在 PATH 中则不动"""
    if not directory:
        return False
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if directory in parts:
        return False
    parts.append(directory)
    os.environ["PATH"] = os.pathsep.join(parts)
    return True


async def ensure_npm_bin_on_path() -> str | None:
    """把 npm 全局 bin 目录补进 PATH（幂等、限时、失败静默）

    背景：本服务可能被上游以裁剪过的环境启动（网关白名单继承、CI、
    systemd 等场景都可能发生）。若 PATH 缺 npm 全局 bin（如
    C:\\Users\\...\\.local\\npm-global），shutil.which 找不到早已全局
    安装的 CLI，ensure_installed 会误判「未安装」并触发一次数十秒的
    npm i -g：包明明装着，这既慢又多余，还会把调用方的 tools/call
    预算（toolport 网关为 30s）整个耗光。

    做法：问一次 npm 全局目录在哪（npm prefix -g，约 1-2s，进程级缓存），
    把结果补进 os.environ["PATH"]：
    - which() 立刻命中，安装不再被误触发；
    - spawn 的后端通过 mcp 的 get_default_environment() 读 os.environ，
      继承到同一条 PATH，cmd /c <cli> 之类的启动形式也能解析到。

    任何失败都静默返回 None：PATH 自愈只是优化，不能因为它把启动或调用
    搞挂。npm prefix -g 本身限时 15s，超时杀子进程防孤儿。
    """
    global _npm_bin_dir

    if _npm_bin_dir is not None:
        # 已解析过（含失败）：命中缓存的目录时兜底补进 PATH 再返回
        if _npm_bin_dir:
            _patch_path(_npm_bin_dir)
            return _npm_bin_dir
        return None

    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            "npm prefix -g",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # stdin 必须显式 DEVNULL：本服务的 stdin 是上游网关的
            # JSON-RPC 管道。不传的话子进程会继承它——cmd→node→npm-cli
            # 链在读 stdin 时阻塞在网关管道上，communicate() 永不返回，
            # ensure_installed 卡死 → 网关 30s 超时取消 → anyio 取消
            # 作用域被撕裂 → 整个 Server 进程崩溃（2026-10-16 实测根因，
            # toolport 网关 4/4 超时即此故障）。
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            with anyio.fail_after(15):
                stdout, _ = await proc.communicate()
        except TimeoutError:
            await BackendClient._kill_and_reap(proc)
            _npm_bin_dir = ""
            return None
        if proc.returncode != 0:
            _npm_bin_dir = ""
            return None
        prefix = (stdout or b"").decode("utf-8", "replace").strip().strip('"')
        if not prefix:
            _npm_bin_dir = ""
            return None
        # Windows 上 prefix 本身就是 bin 目录（npm 全局 bin 没有 bin 子目录）；
        # POSIX 上 bin 在 prefix/bin。按平台优先，另一个作兜底。
        if os.name == "nt":
            candidates = [prefix, str(Path(prefix) / "bin")]
        else:
            candidates = [str(Path(prefix) / "bin"), prefix]
        for candidate in candidates:
            if Path(candidate).is_dir():
                _npm_bin_dir = candidate
                _patch_path(candidate)
                return candidate
        _npm_bin_dir = ""
        return None
    except Exception:
        _npm_bin_dir = ""
        return None


class BackendBusyError(Exception):
    """后端正在处理其他调用，并发请求被直接拒绝（见 A3 方案 2：单后端串行 + 超限报 busy）

    与 ConnectionError / RuntimeError 区分：它不表示「坏了」，而是「现在忙」，
    调用方可以稍后重试。manager.call_backend_tool 会把它映射成 errorType=backend_busy。
    """


class BackendClient:
    """单个后端 MCP Server 的客户端（常驻连接，支持管理操作）"""

    STATUS_STOPPED = "stopped"
    STATUS_STARTING = "starting"
    STATUS_RUNNING = "running"
    STATUS_ERROR = "error"

    # 心跳间隔 / 心跳探测超时 / 各类后端操作的默认超时（秒）
    HEARTBEAT_INTERVAL = 30
    HEARTBEAT_TIMEOUT = 10
    DEFAULT_TIMEOUT = 30.0
    # 依赖自装：install.command 的执行上限（秒）。npm i -g 小包通常几十秒，
    # 300s 给慢网络留余量；超时会杀掉子进程，不让安装命令在后台变成孤儿。
    INSTALL_TIMEOUT = 300.0
    # 安装锁等待上限（秒）。别人（典型是启动预热）正持锁安装时，本请求不
    # 干等：npm i -g 动辄数十秒，会把调用方的 tools/call 预算（toolport
    # 网关为 30s）耗光。等不到就返回 installing 状态，让调用方轮询。
    INSTALL_LOCK_TIMEOUT = 5.0
    # P0 修复（install 执行门禁）：install.command 来自**进版本库**的
    # data/tools/*.json，旧逻辑让它在「服务启动预热」和「start_backend」两条
    # 路径上被无条件 create_subprocess_shell 执行——来源不可控、无任何
    # destructive/allowDestructive 门禁，与「launch_tool 只返回命令不执行」
    # 的取舍正好相反，构成最小权限倒置。现在**默认拒绝执行**，只有显式授权
    # 才放行，放行后还要再过一道命令形态白名单。
    INSTALL_ENV_VAR = "AWAM_AUTO_INSTALL"
    INSTALL_ENV_TRUTHY = {"1", "true", "yes", "on"}
    # 授权后也只放行常见包管理器的安装形式：命令按空白切词，前缀必须落在
    # 下表内（大小写不敏感）。
    INSTALL_COMMAND_ALLOWLIST: tuple[tuple[str, ...], ...] = (
        ("npm", "i", "-g"),
        ("npm", "install", "-g"),
        ("npm", "install", "--global"),
        ("pnpm", "add", "-g"),
        ("pnpm", "add", "--global"),
        ("yarn", "global", "add"),
        ("winget", "install"),
        ("scoop", "install"),
        ("pip", "install"),
        ("pip3", "install"),
        ("pipx", "install"),
        ("uv", "pip", "install"),
    )
    # shell 元字符：命中即拒绝。install.command 走 create_subprocess_shell，
    # 一个 `&&` / `;` / `$()` 就能在前缀合法的情况下串联任意命令，
    # 前缀白名单必须配合这条才有效。
    INSTALL_FORBIDDEN_CHARS = "&|;<>$`\\\"'\n\r"
    # 工具列表缓存有效期（秒）
    TOOLS_CACHE_TTL = 300

    # A3 方案 2：单后端串行，任意时刻只允许一个 call_tool 在飞；
    # 并发调用直接报 backend_busy（不排队）。要改造成「信号量上限 + 等待队列」，
    # 只需把这里调大并配合 asyncio.Semaphore + 排队超时，其余逻辑不变。
    MAX_CONCURRENT_CALLS = 1

    VALID_LOG_LEVELS = ["debug", "info", "warning", "error"]
    # 级别优先级：低于当前 log_level 的消息不入队
    LOG_LEVEL_PRIORITY = {"debug": 10, "info": 20, "warning": 30, "error": 40}

    # 连接任务收尸的有界等待（秒）。stdio_client 的 MCP 规范拆除流程
    # （关 stdin → 等退出 → 杀进程树）通常 ~2s 内完成；10s 是宽松上限，
    # 超时再补一次 cancel，绝不在清理路径上无限等待。
    CONN_REAP_TIMEOUT = 10.0

    # 常见的「壳包裹」启动形式：connection.command 是壳程序，真正需要在
    # PATH 上找到的是壳后面的目标程序。不剥掉这层壳的话，
    # which("cmd.exe") 永远命中，「没安装」会被误判成「已安装」。
    # 键为壳名（小写、去 .exe），值为该壳「后面跟目标程序」的开关。
    SHELL_WRAPPERS: dict[str, set[str]] = {
        "cmd": {"/c", "/k"},
        "powershell": {"-command", "-c"},
        "pwsh": {"-command", "-c"},
        "bash": {"-c"},
        "sh": {"-c"},
    }

    def __init__(
        self,
        tool_id: str,
        connection_config: dict,
        install_config: dict | None = None,
    ):
        self.tool_id = tool_id
        self.connection_config = connection_config
        # 安装元数据（tools/*.json 的 install 字段）。此前它只是死元数据：
        # schema 声明了、没有任何代码读取，导致包没全局安装时 start() 必然
        # 失败且永不自愈。现在由 ensure_installed() 消费，见该方法文档。
        self.install_config = install_config
        self.session: ClientSession | None = None
        self.status = self.STATUS_STOPPED
        self.last_error: str | None = None
        self.started_at: float | None = None

        # 后端调用超时，可在 connection 配置里用 timeout 覆盖
        self.timeout = float(connection_config.get("timeout") or self.DEFAULT_TIMEOUT)

        # 日志
        self.logs: deque = deque(maxlen=500)  # 最多保留 500 条
        self.log_level = "info"  # debug / info / warning / error

        # 工具列表缓存
        self._cached_tools: list[dict] | None = None
        self._tools_cache_time: float = 0

        # 心跳
        self._heartbeat_task: asyncio.Task | None = None
        self._last_heartbeat: float = 0

        self._lock = asyncio.Lock()
        # 调用串行锁（A3 方案 2）：同一后端任意时刻只允许一个 call_tool 在飞，
        # 避免对同一个 stdio 会话并发发 JSON-RPC 造成的竞态。与 _lock（start/stop
        # 串行）分离，二者职责不同，不能共用同一把锁，否则会互相阻塞。
        self._call_lock = asyncio.Lock()
        # cleanup 独立的锁：心跳失败、stop()、start() 的失败回滚都可能触发清理，
        # 用一把专用锁保证清理串行执行且幂等，避免两个协程同时收尸同一个连接任务
        self._cleanup_lock = asyncio.Lock()

        # 连接生命周期（独占任务模式，详见 _connection_loop 文档）：
        #   _conn_task    持有整个 stdio_client + ClientSession 生命周期的任务
        #   _ready        连接就绪（或失败）信号，start() 等它
        #   _shutdown     停止信号，_cleanup() 置它、连接任务收到后自行拆除
        #   _conn_error   连接任务自行失败时记录原因，供 start() 转抛
        #   _stdio_context / _client_context 仅作内省引用，由连接任务创建和清理，
        #   任何其他代码不得对它们调用 __aexit__（跨任务退出 anyio 作用域会崩）
        self._conn_task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._shutdown: asyncio.Event | None = None
        self._conn_error: BaseException | None = None
        self._stdio_context = None
        self._client_context = None

        # 依赖自愈状态：_install_lock 保证并发预热/启动只装一次；
        # _install_attempted 保证同一客户端生命周期内不重复执行同一条安装
        # 命令（防 npm 被反复锤）。确需重试时重启服务或改配置触发重建即可。
        self._install_lock = asyncio.Lock()
        self._install_attempted = False

    def _log(self, level: str, message: str):
        """记录日志

        过滤必须发生在**入队之前**。self.logs 是定长 500 的 deque：心跳每 30s
        一条 debug，不在这一层拦掉的话，约 4 小时就能把队列填满并把真正的
        error 挤出去 —— 而排查故障时最需要的恰恰就是被挤掉的那部分。
        只在 get_logs() 里按 level 筛是没用的，那时数据已经丢了。
        """
        if self.LOG_LEVEL_PRIORITY.get(level, 20) < self.LOG_LEVEL_PRIORITY.get(self.log_level, 20):
            return

        timestamp = time.strftime("%H:%M:%S")
        self.logs.append({"time": timestamp, "level": level, "message": message})
        # 打印到 stderr，不能污染 stdout 协议通道
        print(f"[awam:{self.tool_id}][{level}] {message}", file=sys.stderr, flush=True)

    @classmethod
    def _resolved_command(cls, connection_config: dict) -> str | None:
        """解析出「真正要在 PATH 上找的程序」

        - 壳包裹（cmd /c foo）→ 返回 foo（剥掉外层引号）；
        - 直接形式（command: foo，或绝对路径）→ 返回 command 本身；
        - 没有 command → None。

        只在能确定「壳 + 开关 + 目标」三段结构时才剥壳；bash -c 跟一串
        脚本参数之类无法可靠解析的情况原样返回 command，由调用方兜底。
        """
        command = connection_config.get("command")
        if not command:
            return None
        args = [str(a) for a in connection_config.get("args", [])]
        # 壳名统一小写并去掉 .exe：cmd.exe 与 cmd 是同一个程序
        shell = Path(command).name.lower()
        if shell.endswith(".exe"):
            shell = shell[: -len(".exe")]
        switches = cls.SHELL_WRAPPERS.get(shell)
        if switches and len(args) >= 2 and args[0].lower() in switches:
            target = args[1].strip().strip('"')
            return target or None
        return command

    def _target_available(self, target: str | None) -> bool:
        """target 是否已在 PATH 上。

        解析不出目标（None）时返回 True：不把「判不了」当成「缺失」，
        交给 spawn 产出原生错误，避免误装、误判。
        """
        if not target:
            return True
        return shutil.which(target) is not None

    def _install_command(self) -> str | None:
        """install.command；配置缺省或畸形时返回 None，绝不让坏数据炸掉启动"""
        install = self.install_config if isinstance(self.install_config, dict) else {}
        command = install.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        return command

    def _install_authorized(self) -> bool:
        """本条目的 install.command 是否被授权自动执行

        两个授权来源，任一即可：

        1. 环境变量 `AWAM_AUTO_INSTALL` 为真 —— 部署者（本机用户）在 MCP
           客户端配置里显式打开，语义是「我知道这会执行 shell，我同意」；
        2. 条目 `install.auto` 严格为 `true` —— 数据文件自己逐工具 opt-in。

        默认都不开。install.command 与 connection.command 有本质区别：
        后者是「启动本工具必需的参数」，前者是一条**会改系统状态的命令**，
        且写在进版本库的数据文件里——不能默认就替用户跑。
        """
        if install_auto_authorized_by_env():
            return True
        install = self.install_config if isinstance(self.install_config, dict) else {}
        return install.get("auto") is True

    @classmethod
    def _install_command_allowed(cls, command: str) -> tuple[bool, str]:
        """授权之后的第二道闸：命令形态是否在受支持的白名单内

        返回 `(是否放行, 拒绝原因)`。两道条件同时满足才放行：

        1. 不含 shell 元字符 —— 命令经 `create_subprocess_shell` 走 shell
           解释，一个 `&&` / `;` / `$(...)` 就能在前缀合法的情况下串联
           任意命令，所以前缀白名单必须配合这条才有效；
        2. 按空白切词后的前缀命中 `INSTALL_COMMAND_ALLOWLIST`，且前缀
           **之后还必须跟有参数**（包名）—— 裸的 `npm i -g` 什么都不装，
           却同样是一条会走 shell 的命令，没有放行的理由。
        """
        bad = sorted({c for c in command if c in cls.INSTALL_FORBIDDEN_CHARS})
        if bad:
            return False, f"包含 shell 元字符 {' '.join(bad)}"
        tokens = [t.lower() for t in command.split()]
        for prefix in cls.INSTALL_COMMAND_ALLOWLIST:
            if tokens[: len(prefix)] == list(prefix) and len(tokens) > len(prefix):
                return True, ""
        allowed = " / ".join(" ".join(p) for p in cls.INSTALL_COMMAND_ALLOWLIST)
        return False, f"前缀不在白名单内（仅支持: {allowed}）"

    @staticmethod
    async def _kill_and_reap(proc, reap: bool = True) -> None:
        """杀掉安装子进程并回收（超时 / 被取消时调用，避免孤儿 npm）"""
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.kill()
        if reap:
            with contextlib.suppress(Exception):
                await proc.wait()

    async def ensure_installed(self) -> dict:
        """确保后端的可执行依赖已安装（按需、幂等、有界等待）

        背景：tools/*.json 的 `install` 字段此前是死元数据——schema 声明了
        它，却没有任何代码读取。后果：包没有全局安装时 start() 必然失败，
        且每次重试都一样失败（命令缺失是秒级失败，不会卡住，但也永远不会自愈）。

        策略：
        1. 解析不出启动目标（例如没配 command）→ 不装也不阻塞，如实报告
           available=False，由 spawn 产出原生错误；
        2. 先做 PATH 自愈（ensure_npm_bin_on_path）：上游可能用裁剪过的
           环境启动本服务，不补齐就会把已装好的包误判成缺失，误触发
           数十秒的无效安装；
        3. 启动命令的真实目标已在 PATH 上 → 直接返回，什么都不做；
        4. 没有 install.command → 返回 available=False 但**不阻塞**：由
           spawn 产出原生错误，行为与接入前完全一致，不把「本来能起」的
           配置误判死（例如 bash -c 跟一串脚本参数，which 无从判定）；
        5. 有 install.command → **先过 P0 门禁**：未显式授权（环境变量
           AWAM_AUTO_INSTALL 或条目 install.auto=true）就不执行；授权了
           还要命令形态落在白名单内才执行。两道都过后才加锁执行一次安装
           （限时 INSTALL_TIMEOUT），装完复检。**锁等待有上限**
           （INSTALL_LOCK_TIMEOUT）：别人正在装（典型是启动预热持锁跑
           npm i -g）时返回 installing 让调用方轮询，不把调用方的
           tools/call 预算耗在干等上。安装真正跑完（无论成败）才记
           attempted；被取消（调用方断开/网关超时取消）时不标记，重试
          还能再装。

        返回 {"success", "available", "installed", "installing"?, "installBlocked"?}：
        success=False 仅表示「装了但还没好」「正在装」或「安装执行失败」。
        被门禁拦下（installBlocked = "unauthorized" / "not_allowed"）时
        success 仍为 True——那是**策略性拒绝**，不是执行失败，由
        installBlocked 表达，调用方据此决定是否阻断启动。
        调用方（start / 启动预热）只记录不阻断——自装只会让启动更容易
        成功，不会让任何原本能启动的配置变得更糟。
        """
        target = self._resolved_command(self.connection_config)
        if target is None:
            # 解析不出启动目标（例如没配 command）：既装不了也验不了，
            # 不阻塞，由 spawn 产出原生错误。
            self._log("debug", "无法从 connection 解析出启动命令目标，跳过自装")
            return {
                "success": True,
                "available": False,
                "installed": False,
                "note": "无法解析启动命令",
            }
        # PATH 自愈：补齐 npm 全局 bin 后再判断「是否已安装」，
        # 避免把已装好的包误判为缺失、触发数十秒的无效安装。
        await ensure_npm_bin_on_path()
        if self._target_available(target):
            return {"success": True, "available": True, "installed": False}

        install_command = self._install_command()
        if not install_command:
            self._log(
                "debug",
                f"启动目标 {target or '(未配置 command)'} 不在 PATH，"
                f"且未配置 install.command，跳过自装",
            )
            return {
                "success": True,
                "available": False,
                "installed": False,
                "note": "未配置安装命令",
            }

        # ── P0 门禁第一道：显式授权 ──────────────────────────────
        # install.command 来自进版本库的 data/tools/*.json，且会经
        # create_subprocess_shell 走 shell 解释。旧逻辑让它无条件执行
        # （服务启动预热 + start_backend 两条路径），与「launch_tool 只返回
        # 命令不执行」方向相反。没人明确同意之前，一条都不跑。
        if not self._install_authorized():
            self._log(
                "info",
                f"启动目标 {target} 不在 PATH，但未授权自动安装"
                f"（设 {self.INSTALL_ENV_VAR}=1 或条目 install.auto=true 可开启），跳过自装",
            )
            return {
                "success": True,
                "available": False,
                "installed": False,
                "installBlocked": "unauthorized",
                "note": (
                    f"依赖缺失且未授权自动安装：如需自动执行 install.command，"
                    f"请设环境变量 {self.INSTALL_ENV_VAR}=1，或在条目里配 install.auto=true"
                ),
            }

        # ── P0 门禁第二道：命令形态白名单 ────────────────────────
        allowed, reason = self._install_command_allowed(install_command)
        if not allowed:
            self._log("error", f"安装命令被门禁拒绝（{reason}）: {install_command}")
            return {
                "success": True,
                "available": False,
                "installed": False,
                "installBlocked": "not_allowed",
                "error": f"安装命令被门禁拒绝（{reason}）: {install_command}",
            }

        # 加锁执行一次安装。锁等待有上限（INSTALL_LOCK_TIMEOUT）：预热
        # 可能正持锁跑 npm i -g（数十秒），干等会把调用方的 tools/call
        # 预算耗光——等不到就返回 installing，让调用方轮询。
        # 限时用 anyio 原生的 fail_after（原因见 start() 中注释）。
        try:
            with anyio.fail_after(self.INSTALL_LOCK_TIMEOUT):
                await self._install_lock.acquire()
        except TimeoutError:
            self._log("info", "另一任务正在安装依赖，本次不等待，返回 installing")
            return {
                "success": False,
                "available": False,
                "installed": False,
                "installing": True,
                "note": "依赖正在安装中，请稍后重试",
            }

        try:
            # 双检：等锁期间可能已被其他协程（如启动预热）装好
            if self._target_available(target):
                return {"success": True, "available": True, "installed": False}
            if self._install_attempted:
                return {
                    "success": False,
                    "available": False,
                    "installed": False,
                    "error": "安装已尝试过，跳过重复执行",
                }

            self._log("info", f"检测到依赖缺失，执行安装: {install_command}")
            proc = None
            try:
                proc = await asyncio.create_subprocess_shell(
                    install_command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    # stdin 同 npm prefix -g：必须 DEVNULL，否则 npm 子
                    # 进程继承网关管道后阻塞在 stdin 上（见
                    # ensure_npm_bin_on_path 中的根因说明）。
                    stdin=asyncio.subprocess.DEVNULL,
                )
                with anyio.fail_after(self.INSTALL_TIMEOUT):
                    _, stderr = await proc.communicate()
            except TimeoutError:
                await self._kill_and_reap(proc)
                error = f"安装超时（>{self.INSTALL_TIMEOUT:.0f}s）: {install_command}"
                self._log("error", error)
                return {
                    "success": False,
                    "available": False,
                    "installed": False,
                    "error": error,
                }
            except asyncio.CancelledError:
                # 调用方断开/网关超时取消：杀子进程防孤儿，且**不**标记
                # attempted——安装并未跑完，重试时应该还能再装。
                await self._kill_and_reap(proc, reap=False)
                raise
            except Exception as e:
                error = f"安装执行失败: {type(e).__name__}: {e}"
                self._log("error", error)
                return {
                    "success": False,
                    "available": False,
                    "installed": False,
                    "error": error,
                }

            # 安装真正跑完（拿到退出码）才记 attempted：被取消时不标记，
            # 否则一次取消就会让后续重试永远跳过安装。
            self._install_attempted = True

            if proc.returncode != 0:
                tail = (stderr or b"").decode("utf-8", "replace").strip()
                if len(tail) > 300:
                    tail = "..." + tail[-300:]
                error = f"安装命令退出码 {proc.returncode}" + (f": {tail}" if tail else "")
                self._log("error", error)
                return {
                    "success": False,
                    "available": False,
                    "installed": False,
                    "error": error,
                }

            if self._target_available(target):
                self._log("info", "✅ 依赖安装成功")
                return {"success": True, "available": True, "installed": True}

            error = "安装命令执行成功，但启动目标仍不在 PATH"
            self._log("warning", error)
            return {
                "success": False,
                "available": False,
                "installed": True,
                "error": error,
            }
        finally:
            self._install_lock.release()

    async def _connection_loop(self, params: StdioServerParameters) -> None:
        """连接独占任务：stdio_client + ClientSession 的一生都在本任务内

        为什么需要独占任务（2026-10-16 实证；toolport 网关 4/4 启动即崩
        与 4/4 调用超时是同一族故障）：
        anyio 的 CancelScope 绑定「进入它的任务」。stdio_client 与
        ClientSession 的 __aenter__ 都会进入各自内部 TaskGroup 的取消
        作用域。若在请求任务里进入、事后再退出，会发生两件事：
        1. 请求任务的作用域栈上留下一个永不闭合的内层作用域。mcp SDK 的
           RequestResponder 在我们的工具响应发出**之后**才退出，一退出就
           踩空：RuntimeError: Attempted to exit a cancel scope that
           isn't the current tasks's current cancel scope → TaskGroup
           收集 → 整个 Server 进程 exit 1。客户端其实已收到成功响应，
           「莫名其妙」的崩溃发生在响应之后；
        2. 清理任务调用 __aexit__ 时报 Attempted to exit cancel scope
           in a different task than it was entered in（被 _cleanup 兜住
           只记警告，但作用域永不闭合，anyio 内部状态泄漏）。

        正确姿势（anyio 结构化并发）：上下文的进入与退出在**同一任务**内
        完成。本任务即连接的所有者——spawn、握手、持有、拆除都在这里；
        其他任务（请求 / 心跳 / 清理）只使用 self.session（memory
        stream + future，跨任务安全）或通过 _ready/_shutdown 事件协作。

        退出路径：
        - 正常停止：_cleanup 置 _shutdown，本任务退出 async with，
          stdio_client 按 MCP 规范拆除（关 stdin → 等退出 → 杀进程树）；
        - start 超时 / 调用方取消：_reap_conn_task(cancel=True) 取消本
          任务，CancelledError 在 async with 的退出逻辑中传播，拆除照常；
        - 自行失败（spawn 失败 / 握手失败 / 后端死亡）：异常记入
          _conn_error 并置 _ready 唤醒 start()，由 start() 转抛。
        """
        try:
            self._stdio_context = stdio_client(params)
            async with self._stdio_context as (read_stream, write_stream):
                self._client_context = ClientSession(read_stream, write_stream)
                # 注意：必须用 async with 而不是手动 __aenter__/__aexit__——
                # 手动调用漏掉 __aexit__ 会让 ClientSession 内部的 TaskGroup
                # 取消作用域永不闭合（anyio 状态泄漏）；async with 保证
                # 正常退出 / 异常 / 取消三条路径都在本任务内闭合。
                async with self._client_context as session:
                    self.session = session
                    # 握手限时用 anyio 原生 fail_after：asyncio.wait_for 会
                    # 从外部取消协程，撕裂 anyio 的取消作用域栈，把整个 MCP
                    # Server 进程带崩（实测报 Attempted to exit a cancel
                    # scope...）。fail_after 在同一任务内取消，作用域栈保持
                    # 完整，超时干净地抛 TimeoutError。
                    with anyio.fail_after(self.timeout):
                        await session.initialize()
                    self.status = self.STATUS_RUNNING
                    self.started_at = time.time()
                    self._last_heartbeat = time.time()
                    # 就绪（或失败）信号放在最后：start() 醒来时状态必已一致
                    self._ready.set()
                    # 持有连接直到收到停止信号；后端死亡由心跳检测后走清理
                    await self._shutdown.wait()
        except asyncio.CancelledError:
            self._conn_error = asyncio.CancelledError("连接任务被取消")
            raise
        except BaseException as e:  # noqa: BLE001 —— 失败原因要带回给 start()
            self._conn_error = e
        finally:
            # 本任务终结即连接已断：清掉所有对外引用，避免别人用到死会话；
            # _ready 必须再置一次，覆盖「失败发生在就绪之前」的情况。
            self.session = None
            self._stdio_context = None
            self._client_context = None
            if self._ready is not None:
                self._ready.set()

    async def _reap_conn_task(self, cancel: bool = False) -> None:
        """结束连接任务并等它收尸（_cleanup / start 失败路径共用）

        两种退出方式都保证 anyio 作用域在连接任务内闭合：
        - cancel=False：置 _shutdown，任务自行退出 async with（stdio_client
          按 MCP 规范关 stdin → 等退出 → 杀进程树，后端有机会优雅退出）；
        - cancel=True ：直接取消任务（start 超时 / 调用方被取消时用，
          等不及优雅退出）。

        有界等待（CONN_REAP_TIMEOUT）：绝不无限期占用清理方——清理可能
        跑在 stop_backend 请求任务里，调用方还有 tools/call 预算。超时
        补一次 cancel 再短等；join 本身被打断（调用方持续取消）也不留
        孤儿：任务已收到取消信号，会自行完成拆除。
        """
        task, self._conn_task = self._conn_task, None
        shutdown, self._shutdown = self._shutdown, None
        self._conn_error = None
        if task is None:
            return
        if not task.done():
            if shutdown is not None:
                shutdown.set()
            if cancel:
                task.cancel()
            _, pending = await asyncio.wait({task}, timeout=self.CONN_REAP_TIMEOUT)
            if pending:
                self._log("warning", "连接任务未能在时限内自行退出，强制取消")
                task.cancel()
                await asyncio.wait({task}, timeout=5)
        # 任务已终结：取出异常避免 "exception was never retrieved" 警告
        if not task.cancelled():
            with contextlib.suppress(Exception):
                exc = task.exception()
                if exc is not None:
                    self._log(
                        "warning", f"连接任务异常退出: {type(exc).__name__}: {exc}"
                    )

    async def start(self) -> dict:
        """启动后端 MCP Server 并建立连接"""
        if self.status == self.STATUS_RUNNING:
            return {"success": True, "message": "已在运行中"}

        async with self._lock:
            if self.status == self.STATUS_RUNNING:
                return {"success": True, "message": "已在运行中"}

            # 上一次连接可能残留（例如心跳失败后置为 error 但没清理完），
            # 必须先收尸干净，否则旧的子进程会变成孤儿进程
            if self._conn_task or self.session:
                self._log("warning", "检测到上次连接的残留资源，先清理")
                await self._cleanup()

            self.status = self.STATUS_STARTING
            self.last_error = None
            self._log("info", "正在启动后端 MCP...")

            try:
                conn_type = self.connection_config.get("type", "stdio")
                if conn_type != "stdio":
                    raise NotImplementedError(f"暂不支持的连接类型: {conn_type}")

                # 依赖自装：启动命令的目标不在 PATH 且配了 install 时，先自动
                # 安装再 spawn。安装失败不阻断——照旧 spawn，让原生错误说话，
                # 行为与接入前完全一致（策略详见 ensure_installed）。
                install_result = await self.ensure_installed()
                if install_result.get("installing"):
                    # 别人正在装（典型是启动预热持锁跑 npm i -g）：此时命令
                    # 还不在 PATH 上，spawn 只会拿到原生「命令不存在」错误。
                    # 直接返回 installing 让调用方轮询——调用方（toolport
                    # 网关）的 tools/call 预算只有 30s，不能耗在干等安装上。
                    self.status = self.STATUS_STOPPED
                    message = "依赖正在安装中，请稍后重试 start_backend"
                    self._log("info", message)
                    return {
                        "success": False,
                        "status": "installing",
                        # Q2（P1-03）：依赖安装中是分钟级忙态、不是错误——
                        # 与 backend_busy 的秒级并发冲突分工不同（复用会
                        # 混淆客户端重试语义），单列枚举第 16 种取值；
                        # 契约卡要求「可重试」（SKILL.md 已同步）
                        "errorType": ErrorType.BACKEND_INSTALLING,
                        "message": message,
                    }
                blocked = install_result.get("installBlocked")
                if blocked and not install_result.get("available"):
                    # P0 门禁拦下且依赖确实缺失：不 spawn—— spawn 只会拿到
                    # 「命令不存在」的原生错误，把真实原因（没授权/命令不合规）
                    # 盖掉。直接返回带 errorType 的结构化失败，调用方知道该
                    # 干什么（开授权 / 换命令），而不是对着 FileNotFoundError 猜。
                    self.status = self.STATUS_STOPPED
                    error_type = (
                        ErrorType.INSTALL_NOT_ALLOWED
                        if blocked == "not_allowed"
                        else ErrorType.INSTALL_REQUIRED
                    )
                    message = (
                        install_result.get("note")
                        or install_result.get("error")
                        or "依赖缺失"
                    )
                    self._log("warning", message)
                    return {
                        "success": False,
                        # Q1（P1-03）：status 曾误用 errorType 值（一个字段
                        # 承载两套语义，客户端按 status 判断后端状态会看到
                        # 不存在的取值）。恢复为状态机会话值——本次启动以
                        # 错误收场；错误分类只由 errorType 表达。
                        "status": "error",
                        "errorType": error_type,
                        "message": message,
                    }
                if install_result.get("installed"):
                    self._log("info", "依赖已自动安装，继续启动")

                command = self.connection_config.get("command")
                args = self.connection_config.get("args", [])
                env = self.connection_config.get("env")

                # args 里可能是数字/布尔，拼接前统一转字符串
                self._log("info", f"启动命令: {command} {' '.join(str(a) for a in args)}")

                params = StdioServerParameters(command=command, args=args, env=env)

                # 连接的整个生命周期收拢进独占任务（原因见 _connection_loop：
                # anyio CancelScope 任务绑定，跨任务进入/退出会崩）。本任务
                # 只负责创建它并等待就绪信号；持有与拆除都在连接任务内完成。
                self._shutdown = asyncio.Event()
                self._ready = asyncio.Event()
                self._conn_error = None
                self._conn_task = asyncio.create_task(
                    self._connection_loop(params),
                    name=f"awam-conn:{self.tool_id}",
                )
                try:
                    # 限时用 anyio 原生 fail_after：asyncio.wait_for 会从
                    # **外部任务**取消协程，撕裂 anyio 的取消作用域栈（实测
                    # 报 RuntimeError: Attempted to exit a cancel scope
                    # that isn't the current tasks's current cancel
                    # scope），把整个 MCP Server 进程带崩。fail_after 在
                    # 同一任务内取消，作用域栈保持完整，超时干净地抛
                    # TimeoutError。
                    with anyio.fail_after(self.timeout):
                        await self._ready.wait()
                except TimeoutError:
                    self._log("error", f"连接后端超时（{self.timeout}s）")
                    await self._reap_conn_task(cancel=True)
                    raise TimeoutError(f"连接后端 MCP 超时（{self.timeout}s）")
                except BaseException:
                    # 被取消（调用方断开 / 网关超时取消）或任何异常：都不能
                    # 把连接任务留成孤儿——子进程会活过整个服务
                    await self._reap_conn_task(cancel=True)
                    raise

                if self._conn_error is not None:
                    # 连接任务自行失败（spawn 失败 / 握手失败）：它已在
                    # 任务内自行退出全部上下文，这里只负责转抛
                    err = self._conn_error
                    self._conn_error = None
                    if isinstance(err, asyncio.CancelledError):
                        err = RuntimeError("连接任务被取消")
                    await self._reap_conn_task()
                    raise err

                self._log("info", "✅ 连接成功")

                # 启动心跳检测
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                # 预热工具列表
                await self._refresh_tools_cache()

                return {"success": True, "message": "启动成功"}

            except Exception as e:
                self.status = self.STATUS_ERROR
                self.last_error = f"{type(e).__name__}: {e}"
                self._log("error", f"❌ 启动失败: {self.last_error}")
                # 清理资源（连接任务已终结时这里是空转，幂等）
                await self._cleanup()
                return {
                    "success": False,
                    "errorType": ErrorType.RUNTIME_ERROR,
                    "error": self.last_error,
                }
            except BaseException as e:
                # start() 会被外部取消：toolport 网关对每次 tools/call 有 30s
                # 预算、超时即取消请求；daemon 周期回收、调用方断开同理。
                # CancelledError 在 Python 3.8+ 继承 BaseException，上面的
                # except Exception 接不住——若不兜底，status 自 621 行置入
                # starting 后永远无人收回，之后所有 get/status/list 调用只会
                # 看到「后端未运行，当前状态: starting」且无任何自愈路径
                # （2026-10-16 实测：被取消的 start 让一个会话实例卡在
                # starting 13+ 分钟，用户侧表现为后端永远起不来）。
                # 取消必须继续向上抛（吞取消会破坏调用方语义），但先把状态
                # 落地、把可能已创建的连接/心跳收尸，防止孤儿子进程。
                self.status = self.STATUS_STOPPED
                self.last_error = f"{type(e).__name__}: 启动被取消"
                self._log("warning", f"启动被取消（{type(e).__name__}），状态已从 starting 收回")
                await self._cleanup()
                raise

    async def stop(self) -> dict:
        """停止后端 MCP Server

        与 start() 共用同一把锁：并发调用时不会有「start 刚建好连接、stop 已把它
        标记为 stopped」这类状态与真实连接不一致的情况。
        """
        if self.status == self.STATUS_STOPPED:
            return {"success": True, "message": "已停止"}

        # 取消心跳任务
        task, self._heartbeat_task = self._heartbeat_task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        async with self._lock:
            if self.status == self.STATUS_STOPPED:
                return {"success": True, "message": "已停止"}

            self._log("info", "正在停止...")
            await self._cleanup()

        self.status = self.STATUS_STOPPED
        self._cached_tools = None
        self._tools_cache_time = 0
        self.started_at = None
        self._log("info", "已停止")
        return {"success": True, "message": "已停止"}

    async def _cleanup(self):
        """清理连接资源（串行且幂等）

        新语义（独占任务模式）：不再直接对 context 调 __aexit__——那会把
        在别的任务里进入的 anyio 取消作用域拉到本任务退出，触发
        "Attempted to exit cancel scope in a different task than it was
        entered in"，且作用域永不闭合。改为：置 _shutdown 通知连接任务
        自行退出 async with，并有界 join 等它把子进程收尸；任务迟迟不
        结束才补刀取消。细节见 _reap_conn_task。
        """
        async with self._cleanup_lock:
            if self._conn_task is None and self.session is None:
                return
            # 先摘会话引用：并发的 call_tool 会立刻看到「后端未运行」，
            # 而不是对着一个正在死去的会话干等
            self.session = None
            await self._reap_conn_task(cancel=False)

    def _mark_broken(self, reason: str):
        """连接已不可用时调用：置为 error 并在后台清理，避免继续复用坏连接"""
        self.status = self.STATUS_ERROR
        self.last_error = reason
        self._log("error", f"连接不可用: {reason}")
        if self._conn_task is not None:
            asyncio.create_task(self._safe_cleanup())

    async def _safe_cleanup(self):
        """后台清理，吞掉异常避免在 task 里抛出无人接收的异常"""
        try:
            await self._cleanup()
        except Exception as e:
            self._log("warning", f"自动清理失败: {e}")

    async def _heartbeat_loop(self):
        """心跳检测循环"""
        while self.status == self.STATUS_RUNNING:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                if self.session:
                    # anyio 原生限时：wait_for 会从外部任务取消协程，撕裂
                    # anyio 取消作用域栈并带崩整个 Server 进程（详见 start()）
                    with anyio.fail_after(self.HEARTBEAT_TIMEOUT):
                        # 探测用 send_ping：mcp 1.x 的 ClientSession 只有
                        # send_ping()，没有 ping()（TS SDK 才叫 ping）。
                        # 曾误用 ping()：生产环境（pyenv 3.12 + mcp 1.29.0）
                        # 每次启动后端后心跳必败 → 状态 error → 连接被清 →
                        # 后端永远留不住（2026-10-16 网关实测复现）。
                        # 测试环境装 1.30 且心跳 fake 自带 ping，双重掩盖。
                        await self.session.send_ping()
                    self._last_heartbeat = time.time()
                    self._log("debug", "心跳正常")
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 心跳失败说明连接已断：标记不可用并主动清理，
                # 否则子进程会一直残留，且状态会停在虚假的 running
                self.status = self.STATUS_ERROR
                self.last_error = f"心跳失败: {e}"
                self._log("warning", f"心跳失败，连接标记为不可用: {e}")
                await self._cleanup()
                break

    async def _refresh_tools_cache(self):
        """刷新工具列表缓存"""
        if not self.session:
            return
        try:
            # anyio 原生限时（不用 asyncio.wait_for，原因见 start() 注释）
            with anyio.fail_after(self.timeout):
                tools = await self.session.list_tools()
            self._cached_tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema
                }
                for t in tools.tools
            ]
            self._tools_cache_time = time.time()
            self._log("info", f"工具列表已刷新，共 {len(self._cached_tools)} 个工具")
        except TimeoutError:
            self._mark_broken(f"刷新工具列表超时（{self.timeout}s）")
        except Exception as e:
            self._log("warning", f"刷新工具列表失败: {e}")

    async def list_tools(self, force_refresh: bool = False) -> list[dict]:
        """列出后端 MCP 提供的所有工具（带缓存）"""
        if self.status != self.STATUS_RUNNING:
            raise ConnectionError(f"后端未运行，当前状态: {self.status}")

        # 缓存有效期见 TOOLS_CACHE_TTL
        cache_age = time.time() - self._tools_cache_time
        if force_refresh or not self._cached_tools or cache_age > self.TOOLS_CACHE_TTL:
            await self._refresh_tools_cache()

        return self._cached_tools or []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用后端 MCP 的工具

        A3 方案 2（单后端串行 + 超限报 busy）：任意时刻只允许一个 call_tool 在飞。
        若已有调用在执行，立即抛 BackendBusyError 由上层转成 backend_busy，
        不排队、不积压。这样既能避免对同一个 stdio 会话并发发 JSON-RPC 的竞态，
        又不会因无界排队把请求闷在服务内部。
        """
        if self.status != self.STATUS_RUNNING:
            raise ConnectionError(f"后端未运行，当前状态: {self.status}")

        # 单后端串行：已有调用在飞则直接拒绝（只判断不阻塞，避免此处自己 await 造成误判）
        if self._call_lock.locked():
            raise BackendBusyError(
                f"后端工具 '{self.tool_id}' 正在处理其他调用，"
                f"请稍后重试（单后端串行，并发调用会直接拒绝）"
            )

        self._log("debug", f"调用工具: {tool_name}")

        async with self._call_lock:
            try:
                # anyio 原生限时（不用 asyncio.wait_for，原因见 start() 注释）
                with anyio.fail_after(self.timeout):
                    result = await self.session.call_tool(tool_name, arguments)
            except TimeoutError:
                # 超时通常意味着连接已经废了，标记后让后续请求快速失败而不是继续卡
                reason = f"调用工具 '{tool_name}' 超时（{self.timeout}s）"
                self._mark_broken(reason)
                raise TimeoutError(reason) from None
            except Exception as e:
                self._log("error", f"调用工具失败: {e}")
                raise RuntimeError(
                    f"后端 MCP 调用工具 '{tool_name}' 失败: {type(e).__name__}: {e}"
                ) from e

            # 检查后端返回是否有错误
            if hasattr(result, "isError") and result.isError:
                error_texts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        error_texts.append(content.text)
                error_msg = f"后端 MCP 返回错误: {' | '.join(error_texts)}"
                self._log("error", error_msg)
                raise RuntimeError(error_msg)

            # 提取文本结果
            texts = []
            for content in result.content:
                if hasattr(content, "text"):
                    texts.append(content.text)
            return "\n".join(texts)

    def get_status(self) -> dict:
        """获取状态信息"""
        uptime = None
        if self.started_at and self.status == self.STATUS_RUNNING:
            uptime = round(time.time() - self.started_at, 1)

        return {
            "tool_id": self.tool_id,
            "status": self.status,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "uptime_seconds": uptime,
            "log_level": self.log_level,
            "tools_count": len(self._cached_tools) if self._cached_tools else 0,
            "last_heartbeat": self._last_heartbeat,
            "timeout": self.timeout,
        }

    def get_logs(self, level: str | None = None, limit: int = 50) -> list[dict]:
        """获取最近的日志

        limit<=0 明确返回空列表。这里不能用 logs[-limit:] 兜底：Python 里
        -0 == 0，切片 [0:] 返回的是**全部**日志 —— 传 0 本意是「不要」，
        结果变成「全量」，语义正好相反。
        """
        logs = list(self.logs)
        if level:
            logs = [l for l in logs if l["level"] == level]

        if limit is None or limit <= 0:
            return []
        return logs[-limit:]

    def set_log_level(self, level: str) -> dict:
        """设置日志级别"""
        valid_levels = self.VALID_LOG_LEVELS
        if level not in valid_levels:
            return {
                "success": False,
                "errorType": ErrorType.INVALID_PARAMS,
                "error": f"无效的日志级别，可选: {valid_levels}",
            }
        self.log_level = level
        self._log("info", f"日志级别设置为: {level}")
        return {"success": True, "log_level": level}


class BackendManager:
    """后端 MCP 连接管理器"""

    def __init__(self, store: JsonStore):
        self.store = store
        self.clients: dict[str, BackendClient] = {}

    def get_client(self, tool_id: str) -> BackendClient | None:
        """获取（或按需创建）指定 MCP 工具的后端客户端

        缓存会被复用，但创建时快照了一份 connection 配置用于比对：

        - 工具已从磁盘删除 / 不再是 mcp 类型 / 没有 connection → 不再返回旧
          缓存对象，避免出现「配置项已删但还能启后端」的幽灵连接；
        - 空闲（stopped/error，即没有活子进程）且磁盘配置已变 → 就地重建，
          改完 tools/*.json 不必重启 MCP Server 就能生效；
        - 正在运行/启动中时**绝不重建**：那会把正在跑的子进程丢成孤儿，
          且调用方手里的旧引用与被替换的新对象从此不是同一个。
        """
        # 运行中的连接先返回：热替换会让调用方手里的引用与新对象分家，
        # 也会把活着的子进程丢成孤儿。改配置请先 stop 再 start。
        client = self.clients.get(tool_id)
        if client is not None and client.status not in (
            BackendClient.STATUS_STOPPED,
            BackendClient.STATUS_ERROR,
        ):
            return client

        # 空闲（stopped/error）或从未连接：一律以磁盘上的当前配置为准
        tool_config = self.store.load_tool(tool_id)
        if not tool_config:
            return None

        # 只有 mcp 类型的工具才有后端连接
        if tool_config.get("kind") != "mcp":
            return None

        connection = tool_config.get("connection")
        if not connection:
            return None

        install_config = tool_config.get("install")

        # 配置没变就继续复用旧对象，避免平白丢掉日志历史。
        # install 也算配置的一部分：只改 install（例如补一条安装命令）时
        # 同样要重建，否则新的安装元数据不会生效。
        if (
            client is not None
            and client.connection_config == connection
            and client.install_config == install_config
        ):
            return client

        previous = self.clients.pop(tool_id, None)
        client = BackendClient(tool_id, connection, install_config=install_config)
        if previous is not None:
            # 继承日志与日志级别：重建不应让运维刚查过的历史日志凭空消失
            client.logs = previous.logs
            client.log_level = previous.log_level
        self.clients[tool_id] = client
        return client

    def list_mcp_tools(self) -> list[str]:
        """列出所有已配置的 MCP 类工具 ID"""
        tools = self.store.list_tools()
        return [t["id"] for t in tools if t.get("kind") == "mcp"]

    # ---- 期望状态持久化（跨 server 世代的后端自愈，A10）----

    def _load_desired_state(self) -> dict:
        """读取期望状态表；文件不存在 / 损坏都按空处理——恢复是尽力而为，
        绝不能因为状态文件的问题阻断服务启动。"""
        try:
            with open(DESIRED_STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_desired_state(self, state: dict) -> None:
        """原子写：本服务随时可能被网关 kill，截断写会留下半个 JSON。
        与 store.save_json 同样的临时文件 + os.replace 套路。"""
        DESIRED_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(DESIRED_STATE_FILE.parent),
            prefix=".desired.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, DESIRED_STATE_FILE)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def _set_desired(self, tool_id: str, desired: bool) -> None:
        """记录/清除某个后端的启动意图。写失败只警告不抛出——
        意图持久化是增强，不能让它把正常的 start/stop 拖失败。"""
        state = self._load_desired_state()
        if desired:
            state[tool_id] = {"desired": True, "updatedAt": _utc_now_iso()}
        else:
            state.pop(tool_id, None)
        try:
            self._save_desired_state(state)
        except OSError as e:
            _warn(f"期望状态写入失败（不影响本次操作）: {e}")

    async def restore_desired_backends(self) -> dict:
        """新 server 世代启动时恢复 desired=running 的后端

        每个被网关拉起的 server 进程都是「新世代」：内存里没有任何后端
        状态。这里把上次明确启动过的后端逐个拉起来，让用户跨网关回收
        也能随时查到 running。单个工具的失败不影响其他工具。
        """
        state = self._load_desired_state()
        results: dict[str, dict] = {}
        for tool_id, entry in state.items():
            if not (isinstance(entry, dict) and entry.get("desired")):
                continue
            client = self.get_client(tool_id)
            if client is None:
                # 工具已删除 / 不再是 mcp 类型：清掉过期记录，避免每代都白跑
                self._set_desired(tool_id, False)
                continue
            if client.status == BackendClient.STATUS_RUNNING:
                continue  # 已经活着（同代内重复调用），跳过
            try:
                results[tool_id] = await self.start_backend(tool_id)
            except Exception as e:  # noqa: BLE001 —— 单个后端恢复失败不阻断其他
                results[tool_id] = {"success": False, "error": f"{type(e).__name__}: {e}"}
        return results

    async def ensure_all_installed(self) -> dict:
        """启动预热：为所有 MCP 工具提前装好缺失的依赖（只装不启）

        动机：依赖自装接入后，第一次 start_backend 可能要现场等安装
        （npm i -g 可达数十秒）。MCP Server 启动时在后台预热一遍，
        把安装成本挪到服务启动阶段，首个 start_backend 不再被拖慢。

        只装不启：预热阶段拉起所有子进程会无谓占用资源，而这些工具
        可能很久都不会被调用。单个工具的失败不影响其他工具。

        **P0 之后**：预热不再意味着「一定会装」。门禁在 ensure_installed
        内部统一生效——未授权时预热退化为一次纯检测（只读 PATH，不执行
        任何外部命令），结果里 blocked 计数与 details 逐条说明缺谁、缺什么。
        """
        details = []
        for tool_id in self.list_mcp_tools():
            client = self.get_client(tool_id)
            if not client:
                continue
            try:
                result = await client.ensure_installed()
            except Exception as e:
                result = {"success": False, "error": f"{type(e).__name__}: {e}"}
            details.append({"tool_id": tool_id, **result})

        return {
            "success": True,
            "total": len(details),
            "available": sum(1 for d in details if d.get("available")),
            "installed": sum(1 for d in details if d.get("installed")),
            "blocked": sum(1 for d in details if d.get("installBlocked")),
            "details": details,
        }

    async def start_backend(self, tool_id: str) -> dict:
        """启动指定后端，成功后自动同步工具目录，并记录启动意图（A10）"""
        client = self.get_client(tool_id)
        if not client:
            return {
                "success": False,
                "errorType": ErrorType.CONFIG_ERROR,
                "error": f"工具 '{tool_id}' 未配置 MCP 后端连接",
            }

        result = await client.start()
        if result.get("success"):
            # 启动意图落盘：网关周期性回收 server 进程后，新世代靠它自动恢复
            self._set_desired(tool_id, True)
            # 自动同步工具目录
            sync_result = await self.sync_tools_catalog(tool_id)
            if sync_result.get("success"):
                result["message"] += f"\n工具目录已自动同步：{sync_result.get('count', 0)} 个工具"
            else:
                result["message"] += f"\n工具目录同步失败：{sync_result.get('error', '')}"
        return result

    async def sync_tools_catalog(self, tool_id: str) -> dict:
        """同步后端工具目录到 backend-tools/"""
        client = self.get_client(tool_id)
        if not client:
            return {
                "success": False,
                "errorType": ErrorType.CONFIG_ERROR,
                "error": f"MCP 工具 '{tool_id}' 未配置",
            }

        if client.status != BackendClient.STATUS_RUNNING:
            return {
                "success": False,
                "errorType": ErrorType.CONNECTION_ERROR,
                "error": "后端未运行，请先启动",
            }

        try:
            tools = await client.list_tools(force_refresh=True)

            # 后端异常时 list_tools 可能返回空列表，此时写盘会用空结果覆盖上一次
            # 成功同步的缓存，导致已缓存的工具目录永久丢失。宁可不同步。
            if not tools:
                return {
                    "success": False,
                    "error": "后端返回空工具列表，保留原缓存未覆盖"
                }

            # 保存到 backend-tools/ 目录
            self.store.save_backend_tools(tool_id, tools)

            return {"success": True, "count": len(tools)}
        except Exception as e:
            return {
                "success": False,
                "errorType": ErrorType.RUNTIME_ERROR,
                "error": f"同步失败: {type(e).__name__}: {e}",
            }

    async def stop_backend(self, tool_id: str) -> dict:
        """停止指定后端，并清除启动意图（A10：用户明确说不要了）"""
        client = self.get_client(tool_id)
        if not client:
            return {
                "success": False,
                "errorType": ErrorType.CONFIG_ERROR,
                "error": f"工具 '{tool_id}' 未配置 MCP 后端连接",
            }
        result = await client.stop()
        if result.get("success"):
            self._set_desired(tool_id, False)
        return result

    def get_backend_status(self, tool_id: str | None = None) -> dict:
        """获取后端状态"""
        if tool_id:
            client = self.get_client(tool_id)
            if not client:
                return {
                    "success": False,
                    "errorType": ErrorType.CONFIG_ERROR,
                    "error": f"MCP 工具 '{tool_id}' 未配置",
                }
            return {"success": True, "status": client.get_status()}

        # 返回所有后端状态
        all_status = []
        for mid in self.list_mcp_tools():
            client = self.get_client(mid)
            if client:
                all_status.append(client.get_status())
        return {"success": True, "backends": all_status}

    def get_backend_logs(self, tool_id: str, level: str | None = None, limit: int = 50) -> dict:
        """获取后端 MCP Server 的运行日志"""
        client = self.get_client(tool_id)
        if not client:
            return {
                "success": False,
                "errorType": ErrorType.CONFIG_ERROR,
                "error": f"MCP 工具 '{tool_id}' 未配置",
            }
        return {"success": True, "logs": client.get_logs(level, limit)}

    def set_backend_log_level(self, tool_id: str, level: str) -> dict:
        """设置后端 MCP Server 的日志级别"""
        client = self.get_client(tool_id)
        if not client:
            return {
                "success": False,
                "errorType": ErrorType.CONFIG_ERROR,
                "error": f"MCP 工具 '{tool_id}' 未配置",
            }
        return client.set_log_level(level)

    async def call_backend_tool(self, tool_id: str, tool_name: str, arguments: dict) -> dict:
        """路由调用：转发到后端 MCP"""
        client = self.get_client(tool_id)
        if not client:
            return {
                "success": False,
                "errorType": ErrorType.CONFIG_ERROR,
                "error": f"工具 '{tool_id}' 未配置 MCP 后端连接"
            }

        try:
            result = await client.call_tool(tool_name, arguments)
            return {"success": True, "result": result}
        except BackendBusyError as e:
            # A3 方案 2：后端正忙，直接透传 busy 类型化错误，调用方可重试
            return {
                "success": False,
                "errorType": ErrorType.BACKEND_BUSY,
                "error": str(e)
            }
        except ConnectionError as e:
            return {
                "success": False,
                "errorType": ErrorType.CONNECTION_ERROR,
                "error": f"无法连接后端 MCP: {e}"
            }
        except RuntimeError as e:
            return {
                "success": False,
                "errorType": ErrorType.RUNTIME_ERROR,
                "error": str(e)
            }
        except Exception as e:
            return {
                "success": False,
                "errorType": ErrorType.UNKNOWN_ERROR,
                "error": f"未知错误 ({type(e).__name__}): {e}"
            }

    async def list_backend_tools(self, tool_id: str, force_refresh: bool = False) -> dict:
        """列出后端 MCP 提供的工具（带完整参数 schema）"""
        client = self.get_client(tool_id)
        if not client:
            return {
                "success": False,
                "errorType": ErrorType.CONFIG_ERROR,
                "error": f"工具 {tool_id} 未配置 MCP 后端连接",
            }

        try:
            tools = await client.list_tools(force_refresh=force_refresh)
            return {"success": True, "tools": tools}
        except Exception as e:
            return {
                "success": False,
                "errorType": ErrorType.RUNTIME_ERROR,
                "error": f"获取后端工具列表失败: {str(e)}",
            }

    async def close_all(self):
        """关闭所有连接"""
        for client in self.clients.values():
            await client.stop()
        self.clients.clear()
