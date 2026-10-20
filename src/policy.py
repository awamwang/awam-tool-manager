"""破坏性门禁判定与部署策略（ARC-P1-05 Phase 1 方案 A，Phase 2 方案 C）。

唯一判定入口 `decide_destructive(kind, tool, sub_op, args)`——执行类
handler（`call_backend_tool` / `run_launcher_action` / `launch_tool` 的
execute 路径 / `install_package` / `launch_package`）只调它，不再各自维护
判定逻辑。Phase 2 起策略
（`data/policy.json`）参与判定：

- `check_call(name)`：`dispatch_tool` 前置的工具级策略（deny 名单优先于
  allow 白名单）；命中即拒绝（permission_denied），完全不进 handler；
- `destructive.mode`：`deny` = 命中破坏性判定直接拒绝，**不读客户端
  `allowDestructive`**（策略赢，H1 修复）；`require_confirm` = 现行为
  （默认）；`allow` = 部署者声明信任，跳过确认；
- 审计：每次破坏性判定产出结构化行（stderr + `<runtime>/audit.log`，
  append-only、不轮转），字段 ts/tool/kind/sub_op/decision/rule_hit/
  client_declared/policy_version/policy_mode。

判定表（默认全部保持接入前行为，未配置新字段时逐字等价）：

- mcp：条目未标 `destructive` → 放行；有 `destructiveTools` → 子工具精确
  匹配才拦；无清单 → 整后端拦。
- launcher：条目未标 `destructive` → 放行；有 `destructiveActions`（新字段）
  → 按动作 `id`/`name`/`type` 做大小写不敏感 glob（fnmatch）匹配，命中才拦；
  无该字段 → 回退条目级全拦（现语义，不回退放宽）。
- app：条目未标 `destructive` → 放行；标了 → 执行启动需
  `allowDestructive=true`（启动即子操作，无更细粒度可言）。
- package（P2-07 包管理面）：**没有条目可标**，破坏性由操作本身定且 fail-closed——
  不在 `PACKAGE_CONFIRMED_OPS`（启动类：`launch_package` / `open_app`）里的包
  操作都要 `allowDestructive=true`；白名单内放行但仍留审计行。
- path（open_path 路径面）：只有打开可执行文件/脚本才算破坏性；`reveal=true`
  只定位不执行，永远放行。后缀判定不读文件系统，handler 已先确认路径存在。

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
import os
import sys
from pathlib import Path

from runtime_paths import get_runtime_dir
from schema import validate_policy

# 部署策略（ARC-P1-05 Phase 2，方案 C）：仓库 `data/policy.json` 为默认
# （随部署分发、可评审），`AWAM_POLICY_FILE` 环境变量可覆盖（机私策略）。
POLICY_ENV_VAR = "AWAM_POLICY_FILE"
DEFAULT_POLICY_PATH = Path(__file__).parent.parent / "data" / "policy.json"

# 无策略文件时的语义 = 全部现状（零配置零变更）：
# 不做 deny/白名单、破坏性走 require_confirm（需客户端显式确认）、审计落盘开。
DEFAULT_POLICY: dict = {
    "version": 1,
    "tools": {"deny": [], "allow": None},
    "destructive": {"mode": "require_confirm"},
    "audit": {"destructive": True},
}

# 破坏性判定审计的落盘位置（append-only、不轮转；Phase 1 起另有 stderr 行）
AUDIT_LOG = get_runtime_dir() / "audit.log"


class PolicyError(RuntimeError):
    """策略文件不可用（读不了 / JSON 坏了 / schema 校验失败）。

    `load_policy()` 在 import 期调用并让异常上抛——坏策略不能带病运行
    （照 P1-04 `validate_specs()` 的快速失败先例）：策略判错等于安全约束
    静默失效，早炸远好过晚炸。
    """


def resolve_policy_path() -> Path:
    """策略文件路径：`AWAM_POLICY_FILE` 覆盖优先，否则仓库默认文件"""
    override = os.environ.get(POLICY_ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_POLICY_PATH


def load_policy(path: str | Path | None = None) -> dict:
    """加载并校验策略。文件不存在 → 返回默认策略（零配置零变更）。

    Raises:
        PolicyError: 文件不可读 / JSON 解析失败 / schema 校验失败。
    """
    policy_path = Path(path) if path is not None else resolve_policy_path()
    if not policy_path.exists():
        # 深拷贝默认值：调用方改返回值不得污染模块级默认
        return json.loads(json.dumps(DEFAULT_POLICY))
    try:
        with open(policy_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(
            f"策略文件不可读或不是合法 JSON（{policy_path}）: {exc}"
        ) from exc
    error = validate_policy(data)
    if error:
        raise PolicyError(f"策略文件校验失败（{policy_path}）: {error}")
    return data


# import 期加载：坏策略快速失败（策略判错 = 安全约束静默失效，不能带病运行）
_POLICY: dict = load_policy()


def get_policy() -> dict:
    """当前生效的策略（测试可用 monkeypatch 替换模块级 `_POLICY`）"""
    return _POLICY

# 判定命中时的规则标识（审计 rule_hit 字段的合法取值）。
RULE_NOT_DESTRUCTIVE = "not_destructive"
RULE_ENTRY_LEVEL = "entry_level"
RULE_DESTRUCTIVE_TOOLS_MATCH = "destructive_tools_match"
RULE_DESTRUCTIVE_TOOLS_MISS = "destructive_tools_miss"
RULE_DESTRUCTIVE_ACTIONS_MATCH = "destructive_actions_match"
RULE_DESTRUCTIVE_ACTIONS_MISS = "destructive_actions_miss"
RULE_PACKAGE_DESTRUCTIVE = "package_destructive"
RULE_PATH_EXECUTABLE = "path_executable"
RULE_UNKNOWN_KIND = "unknown_kind"

# 包管理里「不改动已装清单」的对外工具名白名单，其余一律按破坏性处理。
# 方向是刻意反着写的：新增 `uninstall_package` / `update_package` 时**默认就要
# 授权**（fail-closed），忘了登记只会多一次确认；反过来维护「破坏性名单」
# 才是危险的那一侧——漏一个就静默免检。
# `launch_package` / `open_app` 在放行名单内：起的是用户早已装好的程序，与
# `launch_tool` 同一等级，门禁是「必须显式传 execute=true」，不是 allowDestructive。
PACKAGE_CONFIRMED_OPS = frozenset({"launch_package", "open_app"})

# 会被系统当作可执行代码打开的后缀。open_path 的目标是这些时，一次「打开」
# 等价于一次启动，必须走 allowDestructive 确认（.lnk 可指向任意目标，同样算）。
_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".exe",
        ".com",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".msi",
        ".msix",
        ".msixbundle",
        ".appx",
        ".appxbundle",
        ".scr",
        ".pif",
        ".lnk",
    }
)


def _utc_now_iso() -> str:
    """UTC ISO 时间戳（与 data/ 及审计日志同风格，Z 结尾）。

    不 import backend_client 的同名函数：policy 只依赖 runtime_paths /
    schema 这类轻模块，不被 backend 的 mcp 依赖拖累。
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


def _decide_mcp(
    tool: dict | None, sub_op: dict
) -> tuple[bool, str, str, str]:
    """mcp 判定表。返回 (是否命中, rule_hit, 主体描述, 指令尾)。

    `主体描述 + 指令尾` 拼起来即默认（require_confirm / allow）路径的拒绝
    文案——逐字保持 Phase 1 原文。策略 deny 模式只换指令尾（管理员话术，
    不提 `allowDestructive`——那正是 deny 模式要忽略的输入，提了就是
    「可执行但无效」的建议）。
    """
    tool_name = sub_op.get("tool_name") or ""
    tool_id = (tool or {}).get("id") or ""
    if not tool or not tool.get("destructive"):
        return False, RULE_NOT_DESTRUCTIVE, "目标工具未标记 destructive，放行", ""
    listed = tool.get("destructiveTools")
    subject = (
        f"目标工具 '{tool_id}' 的子工具 '{tool_name}' 被判定为 destructive"
    )
    tail = "，需显式传 allowDestructive=true 才会转发调用"
    if listed is None:
        # 旧语义：条目标了 destructive 又没给子工具清单 → 整个后端都拦
        return True, RULE_ENTRY_LEVEL, subject, tail
    if tool_name in listed:
        return True, RULE_DESTRUCTIVE_TOOLS_MATCH, subject, tail
    return (
        False,
        RULE_DESTRUCTIVE_TOOLS_MISS,
        f"子工具 '{tool_name}' 不在 destructiveTools 清单内，放行",
        "",
    )


