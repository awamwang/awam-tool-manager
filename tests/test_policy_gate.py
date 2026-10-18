"""ARC-P1-05 Phase 1：破坏性门禁统一判定（src/policy.py）的守卫。

四组：

1. **判定表逐行**：mcp 四分支（未标 / 无清单 / 清单命中 / 清单未中）与
   launcher 四分支（未标 / 无 destructiveActions 回退条目级 / glob 命中 /
   glob 未中）+ 未知 kind；
2. **glob 匹配**：按动作 id/name/type 三属性、大小写不敏感、通配位置、
   空值不误命中（partial 清单里 name/type 缺失时 `*` 不能命中一切）；
3. **等价性**：未配置 `destructiveActions` 时与接入前逐字一致——照 P1-04
   golden 思路，用**冻结的旧判定逻辑**（下方 `_legacy_*`，逐字复制自
   接入前的 tool_specs 原地条件）作为对照，逐例比较 `required_confirm`，
   不是只断言「没报错」；判定表本身另以字面期望表（`_DECISION_TABLE`）
   作快照；
4. **审计行格式**：每次判定输出一行 JSON，`decision` / `rule_hit` /
   `client_declared` 三字段齐全且取值正确（allow / block / confirm 三态）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import policy
from policy import decide_destructive


def _decide(kind, tool, sub_op=None, args=None):
    return decide_destructive(kind, tool, sub_op or {}, args or {})


_MCP_DESTRUCTIVE = {"id": "boom", "kind": "mcp", "destructive": True}
_MCP_LISTED = {**_MCP_DESTRUCTIVE, "destructiveTools": ["nuke"]}
_LAUNCHER_DESTRUCTIVE = {"id": "quicker", "kind": "launcher", "destructive": True}
_APP_DESTRUCTIVE = {"id": "boom-app", "kind": "app", "destructive": True}


# ---- 1. 判定表逐行 ----


# (kind, tool, sub_op, 期望 required_confirm, 期望 rule_hit)
# 无 allowDestructive 时 blocked == required_confirm，故不断言 blocked 列。
_DECISION_TABLE = [
    # mcp：未标 destructive → 放行（含工具不存在 / 字段缺失 / false）
    ("mcp", None, {"tool_name": "nuke"}, False, "not_destructive"),
    ("mcp", {"id": "m", "kind": "mcp"}, {"tool_name": "nuke"}, False, "not_destructive"),
    ("mcp", {"id": "m", "destructive": False}, {"tool_name": "nuke"}, False, "not_destructive"),
    # mcp：标了 destructive 且无清单 → 整后端拦（旧语义，不回退）
    ("mcp", _MCP_DESTRUCTIVE, {"tool_name": "nuke"}, True, "entry_level"),
    ("mcp", _MCP_DESTRUCTIVE, {"tool_name": "query"}, True, "entry_level"),
    # mcp：清单内精确命中 → 拦；清单外 → 放行
    ("mcp", _MCP_LISTED, {"tool_name": "nuke"}, True, "destructive_tools_match"),
    ("mcp", _MCP_LISTED, {"tool_name": "query"}, False, "destructive_tools_miss"),
    ("mcp", _MCP_LISTED, {"tool_name": ""}, False, "destructive_tools_miss"),
    # launcher：未标 destructive → 放行
    ("launcher", None, {"action_id": "a"}, False, "not_destructive"),
    ("launcher", {"id": "q", "destructive": False}, {"action_id": "a"}, False, "not_destructive"),
    # launcher：无 destructiveActions → 回退条目级全拦（现语义，不回退）
    ("launcher", _LAUNCHER_DESTRUCTIVE, {"action_id": "a"}, True, "entry_level"),
    ("launcher", _LAUNCHER_DESTRUCTIVE, {"action_name": "任意动作"}, True, "entry_level"),
    # launcher：glob 命中 → 拦；未命中 / 空清单 → 放行
    (
        "launcher",
        {**_LAUNCHER_DESTRUCTIVE, "destructiveActions": ["*卸载*"]},
        {"action_id": "a", "action_name": "卸载微信"},
        True,
        "destructive_actions_match",
    ),
    (
        "launcher",
        {**_LAUNCHER_DESTRUCTIVE, "destructiveActions": ["*卸载*"]},
        {"action_id": "a", "action_name": "打开浏览器"},
        False,
        "destructive_actions_miss",
    ),
    (
        "launcher",
        {**_LAUNCHER_DESTRUCTIVE, "destructiveActions": []},
        {"action_id": "a"},
        False,
        "destructive_actions_miss",
    ),
    # app（launch_tool 的 execute 路径）：未标 destructive → 放行
    ("app", None, {}, False, "not_destructive"),
    ("app", {"id": "a", "kind": "app"}, {}, False, "not_destructive"),
    ("app", {"id": "a", "destructive": False}, {}, False, "not_destructive"),
    # app：标了 destructive → 执行启动需 allowDestructive（entry_level）
    ("app", _APP_DESTRUCTIVE, {}, True, "entry_level"),
    # 未知 kind：默认放行（新增 kind 必须显式接入判定表）
    ("unknown-kind", {"id": "x", "destructive": True}, {}, False, "unknown_kind"),
]


def test_decision_table_rows():
    """判定表逐行：required_confirm 与 rule_hit 与字面快照一致"""
    for kind, tool, sub_op, want_required, want_rule in _DECISION_TABLE:
        decision = _decide(kind, tool, sub_op)
        assert decision["required_confirm"] is want_required, (kind, tool, sub_op)
        assert decision["blocked"] is want_required, "未声明 allowDestructive 时应拒绝"
        assert decision["audit"]["rule_hit"] == want_rule, (kind, tool, sub_op)


def test_default_mode_rejection_messages_are_verbatim():
    """默认（require_confirm）路径的四类拒绝文案逐字冻结。

    Phase 2 把拒绝文案拆成「主体 + 指令尾」以便 deny 模式换管理员话术，
    但默认路径必须与 Phase 1 逐字相同——这里用字面字符串钉死（QA 已验证
    mcp 侧 64/64 逐字相同，改这两段时别破坏默认路径）。
    """
    cases = [
        (
            "mcp",
            _MCP_DESTRUCTIVE,
            {"tool_name": "nuke"},
            "目标工具 'boom' 的子工具 'nuke' 被判定为 destructive，"
            "需显式传 allowDestructive=true 才会转发调用",
        ),
        (
            "mcp",
            _MCP_LISTED,
            {"tool_name": "nuke"},
            "目标工具 'boom' 的子工具 'nuke' 被判定为 destructive，"
            "需显式传 allowDestructive=true 才会转发调用",
        ),
        (
            "launcher",
            _LAUNCHER_DESTRUCTIVE,
            {"action_id": "a"},
            "启动器 'quicker' 被标记为 destructive（未配置 destructiveActions），"
            "动作 'a' 需显式传 allowDestructive=true 才会执行",
        ),
        (
            "launcher",
            {**_LAUNCHER_DESTRUCTIVE, "destructiveActions": ["*delete*"]},
            {"action_id": "delete-all"},
            "启动器 'quicker' 的动作 'delete-all' 命中 destructiveActions"
            "（匹配值: 'delete-all'），需显式传 allowDestructive=true 才会执行",
        ),
        (
            "app",
            _APP_DESTRUCTIVE,
            {},
            "目标应用 'boom-app' 被标记为 destructive，"
            "需显式传 allowDestructive=true 才会执行启动",
        ),
    ]
    for kind, tool, sub_op, expected in cases:
        decision = _decide(kind, tool, sub_op)
        assert decision["blocked"] is True
        assert decision["reason"] == expected, f"{kind}: 默认路径文案被改动"


# ---- 2. glob 匹配 ----


def test_launcher_glob_matches_id_name_and_type_case_insensitively():
    """id / name / type 三属性任一命中即拦；匹配大小写不敏感"""
    tool = {
        **_LAUNCHER_DESTRUCTIVE,
        "destructiveActions": ["uninstall*", "*DELETE*", "xaction"],
    }
    # id 命中（大小写不敏感）
    hit_id = _decide("launcher", tool, {"action_id": "UninstallTool"})
    assert hit_id["required_confirm"] is True
    # name 命中
    hit_name = _decide("launcher", tool, {"action_id": "x", "action_name": "Delete files"})
    assert hit_name["required_confirm"] is True
    # type 命中（type 由 handler 从动作清单解析后传入）
    hit_type = _decide("launcher", tool, {"action_id": "x", "action_type": "XAction"})
    assert hit_type["required_confirm"] is True
    # 三属性均未命中
    miss = _decide(
        "launcher",
        tool,
        {"action_id": "open", "action_name": "打开浏览器", "action_type": "Run"},
    )
    assert miss["required_confirm"] is False


def test_launcher_glob_does_not_match_empty_values():
    """空值不参与匹配：partial 清单里 name/type 缺失时，`*` 不能命中一切"""
    loose = {**_LAUNCHER_DESTRUCTIVE, "destructiveActions": ["*"]}
    decision = _decide(
        "launcher",
        loose,
        {"action_id": "", "action_name": "", "action_type": ""},
    )
    assert decision["required_confirm"] is False, "全空时不应误拦（宁可漏拦不误放）"


# ---- 3. 等价性：未配置 destructiveActions 时与接入前逐字一致 ----


def _legacy_needs_confirm(tool, sub_tool):
    """冻结的旧判定逻辑（逐字复刻接入前 tool_specs._needs_destructive_confirm）"""
    if not tool or not tool.get("destructive"):
        return False
    listed = tool.get("destructiveTools")
    if listed is None:
        return True
    return sub_tool in listed


def _legacy_launcher_needs_confirm(tool):
    """冻结的旧启动器判定（逐字复刻接入前 _handle_run_launcher_action 原地条件）"""
    return bool(tool and tool.get("destructive"))


def test_equivalence_with_legacy_logic_when_new_field_absent():
    """无 destructiveActions / 无 destructiveTools 时判定与旧逻辑逐例一致"""
    mcp_tools = [
        None,
        {},
        {"id": "x"},
        {"id": "x", "destructive": False},
        {"id": "x", "destructive": True},
        {"id": "x", "destructive": True, "destructiveTools": []},
        {"id": "x", "destructive": True, "destructiveTools": ["nuke"]},
        {"id": "x", "destructive": True, "destructiveTools": ["nuke", "rm"]},
    ]
    for tool in mcp_tools:
        for sub_tool in ("", "nuke", "query", "rm"):
            got = _decide("mcp", tool, {"tool_name": sub_tool})
            assert got["required_confirm"] == _legacy_needs_confirm(tool, sub_tool), (
                f"mcp 等价性破坏: tool={tool} sub_tool={sub_tool}"
            )

    launcher_tools = [
        None,
        {},
        {"id": "q"},
        {"id": "q", "destructive": False},
        {"id": "q", "destructive": True},
    ]
    for tool in launcher_tools:
        for sub_op in ({"action_id": "a"}, {"action_name": "b"}, {}):
            got = _decide("launcher", tool, sub_op)
            assert got["required_confirm"] == _legacy_launcher_needs_confirm(tool), (
                f"launcher 等价性破坏: tool={tool} sub_op={sub_op}"
            )


# ---- 4. 审计行格式 ----


def _audit_lines(capsys):
    """从 stderr 捞本测试期内的全部结构化审计行"""
    err = capsys.readouterr().err
    return [
        json.loads(line)
        for line in err.splitlines()
        if '"destructive_gate"' in line
    ]


def test_audit_line_fields_and_three_states(capsys):
    """审计行三字段（decision / rule_hit / client_declared）+ allow/block/confirm 三态"""
    tool = {**_LAUNCHER_DESTRUCTIVE, "destructiveActions": ["*delete*"]}

    # 未命中 → allow
    _decide("launcher", tool, {"action_id": "open"})
    # 命中且未声明 → block
    _decide("launcher", tool, {"action_id": "delete-all"})
    # 命中且声明 → confirm
    _decide("launcher", tool, {"action_id": "delete-all"}, {"allowDestructive": True})

    lines = _audit_lines(capsys)
    assert len(lines) == 3, f"应每次判定一行审计，实际 {len(lines)} 行"
    for line in lines:
        assert set(line) >= {
            "ts", "tool", "kind", "sub_op",
            "decision", "rule_hit", "client_declared",
        }, "审计行缺字段"
        assert line["kind"] == "launcher"
        assert line["tool"] == "quicker"

    assert lines[0]["decision"] == "allow"
    assert lines[0]["client_declared"] is False
    assert lines[1]["decision"] == "block"
    assert lines[1]["rule_hit"] == "destructive_actions_match"
    assert lines[1]["sub_op"] == "delete-all"
    assert lines[2]["decision"] == "confirm"
    assert lines[2]["client_declared"] is True


# ---- 5. app 种（launch_tool 的 execute 路径）----


def test_app_gate_responds_to_allow_destructive(monkeypatch):
    """app 种：allowDestructive=true 解封；策略 deny 模式赢过客户端声明"""
    monkeypatch.setattr(policy, "_POLICY", policy.get_policy())

    # 客户端显式确认 → 放行（confirm 态，与 mcp/launcher 同构）
    confirmed = _decide("app", _APP_DESTRUCTIVE, {}, {"allowDestructive": True})
    assert confirmed["blocked"] is False
    assert confirmed["required_confirm"] is True
    assert confirmed["audit"]["client_declared"] is True

    # 策略 deny → 命中即拒，且不读客户端声明（H1 修复的同款语义）
    denied_policy = {
        **policy.get_policy(),
        "destructive": {"mode": "deny"},
    }
    monkeypatch.setattr(policy, "_POLICY", denied_policy)
    denied = _decide("app", _APP_DESTRUCTIVE, {}, {"allowDestructive": True})
    assert denied["blocked"] is True
    assert denied["error_type"] == "permission_denied"
    assert "客户端无法自行解除" in denied["reason"]


def test_app_gate_audit_line(capsys):
    """app 种的审计行：action 记 launch_tool、kind 记 app、三态正确"""
    _decide("app", _APP_DESTRUCTIVE, {})
    _decide("app", _APP_DESTRUCTIVE, {}, {"allowDestructive": True})

    lines = _audit_lines(capsys)
    assert len(lines) == 2, f"应每次判定一行审计，实际 {len(lines)} 行"
    for line in lines:
        assert line["kind"] == "app"
        assert line["tool"] == "boom-app"
        assert line["action"] == "launch_tool"
        assert line["sub_op"] == "", "启动无子操作，sub_op 应为空"
    assert lines[0]["decision"] == "block"
    assert lines[1]["decision"] == "confirm"


def test_app_gate_audit_action_follows_execution_entry(capsys):
    """app 种的两个执行入口各自标注：help 的判定不能记成 launch_tool"""
    decision = _decide("app", _APP_DESTRUCTIVE, {"action": "get_tool_help"})

    lines = _audit_lines(capsys)
    assert len(lines) == 1, f"应每次判定一行审计，实际 {len(lines)} 行"
    assert lines[0]["action"] == "get_tool_help"
    assert lines[0]["decision"] == "block"
    # 拒绝文案也要跟着入口走：help 被拦时说「才会执行启动」会误导调用方
    assert decision["reason"].endswith("需显式传 allowDestructive=true 才会执行帮助命令")
