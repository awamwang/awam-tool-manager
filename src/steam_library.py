"""Steam 本机游戏库的**只读**解析（P0 接入 Steam，2026-10-19）。

只做三件事：定位 Steam 根、读 `libraryfolders.vdf`、读各库的 `appmanifest_*.acf`。
一次进程都不 spawn——「启动某个游戏」是 `launcher_client` 的 steam 适配器在
destructive 门禁之后干的事。

两条真机勘察结论决定了本模块的形状（2026-10-19 复测）：

1. **位置只有权威登记值可信**。`HKCU\\Software\\Valve\\Steam` 的 `SteamPath` /
   `SteamExe` 本机是 `s:/program files (x86)/steam`（Steam 自己写的是正斜杠形式，
   Windows 一样认）。只穷举 `{ProgramFiles}\\Steam` 会得到「装在非常规盘 → 没装」
   的假阴性，与 NE-10 缺陷④ 同源。
2. **vdf 声明 ≠ 盘上装着**。`libraryfolders.vdf` 声明 3 个库、27 个 appid，但
   `E:\\Games\\SteamLibrary` 目录已不存在——那 11 个 appid 只活在 vdf 里，盘上
   一份 manifest 都没有。所以清单以 **acf 文件**为准，vdf 只用来枚举库、
   并把缺口如实报成 `missingLibraries`，绝不按 vdf 造出 11 条不存在的游戏。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Steam 自己写的登记值（安装器/客户端维护，卸载时会清）
REGISTRY_KEY = r"Software\Valve\Steam"
REGISTRY_HIVE_LABEL = "HKCU"

# 登记值拿不到时的兜底位置（只兜底，不作为主证据）
FALLBACK_DIRS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
)

PROGRAM_ARGV = "steam.exe"

# acf 里我们关心的字段 → 输出字段名（其余字段留在盘上，不搬进清单）
_MANIFEST_FIELDS = {
    "appid": "appid",
    "name": "name",
    "installdir": "installdir",
    "StateFlags": "stateFlags",
    "SizeOnDisk": "sizeOnDisk",
    "LastPlayed": "lastPlayed",
    "buildid": "buildid",
}


class SteamError(Exception):
    """Steam 库读取的业务级失败（路径不存在、格式不认识）"""


# ---- VDF：Valve 的嵌套键值格式，标准库里没有解析器，这里按实测形态手写 ----

_VDF_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\{|\}')


def _unescape(raw: str) -> str:
    return raw.replace("\\\\", "\\").replace('\\"', '"').replace("\\n", "\n")


def parse_vdf(text: str) -> dict[str, Any]:
    """把 VDF 文本解析成嵌套 dict。

    只认实测到的形态：`"key" "value"` 与 `"key" { ... }`，重复键后者覆盖前者
    （Steam 自己不会写重复键）。解析不了就抛 `SteamError`——宁可一次调用失败，
    也不返回半份清单：清单缺一条意味着「那个游戏在本 server 里不存在」，
    而这种缺失看起来完全正常。

    严格性是刻意的：块缺右括号、键后面没值，都是「文件被截断/格式变了」的信号，
    宽容处理的结果是静默少几项，调用方无从察觉。
    """
    tokens = [_unescape(m.group(1)) if m.group(1) is not None else m.group(0)
              for m in _VDF_TOKEN_RE.finditer(text)]
    obj, index = _parse_pairs(tokens, 0, require_close=False)
    if index != len(tokens):
        raise SteamError(f"VDF 在第 {index} 个记号处提前结束（结构不完整）")
    return obj


def _parse_pairs(tokens: list[str], start: int, *, require_close: bool) -> tuple[dict[str, Any], int]:
    obj: dict[str, Any] = {}
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "}":
            return obj, index + 1
        if token == "{":
            raise SteamError(f"VDF 第 {index} 个记号处出现了没有键的块")
        key = token
        index += 1
        if index >= len(tokens):
            raise SteamError(f"VDF 的键 {key!r} 后面没有值（文件被截断）")
        nxt = tokens[index]
        if nxt == "{":
            obj[key], index = _parse_pairs(tokens, index + 1, require_close=True)
        elif nxt == "}":
            raise SteamError(f"VDF 的键 {key!r} 后面直接跟了右括号（没有值）")
        else:
            obj[key] = nxt
            index += 1
    if require_close:
        raise SteamError("VDF 的块缺少右括号（文件被截断）")
    return obj, index


# ---- 定位 Steam ----


def registry_steam_values() -> dict[str, str]:
    """读 `HKCU\\Software\\Valve\\Steam` 的 `SteamPath` / `SteamExe`。

    只 `QueryValueEx`，不写、不创建键；非 Windows、键不存在、值缺失一律返回
    能拿到的一部分（空 dict 也算正常结果，交由调用方决定回退）。
    """
    import winreg  # 局部 import：非 Windows 上模块仍可加载（与 discovery.py 同法）

    found: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            for name in ("SteamPath", "SteamExe"):
                try:
                    value = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    found[name] = value.strip()
    except OSError:
        return found
    return found


def resolve_root(configured: str | None = None) -> tuple[Path | None, str]:
    """定出 Steam 根目录，返回 `(root, 来源说明)`。

    顺序是 `条目配置 → 注册表 → 穷举兜底`：条目里写死的路径是人在登记时核对过的，
    优先级最高；注册表是安装器自己写的、跟着客户端搬家；兜底那两条只在
    「登记值读不到」时才用，且来源说明里会写明是猜的。
    """
    candidates: list[tuple[str, str]] = []
    if configured and configured.strip():
        candidates.append(("条目 launcher.dataDir", configured.strip()))
    try:
        values = registry_steam_values()
    except ImportError:
        values = {}
    if values.get("SteamPath"):
        candidates.append(("注册表 SteamPath", values["SteamPath"]))
    else:
        candidates.extend(
            ("穷举兜底（未经登记值确认）", fallback) for fallback in FALLBACK_DIRS
        )
    for source, raw in candidates:
        path = Path(raw.replace("/", "\\"))
        if path.is_dir():
            # 目录在就算定位成功；`steamapps` 还没生成（全新安装/刚卸载完）时
            # 交给调用方得到空清单，而不是「找不到 Steam」
            return path, source
    return None, "；".join(f"{s}: {r} 不存在" for s, r in candidates) or "没有任何可用来源"


def steam_executable(root: Path) -> Path:
    return root / PROGRAM_ARGV


# ---- 库与清单 ----


def _library_paths(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从 `libraryfolders.vdf` 取库目录；读不到就退回「只有主库」"""
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return [{"index": "0", "path": root, "declaredAppIds": []}], {
            "libraryFolders": "missing",
            "note": (
                f"{vdf} 不存在：只扫主库 {root}\\steamapps。"
                "老版本 Steam 或从未添加过库时就是这个形状"
            ),
        }
    try:
        doc = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SteamError) as exc:
        raise SteamError(f"解析 {vdf} 失败: {exc}") from None
    folders = doc.get("libraryfolders") or {}
    libs: list[dict[str, Any]] = []
    for index in sorted(folders, key=lambda i: int(i) if str(i).isdigit() else 0):
        entry = folders.get(index)
        if not isinstance(entry, dict):
            continue
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            continue
        apps = entry.get("apps")
        libs.append({
            "index": str(index),
            "path": Path(raw_path.replace("/", "\\")),
            "declaredAppIds": sorted(apps) if isinstance(apps, dict) else [],
            "label": str(entry.get("label") or ""),
        })
    if not libs:
        # vdf 在但一个库都没解析出来：格式变了或被截断。明确报错，
        # 而不是静默只扫主库——那会把「多盘游戏突然搜不到」伪装成正常
        raise SteamError(f"{vdf} 里没解析出任何 libraryfolders 条目（格式不认识）")
    return libs, {"libraryFolders": "parsed"}