def _decide_launcher(
    tool: dict | None, sub_op: dict
) -> tuple[bool, str, str, str]:
    """launcher 判定表。返回 (是否命中, rule_hit, 主体描述, 指令尾)。

    两段拼接即默认路径拒绝文案（逐字保持 Phase 1 原文）；注意两个分支的
    指令尾前导逗号不同——entry_level 的原文在「动作 'X'」后直接接「需…」，
    match 分支则带「，」——拼接时保持原样，别统一。
    """
    action_id = sub_op.get("action_id") or ""
    action_name = sub_op.get("action_name") or ""
    action_type = sub_op.get("action_type") or ""
    label = action_id or action_name or "未知动作"
    tool_id = (tool or {}).get("id") or ""
    if not tool or not tool.get("destructive"):
        return False, RULE_NOT_DESTRUCTIVE, "启动器未标记 destructive，放行", ""
    patterns = tool.get("destructiveActions")
    if patterns is None:
        # 旧语义：只标了条目级 destructive、没有动作清单 → 所有动作都要确认
        # 注意指令尾的前导空格：「动作 'X' 需显式传…」——原文如此，逐字保持
        return (
            True,
            RULE_ENTRY_LEVEL,
            f"启动器 '{tool_id}' 被标记为 destructive（未配置 destructiveActions），"
            f"动作 '{label}'",
            " 需显式传 allowDestructive=true 才会执行",
        )
    hit = _match_any(patterns, (action_id, action_name, action_type))
    if hit is not None:
        return (
            True,
            RULE_DESTRUCTIVE_ACTIONS_MATCH,
            f"启动器 '{tool_id}' 的动作 '{label}' 命中 destructiveActions"
            f"（匹配值: '{hit}'）",
            "，需显式传 allowDestructive=true 才会执行",
        )
    return (
        False,
        RULE_DESTRUCTIVE_ACTIONS_MISS,
        f"启动器 '{tool_id}' 的动作 '{label}' 未命中 destructiveActions，放行",
        "",
    )


def _decide_package(
    tool: dict | None, sub_op: dict
) -> tuple[bool, str, str, str]:
    """包管理执行面判定表（P2-07）。返回四元组同其他 `_decide_*`。

    与 app / mcp / launcher 的**根本差别**：那三类判的是「这个条目有没有被人工标成
    destructive」，包操作没有条目可标——`install_package` 会把「改这台机器」的授权点
    从条目配置搬进一次运行时调用，配置里看不到它。所以破坏性由**操作本身**定，
    且方向是 fail-closed：只有一张「不需要授权」白名单（`PACKAGE_CONFIRMED_OPS`），
    名单外一律要 `allowDestructive=true`；将来新增卸载 / 更新工具时忘了登记，
    代价是多一次确认，而不是静默免检。

    - `launch_package`（名单内）：起的是**用户早就装好**的程序，与 `launch_tool`
      同一等级 → 放行，但仍留审计行（谁在什么时候起了哪个包，和启动登记应用一样该有记录）；
    - 其余（`install_package` 及一切未登记的操作）：下载并执行 bucket 里的安装脚本、
      写 apps 目录、生成 shim → 需要 `allowDestructive=true`（策略 `deny` 模式下直接拒）。

    `tool` 只用于把审计的 `tool` 字段填成 `scoop:<包名>` 这类可定位的标识，
    本判定表不读它的 `destructive`——包名不是登记条目，标不标由不得它。
    """
    action = str(sub_op.get("action") or "")
    package = str(sub_op.get("package") or "")
    if action in PACKAGE_CONFIRMED_OPS:
        return (
            False,
            RULE_NOT_DESTRUCTIVE,
            f"包管理操作 '{action}' 不改动已装清单，放行",
            "",
        )
    return (
        True,
        RULE_PACKAGE_DESTRUCTIVE,
        f"包管理操作 '{action}' 会改动本机已装清单（包 '{package}'）",
        "，需显式传 allowDestructive=true 才会执行",
    )


