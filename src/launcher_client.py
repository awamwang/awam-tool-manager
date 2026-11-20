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

名称/清单还有两个**快照来源**（`launcher.nameSources`，2026-11-17 接入）：
Quicker「工具→导出动作列表」的 CSV、维护工具「备份数据」导出目录里的
`actions/action_<ID>_<名>.json`。states 只覆盖本机 212/357 个动作、56 个只剩
ID（EVER重命名搜不到的直接原因）；两份快照合计能把清单补到全量、名称补齐。
快照是手动导出的会过期——已删动作会残留在清单里，但调用只会收到 QuickerStarter
的失败文本，与「搜索不到」相比是更好的失败方向。合并规则：**只补空，不覆盖**
state 缓存里的完整信息（那边带 type/app/description），多个源按配置顺序先到先得。

第二个实现是 uTools。数据来源完全不同：插件目录 `plugins/*.asar`
（Chromium Pickle 封包的归档）里的 `plugin.json.features[].cmds`。
asar 里的声明是**完整**的（有 explain、有全部触发词），不存在 Quicker
那种「尽力而为补名」问题，`source` 恒为 `auto`；代价是调用只有「唤起」
没有「回传」（`run_action` 的 utools 分支，fire-and-forget）。

第三个实现是 Steam。它既不是动作数据库也不是插件目录，而是一堆**安装器自己
写的清单文件**：`libraryfolders.vdf` 声明有哪些库，每个库的 `steamapps/
appmanifest_*.acf` 声明装了哪个游戏。解析规则都在 `steam_library.py`，
本模块只做适配（动作 ID = `app:<appid>`，调用 = `steam.exe -applaunch <appid>`）。
它与前两个的最大差别是**动作可枚举完整**：acf 在就一定列得出，`source` 只会因为
「vdf 声明了某个库但盘上不在了」降级成 `partial`——那 11 个 appid 是真起不来，
不是名称没拿到。
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio

from app_launcher import launch_app
import steam_library

DEFAULT_TIMEOUT = 20.0

# 动作清单缓存多久算过期（秒）。过期后 list/find 会自动重扫一次再回答，
# 而不是永远读盘——启动器不是 MCP，没有 tools/list 可以问，只能靠本地数据；
# 但本地数据是活的（Quicker 随时可能增删动作），所以缓存必须有寿命。
ACTIONS_CACHE_TTL = 300.0

# Quicker 动作清单缓存条目的文本格式（JSON 解析后的样子）：
#   [url:XAction]文件/全文搜索<_global>("用Everything搜索文件；用AnyTXT搜索全文")|92c14d1b-...
QUICKER_ACTION_RE = re.compile(
    r'\[url:(?P<type>[^\]]+)\](?P<name>[^<]+)<(?P<app>[^>]*)>\("(?P<desc>[^"]*)"\)\|(?P<id>[0-9a-fA-F-]{36})'
)

# 动作 ID 也体现在状态文件名上：states/state_<uuid>.json
QUICKER_STATE_FILE_RE = re.compile(r"^state_([0-9a-fA-F-]{36})\.json$", re.IGNORECASE)

# 标准 UUID 形态（小写比较前的格式校验；CSV/备份文件名里都应是它）
QUICKER_ACTION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

# 名称/清单的快照来源之一：维护工具备份导出的动作文件
# `actions/action_<ID>_<名>.json`。ID 从文件名取（稳定），
# 名称/描述从 JSON 正文取（Title/Description——文件名里的名称可能被清洗/截断）
QUICKER_BACKUP_FILE_RE = re.compile(
    r"^action_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_.+\.json$"
)

