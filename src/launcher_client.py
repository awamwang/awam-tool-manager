"""启动器（非 MCP）适配器。

与 `BackendClient` 的关键区别：

- **不走 MCP 协议**：调用是一次性的外部命令（Quicker 的 `QuickerStarter.exe
  runaction:<id>`），没有长连接、心跳、也没有进程生命周期要维持。
- **动作清单不来自 tools/list**：启动器没有 MCP 接口，只能从它自己的本地数据
  里解析（Quicker 的动作数据在 `states/` 目录）。

首个实现是 Quicker。它的数据有个绕不开的限制：动作正文存在**加密的 quicker.db**
里（文件头魔数不是 SQLite，读不了），所以：

- 动作 **ID** 来自 `states/state_<uuid>.json` 的文件名（稳定、必得）
- 动作 **名称** 来自某些动作缓存下来的动作列表变量（`[url:类型]名称<程序>("描述")|UUID`），
  属于**尽力而为**：缓存没覆盖到的动作只有 ID。`source=partial` 就是在说这件事。

只有 ID 也能调用（`runaction:<uuid>` 是官方支持的标识），只是搜不到——
所以同步绝不因为缺名称而失败。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import anyio

DEFAULT_TIMEOUT = 20.0

# Quicker 动作清单缓存条目的文本格式（JSON 解析后的样子）：
#   [url:XAction]文件/全文搜索<_global>("用Everything搜索文件；用AnyTXT搜索全文")|92c14d1b-...
QUICKER_ACTION_RE = re.compile(
    r'\[url:(?P<type>[^\]]+)\](?P<name>[^<]+)<(?P<app>[^>]*)>\("(?P<desc>[^"]*)"\)\|(?P<id>[0-9a-fA-F-]{36})'
)

# 动作 ID 也体现在状态文件名上：states/state_<uuid>.json
QUICKER_STATE_FILE_RE = re.compile(r"^state_([0-9a-fA-F-]{36})\.json$", re.IGNORECASE)

# QuickerStarter 出错时**退出码仍然是 0**（2026-10-16 实测：找不到动作也返回 0），
# 因此不能只看 returncode，必须解析输出文本。
QUICKER_FAILURE_MARKERS = ("未能成功启动动作", "未能找到该动作", "Error：", "Error:")

# Quicker 在中文 Windows 上输出 GBK；按 UTF-8 解会得到乱码。
QUICKER_OUTPUT_ENCODING = "gbk"


class LauncherError(Exception):
    """启动器操作的业务级失败"""


def _log(tool_id: str, level: str, message: str) -> None:
    print(f"[awam:{tool_id}][{level}] {message}", file=sys.stderr, flush=True)


def _iter_strings(node: Any):
    """递归遍历 JSON 结构里的所有字符串值。

    启动器的动作列表常被塞在某个变量里、且是「JSON 数组的字符串」形式，
    只有取到字符串值才能匹配（见 `_scan_quicker` 里的说明）。
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_strings(value)