def _decide_path(
    sub_op: dict
) -> tuple[bool, str, str, str]:
    """open_path 判定表。返回四元组同其他 `_decide_*`。

    `reveal=true` 只让资源管理器选中路径、不执行目标，因此无论后缀都放行。
    其余情况下，可执行文件/脚本的一次「打开」等价于一次启动，按 fail-closed
    要求 `allowDestructive=true`；普通文档/文件夹/图片等照常放行。
    后缀判定不碰文件系统：handler 在调用前已确认路径存在，且 `Path.suffix`
    对缺失文件同样稳定（策略层保持纯函数，便于单测与审计复现）。
    """
    path = str(sub_op.get("path") or "")
    if sub_op.get("reveal"):
        return (
            False,
            RULE_NOT_DESTRUCTIVE,
            f"路径 '{path}' 只做定位（reveal=true），不执行目标",
            "",
        )
    suffix = Path(path).suffix.lower()
    if suffix in _EXECUTABLE_SUFFIXES:
        return (
            True,
            RULE_PATH_EXECUTABLE,
            f"路径 '{path}' 是可执行文件/脚本（{suffix}）",
            "，需显式传 allowDestructive=true 才会打开",
        )
    return (
        False,
        RULE_NOT_DESTRUCTIVE,
        f"路径 '{path}' 不是可执行文件/脚本，放行",
        "",
    )


def _decide_app(
    tool: dict | None, sub_op: dict
) -> tuple[bool, str, str, str]:
    """app 判定表（两个执行入口：`launch_tool` 执行启动 / `get_tool_help` 跑帮助
    命令）。返回四元组同 `_decide_mcp` / `_decide_launcher`。

    app 没有子工具/动作那种细分粒度：条目标了 `destructive` 就连「对它做执行
    动作」也要显式确认。`sub_op` 只用于把拒绝文案与审计标注对齐到实际入口
    （帮助命令被拒时说「才会执行启动」会误导调用方）。
    """
    tool_id = (tool or {}).get("id") or ""
    if not tool or not tool.get("destructive"):
        return False, RULE_NOT_DESTRUCTIVE, "目标应用未标记 destructive，放行", ""
    verb = "执行帮助命令" if sub_op.get("action") == "get_tool_help" else "执行启动"
    return (
        True,
        RULE_ENTRY_LEVEL,
        f"目标应用 '{tool_id}' 被标记为 destructive",
        f"，需显式传 allowDestructive=true 才会{verb}",
    )


def _emit_audit(audit: dict) -> None:
    """审计行：stderr 恒输出；audit.log 追加受策略 `audit.destructive` 控制。

    Phase 1（Q4 裁决）：stderr 一行 JSON；Phase 2 起按策略追加落盘
    `<runtime>/audit.log`（append-only、不轮转——首版刻意不做轮转，
    避免过度工程）。落盘失败只吞异常：审计是旁路，不能拖垮调用。
    """
    line = json.dumps(audit, ensure_ascii=False)
    print(line, file=sys.stderr, flush=True)
    policy = get_policy()
    if not (policy.get("audit") or {}).get("destructive", True):
        return
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _policy_denial(tool_name: str, rule_hit: str, reason: str) -> dict:
    """策略拒绝的 Decision（error_type=permission_denied）"""
    policy = get_policy()
    audit = {
        "event": "policy_check",
        "ts": _utc_now_iso(),
        "action": "dispatch",
        "tool": tool_name,
        "kind": "",
        "sub_op": "",
        "decision": "block",
        "rule_hit": rule_hit,
        "client_declared": False,
        "policy_version": policy.get("version"),
        "policy_mode": (policy.get("destructive") or {}).get("mode")
        or "require_confirm",
    }
    _emit_audit(audit)
    return {
        "blocked": True,
        "required_confirm": False,
        "error_type": "permission_denied",
        "reason": reason,
        "audit": audit,
    }


def check_call(tool_name: str) -> dict | None:
    """`dispatch_tool` 前置的工具级策略检查：deny 名单 / allow 白名单。

    命中返回拒绝 Decision（`error_type=permission_denied`，客户端无法自行
    解除，需管理员调整策略）；放行返回 None。放行不产出审计行——每次调用
    都记会把 audit.log 变成调用流水，冲掉真正有价值的判定记录；拒绝必留痕。

    deny 优先于 allow；`tools.allow` 为 null 表示不启用白名单（默认）。
    """
    policy = get_policy()
    tools_cfg = policy.get("tools") or {}
    if tool_name in (tools_cfg.get("deny") or []):
        return _policy_denial(
            tool_name,
            "deny_list",
            f"工具 '{tool_name}' 在部署策略 deny 名单内（policy.json），"
            f"客户端无法自行解除，需管理员调整策略",
        )
    allow = tools_cfg.get("allow")
    if allow is not None and tool_name not in allow:
        return _policy_denial(
            tool_name,
            "allow_list_miss",
            f"工具 '{tool_name}' 不在部署策略白名单内（policy.json tools.allow），"
            f"需管理员把该工具加入白名单",
        )
    return None


