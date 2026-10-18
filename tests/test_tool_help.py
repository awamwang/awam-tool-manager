"""`src/tool_help.py` 的守卫（P1「工具帮助文档查看」的底座）。

探针脚本现场写进 tmp（与 `test_app_launcher.py` 同一体例）：帮助命令是真
spawn 外部进程，绝不能拿机器上真实的 exe 开刀。

钉住四件事：
1. **取数分层**不越界——默认路径只组装数据、绝不 spawn（`run` 由调用方显式给）；
2. **`help` 字段覆盖** `launch.command` + 默认 `--help` 的优先级，以及
   timeout 的归一化（默认 / 封顶 / 负数 / 乱传）；
3. **输出捕获的判定**：stdout 优先、空则回落 stderr、非零但有文本仍算成功、
   无文本才算失败、超长截断、GBK 与 UTF-8 都能解；
4. **超时必杀**：挂住的帮助命令不能留成孤儿进程。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import tool_help
from tool_help import (
    HELP_OUTPUT_MAX_CHARS,
    HELP_TIMEOUT_DEFAULT,
    HELP_TIMEOUT_MAX,
    backend_summary,
    entry_help,
    launcher_summary,
    resolve_help_command,
    run_help_command,
)

# ---- 探针源码 ----

PROBE_STDOUT = """import sys
print("Usage: demo --thing")
sys.exit(0)
"""

PROBE_STDERR = """import sys
print("help on stderr", file=sys.stderr)
sys.exit(0)
"""

PROBE_NONZERO_WITH_TEXT = """import sys
print("usage text")
sys.exit(2)
"""

PROBE_NONZERO_SILENT = """import sys
sys.exit(4)
"""

PROBE_SILENT_ZERO = """import sys
"""

PROBE_LONG = f"""import sys
sys.stdout.write("x" * {HELP_OUTPUT_MAX_CHARS + 500})
"""

PROBE_GBK = """import sys
sys.stdout.buffer.write("中文帮助".encode("gbk"))
"""

PROBE_SLEEP = """import time
time.sleep(30)
"""


def _probe(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _run(resolved):
    """跑一次帮助命令（同步壳，与项目其他异步用例同一体例）。"""
    return asyncio.run(run_help_command(resolved))


def _resolved(script: Path, timeout: float = 10.0, args=None) -> dict:
    return {
        "command": sys.executable,
        "args": args if args is not None else [str(script)],
        "timeout": timeout,
    }


# ---- 1. 取数分层：默认路径不越界 ----


def test_entry_help_returns_doc_fields_and_preview_without_running():
    """entry_help 只组装数据：带 docUrl/website/description 与命令预览，不 spawn"""
    tool = {
        "id": "demo",
        "description": "演示工具",
        "docUrl": "https://example.org/doc",
        "website": "https://example.org",
        "launch": {"type": "executable", "command": "demo.exe", "args": []},
    }
    help_data = entry_help(tool)

    assert help_data["description"] == "演示工具"
    assert help_data["docUrl"] == "https://example.org/doc"
    assert help_data["website"] == "https://example.org"
    assert help_data["helpCommand"] == {
        "command": "demo.exe",
        "args": ["--help"],
        "timeout": HELP_TIMEOUT_DEFAULT,
    }


def test_entry_help_without_launch_has_no_help_command():
    """既无 help 又无 launch → 不产出 helpCommand（只剩文档链接层）"""
    help_data = entry_help({"id": "mcp-only", "kind": "mcp"})
    assert "helpCommand" not in help_data
    assert help_data["docUrl"] == ""


def test_backend_summary_extracts_params_and_required():
    """mcp 子工具缓存 → 摘要带参数名与必填项，不下发完整 inputSchema"""
    tools = [
        {
            "name": "search_files",
            "description": "按关键词搜文件",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
        },
        {"name": "no_schema"},
    ]
    summary = backend_summary(tools)

    assert summary[0]["name"] == "search_files"
    assert summary[0]["params"] == ["query", "limit"]
    assert summary[0]["required"] == ["query"]
    assert "inputSchema" not in summary[0], "帮助视图不该把整份 schema 塞回调用方"
    assert summary[1] == {
        "name": "no_schema",
        "description": "",
        "params": [],
        "required": [],
    }


def test_launcher_summary_truncates_to_limit():
    """launcher 动作清单摘要按 limit 截断（全量另有 list_launcher_actions）"""
    actions = [{"id": str(i), "name": f"动作{i}", "type": "group"} for i in range(60)]
    summary = launcher_summary(actions, limit=50)
    assert len(summary) == 50
    assert summary[0] == {"id": "0", "name": "动作0", "type": "group"}


# ---- 2. help 字段的优先级与归一化 ----


def test_resolve_help_command_defaults_to_launch_command_and_double_dash():
    resolved = resolve_help_command(
        {"launch": {"type": "executable", "command": "C:\\Tools\\demo.exe"}}
    )
    assert resolved == {
        "command": "C:\\Tools\\demo.exe",
        "args": ["--help"],
        "timeout": HELP_TIMEOUT_DEFAULT,
    }


def test_resolve_help_command_prefers_help_fields():
    """help.command / help.args 覆盖 launch（Windows 图形程序要 `/?` 的场景）"""
    resolved = resolve_help_command(
        {
            "launch": {"type": "executable", "command": "demo.exe"},
            "help": {"command": "demo-helper.exe", "args": ["/?"]},
        }
    )
    assert resolved["command"] == "demo-helper.exe"
    assert resolved["args"] == ["/?"]


def test_resolve_help_command_without_any_command_returns_none():
    assert resolve_help_command({"kind": "mcp"}) is None
    assert resolve_help_command({"launch": {"type": "executable", "command": "  "}}) is None


def test_resolve_help_clamps_timeout_default_cap_and_negative():
    def _timeout(raw):
        return resolve_help_command({"launch": {"command": "x"}, "help": {"timeout": raw}})["timeout"]

    assert _timeout(None) == HELP_TIMEOUT_DEFAULT
    assert _timeout(999) == HELP_TIMEOUT_MAX
    assert _timeout(-5) == 0.0
    assert _timeout("很快") == HELP_TIMEOUT_DEFAULT  # 客户端乱传不炸


def test_resolve_help_args_elements_coerced_to_str():
    """条目里写整数参数也照样能传给 exec"""
    resolved = resolve_help_command(
        {"launch": {"command": "x"}, "help": {"args": [1, "two"]}}
    )
    assert resolved["args"] == ["1", "two"]


# ---- 3. 输出捕获与判定 ----


def test_run_help_captures_stdout_and_zero_exit(tmp_path):
    script = _probe(tmp_path, "p_out.py", PROBE_STDOUT)
    result = _run(_resolved(script))

    assert result["success"] is True, result
    assert result["executed"] is True
    assert result["exitCode"] == 0
    assert "Usage: demo --thing" in result["output"]
    assert result["truncated"] is False


def test_run_help_falls_back_to_stderr_when_stdout_empty(tmp_path):
    """不少程序把 help 打到 stderr：stdout 为空时必须回落"""
    script = _probe(tmp_path, "p_err.py", PROBE_STDERR)
    result = _run(_resolved(script))
    assert result["success"] is True
    assert "help on stderr" in result["output"]


def test_run_help_nonzero_with_text_is_success_with_note(tmp_path):
    """退出码非 0 但有文本：文本才是调用方要的，标 note 不算失败"""
    script = _probe(tmp_path, "p_nonzero_text.py", PROBE_NONZERO_WITH_TEXT)
    result = _run(_resolved(script))
    assert result["success"] is True, result
    assert result["exitCode"] == 2
    assert "usage text" in result["output"]
    assert "退出码" in result["note"]


def test_run_help_nonzero_without_output_is_runtime_error(tmp_path):
    script = _probe(tmp_path, "p_nonzero_silent.py", PROBE_NONZERO_SILENT)
    result = _run(_resolved(script))
    assert result["success"] is False
    assert result["errorType"] == "runtime_error"
    assert "退出码 4" in result["error"]


def test_run_help_zero_exit_but_silent_is_runtime_error(tmp_path):
    """跑完啥也没说 → 等同于没拿到帮助，报 runtime_error 而不是空成功"""
    script = _probe(tmp_path, "p_silent.py", PROBE_SILENT_ZERO)
    result = _run(_resolved(script))
    assert result["success"] is False
    assert result["errorType"] == "runtime_error"
    assert "无任何输出" in result["error"]


def test_run_help_truncates_overlong_output(tmp_path):
    """返回体要进 MCP 消息：超长输出截断并显式标 truncated"""
    script = _probe(tmp_path, "p_long.py", PROBE_LONG)
    result = _run(_resolved(script))
    assert result["success"] is True
    assert len(result["output"]) == HELP_OUTPUT_MAX_CHARS
    assert result["truncated"] is True


def test_run_help_decodes_gbk_output(tmp_path):
    """中文 Windows 控制台输出常是 GBK：UTF-8 严格解失败后必须回落 GBK"""
    script = _probe(tmp_path, "p_gbk.py", PROBE_GBK)
    result = _run(_resolved(script))
    assert result["success"] is True
    assert "中文帮助" in result["output"]


def test_decode_prefers_utf8_and_replaces_on_total_failure():
    assert tool_help._decode("纯 ASCII".encode("utf-8")) == "纯 ASCII"
    assert tool_help._decode(b"") == ""
    # 既不是合法 UTF-8 也不是合法 GBK 的字节 → replace 兜底，不抛
    assert isinstance(tool_help._decode(b"\xff\xfe\x00\x01"), str)


# ---- 4. 失败与超时 ----


def test_run_help_without_resolved_command_is_config_error():
    result = _run(None)
    assert result["success"] is False
    assert result["errorType"] == "config_error"
    assert result["executed"] is False


def test_run_help_missing_executable_is_config_error(tmp_path):
    """命令不存在 → 结构化 config_error，不把 OSError 原生文案抛给客户端"""
    result = _run({"command": "definitely-not-installed-xyz-9f8e", "args": ["--help"], "timeout": 5})
    assert result["success"] is False
    assert result["errorType"] == "config_error"
    assert "不在 PATH" in result["error"]


def test_run_help_timeout_kills_the_process(tmp_path):
    """挂住的帮助命令超时 → runtime_error 且进程被收掉，不留孤儿"""
    script = _probe(tmp_path, "p_sleep.py", PROBE_SLEEP)
    result = _run(_resolved(script, timeout=1.0))

    assert result["success"] is False
    assert result["errorType"] == "runtime_error"
    assert "未退出" in result["error"]
    assert result["executed"] is False  # 失败载荷的统一形状
