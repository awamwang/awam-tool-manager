r"""本机包管理器适配层（P2-07）：只读枚举已安装包 + 解析启动目标 + 构造安装命令。

与 `discovery.py` 的分工：`discover_tools` 回答「这个候选在不在本机」，本模块回答
「这个安装器里装了哪些、每个的可执行入口在哪、要装某个包得跑哪条命令」。两者共用
同一套 `{ENV}` 取值纪律（进程环境 → 注册表回退），所以本模块只 import `discovery`
的 `registry_env_value`，不再实现第二份注册表读取。

**默认零执行**：列清单、搜索、以及「谁有可用更新」的默认口径**只读盘**——scoop 把已装
状态写在 `apps/<name>/<ver>/manifest.json` 与 `install.json`、把可装清单写在
`buckets/<bucket>/bucket/*.json`，一次 `scoop list` 都不必跑（跑了也只是把这些文件读一
遍再格式化）；升级差集就是这两个版本号相比。只有
`install_package` / `launch_package` / 显式 `execute=true` 的 UniGetUI 升级查询才起子进程。

UniGetUI（`uniget.exe`）在本模块里**不是包管理后端**，而是一层可选的只读查询：
它自己也要 spawn 底层安装器，且必须连上一个活的 UniGetUI 会话，所以走
`locate_uniget_cli` + `uniget_*_argv` + 解析函数的显式路径。2026-10-31 起只读面扩到
`status` / `manager list` / `source list` / `package search|installed|details|versions|updates`，
全部是 `run_uniget_read` 一条通路：先 `status` 探会话，`running=false` 就收手，
绝不把参数转给 `UniGetUI.exe` 去冷启动 GUI。安装 / 升级 / 卸载仍不在这里。

真机勘察（2026-10-19，`S:\Libs\Scoop`）决定了这里几个不显眼但关键的取舍：

- `current` 是指向版本目录的连接点，但本机 14 个 app 里有 6 个 `current` 退化成
  **空目录**（bayes / clink / ffmpeg-shared / innounp / oh-my-posh / scrcpy）——
  只读 `current/manifest.json` 会把它们整个漏掉。所以版本目录要能回落，
  并把这种状态如实标成 `current_broken`，而不是假装它们是正常安装。
- bucket 名不在 `manifest.json` 里，在**同版本目录的 `install.json`**
  （`{"bucket": "main", "architecture": "64bit"}`；直链安装写成 `url` 而非 `bucket`）。
- `scoop` 自己的 `current` 是 git 工作区、没有 `manifest.json`，所以「没有 manifest」
  是一个合法状态，不能当异常抛。
- manifest 的 `bin` **不是必填**：`shims\\<包名>.exe|.cmd|.ps1` 才是 scoop 交给
  PATH 的真实入口。本机 `bayes` 有 shim 无 bin、`scoop` 无 manifest 有 shim，
  只读 manifest 会把这两个能跑的包报成不可启动（14 个包里漏 2 个）。
  因此 `launchable` 与 `launch_target` 必须看同一批候选：候选一律从行字段
  （`shortcuts` / `bin` / `shim`）取，不允许一边扫盘一边不扫。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import anyio

from discovery import registry_env_value
from errors import ErrorType

__all__ = [
    "BACKENDS",
    "PackageError",
    "brief",
    "find_installed",
    "install_argv",
    "launch_target",
    "list_installed",
    "locate_uniget_cli",
    "parse_uniget_details",
    "parse_uniget_managers",
    "parse_uniget_packages",
    "parse_uniget_running",
    "parse_uniget_sources",
    "parse_uniget_updates",
    "parse_uniget_versions",
    "read_disk_updates",
    "resolve_root",
    "run_argv",
    "run_uniget_read",
    "search_packages",
    "shim_paths",
    "uniget_details_argv",
    "uniget_installed_argv",
    "uniget_managers_argv",
    "uniget_search_argv",
    "uniget_sources_argv",
    "uniget_status_argv",
    "uniget_updates_argv",
    "uniget_versions_argv",
    "validate_name",
    "validate_package_id",
    "validate_query",
    "validate_uniget_manager",
    "version_key",
]

# 已实现的后端。winget / npm 尚未接线：`winget list` 没有可靠的纯读盘来源，
# 必须 spawn，属于执行面，另立项再接。
BACKENDS = ("scoop",)

STATE_OK = "ok"
STATE_CURRENT_BROKEN = "current_broken"
STATE_NO_MANIFEST = "no_manifest"

# 包名字符集：scoop 的 bucket 文件名实测只用到这些（含 `+`，如 `nodejs-lts` 系）。
# 分隔符与 `..` 一律拒绝——名字会参与路径拼接，这里是边界。
_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,100}$")

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
# 命令输出回传上限（stdout / stderr 各一份）：装包日志能到几百 KB，
# 全量塞进 MCP 响应会把客户端上下文吃掉一半，留尾部才是有用那段。
OUTPUT_MAX_CHARS = 4000
# 但「留尾部」只对**日志**成立：`uniget package updates` 是结构化 JSON，按 4000
# 字符切尾会把文档拦腰截断（本机实测整份 57652 字符 / 238 条可升级），解析必失败。
# 这类调用方自己抬上限，并检查返回里的 `stdoutTruncated`。
UNIGET_OUTPUT_MAX_CHARS = 200_000


def _decode(raw: bytes) -> str:
    """UTF-8 严格解，失败回落 GBK（中文 Windows 控制台输出的常见第二选择）。

    与 `tool_help._decode` 同逻辑但刻意各留一份：那是别人的私有函数，
    跨模块 import 下划线函数等于把两个模块焊在一起改不动。
    """
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class PackageError(Exception):
    """适配层的业务错误。

    自带 `error_type`：错误分类由抛出点决定（那里知道自己为什么失败），
    handler 只做 `fail(str(exc), error_type=exc.error_type)` 的机械转手。
    让 handler 靠关键词猜分类，等于把契约建立在文案上。
    """

    def __init__(self, message: str, error_type: str = ErrorType.RUNTIME_ERROR):
        super().__init__(message)
        self.error_type = error_type


def _clamp_int(
    value: Any, *, default: int, low: int, high: int, what: str
) -> int:
    """把可选整数参数收到 `[low, high]`：只有「没传」才用默认值。

    刻意不写 `int(value or default)`——那样 `0` 会被当成「没传」悄悄放大成
    默认值。传了就越界夹取，调用方看到的 `limit` / `timeout` 回显才是它自己要的。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise PackageError(
            f"{what}必须是整数: {value!r}（允许范围 {low}-{high}）",
            ErrorType.INVALID_PARAMS,
        ) from None
    return max(low, min(number, high))


