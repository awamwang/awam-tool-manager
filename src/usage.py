"""工具调用使用统计：dispatch 出口记一行，聚合按需算。

为什么是 append-only JSONL 而不是内存计数器：上游网关会周期性整池回收本
server 进程（A10 那条边界），进程一活内存就归零，计数必须落在盘上才是真相源。
形态也刻意与 `policy.py` 的 `audit.log` 一致——audit 只记门禁决策，
usage 记所有调用，两份日志可以互相印证。

刻意**不记调用参数**：参数里可能带本机路径、关键词、动作正文，而聚合只需要
「哪个对外工具、动的是哪个 `tool_id`、成没成、快不快」。
"""

import json
import sys
from datetime import datetime, timedelta
from typing import Any

from runtime_paths import get_runtime_dir

# 模块全局，便于单测 monkeypatch 到 tmp（与 policy.AUDIT_LOG 同一体例）
USAGE_LOG = get_runtime_dir() / "usage.log"

DEFAULT_DAYS = 7
MAX_DAYS = 365


def record_call(
    tool: str,
    arguments: dict[str, Any] | None,
    *,
    ok: bool,
    error_type: str | None = None,
    duration_ms: int = 0,
    target: str | None = None,
) -> None:
    """记一行调用统计。

    `target` 由 dispatch 显式传入时优先（意图工具的真实目标是解析出来的
    tool_id / scoop 包 / 后端，而不是调用参数里的某个字段）；未传时保持
    旧口径——取 `arguments["tool_id"]`。

    旁路设施：**任何**落盘失败只往 stderr 告警，绝不向上抛——
    「统计写不进去」不该变成「工具调用失败」。
    """
    args = arguments or {}
    row = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": tool,
        "target": str(target if target is not None else (args.get("tool_id") or "")),
        "ok": bool(ok),
        "ms": max(0, int(duration_ms)),
    }
    if error_type:
        row["errorType"] = error_type

    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(
            f"[awam:usage][warning] 统计落盘失败，已跳过: {e}",
            file=sys.stderr,
            flush=True,
        )


def error_type_from(exc: BaseException) -> str:
    """从 `ToolError` 的 JSON 正文里取 `errorType`。

    `fail()` / `ensure_success()` 抛的异常正文就是那份对外载荷，
    所以统计里的 errorType 与客户端看到的**同源**；解析不出来时如实记
    `unknown_error`，不猜、也不假装成功。
    """
    try:
        payload = json.loads(str(exc))
    except (json.JSONDecodeError, TypeError):
        return "unknown_error"
    if isinstance(payload, dict):
        return str(payload.get("errorType") or "unknown_error")
    return "unknown_error"


def read_rows(days: int | None = None) -> list[dict[str, Any]]:
    """读 `usage.log`；文件不存在返回空，坏行跳过并在 stderr 汇总一次。

    `days` 是**日历日**窗口（含今天）：按 `ts` 前 10 位比对，不引入 timedelta
    边界歧义——用户问「最近 7 天」要的是按天对齐的数。
    """
    try:
        with open(USAGE_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        print(
            f"[awam:usage][warning] 统计读取失败，按无数据处理: {e}",
            file=sys.stderr,
            flush=True,
        )
        return []

    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
        else:
            skipped += 1

    if skipped:
        print(
            f"[awam:usage][warning] {USAGE_LOG.name} 有 {skipped} 行无法解析，已跳过",
            file=sys.stderr,
            flush=True,
        )

    if days is not None:
        rows = _within_days(rows, days)
    return rows


def normalize_days(days: int | None) -> int:
    """把调用方给的窗口收进 `[1, MAX_DAYS]`；缺省 `DEFAULT_DAYS`"""
    if days is None:
        return DEFAULT_DAYS
    return max(1, min(int(days), MAX_DAYS))


def window(days: int | None) -> dict[str, Any]:
    """返回给调用方的窗口描述——「按哪几天算」必须在结果里自证"""
    normalized = normalize_days(days)
    today = datetime.now().astimezone().date()
    return {
        "days": normalized,
        "since": (today - timedelta(days=normalized - 1)).isoformat(),
        "until": today.isoformat(),
    }


def _within_days(rows: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    """按日历日截取最近 `days` 天（含今天）；`ts` 不合法的行留在窗口外"""
    normalized = normalize_days(days)
    today = datetime.now().astimezone().date()
    allowed = {(today - timedelta(days=i)).isoformat() for i in range(normalized)}
    return [r for r in rows if str(r.get("ts", ""))[:10] in allowed]


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """纯函数：把行集合折成统计视图（不做 IO，单测可以直接喂行）

    `byName` 按「哪个对外工具被调得多」排序，`targets` 按「哪个登记的工具
    被动得多」排序——后者才是用户想要的「本机常用工具」榜。
    """
    total = len(rows)
    failed = sum(1 for r in rows if not r.get("ok"))

    by_name: dict[str, dict[str, Any]] = {}
    by_target: dict[str, dict[str, Any]] = {}
    by_day: dict[str, int] = {}
    error_types: dict[str, int] = {}

    for r in rows:
        name = str(r.get("tool") or "<无工具名>")
        ok = bool(r.get("ok"))
        ms = int(r.get("ms") or 0)
        day = str(r.get("ts", ""))[:10] or "<无日期>"
        target = str(r.get("target") or "")

        bucket = by_name.setdefault(name, {
            "tool": name, "count": 0, "failed": 0, "msTotal": 0, "msMax": 0,
        })
        bucket["count"] += 1
        bucket["failed"] += 0 if ok else 1
        bucket["msTotal"] += ms
        bucket["msMax"] = max(bucket["msMax"], ms)

        if target:
            t_bucket = by_target.setdefault(target, {
                "toolId": target, "count": 0, "failed": 0,
            })
            t_bucket["count"] += 1
            t_bucket["failed"] += 0 if ok else 1

        by_day[day] = by_day.get(day, 0) + 1
        if not ok:
            et = str(r.get("errorType") or "unknown_error")
            error_types[et] = error_types.get(et, 0) + 1

    def _named(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(buckets, key=lambda b: (-b["count"], b["tool"]))
        for b in ordered:
            b["msAvg"] = round(b.pop("msTotal") / b["count"], 1)
        return ordered

    return {
        "total": total,
        "success": total - failed,
        "failed": failed,
        "byName": _named(list(by_name.values())),
        "targets": sorted(by_target.values(), key=lambda b: (-b["count"], b["toolId"])),
        "byDay": [{"day": d, "count": c} for d, c in sorted(by_day.items())],
        "errorTypes": [
            {"errorType": k, "count": v}
            for k, v in sorted(error_types.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }
