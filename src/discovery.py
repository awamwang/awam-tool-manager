"""本机工具自动发现（P1-06）：读声明式候选清单 + 只读文件系统探测。

与 `policy.py` 的分工：两者都是「仓库里的 JSON 声明 + 环境变量覆盖」，但
失败时机刻意不同——策略判错等于安全约束静默失效，所以 policy 在 import 期
快速失败；发现清单不是安全面，坏文件只该让 `discover_tools` 这一次调用失败
（`config_error`），不该把整个 server 拖到起不来。

**默认零执行**：探测只做文件系统读——路径存在、目录条目数、以及命令是否在
PATH 上（`shutil.which`，同样不 spawn）。跑命令取版本号（`scoop --version`
这类）要调用方显式传 `run_commands=true` 才发生，与 `launch_tool` 的
`execute` / `get_tool_help` 的 `run` 同一条纪律：能跑出子进程的参数永远是显式
给出来的。

探测面是**声明出来的、不是穷举**：清单里没写路径的安装位置发现不了。所以
结论分三档——命中判 `installed`（高信心），全部声明位置都检查成且都不存在才判
`not_installed`（中信心），只要有位置没检查成（环境变量没设）就判 `unknown`
（无信心）。`status` 复用工具条目的同一套取值，调用方可以把发现结果直接回填给
`list_tools` 的 `status` 筛选。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

DISCOVERY_ENV_VAR = "AWAM_DISCOVERY_FILE"
DEFAULT_DISCOVERY_PATH = Path(__file__).parent.parent / "data" / "discovery.json"

# 探测取值与工具条目 `status` 共用词表（NE-8 的「能筛的维度都能看见」在这里
# 反过来成立：发现结果要能被筛选参数接住）
STATUS_INSTALLED = "installed"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_UNKNOWN = "unknown"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_NONE = "none"

# 与 `schema.py` 的条目 kind 同一套取值（清单里写别的接不上 `list_tools` 筛选）
ENTRY_KINDS = ("app", "mcp", "launcher")

PROBE_PATH_EXISTS = "path_exists"
PROBE_DIR_COUNT = "dir_count"
PROBE_KINDS = (PROBE_PATH_EXISTS, PROBE_DIR_COUNT)

# 探测结论三态。**inconclusive 不是 miss 的同义词**：声明的位置因为环境变量
# 没设而没检查成时，说「没装」是假结论，只能说「不知道」。
VERDICT_HIT = "hit"
VERDICT_MISS = "miss"
VERDICT_INCONCLUSIVE = "inconclusive"

DEFAULT_DIR_LIMIT = 60
MAX_DIRS = 200

# Windows 上能真被 CreateProcess 起起来的后缀。`shutil.which` 先按**裸名**试再按
# PATHEXT 补后缀，而 scoop / pyenv 的 shims 目录里裸名是给 Git Bash 用的脚本，
# 于是裸名先命中、把不可执行的路径交下去，实跑必报 `[WinError 193] %1 不是有效
# 的 Win32 应用程序`（真机 discover_tools run_commands=true 首次暴露）。
WIN_EXEC_SUFFIXES = (".exe", ".com", ".cmd", ".bat")


class DiscoveryError(Exception):
    """发现清单不可用（读不了 / JSON 坏了 / 校验失败）或探测参数非法"""


def resolve_discovery_path() -> Path:
    """清单路径：`AWAM_DISCOVERY_FILE` 覆盖优先，否则仓库默认文件"""
    override = os.environ.get(DISCOVERY_ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_DISCOVERY_PATH


def validate_manifest(data: Any) -> str | None:
    """返回错误描述（None = 合规）。与 `schema.validate_policy` 同体例。

    只做「能不能安全探测」这一层的检查：结构不对就拒绝，不猜、不部分接受。
    """
    if not isinstance(data, dict):
        return "发现清单必须是 JSON 对象"
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return "发现清单缺少 candidates 数组"
    seen: set[str] = set()
    for idx, cand in enumerate(candidates):
        if not isinstance(cand, dict):
            return f"candidates[{idx}] 必须是对象"
        tool_id = cand.get("tool_id")
        if not isinstance(tool_id, str) or not tool_id.strip():
            return f"candidates[{idx}] 的 tool_id 非法"
        if tool_id in seen:
            return f"tool_id 重复: {tool_id}"
        seen.add(tool_id)
        if not isinstance(cand.get("name"), str) or not cand.get("name").strip():
            return f"{tool_id} 缺少 name"
        # kind 与工具条目共用词表：写了词表外的值，返回的 kind 就接不上
        # `list_tools` 的 kind 筛选，等于自造一套分类
        if cand.get("kind") is not None and cand["kind"] not in ENTRY_KINDS:
            return f"{tool_id} 的 kind 取值非法: {cand['kind']}（应为 {' / '.join(ENTRY_KINDS)}）"
        probes = cand.get("probes", [])
        if not isinstance(probes, list):
            return f"{tool_id} 的 probes 必须是数组"
        for p_idx, probe in enumerate(probes):
            error = _validate_probe(probe)
            if error:
                return f"{tool_id}.probes[{p_idx}] {error}"
        command = cand.get("command")
        if command is not None:
            if not isinstance(command, dict):
                return f"{tool_id} 的 command 必须是对象"
            if not _valid_argv(command.get("argv")):
                return f"{tool_id} 的 command.argv 非法（需非空字符串数组）"
            pattern = command.get("version_pattern")
            if pattern is not None:
                if not isinstance(pattern, str):
                    return f"{tool_id} 的 command.version_pattern 必须是字符串"
                try:
                    re.compile(pattern)
                except re.error as exc:
                    return f"{tool_id} 的 command.version_pattern 不是合法正则: {exc}"
    return None


def _validate_probe(probe: Any) -> str | None:
    if not isinstance(probe, dict):
        return "必须是对象"
    kind = probe.get("probe")
    if kind not in PROBE_KINDS:
        return f"探测类型不支持: {kind!r}（可用: {', '.join(PROBE_KINDS)}）"
    paths = _probe_paths(probe)
    if not paths:
        return "至少要给一个 path / paths"
    if any(not isinstance(p, str) or not p.strip() for p in paths):
        return "path 必须是非空字符串"
    limit = probe.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        return "limit 必须是整数"
    return None


def _probe_paths(probe: dict) -> list[str]:
    """统一 path（单个）与 paths（多个）两种写法"""
    if probe.get("paths") is not None:
        value = probe["paths"]
        return list(value) if isinstance(value, list) else []
    single = probe.get("path")
    return [single] if isinstance(single, str) else []


def _valid_argv(argv: Any) -> bool:
    return (
        isinstance(argv, list)
        and bool(argv)
        and all(isinstance(item, str) and item.strip() for item in argv)
    )


def load_manifest(path: str | Path | None = None) -> dict:
    """加载并校验发现清单；文件不存在返回空清单（零配置零发现）。

    Raises:
        DiscoveryError: 读不了 / JSON 坏 / 校验失败。
    """
    manifest_path = Path(path) if path is not None else resolve_discovery_path()
    if not manifest_path.exists():
        return {"version": 1, "candidates": []}
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"发现清单不可读或不是合法 JSON（{manifest_path}）: {exc}") from exc
    error = validate_manifest(data)
    if error:
        raise DiscoveryError(f"发现清单校验失败（{manifest_path}）: {error}")
    return data


def expand_path(template: str) -> tuple[Path | None, str | None]:
    """展开 `{ENV}` 模板成绝对路径。

    返回 `(path, unresolved_env)`：任一 token 在环境里取不到就整体判不命中
    ——**不回退成相对路径**，那会把「没设环境变量」伪装成「这个路径不存在」，
    两种情况在结论里必须分开。

    token 字符集含 `()`，因为 Windows 的真实变量名就长这样
    （`ProgramFiles(x86)`）；展开后仍残留花括号同样按「取不到」处理，
    绝不拿字面量 `{...}` 去试存在性。
    """
    missing: list[str] = []

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if not value:
            missing.append(name)
            return match.group(0)
        return value

    resolved = re.sub(r"\{([A-Za-z_][A-Za-z0-9_()]*)\}", _sub, template)
    if missing:
        return None, ",".join(missing)
    residue = re.search(r"\{[^{}]*\}", resolved)
    if residue:
        return None, f"无法识别的 token {residue.group(0)}"
    try:
        return Path(resolved).expanduser(), None
    except (OSError, ValueError):
        return None, "path 无法解析"


def _probe_dir_count(path: Path, limit: int) -> dict:
    if limit <= 0:
        # 与 `get_backend_logs` 的 `limit<=0` 同口径：显式要 0 条就是 0 条，
        # 不把 0 当「没写」而回落到默认值
        return {"ok": True, "count": 0, "entries": []}
    entries: list[str] = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            entries.append(child.name)
            if len(entries) >= limit:
                break
    except OSError as exc:
        return {"ok": False, "reason": f"目录不可读: {exc}"}
    return {"ok": True, "count": len(entries), "entries": entries}


def run_probes(cand: dict) -> tuple[list[dict], str]:
    """执行一个候选的文件系统探测面，返回 (证据, 结论)。

    结论只有 `path_exists` 能下：`dir_count` 是补充证据（装了几个包），
    目录空不代表没装，所以只 `dir_count` 的候选整体是 inconclusive。
    某个声明位置展开不了（环境变量没设）→ 该次探测没做成 → inconclusive，
    即使别的位置都不存在，也不能判 miss。
    """
    evidence: list[dict] = []
    verdicts: list[str] = []

    for probe in cand.get("probes", []):
        kind = probe["probe"]
        paths = probe.get("paths") or [probe.get("path", "")]
        row: dict[str, Any] = {"probe": kind, "label": probe.get("label") or paths[0]}

        if kind == PROBE_DIR_COUNT:
            target, unresolved = expand_path(paths[0])
            if unresolved:
                row.update({"ok": False, "reason": f"未能展开: {unresolved}"})
            else:
                limit = min(max(int(probe.get("limit", DEFAULT_DIR_LIMIT)), 0), MAX_DIRS)
                row["path"] = str(target)
                row.update(
                    _probe_dir_count(target, limit)
                    if target.is_dir()
                    else {"ok": False, "reason": "目录不存在"}
                )
            evidence.append(row)
            continue

        # path_exists：声明的多个位置逐个试，第一个存在的算命中
        found = None
        checked: list[str] = []
        unresolved: list[str] = []
        for template in paths:
            one, miss = expand_path(template)
            if miss:
                unresolved.append(miss)
                continue
            checked.append(str(one))
            if found is None and one.exists():
                found = one
        if found is not None:
            row.update({"ok": True, "path": str(found)})
            verdicts.append(VERDICT_HIT)
        elif unresolved:
            row.update({
                "ok": False,
                "reason": f"环境变量未设置: {','.join(unresolved)}",
                "checked": checked,
            })
            verdicts.append(VERDICT_INCONCLUSIVE)
        else:
            row.update({"ok": False, "checked": checked})
            verdicts.append(VERDICT_MISS)
        evidence.append(row)

    return evidence, fold_verdicts(verdicts)


def fold_verdicts(verdicts: list[str]) -> str:
    """多个证据维度合成总结论：命中优先，其次「有一个维度没检查成」。

    miss 只有在**所有**能下结论的维度都说没命中、且没有任何维度没检查成时才成立。
    """
    if VERDICT_HIT in verdicts:
        return VERDICT_HIT
    if not verdicts or VERDICT_INCONCLUSIVE in verdicts:
        return VERDICT_INCONCLUSIVE
    return VERDICT_MISS


def status_of(verdict: str) -> tuple[str, str]:
    """结论 → (status, confidence)。词表与工具条目 `status` 一致，可直接回填筛选"""
    if verdict == VERDICT_HIT:
        return STATUS_INSTALLED, CONFIDENCE_HIGH
    if verdict == VERDICT_MISS:
        return STATUS_NOT_INSTALLED, CONFIDENCE_MEDIUM
    return STATUS_UNKNOWN, CONFIDENCE_NONE


def _preview_of(cand: dict) -> str:
    """命令面的人读预览：不跑也给得出，调用方据此决定要不要开 run_commands"""
    command = cand.get("command") or {}
    if command.get("preview"):
        return str(command["preview"])
    argv = command.get("argv")
    return " ".join(argv) if _valid_argv(argv) else ""


def _win_exec_suffix_order() -> tuple[str, ...]:
    """后缀尝试顺序：PATHEXT 的先后为准，只保留可执行集合内的项。

    PATHEXT 里 `.py` `.vbs` `.js` 这类 CreateProcess 起不来的项不参与尝试；PATHEXT
    取不到（MCP 宿主常不给）时退回 `WIN_EXEC_SUFFIXES` 原序。
    """
    pathext = []
    for item in os.environ.get("PATHEXT", "").split(os.pathsep):
        if not item:
            continue
        suffix = item.lower()
        if not suffix.startswith("."):
            suffix = "." + suffix
        if suffix in WIN_EXEC_SUFFIXES:
            pathext.append(suffix)
    return tuple(pathext) or WIN_EXEC_SUFFIXES


def resolve_executable(name: str) -> str | None:
    """PATH 上解析命令名，裸名命中的是不可执行的无扩展名垫片时改取真身。

    只在 Windows 上做二次解析：POSIX 上裸名本来就是正常形态。补后缀时按目录优先
    （PATH 靠前的目录先赢，与系统解析一致），一个都没找到就仍返回 `which` 的原结果
    ——让调用方照旧拿到 `available=true` 并在实跑时报出真实错误，不假装没找到。
    """
    exe = shutil.which(name)
    if exe is None or os.name != "nt":
        return exe
    if Path(exe).suffix.lower() in _win_exec_suffix_order():
        return exe
    for suffix in _win_exec_suffix_order():
        for directory in (os.environ.get("PATH") or os.defpath).split(os.pathsep):
            if not directory:
                continue
            candidate = Path(directory) / (name + suffix)
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return exe


def probe_command(cand: dict, run: bool = False, timeout: float = 8.0) -> dict:
    """命令面探测：`available` 只查 PATH（不 spawn），版本号才需要 `run=True`。

    `run` 由调用方显式给（`discover_tools` 的 `run_commands`），与 `launch_tool`
    的 `execute` / `get_tool_help` 的 `run` 是同一条门禁纪律。跑的时候无 shell
    （argv 走 list 形式）、超时必杀、失败不抛——命令跑不出来只是拿不到版本号，
    不该让整次发现失败。
    """
    command = cand.get("command") or {}
    argv = command.get("argv")
    # `run` 报的是**有没有真的跑过**，不是调用方传了什么：客户端读 `run: false`
    # 时不该需要再去看别的字段才敢确信没有子进程被起过
    result: dict[str, Any] = {"preview": _preview_of(cand), "run": False}
    if not _valid_argv(argv):
        result["reason"] = "清单未给出可执行的命令面"
        return result

    exe = resolve_executable(argv[0])
    result["available"] = exe is not None
    if exe is None:
        if run:
            result.update({"ok": False, "reason": f"命令不在 PATH: {argv[0]}"})
        return result
    result["path"] = exe
    if not run:
        return result
    result["run"] = True

    try:
        # 用 which 解析出的绝对路径执行：.cmd/.bat 垫片在 Windows 上必须是绝对路径
        #
        # stdin 必须显式 DEVNULL（与 `backend_client.py:181`、`tool_help.py:186` 同一条
        # 纪律）：本服务的 stdin 是上游网关的 stdio 协议管道，`.cmd` 垫片经 cmd.exe
        # 执行时会继承它并从中读走字节，等价于吞掉入站 JSON-RPC——真机实测整个
        # server 随即退出。
        done = subprocess.run(  # noqa: S603 —— argv 来自仓库内声明的清单，无 shell 拼接
            [exe, *argv[1:]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            shell=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {**result, "ok": False, "reason": f"命令超时（>{timeout}s）"}
    except OSError as exc:
        return {**result, "ok": False, "reason": f"命令无法执行: {exc}"}

    text = ((done.stdout or "") + (done.stderr or "")).strip()
    result["ok"] = done.returncode == 0
    result["exitCode"] = done.returncode
    pattern = command.get("version_pattern")
    if pattern:
        match = re.search(pattern, text)
        result["version"] = match.group(1) if match and match.groups() else None
    if text:
        result["output"] = text[:400]
    return result


def discover(manifest: dict, registry, run_commands: bool = False) -> dict:
    """把清单 + 探测结果折成对外视图（纯编排，单测直接喂 manifest + 假 registry）"""
    registered_ids = {t.get("id") for t in registry.list_all_tools()}

    rows = []
    summary = {
        STATUS_INSTALLED: 0,
        STATUS_NOT_INSTALLED: 0,
        STATUS_UNKNOWN: 0,
        "registered": 0,
        "unregistered": 0,
    }
    for cand in manifest.get("candidates", []):
        tool_id = cand["tool_id"]
        evidence, verdict = run_probes(cand)
        verdicts = [verdict] if cand.get("probes") else []
        command = None
        if cand.get("command") is not None:
            command = probe_command(cand, run=run_commands)
            verdicts.append(
                VERDICT_INCONCLUSIVE if "available" not in command
                else (VERDICT_HIT if command["available"] else VERDICT_MISS)
            )
        status, confidence = status_of(fold_verdicts(verdicts))
        is_registered = tool_id in registered_ids
        row = {
            "toolId": tool_id,
            "name": cand["name"],
            "kind": cand.get("kind", "app"),
            "purpose": cand.get("purpose", ""),
            "status": status,
            "confidence": confidence,
            "registered": is_registered,
            "evidence": evidence,
        }
        if command is not None:
            row["command"] = command
        rows.append(row)
        summary[status] += 1
        summary["registered" if is_registered else "unregistered"] += 1

    return {"candidates": rows, "summary": summary}


def note_for(result: dict) -> str:
    """探测面不是穷举这件事必须在结果里自证，否则「没发现」会被读成「本机没有」"""
    summary = result["summary"]
    return (
        f"探测面来自 {resolve_discovery_path()}，只覆盖清单里声明的路径与命令："
        "未声明位置的安装发现不了；未命中只判 not_installed（中信心），"
        "声明位置没能检查成时判 unknown，两者都不是「本机没有」的事实结论。"
        f"本机命中 {summary[STATUS_INSTALLED]} 个候选，其中 {summary['unregistered']} 个"
        "尚未登记进 data/tools/（可作为下一步接入对象）"
    )