def read_manifest(path: Path) -> dict[str, Any] | None:
    """读一份 `appmanifest_<appid>.acf` 的关心字段；结构不认识返回 None（跳过该文件）"""
    try:
        doc = parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SteamError):
        return None
    state = doc.get("AppState")
    if not isinstance(state, dict):
        return None
    row = {out: state.get(src) for src, out in _MANIFEST_FIELDS.items()}
    if not row.get("appid"):
        return None
    return row


def installed_games(root: Path) -> dict[str, Any]:
    """扫描所有**盘上存在**的库，按 acf 出已装游戏清单。

    返回体里 `games` 只来自 acf（盘上真有清单），`declaredOnly` 只报数量与缺口库，
    两者不混列——混了就等于把「Steam 记得你装过」说成「现在能启动」。
    """
    libs, meta = _library_paths(root)
    games: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    scanned = 0
    for lib in libs:
        apps_dir = lib["path"] / "steamapps"
        if not apps_dir.is_dir():
            missing.append({
                "index": lib["index"],
                "path": str(lib["path"]),
                "declaredApps": len(lib["declaredAppIds"]),
                "reason": "steamapps 目录不存在（库被删/换盘/移动硬盘没插）",
            })
            continue
        for manifest in sorted(apps_dir.glob("appmanifest_*.acf")):
            scanned += 1
            row = read_manifest(manifest)
            if row is None:
                missing.append({
                    "index": lib["index"],
                    "path": str(lib["path"]),
                    "declaredApps": len(lib["declaredAppIds"]),
                    "reason": f"{manifest.name} 结构不认识，已跳过",
                })
                continue
            row["library"] = str(lib["path"])
            row["libraryIndex"] = lib["index"]
            row["manifest"] = manifest.name
            games.append(row)
    games.sort(key=lambda g: (str(g.get("name") or "").lower(), str(g.get("appid"))))
    declared_total = sum(len(lib["declaredAppIds"]) for lib in libs)
    payload: dict[str, Any] = {
        "root": str(root),
        "libraries": [
            {
                "index": lib["index"],
                "path": str(lib["path"]),
                "exists": (lib["path"] / "steamapps").is_dir(),
                "declaredApps": len(lib["declaredAppIds"]),
            }
            for lib in libs
        ],
        "count": len(games),
        "scannedManifests": scanned,
        "declaredAppTotal": declared_total,
        "games": games,
        **meta,
    }
    if missing:
        payload["missingLibraries"] = missing
        payload["note"] = (
            "有些库在 vdf 里声明了但盘上不在了：这些 appid 不计入 games"
            "（「Steam 记得装过」不等于「现在能启动」）"
        )
    return payload