def _check_manager(manager: str) -> str:
    name = (manager or "scoop").strip().lower()
    if not name:
        name = "scoop"
    if name not in BACKENDS:
        raise PackageError(
            f"暂不支持的安装器: {name!r}（已实现: {', '.join(BACKENDS)}；"
            "winget / npm 需要 spawn 才能列，属执行面，尚未接线）",
            ErrorType.INVALID_PARAMS,
        )
    return name


def validate_name(name: Any, what: str = "包名") -> str:
    text = str(name or "").strip()
    if not text:
        raise PackageError(f"{what}不能为空", ErrorType.INVALID_PARAMS)
    if text.startswith("-"):
        # `_NAME_RE` 本身允许短横，但开头的短横会被 argv 当成选项位
        raise PackageError(
            f"{what}不能以 - 开头（会被当成命令行选项）: {text!r}",
            ErrorType.INVALID_PARAMS,
        )
    if not _NAME_RE.match(text):
        raise PackageError(
            f"{what}含非法字符: {text!r}（只允许字母/数字/点/下划线/短横/加号，"
            "不接受路径分隔符与 ..）",
            ErrorType.INVALID_PARAMS,
        )
    return text


# UniGetUI 的 `--id` / `--query` 是自由文本（winget 的 id 可带点、部分安装器允许空格），
# 不能拿 scoop 包名的字符集去卡。但这两个值会直接进 argv，所以仍要挡住开头的 `-`
# 与控制字符——前者是选项走私，后者会把一条命令拆成两行日志。
_UNIGET_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