class LauncherClient:
    """单个启动器工具的适配器"""

    def __init__(self, tool: dict[str, Any], store: Any):
        self.tool_id = str(tool.get("id", ""))
        self.tool = tool
        self.store = store
        cfg = tool.get("launcher") or {}
        self.adapter = str(cfg.get("type") or "quicker")
        self.command = str(cfg.get("command") or "")
        data_dir = cfg.get("dataDir") or ""
        self.data_dir = Path(data_dir) if data_dir else None
        self.timeout = float(cfg.get("timeout") or DEFAULT_TIMEOUT)

    # ---- 动作清单同步 ----

    def sync_actions(self) -> dict[str, Any]:
        """扫描启动器本地数据，把动作清单落到 launcher-actions/"""
        if self.adapter != "quicker":
            raise LauncherError(f"暂不支持的启动器类型: {self.adapter}")
        if not self.data_dir:
            raise LauncherError("未配置 launcher.dataDir，无法扫描动作")

        actions, source = self._scan_quicker()
        self.store.save_launcher_actions(self.tool_id, actions, source)
        _log(self.tool_id, "info", f"动作清单已同步：{len(actions)} 个（source={source}）")
        return {
            "success": True,
            "toolId": self.tool_id,
            "count": len(actions),
            "source": source,
            "actions": actions,
        }

    def _scan_quicker(self) -> tuple[list[dict[str, Any]], str]:
        """返回 (动作列表, source)；缺名称不视为失败，只是降级为 partial"""
        states = self.data_dir / "states"
        if not states.exists():
            raise LauncherError(f"启动器数据目录不存在: {states}")

        found: dict[str, dict[str, Any]] = {}

        # 1) 动作 ID：状态文件名（Quicker 自己写的，只要动作存在就有）
        for path in states.glob("state_*.json"):
            matched = QUICKER_STATE_FILE_RE.match(path.name)
            if matched:
                action_id = matched.group(1).lower()
                found[action_id] = {
                    "id": action_id,
                    "name": "",
                    "type": "",
                    "app": "",
                    "description": "",
                }

        # 2) 动作名称：从任意 state 文件里缓存的动作列表变量解析
        #
        # 注意必须**先解析 JSON 再取字符串**：那段列表是「JSON 数组形式的字符串」，
        # 文件原文里引号写作 \"，直接拿正则扫原文永远匹配不到
        # （2026-10-16 实测：扫原文得到 0 个名称，改成取字符串值后才拿全）。
        for path in states.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            for text in _iter_strings(payload):
                for matched in QUICKER_ACTION_RE.finditer(text):
                    action_id = matched.group("id").lower()
                    found[action_id] = {
                        "id": action_id,
                        "name": matched.group("name").strip(),
                        "type": matched.group("type").strip(),
                        "app": matched.group("app").strip(),
                        "description": matched.group("desc").strip(),
                    }

        actions = sorted(found.values(), key=lambda a: (a.get("name") or "~", a["id"]))
        named = sum(1 for a in actions if a.get("name"))
        # 一个名称都没拿到才叫 partial 吗？不——只要有没有名字的动作就是 partial，
        # 这样调用方知道「搜索可能漏结果」。
        source = "auto" if actions and named == len(actions) else "partial"
        return actions, source

    # ---- 查询 ----

    def list_actions(
        self, keyword: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        actions = self.store.load_launcher_actions(self.tool_id)
        if keyword:
            needle = keyword.lower()

            def haystack(action: dict[str, Any]) -> str:
                return " ".join(
                    str(action.get(field) or "")
                    for field in ("name", "id", "description", "app", "type")
                ).lower()

            actions = [a for a in actions if needle in haystack(a)]
        if limit and limit > 0:
            actions = actions[:limit]
        return actions

    def find_action(
        self, action_id: str | None = None, action_name: str | None = None
    ) -> dict[str, Any] | None:
        """按 ID 或名称定位动作；先精确后包含"""
        actions = self.store.load_launcher_actions(self.tool_id)
        if action_id:
            needle = action_id.lower()
            for action in actions:
                if str(action.get("id", "")).lower() == needle:
                    return action
        if action_name:
            needle = action_name.strip().lower()
            for action in actions:
                if str(action.get("name") or "").lower() == needle:
                    return action
            for action in actions:
                if needle in str(action.get("name") or "").lower():
                    return action
        return None

    # ---- 调用 ----

    async def run_action(
        self,
        action_id: str | None = None,
        action_name: str | None = None,
        param: str | None = None,
        wait: float | None = None,
    ) -> dict[str, Any]:
        if not self.command:
            raise LauncherError("未配置 launcher.command，无法调用动作")
        if not action_id and not action_name:
            raise LauncherError("必须指定 action_id 或 action_name")

        # 清单里有就更稳（能校验存在性、拿到规范 ID）；清单缺也不阻塞——
        # 动作可能刚添加还没同步，runaction 接受 ID/名称/动作库 ID。
        action = self.find_action(action_id, action_name)
        target = action["id"] if action else (action_id or action_name or "")
        resolved_name = (action or {}).get("name") or ""

        wait_seconds = max(1, int(wait if wait is not None else self.timeout))
        argument = f"runaction:{target}" + (f"?{param}" if param else "")
        command = [self.command, f"-c{wait_seconds}", argument]

        _log(self.tool_id, "info", f"调用动作: {resolved_name or target}")
        try:
            with anyio.fail_after(wait_seconds + 5):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,  # A8：绝不继承 server 的 stdin 管道
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
        except TimeoutError:
            process.kill()  # 超时不留孤儿
            raise LauncherError(f"动作调用超时（等待 {wait_seconds}s 无返回）") from None
        except OSError as exc:
            raise LauncherError(f"无法启动 {self.command}: {exc}") from exc

        output = stdout.decode(QUICKER_OUTPUT_ENCODING, errors="replace").strip()
        error_output = stderr.decode(QUICKER_OUTPUT_ENCODING, errors="replace").strip()
        # 退出码不可信（失败也是 0），按文本判定；同时把原始输出交还调用方判断
        ok = not any(marker in output for marker in QUICKER_FAILURE_MARKERS)
        result: dict[str, Any] = {
            "success": ok,
            "toolId": self.tool_id,
            "actionId": target,
            "actionName": resolved_name,
            "exitCode": process.returncode,
            "output": output,
            "command": " ".join(command),
        }
        if error_output:
            result["stderr"] = error_output
        if not ok:
            result["error"] = output or "启动器返回失败（无输出）"
        return result


class LauncherManager:
    """启动器工具的连接池（很轻：没有长连接，只是按磁盘配置构造适配器）"""

    def __init__(self, registry: Any, store: Any):
        self.registry = registry
        self.store = store
        self._clients: dict[str, LauncherClient] = {}

    def get_client(self, tool_id: str) -> LauncherClient:
        tool = self.registry.get_tool(tool_id)
        if not tool:
            raise LauncherError(f"工具不存在: {tool_id}")
        if tool.get("kind") != "launcher":
            raise LauncherError(f"工具 '{tool_id}' 不是启动器（kind={tool.get('kind')}）")
        # 以磁盘配置为真相源：配置改了下次调用即生效
        client = LauncherClient(tool, self.store)
        self._clients[tool_id] = client
        return client

    def sync(self, tool_id: str) -> dict[str, Any]:
        return self.get_client(tool_id).sync_actions()

    def list_actions(
        self, tool_id: str, keyword: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return self.get_client(tool_id).list_actions(keyword=keyword, limit=limit)

    async def run_action(
        self,
        tool_id: str,
        action_id: str | None = None,
        action_name: str | None = None,
        param: str | None = None,
        wait: float | None = None,
    ) -> dict[str, Any]:
        client = self.get_client(tool_id)
        return await client.run_action(
            action_id=action_id, action_name=action_name, param=param, wait=wait
        )
