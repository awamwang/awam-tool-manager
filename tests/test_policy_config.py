"""部署策略（`data/policy.json`，ARC-P1-05 Phase 2 方案 C）的守卫。

覆盖：

1. **装载语义**：随仓库的默认策略文件合法；无文件时 `load_policy` 返回
   默认策略（零配置零变更）；坏 JSON / 坏模式抛 `PolicyError`；
2. **导入期快速失败**：`AWAM_POLICY_FILE` 指向坏策略时，子进程
   `import policy` 非零退出（坏策略不能带病运行）；
3. **deny / 白名单**：deny 名单拒绝 dispatch（`permission_denied`——同时是
   errorType 覆盖台账的触发用例）；deny 优先于 allow；白名单未收录即拒；
4. **H1 验收**：`destructive.mode=deny` 时，客户端 `allowDestructive=true`
   也不能放行（策略赢过客户端声明）；
5. `mode=allow` 跳过确认；默认 `require_confirm` 保持 Phase 1 行为；
6. 审计落盘：判定行追加到 `audit.log`（一行 JSON）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import policy  # noqa: E402
import server  # noqa: E402
from schema import validate_policy  # noqa: E402


def _set_policy(monkeypatch, **overrides):
    """把生效策略替换成「默认 + overrides」的组合（深拷贝默认值起步）"""
    current = json.loads(json.dumps(policy.DEFAULT_POLICY))
    for key, value in overrides.items():
        current[key] = value
    monkeypatch.setattr(policy, "_POLICY", current)
    return current


_DESTRUCTIVE_ENTRY = {"id": "boom", "kind": "mcp", "destructive": True}


def server_run(name, arguments):
    """dispatch_tool 的同步调用壳（避免每处 asyncio.run 样板）"""
    import asyncio  # noqa: PLC0415

    return asyncio.run(server.dispatch_tool(name, arguments))


# ---- 1. 装载语义 ----


def test_default_policy_file_is_valid():
    """随仓库的 data/policy.json 必须通过 schema 校验（默认 = 零变更）"""
    data = policy.load_policy()
    assert validate_policy(data) is None
    assert (data.get("destructive") or {}).get("mode") == "require_confirm"
    assert not (data.get("tools") or {}).get("deny")
    assert (data.get("tools") or {}).get("allow") is None


def test_load_policy_missing_file_returns_default(tmp_path):
    """无策略文件 → 默认策略（零配置零变更），且返回的是深拷贝"""
    data = policy.load_policy(tmp_path / "no_such_policy.json")
    assert data == policy.DEFAULT_POLICY
    data["destructive"]["mode"] = "deny"  # 改返回值
    assert policy.DEFAULT_POLICY["destructive"]["mode"] == "require_confirm", (
        "返回默认策略时必须是深拷贝，不能污染模块级默认"
    )


def test_load_policy_rejects_bad_mode(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text(
        json.dumps({"version": 1, "destructive": {"mode": "nope"}}), encoding="utf-8"
    )
    with pytest.raises(policy.PolicyError, match="校验失败"):
        policy.load_policy(bad)


def test_load_policy_rejects_broken_json(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text("{ 半个 json", encoding="utf-8")
    with pytest.raises(policy.PolicyError, match="合法 JSON"):
        policy.load_policy(bad)


def test_load_policy_rejects_unknown_fields(tmp_path):
    """策略里拼错字段等于安全约束静默失效——未知字段必须拒绝"""
    bad = tmp_path / "policy.json"
    bad.write_text(
        json.dumps({"version": 1, "tools": {"denied": ["stop_backend"]}}),
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyError, match="校验失败"):
        policy.load_policy(bad)


# ---- 2. 导入期快速失败 ----


def test_bad_policy_fails_at_import(tmp_path):
    """坏策略让 `import policy` 直接失败（照 P1-04 validate_specs 先例）"""
    bad = tmp_path / "policy.json"
    bad.write_text(
        json.dumps({"version": 1, "destructive": {"mode": "nope"}}), encoding="utf-8"
    )
    env = {**os.environ, "AWAM_POLICY_FILE": str(bad)}
    proc = subprocess.run(
        [sys.executable, "-c", "import policy"],
        cwd=str(SRC_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, "坏策略必须让 import 失败"
    assert "PolicyError" in proc.stderr or "策略文件校验失败" in proc.stderr


# ---- 3. deny / 白名单 ----


def test_deny_list_blocks_dispatch_call(monkeypatch):
    """deny 名单命中 → permission_denied（errorType 台账的触发用例）"""
    _set_policy(monkeypatch, tools={"deny": ["stop_backend"], "allow": None})

    with pytest.raises(server.ToolError) as exc:
        server_run("stop_backend", {"tool_id": "mcp1"})

    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "permission_denied"
    assert "deny" in payload["error"]
    assert payload["toolName"] == "stop_backend"


def test_deny_precedence_over_allow(monkeypatch):
    """deny 优先于 allow：同时在两个名单里也拒绝"""
    _set_policy(
        monkeypatch,
        tools={"deny": ["stop_backend"], "allow": ["stop_backend", "search_tools"]},
    )
    with pytest.raises(server.ToolError) as exc:
        server_run("stop_backend", {"tool_id": "mcp1"})
    assert json.loads(str(exc.value))["errorType"] == "permission_denied"


def test_allow_list_whitelist_blocks_unlisted(monkeypatch):
    """allow 非 null → 白名单模式：未收录的工具一律拒绝，收录的放行"""
    _set_policy(monkeypatch, tools={"deny": [], "allow": ["search_tools"]})

    # 未收录 → 拒绝
    with pytest.raises(server.ToolError) as exc:
        server_run("stop_backend", {"tool_id": "mcp1"})
    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "permission_denied"
    assert "白名单" in payload["error"]

    # 收录 → 放行（search_tools 是纯读，真实 handler 可跑）
    text = server_run("search_tools", {"keyword": "quicker"})
    assert json.loads(text)["success"] is True


# ---- 4. H1 验收：策略赢过客户端声明 ----


def test_deny_mode_overrides_client_allowdestructive(monkeypatch):
    """H1：destructive.mode=deny 时，客户端 allowDestructive=true 也拒绝"""
    _set_policy(
        monkeypatch,
        destructive={"mode": "deny"},
        tools={"deny": [], "allow": None},
    )
    monkeypatch.setattr(server.registry, "get_tool", lambda tool_id: _DESTRUCTIVE_ENTRY)

    async def should_not_be_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("策略 deny 下不得转发破坏性调用")

    monkeypatch.setattr(server.backend_manager, "call_backend_tool", should_not_be_called)

    with pytest.raises(server.ToolError) as exc:
        server_run(
            "call_backend_tool",
            {
                "tool_id": "boom",
                "tool_name": "nuke",
                "allowDestructive": True,  # 客户端自声明——策略必须赢
            },
        )
    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "permission_denied", (
        "策略拒绝必须是 permission_denied（客户端无解），不是 destructive_blocked"
    )
    assert "deny" in payload["error"]
    # 文案不得再建议 allowDestructive——那是 deny 模式要忽略的输入，
    # 客户端照做只会再被拒一次（QA #24）；应给管理员话术
    assert "allowDestructive" not in payload["error"]
    assert "需管理员调整策略" in payload["error"]


def test_deny_mode_launcher_message_offers_no_self_service(monkeypatch):
    """deny 模式的 launcher 拒绝文案同样不提 allowDestructive（QA #24）"""
    _set_policy(
        monkeypatch, destructive={"mode": "deny"}, tools={"deny": [], "allow": None}
    )
    monkeypatch.setattr(
        server.registry,
        "get_tool",
        lambda tool_id: {
            "id": tool_id,
            "kind": "launcher",
            "destructive": True,
            "destructiveActions": ["*卸载*"],
        },
    )

    async def should_not_run(*args, **kwargs):  # pragma: no cover
        raise AssertionError("策略 deny 下不得执行动作")

    monkeypatch.setattr(server.launcher_manager, "run_action", should_not_run)

    with pytest.raises(server.ToolError) as exc:
        server_run(
            "run_launcher_action",
            {"tool_id": "quicker", "action_id": "x", "action_name": "卸载微信"},
        )
    payload = json.loads(str(exc.value))
    assert payload["errorType"] == "permission_denied"
    assert "allowDestructive" not in payload["error"]
    assert "需管理员调整策略" in payload["error"]
    assert "卸载微信" in payload["error"], "仍应带上命中的具体动作"


