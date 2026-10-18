"""工具条目的 JSON Schema 定义与校验（A1）

`docs/format.md` 里那份字段规范此前**从未被代码使用过**（`requirements.txt` 也没有
`jsonschema`），于是"文档是纸面标准、data/*.json 是事实标准"，两者已经分叉：
字段拼错、类型写错、必填缺失都只能在运行期以奇怪的方式暴露（例如 `id` 缺失会让
`server.py` 里 `t["id"]` 直接 KeyError）。

本模块把规范变成可执行校验，并且**反过来成为文档的唯一真相源**（A4）：
`docs/format.md` 里的两张字段表由 `scripts/generate_format_doc.py` 读取本文件的
`description` / `enum` / `required` 等注解生成，手写改文档会被测试判为漂移。

设计上做了两处妥协：

- `jsonschema` 是**可选依赖**：没装时退回下面的基础检查，功能降级但不报错，
  避免为了一个校验把整个 MCP Server 变成装不上依赖就起不来；
- 只做"类型 + 必填 + 枚举"校验，未知字段一律放行（`additionalProperties` 不设 false），
  因为数据里有 `actions` / `capabilities` / `install` 这类文档尚未覆盖的扩展字段，
  一刀切会把合法数据判死。

字段说明写在各 `description` 上——这是 JSON Schema 的标准注解关键字，
校验器不会用它做任何断言，只供人读和供文档生成器读取。
"""

from typing import Any

try:  # 可选依赖，缺失时走基础校验
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