# 动作名称的**第二个来源**：Quicker 每次执行动作都会往日志里写一行
#   2026-10-16 22:11:06 [action:识别二维码] ... 执行动作：识别二维码 id=9f530d6e-...
# 只覆盖「运行过的动作」，但日志是累积的（含轮转的 .1~.5），且文本干净。
# 实测本机补全 16 个 state 缓存拿不到的名称（144 → 160 / 212），所以值得扫。
# 注意：只用来补空，绝不覆盖 state 缓存里的名称——那边还带 type/app/description。
QUICKER_LOG_RUN_RE = re.compile(
    r"执行动作：(?P<name>\S+)\s+id=(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# QuickerStarter 出错时**退出码仍然是 0**（2026-10-16 实测：找不到动作也返回 0），
# 因此不能只看 returncode，必须解析输出文本。
QUICKER_FAILURE_MARKERS = ("未能成功启动动作", "未能找到该动作", "Error：", "Error:")

# Quicker 在中文 Windows 上输出 GBK；按 UTF-8 解会得到乱码。
QUICKER_OUTPUT_ENCODING = "gbk"

# uTools 协议调用是 fire-and-forget：`cmd /c start` 只负责把 utools://
# 投递给系统，没有 stdout 可读。wait 参数因此改为「投递后观察窗口」——
# 等 renderer 进程起来（uTools 已运行时实测 ~0.5s 内，未运行时的冷启动
# 更久），封顶 5s：调用方给个 wait=600 也不该把 MCP 调用堵死十分钟。
UTOOLS_MAX_OBSERVE_SECONDS = 5.0

# `cmd /c start` 本身应秒退（ShellExecute 不等待被拉起的应用）；超过这个
# 时间说明 start 卡住了，按超时杀掉不留孤儿。
UTOOLS_SPAWN_TIMEOUT = 10.0

# Steam 动作 ID 的形态：`app:<appid>`，也接受调用方直接甩一个纯数字
# （`-applaunch` 收的就是 appid，多写一层前缀不带来任何信息）
STEAM_ACTION_RE = re.compile(r"^(?:app:)?(\d+)$")

# 已接线的适配器。schema 的 `launcher.type` enum 必须与这里逐字一致，
# 否则「清单里合法、跑起来报不支持」——由 test_launcher_client 钉住
SUPPORTED_ADAPTERS = ("quicker", "utools", "steam")


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


def _read_asar_plugin_json(path: Path) -> dict[str, Any]:
    """从 uTools 插件的 `.asar` 里只读出 plugin.json（纯 Python，无新依赖）。

    asar = Chromium Pickle 封包的归档，字节布局（2026-10-18 对本机 44 个
    真实插件实测；勘察参考脚本 `build_catalog.py` 同款算法）：

        [0:4]   uint32 LE —— 外层 pickle 的 payload 大小（实测为 4，解析不依赖）
        [4:8]   uint32 LE —— header pickle 的 payload 大小（含 4 字节对齐 padding）
        [8:12]  uint32 LE —— 内层 pickle 的 payload 大小
        [12:16] int32  LE —— header JSON 的字节长度
        [16:]   header JSON（UTF-8），`files` 表给出每个文件的 offset/size

    文件内容基址 = `8 + [4:8] 的值`；header 里的 offset/size **可能是字符串**
    （实测同一个 header 里 offset 是字符串、size 是整数），必须 int() 强转。
    只读文件头 + plugin.json 条目，不整包解包（44 个插件全量 <2s）。
    """
    with path.open("rb") as fh:
        prefix = fh.read(16)
        if len(prefix) < 16:
            raise ValueError("asar 文件头不完整（不足 16 字节）")
        header_payload_size = struct.unpack_from("<I", prefix, 4)[0]
        str_len = struct.unpack_from("<i", prefix, 12)[0]
        if str_len <= 0:
            raise ValueError(f"asar header 长度非法: {str_len}")
        header_raw = fh.read(str_len)
        if len(header_raw) < str_len:
            raise ValueError("asar header 不完整（文件被截断）")
        try:
            header = json.loads(header_raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"asar header 不是合法 JSON: {exc}") from exc
        if not isinstance(header, dict):
            raise TypeError("asar header 顶层不是对象")
        entry = (header.get("files") or {}).get("plugin.json")
        if not isinstance(entry, dict) or "offset" not in entry or "size" not in entry:
            raise ValueError("asar header 里没有 plugin.json 条目")
        offset, size = int(entry["offset"]), int(entry["size"])
        fh.seek(8 + header_payload_size + offset)
        data = fh.read(size)
    if len(data) < size:
        raise ValueError("asar 里的 plugin.json 不完整（文件被截断）")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"plugin.json 不是合法 JSON: {exc}") from exc


