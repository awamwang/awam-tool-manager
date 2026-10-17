"""破坏性门禁判定（ARC-P1-05 Phase 1，方案 A）。

唯一入口 `decide_destructive(kind, tool, sub_op, args)`——两个执行类
handler（`call_backend_tool` / `run_launcher_action`）只调它，不再各自
维护判定逻辑。为 Phase 2（方案 C）留位：届时 policy 文件只是在
本函数外围增加策略层，判定产物（Decision）不变。

判定表（默认全部保持接入前行为，未配置新字段时逐字等价）：

- mcp：条目未标 `destructive` → 放行；有 `destructiveTools` → 子工具精确
  匹配才拦；无清单 → 整后端拦。
- launcher：条目未标 `destructive` → 放行；有 `destructiveActions`（新字段）
  → 按动作 `id`/`name`/`type` 做大小写不敏感 glob（fnmatch）匹配，命中才拦；
  无该字段 → 回退条目级全拦（现语义，不回退放宽）。

审计（Q4 裁决：Phase 1 只 stderr）：每次判定（放行/拒绝/确认放行）产出
一行结构化 JSON 到 stderr，字段 `ts / tool / kind / sub_op / decision /
rule_hit / client_declared`。`client_declared` 明确记录「授权来自客户端
参数」这一事实，为 Phase 2 的「策略赢过客户端声明」对比用。

行为变更面（需申报）：`destructiveActions` 是新增可选字段；未配置时行为
与接入前逐字等价；配置后「未匹配动作」由拦转放（安全属性放宽，必须由
部署者在工具条目里显式启用）。
"""

from __future__ import annotations

import datetime
import fnmatch
import json
import sys

# 判定命中时的规则标识（审计 rule_hit 字段的合法取值）。
RULE_NOT_DESTRUCTIVE = "not_destructive"
RULE_ENTRY_LEVEL = "entry_level"
RULE_DESTRUCTIVE_TOOLS_MATCH = "destructive_tools_match"
RULE_DESTRUCTIVE_TOOLS_MISS = "destructive_tools_miss"
RULE_DESTRUCTIVE_ACTIONS_MATCH = "destructive_actions_match"
RULE_DESTRUCTIVE_ACTIONS_MISS = "destructive_actions_miss"
RULE_UNKNOWN_KIND = "unknown_kind"


def _utc_now_iso() -> str:
    """UTC ISO 时间戳（与 data/ 及审计日志同风格，Z 结尾）。

    不 import backend_client 的同名函数：policy 保持零项目内依赖，
    Phase 2 扩展（策略加载/审计落盘）时也不该被 backend 的 mcp 依赖拖累。
    """
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _match_any(patterns, values) -> str | None:
    """大小写不敏感 glob 匹配：返回命中的第一个动作属性值，未命中返回 None。

    fnmatchcase 是平台无关的（不用 fnmatch.fnmatch——它在 Windows 上会
    经 os.path.normcase 做盘符相关的归一化）；两边同时 lower 实现大小写
    不敏感。空值跳过：动作清单 partial 时 name/type 可能缺失，空串参与
    匹配会让 `*` 之类的模式误命中一切。
    """
    for value in values:
        if not value:
            continue
        lowered = str(value).lower()
        for pattern in patterns:
            if fnmatch.fnmatchcase(lowered, str(pattern).lower()):
                return str(value)
    return None


def _decide_mcp(tool: dict | None, sub_op: dict) -> tuple[bool, str, str]:
    """mcp 判定表。返回 (是否命中破坏性判定, rule_hit, 人类可读理由)。"""
    tool_name = sub_op.get("tool_name") or ""
    tool_id = (tool or {}).get("id") or ""
    if not tool or not tool.get("destructive"):
        return False, RULE_NOT_DESTRUCTIVE, "目标工具未标记 destructive，放行"
    listed = tool.get("destructiveTools")
    if listed is None:
        # 旧语义：条目标了 destructive 又没给子工具清单 → 整个后端都拦
        return (
            True,
            RULE_ENTRY_LEVEL,
            f"目标工具 '{tool_id}' 的子工具 '{tool_name}' 被判定为 destructive，"
            f"需显式传 allowDestructive=true 才会转发调用",
        )
    if tool_name in listed:
        return (
            True,
            RULE_DESTRUCTIVE_TOOLS_MATCH,
            f"目标工具 '{tool_id}' 的子工具 '{tool_name}' 被判定为 destructive，"
            f"需显式传 allowDestructive=true 才会转发调用",
        )
    return (
        False,
        RULE_DESTRUCTIVE_TOOLS_MISS,
        f"子工具 '{tool_name}' 不在 destructiveTools 清单内，放行",
    )