TOOL_ENTRY_SCHEMA: dict[str, Any] = {
    "title": "ToolEntry",
    "type": "object",
    "required": ["id", "name", "kind"],
    "properties": {
        "id": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^[A-Za-z0-9._-]+$",
            "description": "工具唯一 ID，**必须与文件名一致**",
        },
        "name": {"type": "string", "minLength": 1, "description": "工具名称（英文/技术名）"},
        "displayName": {"type": "string", "description": "显示名称（中文/用户友好），缺省回落 `name`"},
        "kind": {
            "type": "string",
            "enum": ["app", "mcp", "launcher"],
            "description": (
                "工具类型：`app` 普通应用（`launch_tool` 返回命令，显式 `execute=true` 时启动）、"
                "`mcp` 可建后端连接、"
                "`launcher` 启动器（非 MCP 协议，但有可列/可搜/可调用的动作清单，见 `launcher`）"
            ),
        },
        "description": {"type": "string", "description": "工具功能描述（参与搜索匹配）"},
        "status": {
            "type": "string",
            "enum": ["installed", "not_installed", "unknown"],
            "description": "安装状态",
        },
        "version": {"type": "string", "description": "版本号"},
        "publisher": {"type": "string", "description": "发布者"},
        "website": {"type": "string", "description": "官方网站"},
        "docUrl": {"type": "string", "description": "文档地址"},
        "icon": {"type": "string", "description": "图标路径或 URL"},
        "launch": {
            "type": "object",
            "description": "**仅 `kind=app`**：启动方式，`launch_tool` 据此返回命令；`execute=true` 时按此 spawn 进程",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["executable", "protocol", "command"],
                    "description": "启动方式类型",
                },
                "command": {"type": "string", "description": "启动命令或可执行文件路径"},
                "args": {"type": "array", "description": "启动参数（元素建议统一写字符串）"},
            },
        },
        "help": {
            "type": "object",
            "description": "帮助信息获取方式，`get_tool_help` 据此取参数；缺省用 `launch.command` + `--help`",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "专用帮助命令（缺省回落 `launch.command`）",
                },
                "args": {
                    "type": "array",
                    "description": "帮助参数（缺省 `--help`；Windows 图形程序常为 `/?`）",
                },
                "timeout": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 5,
                    "description": "执行帮助命令的超时秒数（封顶 30）",
                },
            },
        },
        "connection": {
            "type": "object",
            "description": "**仅 `kind=mcp`**：后端 MCP 连接方式，`BackendClient` 据此拉起子进程",
            "required": ["type", "command"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["stdio"],
                    "description": "连接类型，目前仅支持 stdio",
                },
                "command": {"type": "string", "description": "拉起子进程的命令"},
                "args": {"type": "array", "description": "子进程参数"},
                "env": {"type": "object", "description": "子进程环境变量"},
                "timeout": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 30,
                    "description": "后端操作超时（秒），覆盖 initialize / list_tools / call_tool / ping",
                },
            },
        },
        "update": {
            "type": "object",
            "description": "更新方式",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["winget", "scoop", "npm", "manual"],
                    "description": "更新渠道",
                },
                "packageId": {"type": "string", "description": "包管理器中的包 ID"},
            },
        },
        "install": {
            "type": "object",
            "description": "安装方式；`start_backend` 检测到启动命令缺失时，仅在**显式授权**后才会执行一次 `command`（默认不执行，见安全约定），与 `update` 并存",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["npm", "winget", "scoop", "manual"],
                    "description": "安装渠道",
                },
                "command": {"type": "string", "description": "安装命令"},
                "auto": {
                    "type": "boolean",
                    "description": "是否允许自动执行 `command`；默认 false。需显式设为 true，或设环境变量 AWAM_AUTO_INSTALL=1，否则 install.command 永不被自动执行",
                },
            },
        },
        "launcher": {
            "type": "object",
            "description": "启动器适配配置；仅 `kind=launcher` 有意义，决定如何列出与调用它的动作",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["quicker", "utools"],
                    "description": (
                        "适配器类型：`quicker` 走 QuickerStarter 命令行（有 stdout 回传）；"
                        "`utools` 走 utools:// 协议唤起（fire-and-forget，无回传，结果只在 uTools UI）"
                    ),
                },
                "command": {"type": "string", "description": "启动器可执行文件路径（如 QuickerStarter.exe；utools 协议调用不需要）"},
                "dataDir": {
                    "type": "string",
                    "description": (
                        "启动器数据目录（Quicker 的 states 所在目录；uTools 的插件 asar 所在目录，"
                        "utools 适配器会展开 %APPDATA% 等环境变量）"
                    ),
                },
                "timeout": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 20,
                    "description": "单次动作调用的最长等待（秒）",
                },
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}, "description": "标签分类（参与搜索匹配）"},
        "actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "支持的动作（如 `launch` / `search`）",
        },
        "capabilities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "能力标签（如 `search` / `index`）",
        },
        "readOnly": {"type": "boolean", "description": "是否只读（对应 MCP 的 `readOnlyHint`）"},
        "destructive": {
            "type": "boolean",
            "description": "是否有破坏性（对应 `destructiveHint`，`call_backend_tool` 会据此拦截）",
        },
        "destructiveTools": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "子工具级破坏性清单：只有列在这里的后端子工具需要 `allowDestructive`"
                " 确认；未配置时回退到条目级 `destructive`（即整个后端的所有调用都要确认）"
            ),
        },
        "destructiveActions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "启动器动作级破坏性清单（glob，大小写不敏感）：按动作的 id/name/type"
                " 匹配，命中才需要 `allowDestructive` 确认；未配置时回退到条目级"
                " `destructive`（即该启动器的所有动作都要确认）"
            ),
        },
        "metadata": {"type": "object", "description": "扩展元数据，键值对自由"},
        "createdAt": {"type": "string", "description": "创建时间，ISO 8601"},
        "updatedAt": {"type": "string", "description": "更新时间，ISO 8601"},
    },
}


BACKEND_CATALOG_SCHEMA: dict[str, Any] = {
    "title": "BackendCatalog",
    "type": "object",
    "required": ["parent", "tools"],
    "properties": {
        "parent": {"type": "string", "description": "父 MCP 工具的 ID"},
        "lastSynced": {"type": "string", "description": "上次同步时间，ISO 8601；用于判断缓存有多旧"},
        "source": {
            "type": "string",
            "enum": ["auto"],
            "description": "来源，表示由后端自动拉取而非人工维护",
        },
        "tools": {
            "type": "array",
            "description": "后端 MCP 提供的子工具列表",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "子工具名，即 `call_backend_tool` 的 `tool_name` 参数",
                    },
                    "description": {"type": "string", "description": "子工具描述"},
                    "inputSchema": {"type": "object", "description": "透传自后端 MCP 的 JSON Schema"},
                },
            },
        },
    },
}