def _validate_uniget_text(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PackageError(f"{what}不能为空", ErrorType.INVALID_PARAMS)
    if text.startswith("-"):
        raise PackageError(
            f"{what}不能以 - 开头（会被当成命令行选项）: {text!r}",
            ErrorType.INVALID_PARAMS,
        )
    if not _UNIGET_TEXT_RE.match(text):
        raise PackageError(
            f"{what}含非法字符（含控制字符或超过 200 字符）: {text[:80]!r}",
            ErrorType.INVALID_PARAMS,
        )
    return text


def validate_package_id(value: Any, what: str = "包 id") -> str:
    """UniGetUI `package details|versions --id` 的取值校验（只收字符面，不猜格式）"""
    return _validate_uniget_text(value, what)


def validate_query(value: Any, what: str = "搜索关键词") -> str:
    """UniGetUI `package search --query` 的取值校验"""
    return _validate_uniget_text(value, what)


def resolve_root(manager: str = "scoop") -> tuple[str | None, str]:
    """定位安装器根目录：进程环境 → 注册表回退 → `%USERPROFILE%\\scoop`。

    返回 `(root, reason)`；root 为 None 时 reason 说明为什么定不出来。与
    `discovery.expand_path` 同一条纪律：**取不到就说取不到**，不回退成一个
    看起来能用的相对路径。
    """
    name = _check_manager(manager)
    if name != "scoop":  # pragma: no cover —— 新后端接线后才会走到
        raise PackageError(f"{name} 暂不支持根目录解析", ErrorType.INVALID_PARAMS)
    for source, raw in (
        ("进程环境 SCOOP", os.environ.get("SCOOP")),
        ("注册表 SCOOP", registry_env_value("SCOOP")),
    ):
        text = (raw or "").strip().strip('"')
        if text:
            p = Path(text)
            if p.is_dir():
                return str(p), source
            return None, f"{source} 指向的目录不存在: {p}"
    profile = (
        os.environ.get("USERPROFILE")
        or registry_env_value("USERPROFILE")
        or ""
    ).strip()
    if not profile:
        return None, "进程环境与注册表都取不到 SCOOP，也取不到 USERPROFILE"
    default = Path(profile) / "scoop"
    if default.is_dir():
        return str(default), f"USERPROFILE 默认位置 {default}"
    return None, f"SCOOP 未设置且默认位置不存在: {default}"


def _load_json(path: Path) -> dict | None:
    """读一个 JSON，编码按 utf-8-sig（scoop 的 manifest 常带 BOM）。坏文件返回 None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def version_key(value: Any) -> list:
    """版本号的比较键：逐段比数值，非数字段回落小写串。

    scoop 版本号形如 `25.01` / `2025.12.0` / `0.50`，字符串排序会把 `19.8.3` 排在
    `2.3.1` 前面。空值返回 `[]`，任何非空版本都比它大——所以「读不到已装版本」的行
    必须在调用侧先挡掉，不能靠这里兜（否则会给出「有更新」的假信号）。
    """
    parts = []
    for seg in re.split(r"[._-]+", str(value or "")):
        if not seg:
            continue  # 前后分隔符产生的空段不算一层，否则 `version_key(None)` 不是 []
        parts.append((0, int(seg)) if seg.isdigit() else (1, seg.lower()))
    return parts


def _version_dirs(app_dir: Path) -> list[str]:
    """app 目录下的版本目录名（排除 `current`），按「点分段数字优先」排序。"""
    try:
        entries = [p.name for p in app_dir.iterdir() if p.is_dir() and p.name != "current"]
    except OSError:
        return []

    return sorted(entries, key=version_key, reverse=True)


def _abs_under(base: Path, rel: str) -> Path | None:
    """把 manifest 里的相对路径安全地解析到 base 之下，越界返回 None。"""
    text = str(rel or "").strip().lstrip("\\/")
    if not text:
        return None
    cand = (base / text).resolve()
    try:
        cand.relative_to(base.resolve())
    except ValueError:
        return None
    return cand


def _as_entries(value: Any) -> list:
    """manifest 的 bin / shortcuts 既可能是 str（单个），也可能是 list。

    写成 str 时按字符迭代会产出 `base/'b'`、`base/'w'` 这类垃圾路径（真机上
    表现成「bw.exe 有 6 个 bin」），所以必须先归一成列表再逐项取头。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _first_field(value: Any) -> str | None:
    """条目本身是 str 就用它；是 [exe, alias] 这种列表就取头一个"""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value:
        head = value[0]
        return head if isinstance(head, str) else None
    return None


SHIM_EXTS = (".exe", ".cmd", ".bat", ".ps1")

# CreateProcess 能直接起的扩展名（真机实测 2026-10-19：`.cmd` 由 Windows 关联到
# cmd.exe 可起，退出码/输出都正常；`.ps1` 抛 OSError [WinError 193]
# "%1 不是有效的 Win32 应用程序"）。scoop 的 shim 两种都发，所以
# 「能不能起」是入口的属性，不能等 spawn 时再炸。
_SPAWNABLE_EXTS = (".exe", ".cmd", ".bat")


def _spawnable(path: str | None) -> bool:
    return bool(path) and Path(path).suffix.lower() in _SPAWNABLE_EXTS


def shim_paths(root: str, name: str) -> list[str]:
    """scoop 给某个包生成的垫片入口（`shims/<name>.exe|.cmd|.bat|.ps1`）。

    为什么要单独查这一层：manifest 的 `bin` 不是必填。真机 `bayes` 的 manifest
    只有 `architecture/description/homepage/license/version` 五个键、`bin` 为空，
    但 `shims\\bayes.exe` 实实在在能跑——只看 manifest 就会把这个可启动的包报成
    「不可启动」。只按**包名同名**取，不猜别名（7zip 的 shim 叫 7z/7zfm/7zg，
    那批本来就在 manifest 的 bin 里），避免把别人的入口算到这个包头上。
    """
    shims = Path(root) / "shims"
    if not shims.is_dir():
        return []
    out = []
    try:
        entries = sorted(shims.iterdir())
    except OSError:
        return []
    for p in entries:
        if p.stem.lower() != name.lower() or p.suffix.lower() not in SHIM_EXTS:
            continue
        if p.is_file():
            out.append(str(p))
    return out


def installed_one(app_dir: Path, name: str, root: str | None = None) -> dict:
    """一个已装 app 的读盘视图（不 spawn）。

    版本目录选择顺序：`current/manifest.json` → 最新版本目录的 `manifest.json`。
    前者是 scoop 认定的当前版本；后者兜住本机实测到的「current 退化成空目录」，
    并把这种兜底标成 `current_broken`——状态必须可见，否则客户端会把一个断链的
    安装当成正常安装。

    传 `root` 时把该包的 shim 入口一并读进来，`launchable` 才算完整：真机 `bayes`
    的 manifest 没有 `bin`，但 `shims\\bayes.exe` 能跑——只看 manifest 会同时给出
    「不可启动」的行和「可启动」的 `launch_target`，两处口径打架。
    """
    current = app_dir / "current"
    current_manifest = current / "manifest.json"
    manifest: dict | None = None
    install_dir = None
    state = STATE_OK

    if current_manifest.is_file():
        manifest = _load_json(current_manifest)
        install_dir = current
    if manifest is None:
        for v in _version_dirs(app_dir):
            vd = app_dir / v
            m = _load_json(vd / "manifest.json")
            if m is not None:
                manifest = m
                install_dir = vd
                state = STATE_CURRENT_BROKEN
                break
    if manifest is None:
        # scoop 本体就是这样：current 是 git 工作区，没有 manifest.json。合法状态。
        # 字段集与正常行保持一致，避免调用方按「有没有这个键」分支。
        shims = shim_paths(root, name) if root else []
        row = {
            "name": name,
            "manager": "scoop",
            "state": STATE_NO_MANIFEST,
            "version": None,
            "bucket": None,
            "architecture": None,
            "sourceUrl": None,
            "description": None,
            "homepage": None,
            "installPath": str(current if current.is_dir() else app_dir),
            "currentAlive": False,
            "bin": [],
            "shortcuts": [],
            "shim": shims,
        }
        row["launchable"] = any(
            c["exists"] and c["runnable"] for c in _candidates(row)
        )
        return row
    # bucket 与架构来自同版本目录的 install.json（manifest.json 里没有 source）
    bucket = None
    arch = manifest.get("architecture")
    source_url = None
    if install_dir is not None:
        ij = _load_json(install_dir / "install.json")
        if ij:
            bucket = ij.get("bucket")
            source_url = ij.get("url")
            arch = ij.get("architecture") or arch

    real_dir = install_dir.resolve() if install_dir is not None else None
    bins: list[str] = []
    for item in _as_entries(manifest.get("bin")):
        rel = _first_field(item)
        target = _abs_under(real_dir, rel) if real_dir else None
        if target is not None:
            bins.append(str(target))
    shortcuts: list[dict] = []
    for item in _as_entries(manifest.get("shortcuts")):
        if not isinstance(item, (list, tuple)) or not item:
            continue
        rel = item[0] if isinstance(item[0], str) else None
        label = item[1] if len(item) > 1 and isinstance(item[1], str) else None
        target = _abs_under(real_dir, rel) if real_dir else None
        if target is None:
            continue
        shortcuts.append({"name": label or Path(rel or "").stem, "path": str(target)})
    shims = shim_paths(root, name) if root else []
    row = {
        "name": name,
        "manager": "scoop",
        "state": state,
        "version": manifest.get("version"),
        "bucket": bucket if isinstance(bucket, str) else None,
        "architecture": arch if isinstance(arch, str) else None,
        "sourceUrl": source_url if isinstance(source_url, str) else None,
        "description": manifest.get("description") if isinstance(manifest.get("description"), str) else None,
        "homepage": manifest.get("homepage") if isinstance(manifest.get("homepage"), str) else None,
        "installPath": str(real_dir) if real_dir else None,
        "currentAlive": current_manifest.is_file(),
        "bin": bins,
        "shortcuts": shortcuts,
        "shim": shims,
    }
    # 与 `launch_target` 同一个谓词：行的 launchable 就是「launch_target 默认能挑中」
    row["launchable"] = any(c["exists"] and c["runnable"] for c in _candidates(row))
    return row


def list_installed(root: str) -> list[dict]:
    """枚举已安装 app（纯读盘）。apps 目录读不到就返回空，不抛。"""
    apps = Path(root) / "apps"
    if not apps.is_dir():
        return []
    rows = []
    try:
        names = sorted(p.name for p in apps.iterdir() if p.is_dir())
    except OSError:
        return []
    for n in names:
        rows.append(installed_one(apps / n, n, root))
    return rows


def find_installed(root: str, name: str) -> dict | None:
    app_dir = Path(root) / "apps" / name
    if not app_dir.is_dir():
        return None
    return installed_one(app_dir, name, root)


def brief(row: dict) -> dict:
    """列表用的瘦身视图：完整行的入口路径数组在 14 个包 × 数个入口时会把响应撑大，
    列表页只需要「有哪些、什么状态、能不能起、有几个入口」，路径留给单包视图。
    """
    return {
        "name": row["name"],
        "state": row["state"],
        "version": row["version"],
        "bucket": row["bucket"],
        "description": row["description"],
        "homepage": row["homepage"],
        "launchable": row["launchable"],
        "entryCount": len(row["shortcuts"]) + len(row["bin"]) + len(row["shim"]),
    }


def _candidates(row: dict) -> list[dict]:
    """把一个已装行的 `shortcuts` / `bin` / `shim` 三段展平成候选列表。

    顺序即优先级（快捷方式 = 用户认得的那个图形入口，bin 次之，shim 兜底），
    `launch_target` 与 `launchable` 都走这里——两处必须同一个口径，否则
    「行说不可启动、target 却给得出路径」又会分叉。
    """
    candidates = []
    for s in row.get("shortcuts") or []:
        p = s.get("path")
        candidates.append({
            "kind": "shortcut",
            "name": s.get("name") or Path(p or "").name,
            "path": p,
            "exists": bool(p) and Path(p).is_file(),
            "runnable": _spawnable(p),
        })
    for b in row.get("bin") or []:
        candidates.append({"kind": "bin", "name": Path(b).name, "path": b,
                           "exists": Path(b).is_file(), "runnable": _spawnable(b)})
    for sh in row.get("shim") or []:
        candidates.append({"kind": "shim", "name": Path(sh).name, "path": sh,
                           "exists": Path(sh).is_file(), "runnable": _spawnable(sh)})
    return candidates


# `launch_target` 的失败分类（结构化，不做文案匹配）：handler 按 code 映射
# ErrorType，客户端按 code 决定下一步（换 entry / 修安装 / 别指望这个包能起）。
NO_SUCH_ENTRY = "no_such_entry"
MISSING_FILE = "missing_file"
NOT_SPAWNABLE = "not_spawnable"
NO_LIVE_ENTRY = "no_live_entry"
NO_ENTRIES = "no_entries"


def launch_target(row: dict, want: str | None = None) -> dict:
    """从已装视图里挑一个可执行入口。

    `want` 缺省时按 **快捷方式 → bin → shim** 的顺序取第一个**文件在盘上且形态可
    直接 spawn** 的候选。显式 `want` 在候选的名字或文件名上匹配（大小写不敏感）。
    每个候选带两个标记：
    - `exists`：断链安装（本机 6 个 `current_broken`）的 manifest 会声明指向已不在
      盘上的路径，不标就等于把死路径当好路径给出去；
    - `runnable`：扩展名是否是 CreateProcess 直接起得了的形态（见 `_SPAWNABLE_EXTS`
      的实测依据）。`scoop.ps1` 就是「文件确实在、但起不来」那一类。
    挑不出来时 `ok=False`，附 `code`（见上面几个常量）与全部候选，让调用方自己看
    差在哪，而不是猜一个跑起来。

    候选只取自行内字段，不再回头扫 root 目录：行的口径就是唯一口径。
    """
    candidates = _candidates(row)

    if want:
        needle = str(want).strip().lower()
        for c in candidates:
            if str(c["name"]).lower() == needle or Path(c["path"] or "").name.lower() == needle:
                if not c["exists"]:
                    return {"ok": False, "path": c["path"], "kind": c["kind"],
                            "code": MISSING_FILE, "candidates": candidates,
                            "reason": "目标文件不存在"}
                if not c["runnable"]:
                    return {"ok": False, "path": c["path"], "kind": c["kind"],
                            "code": NOT_SPAWNABLE, "candidates": candidates,
                            "reason": f"{c['path']} 的扩展名不能直接 spawn"
                                      f"（可直接启动: {', '.join(_SPAWNABLE_EXTS)}）"}
                return {"ok": True, "path": c["path"], "kind": c["kind"],
                        "code": None, "candidates": candidates, "reason": None}
        return {"ok": False, "path": None, "kind": None, "code": NO_SUCH_ENTRY,
                "candidates": candidates, "reason": f"没有名为 {want!r} 的入口"}

    for c in candidates:
        if c["exists"] and c["runnable"]:
            return {"ok": True, "path": c["path"], "kind": c["kind"],
                    "code": None, "candidates": candidates, "reason": None}
    if candidates:
        return {"ok": False, "path": None, "kind": None, "code": NO_LIVE_ENTRY,
                "candidates": candidates,
                "reason": "声明的入口没有一个「在盘上且可直接 spawn」"
                          "（断链安装或入口是非可执行脚本）"}
    return {"ok": False, "path": None, "kind": None, "code": NO_ENTRIES,
            "candidates": [], "reason": "该包没有声明 bin / shortcuts / shim"}


def search_packages(root: str, query: str, limit: Any = None) -> dict:
    """在已添加 bucket 的清单里搜包名（纯读盘）。

    bucket manifest 文件名即包名，所以先按文件名筛、只对命中项读 JSON——
    本机 4 个 bucket 共 4287 个 manifest，全量读一遍是几百毫秒级的浪费。
    排序：完全同名 → 前缀 → 包含。
    """
    term = str(query or "").strip().lower()
    if not term:
        raise PackageError("搜索关键词不能为空", ErrorType.INVALID_PARAMS)
    cap = _clamp_int(limit, default=DEFAULT_SEARCH_LIMIT, low=1,
                     high=MAX_SEARCH_LIMIT, what="limit")

    buckets_dir = Path(root) / "buckets"
    if not buckets_dir.is_dir():
        return {"rows": [], "scannedBuckets": 0, "scannedManifests": 0, "limit": cap}

    exact: list[dict] = []
    prefix: list[dict] = []
    contains: list[dict] = []
    scanned_buckets = 0
    scanned_manifests = 0
    for bucket in sorted(p for p in buckets_dir.iterdir() if p.is_dir()):
        bucket_dir = bucket / "bucket"
        if not bucket_dir.is_dir():
            bucket_dir = bucket
        scanned_buckets += 1
        try:
            files = sorted(p for p in bucket_dir.iterdir() if p.is_file() and p.suffix == ".json")
        except OSError:
            continue
        scanned_manifests += len(files)
        for f in files:
            stem = f.stem.lower()
            if stem == term:
                rank = exact
            elif stem.startswith(term):
                rank = prefix
            elif term in stem:
                rank = contains
            else:
                continue
            m = _load_json(f)
            if m is None:
                continue
            rank.append({
                "name": f.stem,
                "manager": "scoop",
                "bucket": bucket.name,
                "version": m.get("version") if isinstance(m.get("version"), str) else None,
                "description": m.get("description") if isinstance(m.get("description"), str) else None,
                "homepage": m.get("homepage") if isinstance(m.get("homepage"), str) else None,
                "license": m.get("license") if isinstance(m.get("license"), str) else None,
            })

    rows = (exact + prefix + contains)[:cap]
    return {"rows": rows, "scannedBuckets": scanned_buckets,
            "scannedManifests": scanned_manifests, "limit": cap}


# ---- 升级探测：本地 bucket 索引差集（零 spawn）+ UniGetUI 只读查询（显式执行）----

# `uniget package updates --manager <id>` 的稳定 id 白名单（docs/CLI.md 的取值表，
# 2026-10-24 对照本机 v2026.3.2 核对）。argv 直传不过 shell，但参数位一旦开放就会被
# `--elevated` 这类选项字符串走私，所以只认这批固定值——与 `install_argv` 不预留
# 透传 flag 位是同一条纪律。
UNIGET_MANAGERS = (
    "apt", "bun", "cargo", "chocolatey", "dnf", "dotnet-tool", "flatpak",
    "homebrew", "npm", "pacman", "pip", "pwsh", "scoop", "snap", "vcpkg",
    "winget", "winps",
)

# 命令行入口的显式覆盖（同 AWAM_* 系列：由部署者指路径）
UNIGET_CLI_ENV_VAR = "AWAM_UNIGET_CLI"

# 单次探测最多回传多少条升级项：兜住异常大的机器，本机实测读盘差集是 4 条
MAX_UPDATE_ROWS = 100


def validate_uniget_manager(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in UNIGET_MANAGERS:
        raise PackageError(
            f"不支持的安装器标识: {value!r}（UniGetUI 的稳定 id 只有: "
            f"{', '.join(UNIGET_MANAGERS)}）",
            ErrorType.INVALID_PARAMS,
        )
    return text


def bucket_version(root: str, bucket: str, name: str) -> str | None:
    """本地 bucket 索引里这个包的最新版本（纯读盘）。

    目录布局与 `search_packages` 同源：`buckets/<bucket>/bucket/<name>.json`，少数
    平铺的 bucket 回落 `buckets/<bucket>/<name>.json`。`bucket` 取自 install.json、
    `name` 取自我们的已装清单，两者都参与路径拼接，所以一个用 `_abs_under` 收越界、
    一个用 `validate_name` 收字符集——本地盘上的数据不算可信输入。
    """
    base = _abs_under(Path(root) / "buckets", str(bucket or ""))
    if base is None:
        return None
    try:
        safe = validate_name(name)
    except PackageError:
        return None
    for cand in (base / "bucket" / f"{safe}.json", base / f"{safe}.json"):
        if cand.is_file():
            version = (_load_json(cand) or {}).get("version")
            return version.strip() if isinstance(version, str) and version.strip() else None
    return None


def read_disk_updates(root: str) -> dict:
    """已装版本 vs 本地 bucket 索引版本 的差集：**纯读盘**，不 spawn、不联网。

    口径要说清楚：它回答的是「本地 bucket 索引认为谁有新版本」，索引本身新旧取决于
    上次 `scoop update`，不等于上游最新发布。比不了的包进 `unknown` 并给原因
    （直链安装没有 bucket、`no_manifest` 读不到已装版本、索引里没有这个包），
    不猜成「已是最新」——`version_key` 对空值返回 `[]`，任何版本都比它大，
    正因如此缺基准的行必须在这里先挡住。
    """
    installed = list_installed(root)
    rows: list[dict] = []
    unknown: list[dict] = []
    for row in installed:
        name = row["name"]
        bucket = row.get("bucket")
        version = row.get("version")
        if not bucket and not version:
            # 本机实测：scoop / openssl-light / scoop-search 连 install.json 都没有
            unknown.append({
                "name": name,
                "reason": "install.json 读不到（scoop 本体与手工目录都是这种），已装版本和 bucket 都缺",
            })
        elif not bucket:
            # 直链安装：install.json 写的是 url 而不是 bucket（本机 bayes / oh-my-posh）
            unknown.append({
                "name": name,
                "reason": "直链安装（install.json 记 url 而非 bucket），本地 bucket 索引里没有可比版本",
            })
        elif not version:
            unknown.append({
                "name": name,
                "reason": f"已装版本读不到（state={row['state']}），没有比较基准",
            })
        else:
            available = bucket_version(root, bucket, name)
            if available is None:
                unknown.append({
                    "name": name,
                    "reason": f"bucket '{bucket}' 的索引里没有 {name}.json",
                })
            elif version_key(available) > version_key(version):
                rows.append({
                    "manager": "scoop",
                    "name": name,
                    "installedVersion": version,
                    "availableVersion": available,
                    "bucket": bucket,
                    "state": row["state"],
                })
    rows.sort(key=lambda r: r["name"].lower())
    return {
        "rows": rows[:MAX_UPDATE_ROWS],
        "truncated": len(rows) > MAX_UPDATE_ROWS,
        "scanned": len(installed),
        "unknown": unknown,
    }


def locate_uniget_cli() -> tuple[str | None, str]:
    """定位 UniGetUI 的命令行入口 `uniget.exe`（只做文件检查，不 spawn）。

    候选只有显式覆盖和安装器会写的两个位置：`UniGetUI.iss` 的 `DefaultDirName` 是
    `{autopf64}\\UniGetUI`，per-user 装即 `%LOCALAPPDATA%\\Programs\\UniGetUI`（本机
    实测就在这），系统装即 `%ProgramFiles%\\UniGetUI`。**不走 PATH 兜底**——与
    `install_argv` 同一条纪律：要跑外部入口，就得说清跑的是哪一个。
    """
    override = os.environ.get(UNIGET_CLI_ENV_VAR, "").strip().strip('"')
    if override:
        exe = Path(override)
        if exe.is_file():
            return str(exe), f"环境变量 {UNIGET_CLI_ENV_VAR}"
        return None, f"{UNIGET_CLI_ENV_VAR} 指向的文件不存在: {exe}"

    candidates = []
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        candidates.append(Path(local) / "Programs" / "UniGetUI")
    for var in ("ProgramW6432", "ProgramFiles"):
        text = (os.environ.get(var) or "").strip()
        if text:
            candidates.append(Path(text) / "UniGetUI")
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return None, (
            f"LOCALAPPDATA / ProgramFiles 都取不到，无法定位 uniget.exe"
            f"（可用 {UNIGET_CLI_ENV_VAR} 显式指定）"
        )
    for root in candidates:
        exe = root / "uniget.exe"
        if exe.is_file():
            return str(exe), f"官方安装位置 {root}"
    return None, (
        "没找到 uniget.exe（试过: "
        + ", ".join(str(root / "uniget.exe") for root in candidates)
        + f"；可用 {UNIGET_CLI_ENV_VAR} 显式指定）"
    )


def uniget_status_argv(exe: str) -> list[str]:
    """构造 `uniget status`：只问「本机有没有活的 UniGetUI 会话」。

    这一步不是冗余：`status` 是唯一不需要 token 的命令，而其余数据命令在没有会话时
    会把参数转给 `UniGetUI.exe` 去冷启动——先探一次，就把「顺手弹出一个 GUI」的
    可能挡在门外。
    """
    return [str(exe), "status"]


def uniget_updates_argv(exe: str, manager: str | None = None) -> list[str]:
    """构造 `uniget package updates`（只读查询）。只构造、不执行。"""
    argv = [str(exe), "package", "updates"]
    if manager:
        argv += ["--manager", validate_uniget_manager(manager)]
    return argv


def uniget_managers_argv(exe: str) -> list[str]:
    """构造 `uniget manager list`：安装器及其就绪状态，不询问具体包"""
    return [str(exe), "manager", "list"]


def uniget_sources_argv(exe: str, manager: str | None = None) -> list[str]:
    """构造 `uniget source list`（只读）。manager 可选，限定到单个安装器"""
    argv = [str(exe), "source", "list"]
    if manager:
        argv += ["--manager", validate_uniget_manager(manager)]
    return argv


def uniget_search_argv(exe: str, manager: str, query: Any) -> list[str]:
    """构造 `uniget package search --manager <id> --query <关键词>`。

    实测不带 `--manager` 的搜索会以
    `{"status":"error","message":"The operation was canceled."}` 收场（v2026.3.2），
    所以这个入口把 manager 设成必填，由调用方明确问哪一个安装器。
    """
    return [
        str(exe), "package", "search",
        "--manager", validate_uniget_manager(manager),
        "--query", validate_query(query),
    ]


def uniget_installed_argv(exe: str, manager: str) -> list[str]:
    """构造 `uniget package installed --manager <id>`（只读）"""
    return [
        str(exe), "package", "installed",
        "--manager", validate_uniget_manager(manager),
    ]


def uniget_details_argv(exe: str, manager: str, package_id: Any) -> list[str]:
    """构造 `uniget package details --manager <id> --id <包 id>`（只读）"""
    return [
        str(exe), "package", "details",
        "--manager", validate_uniget_manager(manager),
        "--id", validate_package_id(package_id),
    ]


def uniget_versions_argv(exe: str, manager: str, package_id: Any) -> list[str]:
    """构造 `uniget package versions --manager <id> --id <包 id>`（只读）"""
    return [
        str(exe), "package", "versions",
        "--manager", validate_uniget_manager(manager),
        "--id", validate_package_id(package_id),
    ]


def _parse_uniget_json(text: str, what: str) -> dict:
    data: Any
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise PackageError(
            f"{what} 的输出不是 JSON: {str(text)[:200]!r}", ErrorType.RUNTIME_ERROR
        ) from None
    if not isinstance(data, dict):
        raise PackageError(f"{what} 的输出不是 JSON 对象", ErrorType.RUNTIME_ERROR)
    return data


def parse_uniget_running(text: str) -> bool:
    """`uniget status` → 会话是否活着（实测字段 `running`，非 true 一律按不可用处理）"""
    return _parse_uniget_json(text, "uniget status").get("running") is True


def parse_uniget_updates(text: str) -> dict:
    """`uniget package updates` 的 JSON → 我们的行。

    UniGetUI 失败时给出的仍是 JSON，只是 `status` 为 `error`（实测两种：没有活会话
    `The IPC API token is not available`；某个 manager 没就绪
    `No ready package manager matching "..."`），所以先看 status 再取数组。
    数组键按命令不同：`package updates` 是 `updates`，`package installed` 才是
    `packages`（本机各取过一份真实应答核对）——读错键会得到「没有更新」的假阴性。
    `name` 取 `id`（能直接交给安装命令的那个标识），显示名只在与 id 不同时才带。
    """
    data = _parse_uniget_json(text, "uniget package updates")
    if data.get("status") != "success":
        raise PackageError(
            f"uniget package updates 返回失败: "
            f"{data.get('message') or data.get('status')}",
            ErrorType.RUNTIME_ERROR,
        )
    rows: list[dict] = []
    items = data.get("updates")
    if not isinstance(items, list):
        raise PackageError(
            "uniget package updates 的成功响应里没有 updates 数组"
            f"（顶层键: {', '.join(sorted(data)) or '无'}）；"
            "把它读成「没有可用更新」会是假阴性",
            ErrorType.RUNTIME_ERROR,
        )
    for item in items:
        if not isinstance(item, dict):
            continue
        if not (item.get("isUpgradable") or item.get("newVersion")):
            continue
        name = str(item.get("id") or item.get("name") or "").strip()
        if not name:
            continue
        display = str(item.get("name") or "").strip()
        row = {
            "manager": str(item.get("manager") or ""),
            "name": name,
            "installedVersion": item.get("version") or None,
            "availableVersion": item.get("newVersion") or None,
            "source": item.get("source") or None,
        }
        if display and display != name:
            row["displayName"] = display
        rows.append(row)
    rows.sort(key=lambda r: (r["manager"], r["name"].lower()))
    return {"rows": rows[:MAX_UPDATE_ROWS], "truncated": len(rows) > MAX_UPDATE_ROWS}


def _uniget_ok(data: dict, what: str) -> dict:
    """UniGetUI 的错误应答通常是退出码 0 的 JSON（`status=error` + `message`），
    所以每条解析都必须先看 `status`，不能拿进程退出码当成功信号。极少数命令
    （`package details` 的 not-found）退出码非 0，正文形状不变，失败分支会先把它
    交给这里再决定错误类型。

    `No package matching id ... was found.` 在测过的命令里是「包确实不存在」，
    单独归到 `not_found`，让调用方知道换个 id 可能就行了。
    """
    if data.get("status") != "success":
        message = str(data.get("message") or data.get("status") or "")
        error_type = (
            ErrorType.NOT_FOUND if "No package matching" in message
            else ErrorType.RUNTIME_ERROR
        )
        raise PackageError(f"{what} 返回失败: {message}", error_type)
    return data


def _uniget_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _uniget_package_row(item: dict) -> dict | None:
    """`search` / `installed` 共用的行形状：`name` 取 `id`，显示名不同才带。

    与 `parse_uniget_updates` 同一约定——`id` 是能直接交给安装命令的规范标识。
    """
    pid = str(item.get("id") or item.get("name") or "").strip()
    if not pid:
        return None
    display = str(item.get("name") or "").strip()
    row = {
        "manager": str(item.get("manager") or ""),
        "name": pid,
        "version": _uniget_text(item.get("version")),
        "availableVersion": _uniget_text(item.get("newVersion")),
        "source": _uniget_text(item.get("source")),
        "upgradable": bool(item.get("isUpgradable")),
    }
    if display and display != pid:
        row["displayName"] = display
    return row


def parse_uniget_packages(text: str, what: str = "uniget package installed") -> dict:
    """`package search` / `package installed` 的 JSON → 行。

    两条命令共用 `packages` 数组键（`updates` 用 `updates`，见
    `parse_uniget_updates`）。成功但没有 `packages` 数组时抛错：那可能是命令
    换了形状，读成空清单会让「装了什么」静默变成「什么都没装」。
    """
    data = _uniget_ok(_parse_uniget_json(text, what), what)
    items = data.get("packages")
    if not isinstance(items, list):
        raise PackageError(
            f"{what} 的成功响应里没有 packages 数组"
            f"（顶层键: {', '.join(sorted(data)) or '无'}）",
            ErrorType.RUNTIME_ERROR,
        )
    rows: list[dict] = []
    for item in items:
        if isinstance(item, dict):
            row = _uniget_package_row(item)
            if row is not None:
                rows.append(row)
    return {"rows": rows}


def parse_uniget_managers(text: str) -> dict:
    """`manager list` 的 JSON → 安装器就绪清单（不展开 capabilities 噪声）"""
    data = _uniget_ok(_parse_uniget_json(text, "uniget manager list"),
                      "uniget manager list")
    items = data.get("managers")
    if not isinstance(items, list):
        raise PackageError(
            "uniget manager list 的成功响应里没有 managers 数组",
            ErrorType.RUNTIME_ERROR,
        )
    rows: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _uniget_text(item.get("name"))
        if not name:
            continue
        row = {
            "name": name,
            "displayName": _uniget_text(item.get("displayName")) or name,
            "enabled": bool(item.get("enabled")),
            "ready": bool(item.get("ready")),
        }
        executable = _uniget_text(item.get("executablePath"))
        if executable:
            row["executablePath"] = executable
        rows.append(row)
    return {"rows": rows}


def parse_uniget_sources(text: str) -> dict:
    """`source list` 的 JSON → 软件源清单（含是否配置/是否已知）"""
    data = _uniget_ok(_parse_uniget_json(text, "uniget source list"),
                      "uniget source list")
    items = data.get("sources")
    if not isinstance(items, list):
        raise PackageError(
            "uniget source list 的成功响应里没有 sources 数组",
            ErrorType.RUNTIME_ERROR,
        )
    rows: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _uniget_text(item.get("name"))
        manager = _uniget_text(item.get("manager"))
        if not name or not manager:
            continue
        count = item.get("packageCount")
        rows.append({
            "manager": manager,
            "name": name,
            "displayName": _uniget_text(item.get("displayName")) or name,
            "url": _uniget_text(item.get("url")),
            "packageCount": count if isinstance(count, int) else None,
            "updateDate": _uniget_text(item.get("updateDate")),
            "isKnown": bool(item.get("isKnown")),
            "isConfigured": bool(item.get("isConfigured")),
        })
    return {"rows": rows}


# `package details` 里返回的字段很多，这里只透传对「判断要不要装/查问题」有用的
_UNIGET_DETAIL_TEXT_FIELDS = (
    "description", "publisher", "author", "homepageUrl", "license", "licenseUrl",
    "installerType", "installerUrl", "installerHash", "manifestUrl",
    "installLocation", "releaseNotes", "releaseNotesUrl", "iconUrl", "updateDate",
)
_UNIGET_DETAIL_LIST_FIELDS = ("tags", "screenshots", "dependencies", "versions")


def parse_uniget_details(text: str) -> dict:
    """`package details` 的 JSON → 单包详情（字段白名单，不透传未知键）"""
    data = _uniget_ok(_parse_uniget_json(text, "uniget package details"),
                      "uniget package details")
    item = data.get("package")
    if not isinstance(item, dict):
        raise PackageError(
            "uniget package details 的成功响应里没有 package 对象",
            ErrorType.RUNTIME_ERROR,
        )
    row = _uniget_package_row(item)
    if row is None:
        raise PackageError(
            "uniget package details 的 package 没有 id/name",
            ErrorType.RUNTIME_ERROR,
        )
    row["ignoredVersion"] = _uniget_text(item.get("ignoredVersion"))
    size = item.get("installerSize")
    row["installerSize"] = size if isinstance(size, int) else None
    for key in _UNIGET_DETAIL_TEXT_FIELDS:
        row[key] = _uniget_text(item.get(key))
    for key in _UNIGET_DETAIL_LIST_FIELDS:
        value = item.get(key)
        row[key] = value if isinstance(value, list) else []
    return {"package": row}


# 单个包的版本历史不该无限回传（chocolatey 上有的包几十个版本）
MAX_VERSION_ROWS = 200


def parse_uniget_versions(text: str) -> dict:
    """`package versions` 的 JSON → 版本字符串列表（scoop 实测返回空数组，合法）"""
    data = _uniget_ok(_parse_uniget_json(text, "uniget package versions"),
                      "uniget package versions")
    items = data.get("versions")
    if not isinstance(items, list):
        raise PackageError(
            "uniget package versions 的成功响应里没有 versions 数组",
            ErrorType.RUNTIME_ERROR,
        )
    versions = [str(v).strip() for v in items if str(v).strip()]
    return {
        "versions": versions[:MAX_VERSION_ROWS],
        "count": len(versions),
        "truncated": len(versions) > MAX_VERSION_ROWS,
    }


async def run_uniget_read(
    make_argv: Callable[[str], list[str]],
    timeout: int = DEFAULT_TIMEOUT,
    *,
    parse: Any = None,
) -> dict:
    """跑一条 UniGetUI 只读命令：定位 → `status` 探会话 → 执行 → 解析。

    四条约束来自真机（v2026.3.2，2026-10-31 复核）：

    - 数据命令必须连一个活会话；没有 token 时它会把参数转给 `UniGetUI.exe`
      冷启动 GUI。所以先发 `status`（唯一不需要 token 的命令），
      `running=false` 就**不再发第二条**；
    - 外部查询会 spawn 底层安装器并联网，`timeout` 要原样传给两条命令；
    - 输出是 JSON，用 `UNIGET_OUTPUT_MAX_CHARS` 而不是默认 4000，仍被截时
      要报「被截断」并给出缩小范围的下一步，不能把半个 JSON 读成空清单；
    - 错误应答通常也是退出码 0 的 JSON，解析器负责看 `status`；个别命令
      （`package details` 的 not-found 实测退出码 2）退出码非 0，但正文仍是
      同形状 JSON，所以失败分支也先尝试解析再归类。

    `parse` 失败同样收敛成 `{available: False, phase: "parse"}`，把
    「命令跑通了但形状不对」与「命令根本没跑」分开，调用方才能给出对应错误类型。
    """
    exe, exe_source = locate_uniget_cli()
    if exe is None:
        return {"available": False, "exe": None, "phase": "locate",
                "reason": exe_source, "data": None,
                "errorType": ErrorType.INSTALL_REQUIRED}

    argv = make_argv(exe)

    status = await run_argv(uniget_status_argv(exe), timeout)
    if not status["ok"]:
        return {
            "available": False, "exe": exe, "phase": "status", "data": None,
            "errorType": ErrorType.CONNECTION_ERROR,
            "reason": f"uniget status 未成功（退出码 {status['exitCode']}"
            + ("，已超时终止" if status["timedOut"] else "")
            + f"）: {status['stderr'][:200]}",
        }
    try:
        running = parse_uniget_running(status["stdout"])
    except PackageError as exc:
        return {"available": False, "exe": exe, "phase": "status",
                "reason": str(exc), "data": None, "errorType": exc.error_type}
    if not running:
        return {
            "available": False, "exe": exe, "phase": "status", "data": None,
            "errorType": ErrorType.CONNECTION_ERROR,
            "reason": "UniGetUI 会话未运行（status 返回 running=false）；"
            "不代为冷启动它的窗口，需要外部口径请启动 UniGetUI 后重试",
        }

    command = "uniget " + " ".join(argv[1:])
    run = await run_argv(argv, timeout, max_output=UNIGET_OUTPUT_MAX_CHARS)
    if not run["ok"]:
        # 退出码不是可靠的成败信号：`package details` 的 not-found 实测退出码 2，
        # 正文却仍是 `status=error` 的同形状 JSON。能解析就沿用解析器的分类
        # （not_found / runtime_error），解析不了才按进程失败归类。
        error_type = ErrorType.RUNTIME_ERROR
        try:
            _uniget_ok(_parse_uniget_json(run["stdout"], command), command)
        except PackageError as exc:
            error_type = exc.error_type
        return {
            "available": False, "exe": exe, "phase": "query", "data": None,
            "errorType": error_type,
            "reason": f"{command} 未成功（退出码 {run['exitCode']}"
            + ("，已超时终止" if run["timedOut"] else "")
            + f"）: {(run['stdout'] or run['stderr'])[:200]}",
        }
    if run.get("stdoutTruncated"):
        return {
            "available": False, "exe": exe, "phase": "parse", "data": None,
            "errorType": ErrorType.RUNTIME_ERROR,
            "reason": f"{command} 输出超过 {UNIGET_OUTPUT_MAX_CHARS} 字符被截断；"
            "可用 manager 限定单个安装器后重试",
        }
    if parse is None:
        return {"available": True, "exe": exe, "phase": None,
                "reason": None, "data": run["stdout"], "errorType": None}
    try:
        data = parse(run["stdout"])
    except PackageError as exc:
        return {"available": False, "exe": exe, "phase": "parse",
                "reason": str(exc), "data": None, "errorType": exc.error_type}
    return {"available": True, "exe": exe, "phase": None, "reason": None,
            "data": data, "errorType": None}


def install_argv(root: str, name: str) -> list[str]:
    """构造安装命令。只构造、不执行——执行权在 handler，且要过 destructive 门禁。

    刻意不开 `--global` 之类的透传参数位：多一个入口就多一份注入面，而
    「装一个包」这件事本身不需要它。真要支持，得按 flag 逐个白名单，
    不是把调用方给的字符串拼进 argv。
    """
    safe = validate_name(name)
    shim = Path(root) / "shims" / "scoop.cmd"
    if not shim.is_file():
        raise PackageError(
            f"找不到 scoop 入口: {shim}（不用 PATH 兜底：装包会跑 bucket 里的安装脚本，"
            "必须确认用的是这套 root 自己的 shim）",
            ErrorType.INSTALL_REQUIRED,
        )
    return [str(shim), "install", safe]


async def run_argv(
    argv: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    max_output: int = OUTPUT_MAX_CHARS,
) -> dict:
    """执行一条已构造好的命令并捕获输出（异步，照 `tool_help.run_help_command` 的路子）。

    四点不是风格而是约束：
    - **`stdin=DEVNULL`**：子进程若继承 server 的 stdio 管道，会偷走 inbound
      JSON-RPC 字节，真机上打死过整个网关（见 TASKS.md 缺陷②）；
    - **不用 `subprocess.run`**：handler 跑在事件循环里，装包是分钟级动作，
      同步等待会把整个 MCP server 冻住；
    - **限时用 `anyio.fail_after`**：`asyncio.wait_for` 从外部任务取消协程会撕裂
      传输层 scope（backend_client 里四处注释反复交代过同一个坑）。
    - **`max_output` 留的是尾部**：对日志是对的（有用的是最后那段），对结构化
      输出（JSON）是错的——这类调用方要么抬 `max_output`，要么必须查
      `stdoutTruncated`，不要把截断串当完整文档解析。
    """
    cap = _clamp_int(timeout, default=DEFAULT_TIMEOUT, low=1,
                     high=MAX_TIMEOUT, what="timeout")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise PackageError(f"命令不存在: {argv[0]}", ErrorType.CONFIG_ERROR) from None
    except OSError as exc:
        raise PackageError(
            f"命令启动失败: {exc}", ErrorType.CONFIG_ERROR
        ) from exc

    timed_out = False
    try:
        with anyio.fail_after(cap):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        timed_out = True
        stdout = stderr = b""
        try:
            process.kill()
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2)
        except Exception:
            pass

    stdout_text = _decode(stdout)
    stderr_text = _decode(stderr)
    return {
        "ok": (not timed_out) and process.returncode == 0,
        "exitCode": process.returncode,
        "timedOut": timed_out,
        "stdout": stdout_text[-max_output:],
        "stderr": stderr_text[-max_output:],
        "stdoutTruncated": len(stdout_text) > max_output,
        "argv": argv,
        "timeout": cap,
    }
