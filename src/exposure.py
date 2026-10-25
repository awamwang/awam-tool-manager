"""对外工具暴露模式（lazy / default / full）与 MCP instructions 生成。

三种模式是**同一张声明表的不同切片**，不产生第二份工具定义：

- `lazy`：19 个治理、能力路由与运行时工具，高频能力走
  `list_tools → list_capabilities → call_capability`；
- `default`：在 lazy 之上补 7 个意图级前门工具（文件搜索、打开路径、打开应用、
  Steam 游戏、启动器动作），模型看到工具名就知道能力存在；
- `full`：再补 5 个低频/执行面工具（文件元信息、Everything 状态、winget 搜索与安装、
  包升级查询）。

模式在进程启动时解析一次（MCP 客户端需重连才会拿到新的 tools/list）。
优先级：`EXPOSURE_MODE` 环境变量 > `data/exposure.json` 的 `mode` > `lazy`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MODES = ("lazy", "default", "full")
DEFAULT_MODE = "lazy"
MODE_ENV_VAR = "EXPOSURE_MODE"
EXPOSURE_FILE_ENV_VAR = "EXPOSURE_FILE"
DEFAULT_EXPOSURE_PATH = Path(__file__).parent.parent / "data" / "exposure.json"


class ExposureError(RuntimeError):
    """暴露模式配置不可用（取值非法 / 文件读不了或 JSON 坏了）。

    与 `policy.PolicyError` 同一取舍：配置决定「模型能看见什么」，坏配置
    不能带病运行——启动即失败远好过静默退回某个模式。
    """


def resolve_exposure_path() -> Path:
    override = os.environ.get(EXPOSURE_FILE_ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_EXPOSURE_PATH


def normalize_mode(value: object, source: str = "配置") -> str:
    mode = str(value or "").strip().lower()
    if mode not in MODES:
        raise ExposureError(
            f"{source} 的暴露模式非法: {value!r}（可选值: {', '.join(MODES)}）"
        )
    return mode


def load_exposure_config(path: str | Path | None = None) -> dict:
    """文件不存在 → 默认 lazy；存在则必须是带 `mode` 的 JSON 对象。"""
    exposure_path = Path(path) if path is not None else resolve_exposure_path()
    if not exposure_path.exists():
        return {"mode": DEFAULT_MODE}
    try:
        with open(exposure_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ExposureError(
            f"暴露模式文件不可读或不是合法 JSON（{exposure_path}）: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ExposureError(f"暴露模式文件顶层必须是 JSON 对象（{exposure_path}）")
    mode = data.get("mode", DEFAULT_MODE)
    return {"mode": normalize_mode(mode, source=str(exposure_path))}


def resolve_exposure_mode(path: str | Path | None = None) -> str:
    """环境变量优先；否则读配置文件；都没有则 lazy。"""
    env_mode = os.environ.get(MODE_ENV_VAR, "").strip()
    if env_mode:
        return normalize_mode(env_mode, source=f"环境变量 {MODE_ENV_VAR}")
    return load_exposure_config(path)["mode"]


# ---- instructions：能力清单按「意图家族」组织 ----
# 工具名在这里出现一次是为了写人话；`validate_instruction_names()` 在测试里
# 钉住两边完全一致（声明表新增工具却忘了进 instructions 会红）。
_FAMILIES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "文件",
        (
            ("search_files", "Everything 全盘搜索文件/文件夹；参数 keyword（必填）、limit、match_path、regex"),
            ("open_path", "按确切路径打开文件或文件夹；参数 path（必填）、reveal、execute；默认只预览"),
            ("get_file_info", "读取单个文件/文件夹的元信息（大小、时间、属性）"),
            ("get_everything_status", "查看 Everything 后端与索引状态"),
        ),
    ),
    (
        "应用与游戏",
        (
            ("open_app", "按名称打开已登记应用、Steam 客户端或 scoop 已装程序；默认只预览"),
            ("search_games", "搜索 Steam 已安装游戏，返回 appid / name / installedDir"),
            ("open_game", "按 appid 或唯一游戏名启动 Steam 游戏；多候选返回列表；默认只预览"),
            ("launch_tool", "按 tool_id 返回或执行已登记应用的启动命令"),
        ),
    ),
    (
        "快捷动作",
        (
            ("search_actions", "跨 Quicker / uTools 搜索动作或插件，返回 source / action_id / name"),
            ("run_action", "按动作 ID 或唯一名称执行 Quicker / uTools 动作；默认只预览"),
        ),
    ),
    (
        "包管理",
        (
            ("list_packages", "列出 scoop 已安装的包"),
            ("search_packages", "在 scoop 已添加的 bucket 清单里搜索可安装包"),
            ("launch_package", "启动已安装的 scoop 包；默认只预览入口"),
            ("install_package", "安装 scoop 包；需 execute=true 且 allowDestructive=true"),
            ("search_winget_packages", "用 winget 搜索可安装/可升级的包"),
            ("install_winget_package", "用 winget 安装或升级包；需 execute=true 且 allowDestructive=true"),
            ("check_package_updates", "查哪些包有可用更新；默认读 scoop 盘上的版本差集，execute=true 才问 UniGetUI"),
        ),
    ),
    (
        "工具、能力与运行时",
        (
            ("search_tools", "按关键词搜索已登记工具，可按 kind / status / publisher 收窄"),
            ("list_tools", "列出所有已登记工具"),
            ("get_tool_info", "查看工具详情与运行状态"),
            ("get_tool_help", "取工具帮助信息与能力概要"),
            ("discover_tools", "按声明清单探测本机是否安装了某个工具"),
            ("list_capabilities", "列出某个工具的能力（MCP 子工具 / 启动器动作 / 应用的 launch）及完整 inputSchema"),
            ("call_capability", "调用某个工具的能力；arguments 是能力自己的参数，默认只预览"),
            ("sync_capabilities", "同步某个工具的能力清单（MCP 刷新目录 / 启动器重扫本地数据）"),
            ("start_runtime", "启动 MCP 提供者的运行时并同步能力目录"),
            ("stop_runtime", "停止 MCP 提供者的运行时"),
            ("get_runtime_status", "查看 MCP 运行时状态"),
            ("get_runtime_logs", "查看 MCP 运行时日志"),
            ("set_runtime_log_level", "设置 MCP 运行时日志级别"),
            ("get_usage_stats", "查询工具调用统计"),
        ),
    ),
)

_MODE_NOTES = {
    "lazy": (
        "lazy：只暴露治理、能力路由与运行时工具。找文件/游戏/动作先 "
        "list_tools → list_capabilities → call_capability。"
    ),
    "default": (
        "default：高频意图已直接暴露为工具名。文件、应用、游戏、快捷动作优先用意图工具；"
        "长尾能力仍走 list_capabilities → call_capability。"
    ),
    "full": (
        "full：全部能力都已直接暴露；长尾仍可用 list_capabilities → call_capability。"
    ),
}

_COMMON_RULES = (
    "1. open_path / open_app / open_game / run_action 默认只预览，execute=true 才产生副作用。",
    "2. 破坏性操作必须显式传 allowDestructive=true；多候选返回列表时先让用户确认，不要自动选。",
    "3. 文件搜索优先 search_files，禁止用 PowerShell 逐盘递归。",
    "4. MCP 能力调用会自动拉起运行时；需要预热或排查时才用 start_runtime / get_runtime_status / get_runtime_logs。",
    "5. 长尾能力先 list_capabilities 取 inputSchema，再按 capability_id + 嵌套 arguments 调 call_capability。",
)

# lazy 下前门工具不可见，规则里不能出现它们的名字——否则 instructions 会
# 建议模型去调一个 tools/list 里根本不存在的工具。
_LAZY_RULES = (
    "1. launch_tool 默认只取预览，execute=true 才产生副作用。",
    "2. 破坏性操作必须显式传 allowDestructive=true；多候选返回列表时先让用户确认，不要自动选。",
    "3. 文件搜索走 list_capabilities(tool_id='everything-search-mcp') → "
    "call_capability(capability_id='search_files')，禁止用 PowerShell 逐盘递归。",
    "4. MCP 能力调用会自动拉起运行时；排查用 get_runtime_status / get_runtime_logs。",
    "5. 长尾能力先 list_capabilities 取 inputSchema，再按 capability_id + 嵌套 arguments 调 call_capability。",
)


def exposed_tool_names(mode: str) -> frozenset[str]:
    from tool_specs import SERVER_TOOL_SPECS, is_exposed

    return frozenset(s.name for s in SERVER_TOOL_SPECS if is_exposed(s, mode))


def build_instructions(mode: str) -> str:
    """按模式生成 initialize.instructions：只列该模式真正可见的工具。"""
    mode = normalize_mode(mode)
    visible = exposed_tool_names(mode)

    lines = [
        "你是 awam-tool-manager，本机工具与能力的统一入口。",
        f"当前暴露模式：{mode}。",
        _MODE_NOTES[mode],
        "",
    ]
    for family, entries in _FAMILIES:
        shown = [(name, hint) for name, hint in entries if name in visible]
        if not shown:
            continue
        lines.append(f"【{family}】")
        for name, hint in shown:
            lines.append(f"- {name}：{hint}。")
        lines.append("")

    lines.append("规则：")
    lines.extend(_LAZY_RULES if mode == "lazy" else _COMMON_RULES)
    return "\n".join(lines).strip() + "\n"


def validate_instruction_names() -> None:
    """自检：instructions 的工具名与声明表一一覆盖（不重不漏）。"""
    from tool_specs import SERVER_TOOL_SPECS

    spec_names = {spec.name for spec in SERVER_TOOL_SPECS}
    listed = [name for _, entries in _FAMILIES for name, _ in entries]
    duplicates = {name for name in listed if listed.count(name) > 1}
    if duplicates:
        raise ExposureError(f"instructions 里重复列出的工具: {sorted(duplicates)}")
    missing = spec_names - set(listed)
    extra = set(listed) - spec_names
    if missing or extra:
        raise ExposureError(
            f"instructions 与工具声明不一致：缺少 {sorted(missing)}，多出 {sorted(extra)}"
        )