class LauncherClient:
    """单个启动器工具的适配器"""

    def __init__(self, tool: dict[str, Any], store: Any):
        self.tool_id = str(tool.get("id", ""))
        self.tool = tool
        self.store = store
        cfg = tool.get("launcher") or {}
        self.adapter = str(cfg.get("type") or "quicker")
        # 命令与数据目录里的 %VAR%（%LOCALAPPDATA% 等）按本机解析：可执行/数据
        # 位置属于本机事实，不硬编码含用户名的绝对路径进仓库。无 % 的绝对路径
        # expandvars 逐字不变，故对既有 utools/steam/quicker 行为完全等价。
        self.command = os.path.expandvars(str(cfg.get("command") or ""))
        data_dir = os.path.expandvars(str(cfg.get("dataDir") or ""))
        self.data_dir = Path(data_dir) if data_dir else None
        # 补充名称/清单的快照数据源（Quicker 专用）。路径允许写
        # %USERPROFILE% 等环境变量——快照位置属于本机事实，不该硬编码进仓库
        self.name_sources = [
            Path(os.path.expandvars(str(source).strip()))
            for source in (cfg.get("nameSources") or [])
            if str(source).strip()
        ]
        self.timeout = float(cfg.get("timeout") or DEFAULT_TIMEOUT)

    # ---- 动作清单同步 ----

    def sync_actions(self) -> dict[str, Any]:
        """扫描启动器本地数据，把动作清单落到 launcher-actions/"""
        if self.adapter not in SUPPORTED_ADAPTERS:
            raise LauncherError(f"暂不支持的启动器类型: {self.adapter}")
        if self.adapter == "steam":
            # steam 的 dataDir 是可选的：条目没写就用 Steam 自己登记的 SteamPath
            actions, source = self._scan_steam()
        else:
            if not self.data_dir:
                raise LauncherError("未配置 launcher.dataDir，无法扫描动作")
            if self.adapter == "utools":
                actions, source = self._scan_utools()
            else:
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

        # 3) 名称/清单补全（快照来源）：用户导出的动作列表 CSV、维护工具备份
        #    目录（launcher.nameSources，按序先到先得）。只补空不覆盖 state
        #    缓存里的完整信息；快照里有而 states 没有的动作 ID 也并入清单
        for action_id, row in self._scan_quicker_name_sources().items():
            entry = found.get(action_id)
            if entry is None:
                found[action_id] = row
                continue
            for field in ("name", "type", "app", "description", "panel"):
                if not entry.get(field) and row.get(field):
                    entry[field] = row[field]

        # 4) 名称补漏：从 Quicker 运行日志里捞（只补空，不覆盖上面拿到的）
        log_names = self._scan_quicker_log_names()
        for action_id, name in log_names.items():
            entry = found.get(action_id)
            if entry and not entry.get("name"):
                entry["name"] = name

        actions = sorted(found.values(), key=lambda a: (a.get("name") or "~", a["id"]))
        named = sum(1 for a in actions if a.get("name"))
        # 一个名称都没拿到才叫 partial 吗？不——只要有没有名字的动作就是 partial，
        # 这样调用方知道「搜索可能漏结果」。
        source = "auto" if actions and named == len(actions) else "partial"
        return actions, source

    def _scan_quicker_name_sources(self) -> dict[str, dict[str, Any]]:
        """扫描快照数据源（launcher.nameSources），返回 动作ID → 动作条目。

        states 缓存 + 运行日志只覆盖部分动作（本机实测 212/357，56 个只剩 ID，
        「EVER重命名」搜不到的直接原因）。用户导出的快照是完整清单：CSV 导出
        357 个全带名称、维护工具备份 349 个——把它们并进来补全缺的动作 ID 与
        名称。快照会过期（已删动作残留），调用时按 QuickerStarter 失败文本
        如实报错即可，不值得为过期放弃补全。

        单个源读不了只告警跳过，绝不拖垮整个同步（与 asar/日志同一纪律）。
        """
        merged: dict[str, dict[str, Any]] = {}
        for source_path in self.name_sources:
            try:
                if source_path.suffix.lower() == ".csv":
                    rows = self._read_quicker_csv(source_path)
                elif source_path.is_dir():
                    rows = self._read_quicker_backup_dir(source_path)
                else:
                    _log(
                        self.tool_id,
                        "warning",
                        f"跳过不存在的 nameSource: {source_path}",
                    )
                    continue
            except OSError as exc:
                _log(
                    self.tool_id,
                    "warning",
                    f"读取 nameSource 失败，跳过 {source_path}: {exc}",
                )
                continue
            for row in rows:
                action_id = row.pop("id")
                # 同一动作出现在多个源里：先配置的先得（fill-empty 语义在调用方）
                if action_id not in merged:
                    merged[action_id] = {"id": action_id, **row}
        return merged

    def _read_quicker_csv(self, path: Path) -> list[dict[str, Any]]:
        """Quicker「工具→导出动作列表」的 CSV（中文表头，GBK 或 UTF-8 编码）"""
        raw = path.read_bytes()
        text = ""
        for encoding in ("utf-8-sig", "gbk"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("gbk", errors="replace")
        # 残留 BOM 会粘住表头首列（"\ufeffId"），查列名就永远查不到 Id
        text = text.lstrip("\ufeff")
        actions: list[dict[str, Any]] = []
        for row in csv.DictReader(io.StringIO(text)):
            action_id = str(row.get("Id") or "").strip().lower()
            if not QUICKER_ACTION_ID_RE.match(action_id):
                continue
            actions.append({
                "id": action_id,
                "name": str(row.get("名称") or "").strip(),
                "type": str(row.get("类型") or "").strip(),
                "app": str(row.get("EXE") or "").strip(),
                "description": str(row.get("说明") or "").strip(),
                "panel": str(row.get("动作页") or "").strip(),
            })
        return actions

    def _read_quicker_backup_dir(self, path: Path) -> list[dict[str, Any]]:
        """维护工具「备份数据」导出目录里的动作文件。

        接受备份根目录（含 `actions/` 子目录）或 `actions/` 目录本身；
        ID 取文件名，名称/描述取 JSON 正文（`Title` / `Description`）。
        """
        candidates = set(path.glob("action_*.json")) | set(
            path.glob("actions/action_*.json")
        )
        actions: list[dict[str, Any]] = []
        for file_path in sorted(candidates):
            matched = QUICKER_BACKUP_FILE_RE.match(file_path.name)
            if not matched:
                continue
            try:
                payload = json.loads(
                    file_path.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            actions.append({
                "id": matched.group(1).lower(),
                "name": str(payload.get("Title") or "").strip(),
                "type": "",
                "app": "",
                "description": str(payload.get("Description") or "").strip(),
            })
        return actions

    def _scan_utools(self) -> tuple[list[dict[str, Any]], str]:
        """扫描 uTools 插件目录（dataDir/*.asar），返回 (动作列表, source)。

        动作 ID = `"<插件名>/<关键词>"`——协议调用（`utools://<插件名>/<关键词>`）
        要用的两个组分。一个功能的多个关键词各自成为独立动作（每个关键词都是
        可直达的协议入口），`keywords` 字段收齐该功能的全部触发词供搜索。

        asar 里的声明是完整的，不存在 Quicker 的 partial 问题，source 恒 auto。
        单个 asar 解析失败只记日志跳过，绝不拖垮整个同步。
        """
        if not self.data_dir:
            raise LauncherError("未配置 launcher.dataDir，无法扫描动作")
        if not self.data_dir.exists():
            raise LauncherError(f"启动器数据目录不存在: {self.data_dir}")

        found: dict[str, dict[str, Any]] = {}
        origin: dict[str, str] = {}  # action_id → 来源 asar 文件名（诊断重复插件名）
        for asar_path in sorted(self.data_dir.glob("*.asar")):
            try:
                plugin = _read_asar_plugin_json(asar_path)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                # 装到一半的插件 / 损坏的 asar 只跳过：局部坏数据不该让
                # 「同步」整个失败（Quicker 的尽力而为原则）
                _log(self.tool_id, "warning", f"跳过无法解析的 asar {asar_path.name}: {exc}")
                continue
            plugin_name = str(plugin.get("pluginName") or plugin.get("name") or asar_path.stem)
            for feature in plugin.get("features") or []:
                if not isinstance(feature, dict):
                    continue
                keywords = [
                    cmd
                    for cmd in (feature.get("cmds") or [])
                    if isinstance(cmd, str) and cmd.strip()
                ]
                if not keywords:
                    # 没有关键词 = 没有协议入口（纯 matcher 功能经 URL 不可达）
                    continue
                explain = str(feature.get("explain") or "").strip()
                for keyword in keywords:
                    action_id = f"{plugin_name}/{keyword}"
                    if action_id in found:
                        # 同一关键词重复声明（同插件多功能共用，或两个 asar
                        # 声明同名插件）→ 协议入口相同，去重；后者额外告警，
                        # 否则用户装了新旧两版插件只会莫名少一批动作
                        if origin.get(action_id) != asar_path.name:
                            _log(
                                self.tool_id,
                                "warning",
                                f"插件名重复: {plugin_name}（{asar_path.name} 与 "
                                f"{origin.get(action_id)}），沿用先扫到的条目",
                            )
                        continue
                    origin[action_id] = asar_path.name
                    found[action_id] = {
                        "id": action_id,
                        "name": explain or keyword,
                        "type": "",
                        "app": "",
                        "description": explain,
                        "keywords": keywords,
                    }

        actions = sorted(found.values(), key=lambda a: (a.get("name") or "~", a["id"]))
        return actions, "auto"

    def _scan_steam(self) -> tuple[list[dict[str, Any]], str]:
        """扫 Steam 本机库（以 acf 为准），返回 (游戏动作清单, source)。

        动作 ID = `app:<appid>`——正是 `steam.exe -applaunch <appid>` 要的那个数。
        `source` 只有在「vdf 声明了某个库、但那个库盘上不在了」时才降级为
        `partial`：那批 appid 真的起不来（换盘/删库/移动硬盘没插），列出来只会
        让调用方以为能启动。这与 Quicker 的 partial 不同向——那边是「ID 有、名称
        没拿到，搜索会漏」，这边是「清单本身不完整」。
        """
        configured = str(self.data_dir) if self.data_dir else None
        root, root_source = steam_library.resolve_root(configured)
        if root is None:
            raise LauncherError(f"定位不到 Steam 根目录：{root_source}")
        try:
            data = steam_library.installed_games(root)
        except steam_library.SteamError as exc:
            raise LauncherError(f"读取 Steam 库失败: {exc}") from None

        actions: list[dict[str, Any]] = []
        for game in data["games"]:
            appid = str(game.get("appid") or "")
            if not appid:
                continue
            size = game.get("sizeOnDisk")
            description = " / ".join(
                part for part in (
                    f"库 {Path(str(game.get('library') or '')).name or game.get('library')}",
                    f"{int(size) // (1024 * 1024)} MB" if size and str(size).isdigit() else "",
                ) if part
            )
            actions.append({
                "id": f"app:{appid}",
                "name": str(game.get("name") or f"Steam App {appid}"),
                "type": "game",
                "app": "",
                "description": description,
                "appid": appid,
                "installdir": game.get("installdir") or "",
                "library": game.get("library") or "",
                "sizeOnDisk": size,
                "lastPlayed": game.get("lastPlayed"),
            })
        _log(
            self.tool_id,
            "info",
            f"Steam 根={root}（来源：{root_source}），库 {len(data['libraries'])} 个 / "
            f"清单 {data['scannedManifests']} 份 / 已装 {len(actions)} 项 / "
            f"vdf 声明 {data['declaredAppTotal']} 项",
        )
        return actions, "partial" if data.get("missingLibraries") else "auto"

    def _scan_quicker_log_names(self) -> dict[str, str]:
        """从 Quicker 日志（含轮转文件）解析 动作ID → 名称"""
        if not self.data_dir:
            return {}
        logs_dir = self.data_dir / "logs"
        if not logs_dir.exists():
            return {}
        names: dict[str, str] = {}
        # 轮转文件可能很大（本机 quicker.log 约 8MB 级），只读文本即可；
        # 解析失败一律跳过——名称是尽力而为的字段，绝不能拖垮同步。
        for path in logs_dir.glob("quicker.log*"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for matched in QUICKER_LOG_RUN_RE.finditer(text):
                names[matched.group("id").lower()] = matched.group("name")
        return names

    # ---- 缓存新鲜度 ----

    def _cache_age(self) -> float | None:
        """缓存距现在多少秒；没有缓存或时间戳不可解析 → None"""
        catalog = self.store.load_launcher_catalog(self.tool_id)
        if not catalog:
            return None
        raw = catalog.get("lastSynced")
        if not isinstance(raw, str):
            return None
        try:
            synced = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            return None
        return (
            datetime.datetime.now(datetime.timezone.utc) - synced
        ).total_seconds()

    def _ensure_fresh(self, refresh: bool = False) -> None:
        """必要时重扫一次动作清单。

        静默失败：扫描要读启动器本地数据，失败（目录没了、Quicker 没装）不该让
        「列出动作」整个炸掉——退回旧缓存继续服务，调用方看 lastSynced 就知道有多旧。
        """
        if not refresh:
            age = self._cache_age()
            if age is not None and age < ACTIONS_CACHE_TTL:
                return
        try:
            self.sync_actions()
        except LauncherError as exc:
            _log(self.tool_id, "warning", f"动作清单刷新失败，沿用旧缓存: {exc}")

    # ---- 查询 ----

    def list_actions(
        self, keyword: str | None = None, limit: int | None = None, refresh: bool = False
    ) -> list[dict[str, Any]]:
        self._ensure_fresh(refresh=refresh)
        actions = self.store.load_launcher_actions(self.tool_id)
        if keyword:
            needle = keyword.lower()

            def haystack(action: dict[str, Any]) -> str:
                parts = [
                    str(action.get(field) or "")
                    for field in ("name", "id", "description", "app", "type", "panel")
                ]
                # uTools 动作带 keywords 列表（该功能的全部触发词）：
                # 非首个关键词（如 "全部站点"）也要能搜到
                extra = action.get("keywords")
                if isinstance(extra, list):
                    parts.extend(str(item) for item in extra)
                return " ".join(parts).lower()

            actions = [a for a in actions if needle in haystack(a)]
        if limit and limit > 0:
            actions = actions[:limit]
        return actions

    @staticmethod
    def _match(
        actions: list[dict[str, Any]],
        action_id: str | None = None,
        action_name: str | None = None,
    ) -> dict[str, Any] | None:
        """先精确后包含（纯函数，便于「重扫后再找一次」）"""
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

    def find_action(
        self, action_id: str | None = None, action_name: str | None = None
    ) -> dict[str, Any] | None:
        """按 ID 或名称定位动作。

        缓存里找不到时，若缓存已陈旧就重扫一次再找——用户刚在 Quicker 里新建的
        动作不该因为「上次同步是 10 分钟前」而调不到。仍找不到就返回 None，
        `run_action` 会拿原始 ID/名称直接调（runaction 本来就接受两者）。
        """
        hit = self._match(self.store.load_launcher_actions(self.tool_id), action_id, action_name)
        if hit is not None:
            return hit
        age = self._cache_age()
        if age is None or age >= ACTIONS_CACHE_TTL:
            try:
                self.sync_actions()
            except LauncherError:
                return None
            return self._match(
                self.store.load_launcher_actions(self.tool_id), action_id, action_name
            )
        return None

    # ---- 调用 ----

    async def run_action(
        self,
        action_id: str | None = None,
        action_name: str | None = None,
        param: str | None = None,
        wait: float | None = None,
    ) -> dict[str, Any]:
        if not self.command and self.adapter not in ("utools", "steam"):
            # utools 走 utools:// 协议唤起，不需要可执行文件路径；
            # steam 未配置 command 时按 Steam 自己登记的 SteamPath 推 steam.exe
            raise LauncherError("未配置 launcher.command，无法调用动作")
        if not action_id and not action_name:
            raise LauncherError("必须指定 action_id 或 action_name")

        # 清单里有就更稳（能校验存在性、拿到规范 ID）；清单缺也不阻塞——
        # 动作可能刚添加还没同步，runaction 接受 ID/名称/动作库 ID。
        action = self.find_action(action_id, action_name)
        target = action["id"] if action else (action_id or action_name or "")
        resolved_name = (action or {}).get("name") or ""

        if self.adapter == "utools":
            return await self._run_utools(target, resolved_name, param, wait)
        if self.adapter == "steam":
            return await self._run_steam(target, resolved_name, param, wait)

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

    async def _run_utools(
        self,
        target: str,
        resolved_name: str,
        param: str | None,
        wait: float | None,
    ) -> dict[str, Any]:
        """utools:// 协议唤起（fire-and-forget）。

        成功判定：`cmd /c start` 退出码 0 = 协议已被系统接收并投递；
        **没有结果回传**——执行结果只在 uTools UI 里，返回体的
        `fireAndForget: true` 就是在说这件事。
        """
        plugin_name, separator, keyword = target.partition("/")
        if not separator or not plugin_name.strip() or not keyword.strip():
            raise LauncherError(
                f"utools 动作 ID 必须为 <插件名>/<关键词> 格式（收到: {target!r}）；"
                "可先 list_capabilities(tool_id='utools') 查可用动作"
            )
        # 路径段全编码：uTools 主进程对整串 decodeURIComponent 后按第一个
        # "/" 拆 label，把名字里的 "/" 编码掉才能保证拆出正确的两组分；
        # 参数段 quote(safe='') 连 ? / & # 一起编码（payload 里这些字符
        # 本就安全，编码更稳；空格 → %20 已实测）
        url = f"utools://{quote(plugin_name, safe='')}/{quote(keyword, safe='')}"
        if param:
            url += f"?{quote(param, safe='')}"
        command = ["cmd", "/c", "start", "", url]

        # wait 语义：投递后的观察窗口（默认取条目 timeout，封顶
        # UTOOLS_MAX_OBSERVE_SECONDS），只用于等 renderer 起来
        observe = wait if wait is not None else self.timeout
        observe = max(0.0, min(float(observe), UTOOLS_MAX_OBSERVE_SECONDS))

        _log(self.tool_id, "info", f"唤起 uTools 功能: {resolved_name or target}")
        process = None
        try:
            with anyio.fail_after(UTOOLS_SPAWN_TIMEOUT):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,  # A8：绝不继承 server 的 stdin 管道
                    stdout=asyncio.subprocess.DEVNULL,  # fire-and-forget，无可读输出
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await process.communicate()
        except TimeoutError:
            if process is not None:
                process.kill()  # 超时不留孤儿
            raise LauncherError(
                f"utools:// 协议投递超时（等待 {UTOOLS_SPAWN_TIMEOUT}s start 未返回）"
            ) from None
        except OSError as exc:
            raise LauncherError(f"无法启动 cmd（utools:// 协议投递失败）: {exc}") from exc

        result: dict[str, Any] = {
            "success": process.returncode == 0,
            "toolId": self.tool_id,
            "actionId": target,
            "actionName": resolved_name,
            "exitCode": process.returncode,
            "command": " ".join(command),
            # 明示无结果回传：结果只在 uTools UI，调用方别等 stdout
            "fireAndForget": True,
        }
        if not result["success"]:
            result["error"] = f"utools:// 协议投递失败（start 退出码 {process.returncode}）"
            return result
        if observe > 0:
            # 观察窗口：给 renderer 一点起来的时间（实测 ~0.5s 内）
            await asyncio.sleep(observe)
        result["message"] = (
            "已通过 utools:// 协议唤起（fire-and-forget，无结果回传，结果只在 uTools UI）"
        )
        return result

    async def _run_steam(
        self,
        target: str,
        resolved_name: str,
        param: str | None,
        wait: float | None,
    ) -> dict[str, Any]:
        """`steam.exe -applaunch <appid>`（执行经 `app_launcher.launch_app`）。

        复用 app_launcher 而不是在这里自己 spawn：`stdin=DEVNULL`、进程保活、
        观察窗口与结构化失败载荷都在那边，多一条 spawn 路径就多一处会漏的纪律
        （A8 那次网关被子进程偷走 stdin 的教训已经证明这件事）。

        成功判定的诚实边界：退出码 0 只代表 **steam.exe 接受了指令**。Steam 是
        单实例客户端，`-applaunch` 由它转达给游戏进程，本进程随即退出；游戏
        起没起、是不是在更新，没有任何回传。所以这里给 `deliveryOnly`，
        与 uTools 的 `fireAndForget` 同义但不同形——那条连进程都不代表。
        """
        matched = STEAM_ACTION_RE.match(str(target).strip())
        if not matched:
            raise LauncherError(
                f"steam 动作 ID 必须是 app:<appid> 或纯数字 appid（收到: {target!r}）；"
                "可先 list_capabilities(tool_id='steam') 查本机已装游戏"
            )
        appid = matched.group(1)
        command = self.command
        if not command:
            root, root_source = steam_library.resolve_root(
                str(self.data_dir) if self.data_dir else None
            )
            if root is None:
                raise LauncherError(f"未配置 launcher.command，且定位不到 Steam 根目录：{root_source}")
            command = str(steam_library.steam_executable(root))

        argv = [command, "-applaunch", appid]
        _log(self.tool_id, "info", f"启动 Steam 游戏: {resolved_name or appid}（appid={appid}）")
        res = await launch_app(
            {"type": "executable", "command": command, "args": ["-applaunch", appid]},
            self.tool_id,
            resolved_name or f"Steam App {appid}",
            wait,
        )
        result: dict[str, Any] = {
            **res,
            "actionId": f"app:{appid}",
            "actionName": resolved_name,
            "command": " ".join(argv),
            "deliveryOnly": True,
        }
        note = "退出码只代表 steam.exe 收到 -applaunch 指令，游戏是否真的启动无从回传"
        if param:
            # 不悄悄吞掉：调用方传了 param 却什么都没发生，是最难查的一种失败
            result["ignoredParam"] = param
            note += f"；Steam 侧没有动作传参通道，param 已被忽略（收到: {param!r}）"
        result["note"] = note
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
        self,
        tool_id: str,
        keyword: str | None = None,
        limit: int | None = None,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        return self.get_client(tool_id).list_actions(
            keyword=keyword, limit=limit, refresh=refresh
        )

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