# ---- 5. 三种 mode 的行为 ----


def test_mode_allow_skips_confirmation(monkeypatch):
    """mode=allow：部署者声明信任，破坏性调用免确认（也不挂 warning）"""
    _set_policy(
        monkeypatch, destructive={"mode": "allow"}, tools={"deny": [], "allow": None}
    )
    monkeypatch.setattr(server.registry, "get_tool", lambda tool_id: _DESTRUCTIVE_ENTRY)

    async def fake_call(tool_id, tool_name, arguments):
        return {"success": True, "result": "done"}

    monkeypatch.setattr(server.backend_manager, "call_backend_tool", fake_call)

    text = server_run(
        "call_backend_tool", {"tool_id": "boom", "tool_name": "nuke"}
    )
    payload = json.loads(text)
    assert payload["success"] is True
    assert "warning" not in payload, "mode=allow 下不产生确认流程，也不挂 destructive 告警"


def test_default_mode_keeps_phase1_behavior(monkeypatch):
    """默认 require_confirm：无 allowDestructive → destructive_blocked（Phase 1 行为）"""
    _set_policy(
        monkeypatch,
        destructive={"mode": "require_confirm"},
        tools={"deny": [], "allow": None},
    )
    monkeypatch.setattr(server.registry, "get_tool", lambda tool_id: _DESTRUCTIVE_ENTRY)

    with pytest.raises(server.ToolError) as exc:
        server_run("call_backend_tool", {"tool_id": "boom", "tool_name": "nuke"})
    assert json.loads(str(exc.value))["errorType"] == "destructive_blocked"


# ---- 6. 审计落盘 ----


def test_audit_rows_are_appended_to_log(tmp_path, monkeypatch):
    """判定行追加到 audit.log（append-only，一行 JSON）"""
    log_path = tmp_path / "audit.log"
    monkeypatch.setattr(policy, "AUDIT_LOG", log_path)
    _set_policy(monkeypatch, tools={"deny": [], "allow": None})

    policy.decide_destructive(
        "mcp", _DESTRUCTIVE_ENTRY, {"tool_name": "nuke"}, {}
    )
    policy.decide_destructive(
        "mcp", _DESTRUCTIVE_ENTRY, {"tool_name": "nuke"}, {"allowDestructive": True}
    )

    lines = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    assert lines[0]["decision"] == "block"
    assert lines[0]["rule_hit"] == "entry_level"
    assert lines[0]["policy_mode"] == "require_confirm"
    assert lines[1]["decision"] == "confirm"
    assert lines[1]["client_declared"] is True