def decide_destructive(
    kind: str, tool: dict | None, sub_op: dict, args: dict
) -> dict:
    """破坏性门禁的唯一判定入口。

    Args:
        kind: `"mcp"` / `"launcher"` / `"app"` / `"package"` / `"path"`（其他值
            放行——新增 kind 必须显式接入判定表，默认不拦以免拖垮未知类型）。
        tool: 工具条目（`registry.get_tool()` 的返回）；None 表示工具不存在。
        sub_op: 子操作标识。mcp → `{"tool_name": str}`；launcher →
            `{"action_id", "action_name", "action_type"}`；app → `{}` 或
            `{"action": str}`（执行入口名，只用于审计标注）；path →
            `{"path": str, "reveal": bool}`。值可为 None/空串。
        args: 对外工具的调用参数——只读取 `allowDestructive` 用于审计
            `client_declared`，判定本身与客户端声明无关。

    Returns:
        `{"blocked": bool, "required_confirm": bool, "error_type": str,
        "reason": str, "audit": dict}`
        - `required_confirm`：判定命中（与客户端声明无关）；
        - `blocked`：最终是否应拒绝 = 判定命中 且（策略 deny 模式，或客户端
          未声明 `allowDestructive`）；
        - `error_type`：拒绝时给客户端的分类——默认 `destructive_blocked`
          （客户端传 `allowDestructive=true` 可自解），策略 deny 模式下为
          `permission_denied`（客户端无解，需改策略）；
        - `reason`：拒绝时直接作为 ToolError 文案（带上具体子操作）。
    """
    if kind == "mcp":
        required, rule_hit, subject, tail = _decide_mcp(tool, sub_op)
        sub_op_label = sub_op.get("tool_name") or ""
        audit_action = "call_backend_tool"
    elif kind == "launcher":
        required, rule_hit, subject, tail = _decide_launcher(tool, sub_op)
        sub_op_label = (
            sub_op.get("action_id")
            or sub_op.get("action_name")
            or ""
        )
        audit_action = "run_launcher_action"
    elif kind == "app":
        required, rule_hit, subject, tail = _decide_app(tool, sub_op)
        sub_op_label = ""
        # app 种有两个执行入口（launch_tool 执行启动 / get_tool_help 跑帮助命令），
        # 审计里的 action 跟着实际入口走，否则 help 的判定被记成启动
        audit_action = str(sub_op.get("action") or "launch_tool")
    elif kind == "package":
        required, rule_hit, subject, tail = _decide_package(tool, sub_op)
        sub_op_label = str(sub_op.get("package") or "")
        audit_action = str(sub_op.get("action") or "")
    elif kind == "path":
        required, rule_hit, subject, tail = _decide_path(sub_op)
        sub_op_label = str(sub_op.get("path") or "")
        audit_action = "open_path"
    else:
        required, rule_hit, tail = False, RULE_UNKNOWN_KIND, ""
        subject = f"未知 kind '{kind}'，放行"
        sub_op_label = ""
        audit_action = kind

    policy = get_policy()
    mode = (policy.get("destructive") or {}).get("mode") or "require_confirm"
    client_declared = bool(args.get("allowDestructive"))

    reason = f"{subject}{tail}"

    blocked = False
    if required:
        if mode == "allow":
            # 部署者显式声明本机信任：跳过确认流程（required 一并清掉，
            # handler 不再打「已确认」日志、响应不挂 warning）
            required = False
        elif mode == "deny":
            # 策略赢（H1 修复）：命中即拒绝，不读客户端 allowDestructive。
            # 文案同样不能再建议 allowDestructive——那是本模式忽略的输入，
            # 客户端照做只会再被拒一次；改用与 check_call deny 名单一致的
            # 管理员话术（QA #24）。
            blocked = True
            reason = (
                f"部署策略 destructive.mode=deny，拒绝该破坏性操作；{subject}，"
                f"客户端无法自行解除，需管理员调整策略"
            )
        elif not client_declared:
            blocked = True
    decision = "block" if blocked else ("confirm" if required else "allow")
    error_type = (
        "permission_denied" if (mode == "deny" and required) else "destructive_blocked"
    )

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
        "policy_version": policy.get("version"),
        "policy_mode": mode,
    }
    _emit_audit(audit)

    return {
        "blocked": blocked,
        "required_confirm": required,
        "error_type": error_type,
        "reason": reason,
        "audit": audit,
    }