def _decide_launcher(tool: dict | None, sub_op: dict) -> tuple[bool, str, str]:
    """launcher 判定表。返回 (是否命中破坏性判定, rule_hit, 人类可读理由)。"""
    action_id = sub_op.get("action_id") or ""
    action_name = sub_op.get("action_name") or ""
    action_type = sub_op.get("action_type") or ""
    label = action_id or action_name or "未知动作"
    tool_id = (tool or {}).get("id") or ""
    if not tool or not tool.get("destructive"):
        return False, RULE_NOT_DESTRUCTIVE, "启动器未标记 destructive，放行"
    patterns = tool.get("destructiveActions")
    if patterns is None:
        # 旧语义：只标了条目级 destructive、没有动作清单 → 所有动作都要确认
        return (
            True,
            RULE_ENTRY_LEVEL,
            f"启动器 '{tool_id}' 被标记为 destructive（未配置 destructiveActions），"
            f"动作 '{label}' 需显式传 allowDestructive=true 才会执行",
        )
    hit = _match_any(patterns, (action_id, action_name, action_type))
    if hit is not None:
        return (
            True,
            RULE_DESTRUCTIVE_ACTIONS_MATCH,
            f"启动器 '{tool_id}' 的动作 '{label}' 命中 destructiveActions"
            f"（匹配值: '{hit}'），需显式传 allowDestructive=true 才会执行",
        )
    return (
        False,
        RULE_DESTRUCTIVE_ACTIONS_MISS,
        f"启动器 '{tool_id}' 的动作 '{label}' 未命中 destructiveActions，放行",
    )


def _emit_audit(audit: dict) -> None:
    """结构化审计行（Q4：Phase 1 只 stderr，机器可解析的 JSON 单行）"""
    print(json.dumps(audit, ensure_ascii=False), file=sys.stderr, flush=True)


def decide_destructive(
    kind: str, tool: dict | None, sub_op: dict, args: dict
) -> dict:
    """破坏性门禁的唯一判定入口。

    Args:
        kind: `"mcp"` 或 `"launcher"`（其他值放行——新增 kind 必须显式接入
            判定表，默认不拦以免拖垮未知类型）。
        tool: 工具条目（`registry.get_tool()` 的返回）；None 表示工具不存在。
        sub_op: 子操作标识。mcp → `{"tool_name": str}`；launcher →
            `{"action_id", "action_name", "action_type"}`（值可为 None/空串）。
        args: 对外工具的调用参数——只读取 `allowDestructive` 用于审计
            `client_declared`，判定本身与客户端声明无关。

    Returns:
        `{"blocked": bool, "required_confirm": bool, "reason": str, "audit": dict}`
        - `required_confirm`：判定命中（与客户端声明无关）；
        - `blocked`：最终是否应拒绝 = 判定命中 且 客户端未声明
          `allowDestructive`；
        - `reason`：拒绝时直接作为 ToolError 文案（带上具体子操作）。
    """
    if kind == "mcp":
        required, rule_hit, reason = _decide_mcp(tool, sub_op)
        sub_op_label = sub_op.get("tool_name") or ""
        audit_action = "call_backend_tool"
    elif kind == "launcher":
        required, rule_hit, reason = _decide_launcher(tool, sub_op)
        sub_op_label = (
            sub_op.get("action_id")
            or sub_op.get("action_name")
            or ""
        )
        audit_action = "run_launcher_action"
    else:
        required, rule_hit = False, RULE_UNKNOWN_KIND
        reason = f"未知 kind '{kind}'，放行"
        sub_op_label = ""
        audit_action = kind

    client_declared = bool(args.get("allowDestructive"))
    blocked = required and not client_declared
    if not required:
        decision = "allow"
    elif client_declared:
        decision = "confirm"
    else:
        decision = "block"

    audit = {
        "event": "destructive_gate",
        "ts": _utc_now_iso(),
        "action": audit_action,
        "tool": (tool or {}).get("id") or "",
        "kind": kind,
        "sub_op": sub_op_label,
        "decision": decision,
        "rule_hit": rule_hit,
        "client_declared": client_declared,
    }
    _emit_audit(audit)

    return {
        "blocked": blocked,
        "required_confirm": required,
        "reason": reason,
        "audit": audit,
    }