def _basic_checks(data: Any) -> str | None:
    """不依赖 jsonschema 的最小校验：保证下游不会 KeyError / 类型错乱"""
    if not isinstance(data, dict):
        return f"顶层不是对象（实际为 {type(data).__name__}）"

    for field in ("id", "name", "kind"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            return f"缺少必填字段或类型错误: {field}"

    kind = data["kind"]
    if kind not in ("app", "mcp", "launcher"):
        return f"kind 取值非法: {kind}（应为 app / mcp / launcher）"

    connection = data.get("connection")
    if kind == "mcp" and connection is not None:
        if not isinstance(connection, dict):
            return "connection 必须是对象"
        if not connection.get("command"):
            return "kind=mcp 的工具必须配置 connection.command"

    return None


def _validate_with_schema(data: Any, schema: dict[str, Any]) -> str | None:
    """按给定 schema 校验，返回第一条错误的描述；合法返回 None

    只报第一条：错误信息最终要出现在 MCP 工具返回值里给 AI 看，
    一堆嵌套错误反而不如"第一个出错的字段"好定位。
    """
    if jsonschema is None:  # 未安装可选依赖，只做基础校验
        return None

    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if not errors:
        return None

    first = errors[0]
    path = ".".join(str(p) for p in first.absolute_path) or "(根)"
    return f"{path}: {first.message}"


LAUNCHER_ACTIONS_SCHEMA: dict[str, Any] = {
    "title": "LauncherActions",
    "type": "object",
    "required": ["parent", "actions"],
    "properties": {
        "parent": {"type": "string", "description": "父启动器工具的 ID"},
        "lastSynced": {"type": "string", "description": "上次同步时间，ISO 8601；用于判断缓存有多旧"},
        "source": {
            "type": "string",
            "enum": ["auto", "partial"],
            "description": (
                "来源：`auto` 完整自动同步；`partial` 表示只拿到了部分字段"
                "（Quicker 的动作名依赖其 state 缓存，缺失时只剩动作 ID）"
            ),
        },
        "actions": {
            "type": "array",
            "description": "启动器提供的动作列表",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string", "description": "动作 ID（`run_launcher_action` 用它指定动作）"},
                    "name": {"type": "string", "description": "动作名称；取不到时为空串（只有 ID 也能调用）"},
                    "type": {"type": "string", "description": "动作类型（Quicker 如 XAction / SendKeys）"},
                    "app": {"type": "string", "description": "动作所属程序（Quicker 的上下文程序，如 `_global`）"},
                    "description": {"type": "string", "description": "动作描述"},
                },
            },
        },
    },
}


def validate_tool_entry(data: Any) -> str | None:
    """校验一条工具条目（`data/tools/{tool_id}.json`），返回错误描述；合法则返回 None"""
    error = _basic_checks(data)
    if error:
        return error
    return _validate_with_schema(data, TOOL_ENTRY_SCHEMA)


def validate_backend_catalog(data: Any) -> str | None:
    """校验一份后端子工具缓存（`data/backend-tools/{tool_id}.json`）

    这类文件是自动生成的，所以只做结构校验：`tools` 不是数组、或子工具缺 `name`
    都会让 `call_backend_tool` 的查表静默失效，不如加载时就告警。
    """
    if not isinstance(data, dict):
        return f"顶层不是对象（实际为 {type(data).__name__}）"
    if "tools" in data and not isinstance(data["tools"], list):
        return "tools 必须是数组"
    return _validate_with_schema(data, BACKEND_CATALOG_SCHEMA)


