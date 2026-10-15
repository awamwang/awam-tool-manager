"""工具条目的 JSON Schema 定义与校验（A1）

`docs/format.md` 里那份字段规范此前**从未被代码使用过**（`requirements.txt` 也没有
`jsonschema`），于是"文档是纸面标准、data/*.json 是事实标准"，两者已经分叉：
字段拼错、类型写错、必填缺失都只能在运行期以奇怪的方式暴露（例如 `id` 缺失会让
`server.py` 里 `t["id"]` 直接 KeyError）。

本模块把规范变成可执行校验。设计上做了两处妥协：

- `jsonschema` 是**可选依赖**：没装时退回下面的基础检查，功能降级但不报错，
  避免为了一个校验把整个 MCP Server 变成装不上依赖就起不来；
- 只做"类型 + 必填 + 枚举"校验，未知字段一律放行（`additionalProperties` 不设 false），
  因为数据里有 `actions` / `capabilities` / `install` 这类文档尚未覆盖的扩展字段，
  一刀切会把合法数据判死。
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
        "id": {"type": "string", "minLength": 1, "pattern": r"^[A-Za-z0-9._-]+$"},
        "name": {"type": "string", "minLength": 1},
        "displayName": {"type": "string"},
        "kind": {"type": "string", "enum": ["app", "mcp"]},
        "description": {"type": "string"},
        "status": {"type": "string", "enum": ["installed", "not_installed", "unknown"]},
        "version": {"type": "string"},
        "publisher": {"type": "string"},
        "website": {"type": "string"},
        "docUrl": {"type": "string"},
        "icon": {"type": "string"},
        "launch": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["executable", "protocol", "command"]},
                "command": {"type": "string"},
                "args": {"type": "array"},
            },
        },
        "connection": {
            "type": "object",
            "required": ["type", "command"],
            "properties": {
                "type": {"type": "string", "enum": ["stdio"]},
                "command": {"type": "string"},
                "args": {"type": "array"},
                "env": {"type": "object"},
                "timeout": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "update": {"type": "object"},
        "install": {"type": "object"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}},
        "capabilities": {"type": "array", "items": {"type": "string"}},
        "readOnly": {"type": "boolean"},
        "destructive": {"type": "boolean"},
        "metadata": {"type": "object"},
        "createdAt": {"type": "string"},
        "updatedAt": {"type": "string"},
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
    if kind not in ("app", "mcp"):
        return f"kind 取值非法: {kind}（应为 app / mcp）"

    connection = data.get("connection")
    if kind == "mcp" and connection is not None:
        if not isinstance(connection, dict):
            return "connection 必须是对象"
        if not connection.get("command"):
            return "kind=mcp 的工具必须配置 connection.command"

    return None


def validate_tool_entry(data: Any) -> str | None:
    """校验一条工具条目，返回错误描述；合法则返回 None"""
    error = _basic_checks(data)
    if error:
        return error

    if jsonschema is None:  # 未安装可选依赖，只做基础校验
        return None

    validator = jsonschema.Draft202012Validator(TOOL_ENTRY_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if not errors:
        return None

    first = errors[0]
    path = ".".join(str(p) for p in first.absolute_path) or "(根)"
    return f"{path}: {first.message}"


def schema_backend_available() -> bool:
    """jsonschema 是否已安装（用于启动日志里说清楚校验强度）"""
    return jsonschema is not None