def validate_launcher_catalog(data: Any) -> str | None:
    """校验一份启动器动作清单（`data/launcher-actions/{tool_id}.json`）

    与后端子工具缓存同理：自动生成，只做结构校验。`actions` 不是数组、或动作缺
    `id` 都会让 `run_launcher_action` 的查表静默失效，不如加载时就告警。
    """
    if not isinstance(data, dict):
        return f"顶层不是对象（实际为 {type(data).__name__}）"
    if "actions" in data and not isinstance(data["actions"], list):
        return "actions 必须是数组"
    return _validate_with_schema(data, LAUNCHER_ACTIONS_SCHEMA)


# ---- 部署策略（ARC-P1-05 Phase 2，方案 C）----
#
# 与 data/tools 的容错取向相反：策略文件的未知字段**一律拒绝**
# （additionalProperties: false）。工具条目里一个拼错的扩展字段只是没被用上，
# 策略里一个拼错的 `deny`/`mode` 却会静默地让安全约束失效——坏策略必须
# 快速失败（policy.load_policy 在 import 期调用本校验）。

POLICY_SCHEMA: dict[str, Any] = {
    "title": "Policy",
    "type": "object",
    "additionalProperties": False,
    "required": ["version"],
    "properties": {
        "version": {
            "type": "integer",
            "const": 1,
            "description": "策略格式版本（当前固定为 1）",
        },
        "tools": {
            "type": "object",
            "additionalProperties": False,
            "description": "工具级策略：`deny` 名单优先于 `allow` 白名单",
            "properties": {
                "deny": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "禁止调用的对外工具名列表（deny 优先于 allow）",
                },
                "allow": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": (
                        "对外工具白名单；`null` 表示不启用（默认），非 null 时"
                        "不在名单内的工具一律拒绝"
                    ),
                },
            },
        },
        "destructive": {
            "type": "object",
            "additionalProperties": False,
            "description": "破坏性门禁策略",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["deny", "require_confirm", "allow"],
                    "description": (
                        "`deny`：任何命中破坏性判定的调用直接拒绝（网关只读部署），"
                        "不读客户端 `allowDestructive`；`require_confirm`：现行为"
                        "（默认，需客户端显式确认）；`allow`：部署者声明本机信任，"
                        "跳过确认（须显式写）"
                    ),
                },
            },
        },
        "audit": {
            "type": "object",
            "additionalProperties": False,
            "description": "审计策略",
            "properties": {
                "destructive": {
                    "type": "boolean",
                    "description": (
                        "是否把每行破坏性判定审计追加到运行时目录的 audit.log"
                        "（append-only、不轮转；默认 true）"
                    ),
                },
            },
        },
    },
}


def _policy_basic_checks(data: Any) -> str | None:
    """不依赖 jsonschema 的最小校验：策略判错等于安全形同虚设，必须兜底"""
    if not isinstance(data, dict):
        return f"顶层不是对象（实际为 {type(data).__name__}）"
    if data.get("version") != 1:
        return f"version 必须为 1（实际为 {data.get('version')!r}）"
    tools = data.get("tools") or {}
    if not isinstance(tools, dict):
        return "tools 必须是对象"
    for key in ("deny", "allow"):
        value = tools.get(key)
        if value is not None and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            return f"tools.{key} 必须是字符串数组或 null"
    destructive = data.get("destructive") or {}
    if not isinstance(destructive, dict):
        return "destructive 必须是对象"
    mode = destructive.get("mode", "require_confirm")
    if mode not in ("deny", "require_confirm", "allow"):
        return f"destructive.mode 取值非法: {mode!r}（应为 deny / require_confirm / allow）"
    audit = data.get("audit") or {}
    if not isinstance(audit, dict):
        return "audit 必须是对象"
    if not isinstance(audit.get("destructive", True), bool):
        return "audit.destructive 必须是布尔值"
    return None


def validate_policy(data: Any) -> str | None:
    """校验部署策略文件（`data/policy.json`），返回错误描述；合法则返回 None"""
    error = _policy_basic_checks(data)
    if error:
        return error
    return _validate_with_schema(data, POLICY_SCHEMA)


def schema_backend_available() -> bool:
    """jsonschema 是否已安装（用于启动日志里说清楚校验强度）"""
    return jsonschema is not None
