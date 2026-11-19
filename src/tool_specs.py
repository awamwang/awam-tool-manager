"""对外 MCP 工具的声明式注册表（P1-04，SSOT）。

搬迁前，同一个工具名在 server.py 里有四个平行维护点：`TOOL_ANNOTATION_HINTS`
注解表、`list_tools()` 里的 `Tool(...)` 字面量、`dispatch_tool` 的 if/elif
分支、分支内的 `json.dumps`，四者无任何机制保证一致。本模块把前三者收敛为
**一条 `ServerToolSpec` 记录**（annotations 与工具同条记录，与工具名同生共死），
序列化收敛到 server.dispatch_tool 的一处。

标准动作：新增一个对外工具 = 在本文件加「1 条 ServerToolSpec + 1 个
async def _handle_xxx」，`build_mcp_tools()` / `dispatch_tool` /
`conftest.EXPECTED_TOOL_NAMES` 的守卫测试全部自动跟随。

与 `schema.py` 的分工：schema.py 管辖**数据条目**（`data/tools/*.json`），
本模块管辖**对外 MCP 工具**（协议层 `tools/call` 入口，按暴露模式切片）。
两者字段名有重叠但语义不同，互不 import、互不引用。

能力的实际路由在 `capabilities.py`：本模块只声明对外工具与门禁入口，
Provider / Capability / Runtime 的解析与调用不在这里复制第二份。
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from mcp.types import Tool

try:  # Tool annotations 同样是 mcp>=1.9 才引入的能力
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    ToolAnnotations = None

from errors import ErrorType, ToolError, fail, ensure_success  # noqa: F401（handler 侧经本模块转手）
import capabilities
from capabilities import (
    EVERYTHING_TOOL_ID,
    STEAM_TOOL_ID,
    WINGET_TOOL_ID,
    RUNTIME_RUNNING,
    ensure_runtime,
)
from app_launcher import launch_app
import file_ops
# 发现清单走「环境变量指路径」而不是模块全局常量，所以按值 import 就够：
# 单测用 monkeypatch.setenv("AWAM_DISCOVERY_FILE", ...) 换清单，不需要打补丁。
from discovery import DiscoveryError, discover, load_manifest, note_for, resolve_discovery_path
from launcher_client import LauncherError
# 包管理面**按模块引用**（同 `usage` 的先例）：单测要把 scoop 根指到 tmp_path，
# `monkeypatch.setattr(pkgman, "resolve_root", ...)` 只有模块引用才打得住；
# 按值 import 会把 handler 焊死在 import 那一刻的函数对象上。
import pkgman
from policy import decide_destructive
# 统计模块**按模块引用**：`usage.USAGE_LOG` 要在调用时取当前值，
# 按值 import 会让单测的 monkeypatch 打不中（policy.AUDIT_LOG 踩过一次）。
import usage
from tool_help import (
    backend_summary,
    entry_help,
    launcher_summary,
    resolve_help_command,
    run_help_command,
)

if TYPE_CHECKING:  # 运行期零耦合：handler 只从 ctx 取 manager，不 import 它们
    from backend_client import BackendManager
    from launcher_client import LauncherManager
    from registry import ToolRegistry
    from store import JsonStore


@dataclass(frozen=True)
class ToolContext:
    """handler 的依赖注入容器。

    handlers **不 import** 任何 manager，只从 `ctx` 取——这是让「handler 可搬走」
    且「不产生循环 import」的关键。`frozen=True`：容器在 server import 期构造
    一次，运行期不可变；测试 monkeypatch 的是 manager 对象的属性，ctx 持有的
    是同一引用，patch 照样生效。
    """

    registry: "ToolRegistry"
    backend_manager: "BackendManager"
    launcher_manager: "LauncherManager"
    store: "JsonStore"
    log: Callable[[str], None]


@dataclass(frozen=True)
class ServerToolSpec:
    """一条对外工具声明。

    字段语义：
    - `annotations`：`dict | None`。**`{}` 与 `None` 都表示不挂注解**——逐一
      复现搬迁前现状：`set_runtime_log_level` 的注解就是 `{}`，而原
      `list_tools()` 的 `if hints:` 对空 dict 取假从而不挂。
    - `output_schema`：`dict | None`，`None` → 回落 `SHARED_OUTPUT_SCHEMA`。
      为将来某个工具需要差异化输出声明时预留。
    - `handler`：`async (arguments: dict, ctx: ToolContext) -> Any`，返回**载荷
      对象**（dict），不返回 JSON 文本——序列化统一由 `server.dispatch_tool`
      负责。迁移期（M03 分批搬迁）未搬迁的条目为 `None`，由 dispatch 的脚手架
      回落到旧 if/elif；`validate_specs()` 在最终态强制非空。
    """

    name: str
    description: str
    input_schema: dict
    annotations: dict | None = None
    output_schema: dict | None = None
    handler: Callable[..., Any] | None = None
    # 本条工具在哪些暴露模式下可见（见 exposure.py 的 lazy/default/full）。
    # 基线管理工具三种模式全可见；意图级前门工具至少 default；低频/执行面工具 full。
    exposure: tuple[str, ...] = ("lazy", "default", "full")
    # 本条工具专属的「规范名 → 别名」覆盖。未列出的规范名回落
    # `DEFAULT_ARG_ALIASES`；列成空元组表示该规范名显式不接别名。
    arg_aliases: dict[str, tuple[str, ...]] | None = None


def is_exposed(spec: ServerToolSpec, mode: str) -> bool:
    """该模式是否暴露这条工具（未知模式一律不暴露，由配置层提前拦）。"""
    return mode in spec.exposure


# 所有工具的成功输出都带顶层 `success`（bool），其余字段随工具而异（如 list 类
# 工具的 `tools` 数组、backend 类工具的 `result` 自由结构）。用一张宽松但准确的
# outputSchema 声明这一不变式：既让客户端能可靠按 `success` 分支，又不会因
# 工具自有的自由字段触发 mcp 1.30 的 outputSchema 校验失败（additionalProperties=true）。
# 注意：只要在 list_tools 里给工具挂了 outputSchema，mcp 就要求每次调用都返回
# structuredContent，否则会返回 "Output validation error"，因此 call_tool 必须为
# 每个成功结果附带 structuredContent（见 server.py 的 _result_to_structured）。
SHARED_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {"success": {"type": "boolean"}},
    "required": ["success"],
    "additionalProperties": True,
}

# ---- 入参别名（让真实 Agent 首轮就命中）----
# 背景：宿主（Qoder 等 MCP 客户端）在**入站时**就按 inputSchema 的 `required`
# 做 JSON Schema 校验，不合格的请求根本到不了 handler——我们连一句「参数名应该
# 是 tool_id」都递不出去。实测一次「找 Everything 相关工具」的真实会话：Agent
# 按直觉写 `{"query": "everything"}`、`{"id": "everything"}`，两次都被挡回
# `params must have required property 'keyword' / 'tool_id'`，第三次它就放弃
# MCP 通道、自己起 shell 去 `which es.exe` 了。
#
# 体例（两处各司其职，声明表仍是唯一真相源）：
# - 声明表里的 `input_schema.required` **只写规范名**（可读性优先，与旧版一致）；
# - `build_mcp_tools()` 导出时把带别名的规范名展开成 `anyOf`（单组）/
#   `allOf`+`anyOf`（多组）的「每组任选其一」，并把别名也声明成 string 属性；
# - `server.dispatch_tool` 在调 handler 前用 `normalize_arguments()` 把别名折回
#   规范名——**handler 一律只认规范名**，不感知别名。
# 退役名（历史上真实存在过、已被统一掉的参数名）不得借别名复活，
# 由 validate_specs 与 tests/test_arg_aliases.py 双向钉住。

RETIRED_PARAM_NAMES = frozenset({"mcp_tool_id", "launcher_id"})

# 规范名 → 可接受别名。**刻意收敛到每个规范名一个**，取舍依据两条：
# ① 成本实测——别名属性平均 111 个字符（43 个砍掉的属性去掉 4772 字符），而全表
#    schema 要整体下发进调用方上下文；② 收益面——别名只对「确实读了 schema」的
#    客户端有用，而勘察结论是模型多半**没读到 schema**（见 `export_description`）。
# 留下的三个都是「业界公认 + 真实会话里被写过」的交集：
# - `tool_id ← id`：REST/GraphQL 的实体主键写法；
# - `keyword ← query`：搜索类工具的事实标准；
# - `package ← name`：scoop / brew / winget 都把包标识叫「名称」。
# 砍掉的（连同它们的归一化口子一起关）：camelCase `toolId`/`actionId`/`actionName`
# （与全表 snake_case 自相矛盾）、`q`/`text`/`pkg`（工具生态里没有这条惯例）、
# `log_level`（`level` 才是 syslog/logging 的标准写法，方向本来就是反的）、
# `tool_id ← name`（MCP 协议自己的 `tools/call.name` 指「要调哪个工具」，留着会把
# 错层写法收成假命中）。
DEFAULT_ARG_ALIASES: dict[str, tuple[str, ...]] = {
    "tool_id": ("id",),
    "keyword": ("query",),
    "package": ("name",),
}

# 必填缺失时消息里给的示例取值：让 Agent 一次就照抄对，不用再猜第二轮。
_EXAMPLE_VALUES: dict[str, str] = {
    "tool_id": "everything",
    "tool_name": "search_files",
    "capability_id": "search_files",
    "keyword": "文件",
    "package": "7zip",
    "level": "warning",
    "action_id": "<动作 ID>",
    "action_name": "<动作名称>",
}


def _declared_params(spec: ServerToolSpec) -> list[str]:
    """按**声明顺序**返回参数名。

    不能图省事返回 `set`：`aliases_for` 按它逐个追写别名属性，而 set 的迭代序
    随进程哈希随机（字符串 hash 默认每个进程不同），导出的 `properties` 顺序
    就会在两次运行之间漂移——`tools/golden/tools_list.json` 是逐字节比对的，
    漂移等于让基线检查变成掷骰子。
    """
    return list(spec.input_schema.get("properties", {}))


def aliases_for(spec: ServerToolSpec) -> dict[str, tuple[str, ...]]:
    """本条工具实际生效的「规范名 → 别名」表（默认表 + spec 覆盖，已去掉空组）。"""
    declared = _declared_params(spec)
    overrides = spec.arg_aliases or {}
    out: dict[str, tuple[str, ...]] = {}
    for canon in declared:
        aliases = overrides[canon] if canon in overrides else DEFAULT_ARG_ALIASES.get(canon, ())
        aliases = tuple(a for a in aliases if a)
        if aliases:
            out[canon] = aliases
    return out


def _blank(value: Any) -> bool:
    """None / 空串 / 纯空白都算「没给」——Agent 传 `{"id": ""}` 不是有效目标。"""
    return value is None or (isinstance(value, str) and not value.strip())


def export_input_schema(spec: ServerToolSpec) -> dict:
    """把声明表的规范名 schema 展开为对外 schema（补别名属性 + 必填任选组）。

    无别名的工具原样返回同一个 dict（零拷贝、零差异）；有别名时返回新 dict，
    声明表本身不被改动。
    """
    schema = spec.input_schema
    alias_map = aliases_for(spec)
    if not alias_map:
        return schema

    properties = dict(schema.get("properties", {}))
    for canon, aliases in alias_map.items():
        canon_desc = properties.get(canon, {}).get("description", "")
        for alias in aliases:
            properties[alias] = {
                "type": "string",
                "description": (
                    f"`{canon}` 的别名（与 {canon} 二选一，两者都给以 {canon} 为准）"
                    + (f"：{canon_desc}" if canon_desc else "")
                ),
            }

    required = [r for r in schema.get("required", [])]
    exported = {**schema, "properties": properties}
    # 一个必填规范名 = 一个「任选其一」组：组内每个候选名各自成一条
    # `{"required": [名]}`（JSON Schema 的 `required` 是 AND，把四个名字塞进
    # 同一个 required 等于要求全传——test_protocol_roundtrip 会当场红）。
    # 组与组之间是 AND，多组时用 `allOf` 包起来。
    groups = []
    plain = []
    for canon in required:
        aliases = alias_map.get(canon)
        if aliases:
            groups.append([{"required": [name]} for name in (canon, *aliases)])
        else:
            plain.append(canon)
    if plain:
        exported["required"] = plain
    else:
        exported.pop("required", None)
    if len(groups) == 1:
        exported["anyOf"] = groups[0]
    elif len(groups) > 1:
        exported["allOf"] = [{"anyOf": g} for g in groups]
    return exported


def export_description(spec: ServerToolSpec) -> str:
    """把**必填参数名**（及其可接受的别名）追加进对外 description。

    这是比别名属性更便宜的一层，来自根因勘察：宿主在延迟加载下只把工具名交给
    模型（要 schema 得再调一次发现接口），而改造前实测**17 个带必填参数的工具，
    描述里一个参数名都没提**——模型没拿到词汇，只能按生态先验写 `query` / `id`。
    description 是裁剪时最先保住、也几乎唯一保得住的文本，所以把必填名写进去。
    成本实测：全表 +334 字符（平均每工具 15，最大 `call_backend_tool` +37），
    对比别名属性的平均 111 字符/个。

    只补**没提过**的必填名（描述里已经出现该词就不重复）；别名只带第一个，
    再多就是拿上下文换覆盖率。声明表本身不被改动。
    """
    base = spec.description
    alias_map = aliases_for(spec)
    hints = []
    for name in spec.input_schema.get("required", []):
        if name in base:
            continue
        aliases = alias_map.get(name)
        hints.append(f"{name}（可写 {aliases[0]}）" if aliases else name)
    if not hints:
        return base
    sep = "" if base.endswith("。") else "。"
    return f"{base}{sep}必填：{'、'.join(hints)}"


def normalize_arguments(spec: ServerToolSpec, arguments: dict) -> dict:
    """别名折回规范名 + 必填缺失的自描述错误（dispatch 入口调用）。

    返回新 dict，不改调用方传入的对象。三条规则：
    1. 只给别名 → 搬到规范名，别名键从结果里移除；
    2. 规范名与别名同时给、取值相同 → 按规范名走，不报错；取值**不同** →
       `invalid_params`（两个目标 ID 不猜意图，让调用方自己收敛）；
    3. 必填规范名折完仍为空 → `invalid_params`，消息列出可接受参数名与示例。
    """
    alias_map = aliases_for(spec)
    out = dict(arguments or {})
    for canon, aliases in alias_map.items():
        sources = []
        canon_value = out.get(canon)
        if not _blank(canon_value):
            sources.append((canon, canon_value))
        for alias in aliases:
            if alias not in out:
                continue
            value = out.pop(alias)
            if not _blank(value):
                sources.append((alias, value))
        if not sources:
            continue
        if len({value for _, value in sources}) > 1:
            fail(
                f"参数 {canon} 同时由 "
                + "、".join(f"`{key}`" for key, _ in sources)
                + " 给出且取值不同（"
                + " vs ".join(repr(v) for _, v in sources)
                + "）——只保留一个即可。",
                param=canon,
                acceptedParams=[canon, *aliases],
            )
        out[canon] = sources[0][1]
    for canon in spec.input_schema.get("required", []):
        if _blank(out.get(canon)):
            accepted = [canon, *alias_map.get(canon, ())]
            example = {canon: _EXAMPLE_VALUES.get(canon, "<值>")}
            fail(
                f"缺少必填参数 {canon}。可接受的参数名："
                + " / ".join(f"`{a}`" for a in accepted)
                + f"（任选其一，规范名是 {canon}）。示例：{json.dumps(example, ensure_ascii=False)}。"
                + f"本工具用途：{spec.description}",
                param=canon,
                acceptedParams=accepted,
                example=example,
            )
    # mcp 1.x 的 lowlevel Server 会在 handler 前自动按 inputSchema 做
    # JSON-Schema 校验；v2 明确「schema 只下发、不 enforce」。这里补回 enum 这一
    # 维度：拼错的枚举值在协议面就拒，不会退化成「筛选类接口最坏的失败——把
    # 参数打错读成本机没装」。错误正文含 "is not one of"，与 SDK 原话术对齐。
    for key, prop in spec.input_schema.get("properties", {}).items():
        if not isinstance(prop, dict):
            continue
        allowed = prop.get("enum")
        if allowed is None or key not in out:
            continue
        value = out[key]
        if _blank(value):
            continue
        if value not in allowed:
            fail(
                f"参数 {key} 的值 {value!r} is not one of {list(allowed)}",
                param=key,
                acceptedValues=list(allowed),
            )
    return out


# ---- handlers（M03 分批搬迁）----
# 分支体从 server.dispatch_tool 逐字搬运，只做五类机械替换：
#   registry. → ctx.registry.、backend_manager. → ctx.backend_manager.、
#   launcher_manager. → ctx.launcher_manager.、store. → ctx.store.、log( → ctx.log(
# handler 返回**载荷对象**（dict）而非 JSON 文本——序列化统一由
# server.dispatch_tool 负责。本模块禁止出现裸 manager 名（R1：漏做替换
# 必报 NameError，第一批测试即红）。


async def _handle_search_tools(arguments: dict, ctx: ToolContext) -> dict:
    keyword = arguments.get("keyword", "")
    kind = arguments.get("kind")
    tools = ctx.registry.search_tools(
        keyword,
        kind,
        arguments.get("status"),
        arguments.get("publisher"),
    )
    return {
        "success": True,
        "keyword": keyword,
        "count": len(tools),
        "tools": [
            {
                "id": t["id"],
                "name": t.get("displayName", t["name"]),
                "kind": t.get("kind", "unknown"),
                "description": t.get("description", "")[:100],
                "status": t.get("status", "installed"),
                # 可筛选的维度必须同时可见：只让按 publisher 筛却不返回它，
                # 调用方就只能猜发布者写法
                "publisher": t.get("publisher", "")
            }
            for t in tools
        ]
    }


async def _handle_list_tools(arguments: dict, ctx: ToolContext) -> dict:
    kind = arguments.get("kind")
    tools = ctx.registry.list_all_tools(
        kind,
        arguments.get("status"),
        arguments.get("publisher"),
    )
    return {
        "success": True,
        "count": len(tools),
        "tools": [
            {
                "id": t["id"],
                "name": t.get("displayName", t["name"]),
                "kind": t.get("kind", "unknown"),
                "description": t.get("description", "")[:100],
                "status": t.get("status", "installed"),
                "publisher": t.get("publisher", "")
            }
            for t in tools
        ]
    }


async def _handle_get_tool_info(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    tool = ctx.registry.get_tool(tool_id)

    if not tool:
        fail(f"未找到工具: {tool_id}", error_type=ErrorType.NOT_FOUND, toolId=tool_id)

    kind = tool.get("kind", "unknown")
    result_data = {
        "success": True,
        "id": tool["id"],
        "name": tool.get("displayName", tool["name"]),
        "kind": kind,
        "status": tool.get("status", "unknown"),
        "description": tool.get("description", "")
    }

    if kind == "app":
        result_data["version"] = tool.get("version", "未知")
        result_data["tags"] = tool.get("tags", [])
        if tool.get("launch"):
            result_data["launch"] = tool["launch"]

    elif kind == "mcp":
        backend_res = ctx.backend_manager.get_backend_status(tool_id)
        if backend_res.get("success"):
            result_data["backend"] = backend_res.get("status", {})

        backend_tools = ctx.registry.list_backend_tools(tool_id)
        result_data["backendTools"] = [
            {
                "name": t["name"],
                "description": t.get("description", "")
            }
            for t in backend_tools
        ]

    return result_data


async def _handle_launch_tool(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    res = ensure_success(ctx.registry.launch_tool(tool_id))
    if not arguments.get("execute"):
        # 默认路径逐字保持旧行为：只返回命令，不 spawn（e2e 场景②钉住的
        # 硬契约）。execute 缺省/False 时本函数与改造前完全等价。
        return res
    # 执行路径：调用方显式 execute=true 才启动。条目级 destructive 门禁走
    # policy（与 call_capability 同一判定入口）；
    # install 门禁（ARC-P0-01）防的是「无意图执行」，本路径的意图就是
    # 这次显式调用本身。
    target_tool = ctx.registry.get_tool(tool_id)
    decision = decide_destructive("app", target_tool, {}, arguments)
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行启动: {tool_id}")

    ctx.log(f"执行启动: {tool_id}")
    exec_res = await launch_app(
        res["launch"], tool_id, res.get("toolName", ""), arguments.get("wait")
    )
    res.update(exec_res)
    # 执行失败（退出码非 0 等）与其它工具的失败载荷同路：转协议层 isError
    return ensure_success(res)


async def _handle_get_tool_help(arguments: dict, ctx: ToolContext) -> dict:
    """取工具的帮助信息。

    默认路径**零副作用**：文档链接 + 能力清单（mcp 子工具 / launcher 动作）+
    将要执行的帮助命令预览。显式 `run=true` 且 `kind=app` 才真的 spawn 捕获输出
    ——与 `launch_tool` 的「默认只返回」同一取舍。
    """
    tool_id = arguments.get("tool_id", "")
    tool = ctx.registry.get_tool(tool_id)
    if not tool:
        fail(f"未找到工具: {tool_id}", error_type=ErrorType.NOT_FOUND, toolId=tool_id)

    kind = tool.get("kind", "unknown")
    payload = {
        "success": True,
        "toolId": tool_id,
        "toolName": tool.get("displayName", tool.get("name", "")),
        "kind": kind,
    }
    payload.update(entry_help(tool))

    if kind == "mcp":
        backend_tools = ctx.registry.list_backend_tools(tool_id)
        payload["backendTools"] = backend_summary(backend_tools)
        payload["backendToolCount"] = len(backend_tools)
        if not backend_tools:
            payload["note"] = (
                "能力缓存为空：先 start_runtime 或 sync_capabilities 再取帮助"
            )
    elif kind == "launcher":
        catalog = ctx.store.load_launcher_catalog(tool_id) or {}
        actions = catalog.get("actions") or []
        payload["actions"] = launcher_summary(actions)
        payload["actionCount"] = len(actions)
        payload["lastSynced"] = catalog.get("lastSynced")
        payload["source"] = catalog.get("source")
        if not actions:
            payload["note"] = "动作清单缓存为空：先 sync_capabilities 再取帮助"

    if not arguments.get("run"):
        return payload

    if kind != "app":
        # 缓存里已有的就是它的全部「帮助」，没有可执行的 --help 这回事
        payload["note"] = f"kind={kind} 没有可执行的帮助命令，已返回数据层来源"
        return payload

    resolved = resolve_help_command(tool, arguments.get("timeout"))
    if not resolved:
        fail(
            f"工具 '{tool_id}' 未配置帮助来源（launch.command / help.command 均缺失）",
            error_type=ErrorType.CONFIG_ERROR,
            toolId=tool_id,
        )

    # 跑 --help 同样是一次 spawn：条目 destructive → 需 allowDestructive
    # （与 launch_tool execute 走同一判定入口）。这不是过度设防——图形程序
    # 不认 --help 时会把它当普通启动参数跑起来。
    decision = decide_destructive("app", tool, {"action": "get_tool_help"}, arguments)
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行帮助命令: {tool_id}")

    ctx.log(f"执行帮助命令: {tool_id}")
    exec_res = await run_help_command(resolved)
    payload.update(exec_res)
    # 帮助命令自身失败（命令缺失 / 无输出 / 超时）照旧转协议层 isError
    return ensure_success(payload)


async def _handle_list_capabilities(arguments: dict, ctx: ToolContext) -> dict:
    return await capabilities.list_capabilities(
        ctx,
        arguments.get("tool_id", ""),
        keyword=arguments.get("keyword"),
        limit=arguments.get("limit"),
        refresh=bool(arguments.get("refresh")),
    )


async def _handle_call_capability(arguments: dict, ctx: ToolContext) -> dict:
    return await capabilities.call_capability(
        ctx,
        arguments.get("tool_id", ""),
        arguments.get("capability_id", ""),
        arguments.get("arguments") or {},
        execute=arguments.get("execute"),
        allow_destructive=bool(arguments.get("allowDestructive")),
        wait=arguments.get("wait"),
    )


async def _handle_sync_capabilities(arguments: dict, ctx: ToolContext) -> dict:
    return await capabilities.sync_capabilities(ctx, arguments.get("tool_id", ""))


async def _handle_start_runtime(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"启动运行时: {tool_id}")
    return ensure_success(await ctx.backend_manager.start_backend(tool_id))


async def _handle_stop_runtime(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"停止运行时: {tool_id}")
    return ensure_success(await ctx.backend_manager.stop_backend(tool_id))


async def _handle_get_runtime_status(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id")
    return ensure_success(ctx.backend_manager.get_backend_status(tool_id))


async def _handle_get_runtime_logs(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    level = arguments.get("level")
    limit = arguments.get("limit", 50)
    return ensure_success(ctx.backend_manager.get_backend_logs(tool_id, level, limit))


async def _handle_set_runtime_log_level(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    level = arguments.get("level", "")
    return ensure_success(ctx.backend_manager.set_backend_log_level(tool_id, level))


async def _handle_get_usage_stats(arguments: dict, ctx: ToolContext) -> dict:
    days = arguments.get("days")
    only_target = str(arguments.get("tool_id") or "").strip()
    window = usage.window(days)
    rows = usage.read_rows(window["days"])
    if only_target:
        rows = [r for r in rows if str(r.get("target") or "") == only_target]
    stats = usage.aggregate(rows)
    # 聚合视图**嵌在 stats 下**而不是平铺：`aggregate` 自己有一个 `success`
    # （成功次数，int），平铺会把载荷信封的 `success: true`（bool）覆盖掉——
    # 那是 SHARED_OUTPUT_SCHEMA 的唯一必填字段，所有工具共用同一条语义。
    payload = {
        "success": True,
        "logPath": str(usage.USAGE_LOG),
        "window": window,
        "stats": stats,
    }
    if only_target:
        payload["toolId"] = only_target
    if stats["total"] == 0:
        # 空窗口要如实说明覆盖范围，否则「0 次调用」会被读成「这个工具没人用」
        payload["note"] = (
            "窗口内没有调用记录。统计只覆盖经本 server 分发的调用："
            "用户直接启动工具、其他进程或其他日期的历史调用都不在计数内"
        )
    return payload


async def _handle_discover_tools(arguments: dict, ctx: ToolContext) -> dict:
    """按声明清单发现本机工具：默认只做文件系统读，不 spawn 任何进程"""
    run_commands = bool(arguments.get("run_commands"))
    status = str(arguments.get("status") or "").strip()
    try:
        manifest = load_manifest()
    except DiscoveryError as exc:
        # 清单坏了只让这一次调用失败：发现不是安全面，不该影响其他工具
        fail(str(exc), error_type=ErrorType.CONFIG_ERROR)
    result = discover(manifest, ctx.registry, run_commands=run_commands)
    candidates = result["candidates"]
    if status:
        candidates = [c for c in candidates if c["status"] == status]
    payload = {
        "success": True,
        "manifestPath": str(resolve_discovery_path()),
        "runCommands": run_commands,
        "summary": result["summary"],
        "returned": len(candidates),
        "candidates": candidates,
        "note": note_for(result),
    }
    if status:
        # summary 计的是**整份清单**，filter 只裁 candidates；不标注筛了什么，
        # 调用方会把「summary.installed=3 / 返回 1 条」读成结果自相矛盾
        payload["statusFilter"] = status
    if not manifest.get("candidates"):
        payload["note"] = (
            f"发现清单为空或文件不存在（{resolve_discovery_path()}）："
            "没有候选可探测。这是「还没声明要找什么」，不是「本机没有工具」"
        )
    return payload


# ---- 包管理面（P2-07）：scoop 先行，四个工具共用一个根目录解析 ----


def _resolve_pkg_root(arguments: dict) -> tuple[str, str]:
    """四个包管理工具共用：定位安装器根目录。

    定不出来一律 `install_required`：话术必须区分「这台机器上没有这个安装器」
    和「这个安装器里没装包」——后者是空清单，是正常结果（`list_packages` 走那条）。
    """
    manager = str(arguments.get("manager") or "scoop")
    try:
        root, reason = pkgman.resolve_root(manager)
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type)
    if root is None:
        fail(
            f"定位不到 {manager} 的安装根目录：{reason}",
            error_type=ErrorType.INSTALL_REQUIRED,
            manager=manager,
        )
    return root, reason


async def _handle_list_packages(arguments: dict, ctx: ToolContext) -> dict:
    """列已安装的包：**纯读盘**（读 apps 下的 manifest / install.json，不跑 scoop list）"""
    root, root_reason = _resolve_pkg_root(arguments)
    state = str(arguments.get("state") or "").strip()
    rows = pkgman.list_installed(root)
    states: dict[str, int] = {}
    for r in rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
    selected = [r for r in rows if r["state"] == state] if state else rows
    payload = {
        "success": True,
        "manager": "scoop",
        "root": root,
        "rootSource": root_reason,
        "count": len(rows),
        "returned": len(selected),
        "states": states,
        "launchable": sum(1 for r in rows if r["launchable"]),
        "packages": [pkgman.brief(r) for r in selected],
    }
    if state:
        # 与 discover_tools 的 status 过滤同口径：count 永远是整份清单的，
        # 不标筛选条件就会被读成「筛完还剩这么多」
        payload["stateFilter"] = state
    if not rows:
        payload["note"] = (
            f"{root}\\apps 下没有任何包目录。这是「这个安装器还没装东西」，"
            "不是「读不到」——读不到的路径会在 rootSource 里说明"
        )
    elif states.get(pkgman.STATE_CURRENT_BROKEN) or states.get(pkgman.STATE_NO_MANIFEST):
        payload["note"] = (
            "state 说明：ok=current 连接点正常；current_broken=current 已断链"
            "（数据取自残留的版本目录，包可能实际不可用）；no_manifest=目录存在"
            "但没有 manifest（scoop 自身就是这种）。三种都用同一组字段，"
            "差别在 state 与 currentAlive"
        )
    return payload


async def _handle_search_packages(arguments: dict, ctx: ToolContext) -> dict:
    """在已添加 bucket 的清单里搜可安装的包：**纯读盘**，命中按文件名筛后再读 JSON"""
    root, _ = _resolve_pkg_root(arguments)
    try:
        result = pkgman.search_packages(
            root,
            str(arguments.get("keyword") or ""),
            arguments.get("limit"),
        )
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type, manager="scoop")
    payload = {
        "success": True,
        "manager": "scoop",
        "root": root,
        "keyword": str(arguments.get("keyword") or "").strip(),
        "count": len(result["rows"]),
        "scannedBuckets": result["scannedBuckets"],
        "scannedManifests": result["scannedManifests"],
        "limit": result["limit"],
        "packages": result["rows"],
    }
    if not result["rows"]:
        payload["note"] = (
            f"在 {result['scannedBuckets']} 个已添加 bucket 的 "
            f"{result['scannedManifests']} 份清单里没有匹配项。"
            "这是「已知 bucket 里没有」，不是「装不了」——scoop 还能 bucket add / checkver"
        )
    return payload


async def _handle_launch_package(arguments: dict, ctx: ToolContext) -> dict:
    """启动一个已安装的包。

    默认路径**不 spawn**：只回「会起哪个入口、为什么是它」（候选带 exists /
    runnable 两个标记）。显式 `execute=true` 才真的起，起法与 `launch_tool`
    同一条路（`app_launcher.launch_app`，stdio 全 DEVNULL + 保活注册表）。
    """
    root, root_reason = _resolve_pkg_root(arguments)
    try:
        name = pkgman.validate_name(arguments.get("package"))
        row = pkgman.find_installed(root, name)
        if row is None:
            fail(
                f"scoop 里没有安装 '{name}'（{root}\\apps\\{name} 不存在）。"
                "先用 search_packages 确认可装包名，再 install_package",
                error_type=ErrorType.NOT_FOUND,
                manager="scoop",
                package=name,
            )
        entry = arguments.get("entry")
        target = pkgman.launch_target(row, str(entry) if entry else None)
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type, manager="scoop")
    if not target["ok"]:
        # 连预览都挑不出可起的入口：把 code / reason / 候选一起回，
        # 调用方看到的是「差在哪」，不是一个光秃秃的失败
        fail(
            f"无法确定 '{row['name']}' 的可启动入口：{target['reason']}",
            error_type=ErrorType.RUNTIME_ERROR,
            manager="scoop",
            package=row["name"],
            state=row["state"],
            code=target["code"],
            candidates=target["candidates"],
        )

    payload = {
        "success": True,
        "manager": "scoop",
        "root": root,
        "package": row["name"],
        "state": row["state"],
        "version": row["version"],
        "kind": target["kind"],
        "path": target["path"],
        "candidates": target["candidates"],
        "rootSource": root_reason,
    }
    if not arguments.get("execute"):
        payload["executed"] = False
        payload["note"] = (
            "未执行：传 execute=true 才会真的启动。入口按 快捷方式 → bin → shim 取，"
            "指定某个入口用 entry（候选里的 name 或文件名）"
        )
        return payload

    decision = decide_destructive(
        "package", {"id": f"scoop:{row['name']}"},
        {"action": "launch_package", "package": row["name"]}, arguments,
    )
    if decision["blocked"]:
        fail(decision["reason"], error_type=decision["error_type"],
             package=row["name"])
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行包启动: {row['name']}")

    ctx.log(f"执行包启动: {row['name']} -> {target['path']}")
    exec_res = await launch_app(
        {"type": "executable", "command": target["path"]},
        f"scoop/{row['name']}",
        row["name"],
        arguments.get("wait"),
    )
    payload.update(exec_res)
    return ensure_success(payload)


async def _handle_install_package(arguments: dict, ctx: ToolContext) -> dict:
    """安装一个包。

    默认路径**不执行**：只回将要跑的命令（argv）。真的装要同时满足两件事——
    显式 `execute=true`（意图）+ `allowDestructive=true`（授权）：安装会下载并跑
    bucket 里的安装脚本、改本机已装清单，属 `policy` 判定表里的破坏性操作，
    策略 `destructive.mode=deny` 时客户端无法自行解除。
    """
    root, root_reason = _resolve_pkg_root(arguments)
    try:
        name = pkgman.validate_name(arguments.get("package"))
        argv = pkgman.install_argv(root, name)
        already = pkgman.find_installed(root, name)
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type, manager="scoop")

    payload = {
        "success": True,
        "manager": "scoop",
        "root": root,
        "package": name,
        "argv": argv,
        "rootSource": root_reason,
        "alreadyInstalled": bool(already),
    }
    if already:
        payload["state"] = already["state"]
        payload["version"] = already["version"]

    if not arguments.get("execute"):
        payload["executed"] = False
        payload["note"] = (
            "未执行：安装会改动本机。要真的装，需同时传 execute=true 与 "
            "allowDestructive=true"
        )
        return payload

    decision = decide_destructive(
        "package", {"id": f"scoop:{name}"},
        {"action": "install_package", "package": name}, arguments,
    )
    if decision["blocked"]:
        fail(decision["reason"], error_type=decision["error_type"], package=name)
    ctx.log(f"⚠️ 已确认执行包安装: {name}（{argv}）")

    try:
        run = await pkgman.run_argv(argv, arguments.get("timeout"))
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type, package=name, argv=argv)

    after = pkgman.find_installed(root, name)
    payload.update(
        {
            "executed": True,
            "exitCode": run["exitCode"],
            "timedOut": run["timedOut"],
            "stdout": run["stdout"],
            "stderr": run["stderr"],
            "installed": after,
        }
    )
    if not run["ok"]:
        payload["success"] = False
        payload["errorType"] = ErrorType.RUNTIME_ERROR
        payload["error"] = (
            f"scoop install 未成功（退出码 {run['exitCode']}"
            + ("，已超时终止" if run["timedOut"] else "")
            + "）；stdout/stderr 见本载荷"
        )
        return payload
    if after is None:
        payload["note"] = (
            "命令退出码 0，但 apps 目录下仍读不到这个包——装到了别处"
            "（如 --global 装的目录）或清单尚未落盘，不要当成安装成功"
        )
    return payload


async def _uniget_update_probe(manager: str, timeout: Any) -> dict:
    """跑一遍 UniGetUI 的只读升级查询，载荷收敛成 `check_package_updates` 的形状。

    会话探测、输出上限、截断与解析失败的处理都在 `pkgman.run_uniget_read`；
    这里只做「可用→行 + truncated / 不可用→原因」的转手，不复制第二份协议。
    """
    probe = await pkgman.run_uniget_read(
        lambda exe: pkgman.uniget_updates_argv(exe, manager or None),
        timeout,
        parse=pkgman.parse_uniget_updates,
    )
    if not probe["available"]:
        return {"available": False, "exe": probe["exe"], "reason": probe["reason"]}
    return {
        "available": True,
        "exe": probe["exe"],
        "rows": probe["data"]["rows"],
        "truncated": probe["data"]["truncated"],
    }


async def _handle_check_package_updates(arguments: dict, ctx: ToolContext) -> dict:
    """查「哪些包有可用更新」，默认口径**零 spawn**。

    两条路共用一个工具，但只有一条是默认值：
    - 默认读盘：scoop 的已装版本 vs 本地 bucket 索引里的版本，不 spawn、不联网；
    - `execute=true` 才问 UniGetUI（`uniget package updates`，只读，但它自己会
      spawn 底层安装器并联网，且必须连一个活的会话）。探不到会话就退回读盘层，
      并把「为什么退回来」原样带回 `uniget.reason`，不静默换口径。
    """
    manager_arg = str(arguments.get("manager") or "").strip().lower()
    if manager_arg:
        try:
            manager_arg = pkgman.validate_uniget_manager(manager_arg)
        except pkgman.PackageError as exc:
            fail(str(exc), error_type=exc.error_type, manager=manager_arg)

    uniget: dict[str, Any] = {"available": False, "exe": None}
    if arguments.get("execute"):
        uniget = await _uniget_update_probe(
            manager_arg, arguments.get("timeout")
        )
    else:
        uniget["reason"] = "未启用外部查询：UniGetUI 口径需要显式 execute=true"

    if uniget["available"]:
        return {
            "success": True,
            "engine": "uniget",
            "manager": manager_arg or None,
            "count": len(uniget["rows"]),
            "updates": uniget["rows"],
            "truncated": uniget["truncated"],
            "cli": uniget["exe"],
            "note": (
                "这份清单来自 UniGetUI（它会 spawn 底层安装器并联网比版本），"
                "只含**可升级项**，已最新的包不在其中，所以没有总数；"
                "要和本机 scoop 的读盘差集对照，请用 execute=false"
            ),
        }

    if manager_arg and manager_arg != "scoop":
        # 读盘层覆盖不到这个安装器：空清单会被读成「没有更新」，必须显式失败
        fail(
            f"{manager_arg} 的升级查询只能由 UniGetUI 提供，而它这次不可用："
            f"{uniget.get('reason')}（纯读盘口径目前只覆盖 scoop）",
            error_type=ErrorType.INSTALL_REQUIRED,
            manager=manager_arg,
        )

    try:
        root, root_reason = pkgman.resolve_root("scoop")
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type, manager="scoop")
    if root is None:
        fail(
            f"定位不到 scoop 的安装根目录：{root_reason}；"
            f"UniGetUI 也不可用（{uniget.get('reason')}）",
            error_type=ErrorType.INSTALL_REQUIRED,
            manager="scoop",
        )

    diff = pkgman.read_disk_updates(root)
    payload: dict[str, Any] = {
        "success": True,
        "engine": "read_disk",
        "manager": "scoop",
        "root": root,
        "rootSource": root_reason,
        "scanned": diff["scanned"],
        "count": len(diff["rows"]),
        "updates": diff["rows"],
        "unknown": diff["unknown"],
        "truncated": diff["truncated"],
        "cli": uniget.get("exe"),
        "uniget": uniget,
    }
    if not diff["rows"]:
        payload["note"] = (
            "读盘差集为空：本地 bucket 索引里没有比已装版本更高的条目。"
            "这只说明**索引认为**没更新——索引新旧取决于上次 `scoop update`，"
            "不等于上游最新发布；要联网比版本用 execute=true"
        )
    else:
        payload["note"] = (
            f"口径：已装版本 vs 本地 bucket 索引里的版本（{diff['scanned']} 个已装包里 "
            f"{len(diff['rows'])} 个更高）。索引本身的新旧取决于上次 `scoop update`，"
            "不等于上游最新发布；unknown 里的包没有可比基准，不代表它们已最新"
        )
    return payload


# ---- UniGetUI 只读意图工具（full）----
# 这四个工具回答「装了什么 / 能装什么 / 这个包是什么 / 哪些安装器可用」，
# 全部走 `run_uniget_read` 的同一条会话探测与输出上限纪律；安装/升级/卸载不在此列。

def _uniget_manager_arg(arguments: dict) -> str:
    raw = arguments.get("manager")
    if not str(raw or "").strip():
        fail(
            "manager 必填：UniGetUI 的查询要明确问哪一个安装器（如 scoop / winget）",
            error_type=ErrorType.INVALID_PARAMS,
        )
    try:
        return pkgman.validate_uniget_manager(raw)
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type, manager=str(raw))


def _uniget_or_fail(probe: dict, what: str, **extra: Any) -> dict:
    """把 `run_uniget_read` 的失败相位折算成客户端可行动的 errorType。

    相位是「卡在哪」：locate 没装 CLI → install_required；status 没活会话 →
    connection_error；query/parse 命令失败或形状不对 → runtime_error（解析器
    若识别出「包不存在」会带 not_found）。reason 原样带上，不做文案再加工。
    """
    if probe["available"]:
        return probe["data"]
    fail(
        f"{what}：{probe['reason']}",
        error_type=probe.get("errorType") or ErrorType.RUNTIME_ERROR,
        phase=probe["phase"],
        cli=probe["exe"],
        **extra,
    )


async def _handle_list_package_managers(arguments: dict, ctx: ToolContext) -> dict:
    """列 UniGetUI 能驱动的安装器及其就绪状态（只读；需要活的 UniGetUI 会话）"""
    timeout = arguments.get("timeout")
    probe = await pkgman.run_uniget_read(
        pkgman.uniget_managers_argv, timeout,
        parse=pkgman.parse_uniget_managers,
    )
    rows = _uniget_or_fail(probe, "列 UniGetUI 安装器失败")["rows"]
    payload: dict[str, Any] = {
        "success": True,
        "source": "uniget",
        "cli": probe["exe"],
        "count": len(rows),
        "readyCount": sum(1 for r in rows if r["ready"]),
        "managers": rows,
    }
    if arguments.get("include_sources"):
        sources = await pkgman.run_uniget_read(
            pkgman.uniget_sources_argv, timeout,
            parse=pkgman.parse_uniget_sources,
        )
        if sources["available"]:
            payload["sources"] = sources["data"]["rows"]
            payload["sourceCount"] = len(sources["data"]["rows"])
        else:
            # 已经拿到安装器清单，软件源查不到不该把整次调用判失败
            payload["sourcesUnavailable"] = sources["reason"]
    payload["note"] = (
        "ready=false 表示该安装器本次不可用（未安装/未配置/会话里没就绪）；"
        "enabled 是 UniGetUI 设置里的开关，与本次查询能否成功不是一回事。"
        "本工具需要 UniGetUI 正在运行，不会替你冷启动它的窗口"
    )
    return payload


async def _handle_search_uniget_packages(arguments: dict, ctx: ToolContext) -> dict:
    """在指定安装器的远端索引里搜包（只读）。manager 必填，见 pkgman 的实测说明"""
    manager = _uniget_manager_arg(arguments)
    try:
        query = pkgman.validate_query(arguments.get("keyword"))
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type, manager=manager)
    limit = _clamp_int(
        arguments.get("limit"),
        default=pkgman.DEFAULT_SEARCH_LIMIT,
        low=1,
        high=pkgman.MAX_SEARCH_LIMIT,
    )
    probe = await pkgman.run_uniget_read(
        lambda exe: pkgman.uniget_search_argv(exe, manager, query),
        arguments.get("timeout"),
        parse=lambda text: pkgman.parse_uniget_packages(text, "uniget package search"),
    )
    rows = _uniget_or_fail(
        probe, f"在 {manager} 里搜 '{query}' 失败", manager=manager,
    )["rows"]
    kept = rows[:limit]
    return {
        "success": True,
        "source": "uniget",
        "manager": manager,
        "keyword": query,
        "count": len(rows),
        "returned": len(kept),
        "limit": limit,
        "truncated": len(rows) > limit,
        "cli": probe["exe"],
        "packages": kept,
        "note": (
            "结果来自该安装器的远端索引，只说明**可安装**；"
            "是否已装用 list_uniget_packages、包详情用 get_package_details"
        ),
    }


async def _handle_list_uniget_packages(arguments: dict, ctx: ToolContext) -> dict:
    """列指定安装器里已安装的包（只读）。manager 必填，结果按规范名排序"""
    manager = _uniget_manager_arg(arguments)
    limit = _clamp_int(arguments.get("limit"), default=50, low=1, high=100)
    probe = await pkgman.run_uniget_read(
        lambda exe: pkgman.uniget_installed_argv(exe, manager),
        arguments.get("timeout"),
        parse=pkgman.parse_uniget_packages,
    )
    rows = sorted(
        _uniget_or_fail(probe, f"列 {manager} 已装包失败", manager=manager)["rows"],
        key=lambda r: r["name"].lower(),
    )
    kept = rows[:limit]
    return {
        "success": True,
        "source": "uniget",
        "manager": manager,
        "count": len(rows),
        "returned": len(kept),
        "limit": limit,
        "truncated": len(rows) > limit,
        "cli": probe["exe"],
        "packages": kept,
        "note": (
            "这份清单来自 UniGetUI 的安装器适配层，与读盘口径（list_packages）"
            "可能不等：直链安装、安装器本体等条目两侧处理方式不同"
        ),
    }


async def _handle_get_package_details(arguments: dict, ctx: ToolContext) -> dict:
    """查单个包的详情，可选带版本历史（只读）"""
    manager = _uniget_manager_arg(arguments)
    try:
        package_id = pkgman.validate_package_id(arguments.get("package"))
    except pkgman.PackageError as exc:
        fail(str(exc), error_type=exc.error_type, manager=manager)
    timeout = arguments.get("timeout")
    probe = await pkgman.run_uniget_read(
        lambda exe: pkgman.uniget_details_argv(exe, manager, package_id),
        timeout,
        parse=pkgman.parse_uniget_details,
    )
    details = _uniget_or_fail(
        probe, f"查 '{package_id}' 的详情失败",
        manager=manager, package=package_id,
    )["package"]
    payload: dict[str, Any] = {
        "success": True,
        "source": "uniget",
        "manager": manager,
        "package": package_id,
        "cli": probe["exe"],
        "details": details,
    }
    if arguments.get("include_versions") is not False:
        versions = await pkgman.run_uniget_read(
            lambda exe: pkgman.uniget_versions_argv(exe, manager, package_id),
            timeout,
            parse=pkgman.parse_uniget_versions,
        )
        if versions["available"]:
            payload["versions"] = versions["data"]["versions"]
            payload["versionCount"] = versions["data"]["count"]
            payload["versionsTruncated"] = versions["data"]["truncated"]
        else:
            # 详情已经拿到了，版本历史查不到只降级，不把整次调用判失败
            payload["versionsUnavailable"] = versions["reason"]
    payload["note"] = (
        "详情来自该安装器的远端索引；installLocation 等字段反映当前安装状态。"
        "versions 为空通常表示该安装器不提供版本历史（scoop 实测如此），不是查询失败"
    )
    return payload


# ---- 意图级前门工具（default）与低频补充工具（full）----
# 这些工具仍是同一张声明表里的普通条目：policy / usage / 生命周期走同一条
# dispatch 出口。它们只做「解析目标 → 门禁 → 调用既有能力」，不复制后端实现。

def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        fail(f"limit 必须是整数（收到: {value!r}）", error_type=ErrorType.INVALID_PARAMS)
    return max(low, min(number, high))


async def _call_backend_json(
    ctx: ToolContext, tool_id: str, tool_name: str, arguments: dict
) -> dict:
    """调用后端子工具，并把后端返回的 JSON 文本展开成载荷。

    后端（Everything / winget）返回的都是 `{success, ...}` 的 JSON 文本；
    解析失败时保留 `result` 原文，不假装结构化。
    """
    res = ensure_success(
        await ctx.backend_manager.call_backend_tool(tool_id, tool_name, arguments)
    )
    text = res.get("result")
    payload: dict[str, Any] = {
        "success": True,
        "backend": tool_id,
        "backendTool": tool_name,
        "_usageTarget": tool_id,
    }
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            payload["result"] = text
        else:
            if isinstance(parsed, dict):
                payload.update(parsed)
            else:
                payload["result"] = parsed
    else:
        payload["result"] = text
    return payload


def _launch_preview(launch: dict | None) -> str:
    launch = launch or {}
    return " ".join(
        [str(launch.get("command") or ""), *(str(a) for a in launch.get("args") or [])]
    ).strip()


def _scoop_root() -> str | None:
    try:
        root, _ = pkgman.resolve_root("scoop")
    except pkgman.PackageError:
        return None
    return root


def _app_candidates(ctx: ToolContext, name: str) -> list[dict]:
    """按名称解析可启动目标：已登记应用（含 Steam 客户端）→ scoop 已装包。"""
    needle = name.strip().lower()
    scored: list[tuple[int, dict]] = []

    for tool in ctx.registry.list_all_tools():
        launch = tool.get("launch")
        if not launch:
            continue
        labels = [
            str(tool.get("id") or ""),
            str(tool.get("name") or ""),
            str(tool.get("displayName") or ""),
            *[str(tag) for tag in tool.get("tags") or []],
        ]
        lowered = [label.lower() for label in labels]
        if needle in lowered:
            score = 0
        elif any(needle in label for label in lowered):
            score = 1
        else:
            continue
        scored.append(
            (
                score,
                {
                    "source": "app",
                    "toolId": tool["id"],
                    "kind": tool.get("kind"),
                    "name": tool.get("displayName") or tool.get("name") or tool["id"],
                    "commandPreview": _launch_preview(launch),
                },
            )
        )

    root = _scoop_root()
    if root:
        for row in pkgman.list_installed(root):
            package = str(row.get("name") or "")
            lowered = package.lower()
            if needle == lowered:
                score = 0
            elif needle in lowered:
                score = 1
            else:
                continue
            target = pkgman.launch_target(row)
            scored.append(
                (
                    score,
                    {
                        "source": "scoop",
                        "package": package,
                        "root": root,
                        "state": row.get("state"),
                        "version": row.get("version"),
                        "path": target.get("path"),
                        "launchable": bool(target.get("ok")),
                        "reason": target.get("reason"),
                    },
                )
            )

    scored.sort(key=lambda item: (item[0], str(item[1].get("toolId") or item[1].get("package"))))
    return [candidate for _, candidate in scored]


async def _handle_search_files(arguments: dict, ctx: ToolContext) -> dict:
    keyword = str(arguments.get("keyword") or "").strip()
    backend_args: dict[str, Any] = {"query": keyword}
    if arguments.get("limit") is not None:
        backend_args["maxResults"] = _clamp_int(
            arguments.get("limit"), default=20, low=1, high=200
        )
    if arguments.get("match_path"):
        backend_args["matchPath"] = True
    if arguments.get("match_case"):
        backend_args["matchCase"] = True
    if arguments.get("regex"):
        backend_args["regex"] = True

    await ensure_runtime(ctx, EVERYTHING_TOOL_ID)
    payload = await _call_backend_json(
        ctx, EVERYTHING_TOOL_ID, "search_files", backend_args
    )
    payload["keyword"] = keyword
    return payload


async def _handle_get_file_info(arguments: dict, ctx: ToolContext) -> dict:
    path = str(arguments.get("path") or "").strip()
    await ensure_runtime(ctx, EVERYTHING_TOOL_ID)
    payload = await _call_backend_json(
        ctx, EVERYTHING_TOOL_ID, "get_file_info", {"path": path}
    )
    payload["path"] = path
    return payload


async def _handle_get_everything_status(arguments: dict, ctx: ToolContext) -> dict:
    """只读状态：不自动拉起后端（否则「查状态」本身会改变状态）。"""
    status_res = ctx.backend_manager.get_backend_status(EVERYTHING_TOOL_ID)
    if not status_res.get("success"):
        fail(
            status_res.get("error") or "Everything 后端未配置",
            error_type=status_res.get("errorType") or ErrorType.CONFIG_ERROR,
            toolId=EVERYTHING_TOOL_ID,
        )
    status = status_res.get("status") or {}
    payload: dict[str, Any] = {
        "success": True,
        "toolId": EVERYTHING_TOOL_ID,
        "backendStatus": status,
        "_usageTarget": EVERYTHING_TOOL_ID,
    }
    if status.get("status") != RUNTIME_RUNNING:
        payload["note"] = (
            "Everything 后端未运行；调用 search_files / get_file_info 会自动启动它"
        )
        return payload
    payload.update(
        await _call_backend_json(ctx, EVERYTHING_TOOL_ID, "check_status", {})
    )
    payload["backendStatus"] = status
    return payload


async def _handle_open_path(arguments: dict, ctx: ToolContext) -> dict:
    try:
        path = file_ops.resolve_path(arguments.get("path"))
        info = file_ops.describe_path(path)
    except file_ops.FileOpError as exc:
        fail(str(exc), error_type=ErrorType.INVALID_PARAMS)

    reveal = bool(arguments.get("reveal"))
    execute = bool(arguments.get("execute"))
    payload: dict[str, Any] = {
        "success": True,
        **info,
        "reveal": reveal,
        "executed": False,
    }

    if not info["exists"]:
        if execute:
            fail(
                f"路径不存在: {path}",
                error_type=ErrorType.NOT_FOUND,
                path=str(path),
            )
        payload["note"] = "路径不存在：本调用只做了预览，未执行"
        return payload

    if not execute:
        payload["note"] = (
            "未执行：传 execute=true 才会打开"
            + ("（reveal=true 只定位所在文件夹）" if reveal else "")
        )
        return payload

    decision = decide_destructive(
        "path", None, {"path": str(path), "reveal": reveal}, arguments
    )
    if decision["blocked"]:
        fail(decision["reason"], error_type=decision["error_type"], path=str(path))
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认打开可执行目标: {path}")

    try:
        opened = await file_ops.open_target(path, reveal=reveal)
    except file_ops.FileOpError as exc:
        fail(str(exc), error_type=ErrorType.RUNTIME_ERROR, path=str(path))
    payload.update(opened)
    payload["executed"] = True
    return payload


async def _handle_open_app(arguments: dict, ctx: ToolContext) -> dict:
    name = str(arguments.get("name") or "").strip()
    execute = bool(arguments.get("execute"))
    candidates = _app_candidates(ctx, name)

    if not candidates:
        fail(
            f"没有匹配「{name}」的可启动目标。可用 search_tools / list_tools 查已登记应用，"
            "或用 list_packages 查 scoop 已装包",
            error_type=ErrorType.NOT_FOUND,
            name=name,
        )
    if len(candidates) > 1:
        if execute:
            fail(
                f"「{name}」匹配到 {len(candidates)} 个目标，未执行；"
                "请用更精确的名称重试",
                error_type=ErrorType.INVALID_PARAMS,
                name=name,
                candidates=candidates[:20],
            )
        return {
            "success": True,
            "resolved": False,
            "name": name,
            "count": len(candidates),
            "candidates": candidates[:20],
            "executed": False,
            "note": "匹配到多个目标，未执行；请用更精确的名称重试",
        }

    candidate = candidates[0]
    payload: dict[str, Any] = {
        "success": True,
        "resolved": True,
        "target": candidate,
        "executed": False,
        "_usageTarget": candidate.get("toolId") or f"scoop:{candidate.get('package')}",
    }
    if not execute:
        payload["note"] = "未执行：传 execute=true 才会启动"
        return payload

    if candidate["source"] == "app":
        tool = ctx.registry.get_tool(candidate["toolId"])
        decision = decide_destructive(
            "app", tool, {"action": "open_app"}, arguments
        )
        if decision["blocked"]:
            fail(
                decision["reason"],
                error_type=decision["error_type"],
                toolId=candidate["toolId"],
            )
        if decision["required_confirm"]:
            ctx.log(f"⚠️ 已确认打开应用: {candidate['toolId']}")
        exec_res = await launch_app(
            (tool or {}).get("launch"),
            candidate["toolId"],
            candidate.get("name") or "",
            arguments.get("wait"),
        )
    else:
        package = candidate["package"]
        root = candidate.get("root") or _scoop_root()
        try:
            row = pkgman.find_installed(root, package) if root else None
            if row is None:
                fail(
                    f"scoop 里没有安装 '{package}'",
                    error_type=ErrorType.NOT_FOUND,
                    package=package,
                )
            target = pkgman.launch_target(row)
        except pkgman.PackageError as exc:
            fail(str(exc), error_type=exc.error_type, package=package)
        if not target["ok"]:
            fail(
                f"无法确定 '{package}' 的可启动入口：{target['reason']}",
                error_type=ErrorType.RUNTIME_ERROR,
                package=package,
                candidates=target["candidates"],
            )
        decision = decide_destructive(
            "package",
            {"id": f"scoop:{package}"},
            {"action": "open_app", "package": package},
            arguments,
        )
        if decision["blocked"]:
            fail(decision["reason"], error_type=decision["error_type"], package=package)
        if decision["required_confirm"]:
            ctx.log(f"⚠️ 已确认打开 scoop 包: {package}")
        exec_res = await launch_app(
            {"type": "executable", "command": target["path"]},
            f"scoop/{package}",
            package,
            arguments.get("wait"),
        )

    payload.update(exec_res)
    payload["executed"] = True
    return ensure_success(payload)


def _launcher_tools(
    ctx: ToolContext, source: str | None = None, *, include_steam: bool = False
) -> list[dict]:
    tools = []
    for tool in ctx.registry.list_all_tools(kind="launcher"):
        adapter = str((tool.get("launcher") or {}).get("type") or "")
        if not include_steam and adapter == "steam":
            continue
        if source and adapter != source:
            continue
        tools.append(tool)
    return tools


def _steam_tool(ctx: ToolContext) -> dict:
    for tool in ctx.registry.list_all_tools(kind="launcher"):
        if str((tool.get("launcher") or {}).get("type") or "") == "steam":
            return tool
    fail(
        "未找到 Steam 启动器条目（data/tools/steam.json 未登记或 launcher.type 不是 steam）",
        error_type=ErrorType.NOT_FOUND,
        toolId=STEAM_TOOL_ID,
    )


def _steam_games(ctx: ToolContext, keyword: str | None, limit: int) -> list[dict]:
    return ctx.launcher_manager.list_actions(
        STEAM_TOOL_ID, keyword=keyword, limit=limit
    )


def _steam_game_row(action: dict) -> dict:
    action_id = str(action.get("id") or "")
    appid = str(action.get("appid") or "")
    if not appid and action_id.startswith("app:"):
        appid = action_id.split(":", 1)[1]
    return {
        "appid": appid,
        "actionId": action_id,
        "name": action.get("name") or f"Steam App {appid}",
        "installedDir": action.get("installdir") or "",
        "library": action.get("library") or "",
        "sizeOnDisk": action.get("sizeOnDisk"),
        "lastPlayed": action.get("lastPlayed"),
    }


async def _handle_search_games(arguments: dict, ctx: ToolContext) -> dict:
    tool = _steam_tool(ctx)
    keyword = str(arguments.get("keyword") or "").strip() or None
    limit = _clamp_int(arguments.get("limit"), default=50, low=1, high=200)
    try:
        actions = _steam_games(ctx, keyword, limit)
    except LauncherError as exc:
        fail(str(exc), error_type=ErrorType.LAUNCHER_ERROR, toolId=tool["id"])

    catalog = ctx.store.load_launcher_catalog(tool["id"]) or {}
    payload = {
        "success": True,
        "toolId": tool["id"],
        "keyword": keyword,
        "count": len(actions),
        "lastSynced": catalog.get("lastSynced"),
        "source": catalog.get("source"),
        "games": [_steam_game_row(action) for action in actions],
        "_usageTarget": tool["id"],
    }
    if not actions:
        payload["note"] = (
            "Steam 游戏清单为空：可能未安装 Steam、游戏库不可读，或尚未同步。"
            "可先 sync_capabilities(tool_id='steam') 再查"
        )
    return payload


def _match_steam_games(actions: list[dict], game: str) -> list[dict]:
    needle = game.strip()
    lowered = needle.lower()
    appid_match = re.match(r"^(?:app:)?(\d+)$", needle, re.IGNORECASE)
    if appid_match:
        appid = appid_match.group(1)
        return [a for a in actions if str(a.get("appid") or "") == appid]

    exact = [
        a
        for a in actions
        if str(a.get("name") or "").strip().lower() == lowered
        or str(a.get("id") or "").lower() == lowered
    ]
    if exact:
        return exact
    return [
        a
        for a in actions
        if lowered in str(a.get("name") or "").lower()
        or lowered in str(a.get("id") or "").lower()
    ]


async def _handle_open_game(arguments: dict, ctx: ToolContext) -> dict:
    tool = _steam_tool(ctx)
    game = str(arguments.get("game") or "").strip()
    execute = bool(arguments.get("execute"))
    try:
        actions = _steam_games(ctx, None, 1000)
    except LauncherError as exc:
        fail(str(exc), error_type=ErrorType.LAUNCHER_ERROR, toolId=tool["id"])

    matches = _match_steam_games(actions, game)
    if not matches:
        fail(
            f"Steam 已装游戏里没有匹配「{game}」的条目。"
            "先用 search_games 查可用游戏与 appid",
            error_type=ErrorType.NOT_FOUND,
            game=game,
        )
    if len(matches) > 1:
        rows = [_steam_game_row(action) for action in matches[:20]]
        if execute:
            fail(
                f"「{game}」匹配到 {len(matches)} 个游戏，未启动；请改用 appid 或更精确的名称",
                error_type=ErrorType.INVALID_PARAMS,
                game=game,
                candidates=rows,
            )
        return {
            "success": True,
            "resolved": False,
            "game": game,
            "count": len(matches),
            "candidates": rows,
            "executed": False,
            "note": "匹配到多个游戏，未启动；请改用 appid 或更精确的名称",
        }

    action = matches[0]
    row = _steam_game_row(action)
    command_preview = _launch_preview(tool.get("launch"))
    if command_preview:
        command_preview = f"{command_preview} -applaunch {row['appid']}"
    payload: dict[str, Any] = {
        "success": True,
        "resolved": True,
        "game": row,
        "actionId": row["actionId"],
        "commandPreview": command_preview,
        "executed": False,
        "_usageTarget": tool["id"],
    }
    if not execute:
        payload["note"] = (
            "未执行：传 execute=true 且 allowDestructive=true 才会启动 "
            "（Steam 条目标了 destructive）"
        )
        return payload

    decision = decide_destructive(
        "launcher",
        tool,
        {
            "action_id": row["actionId"],
            "action_name": row["name"],
            "action_type": "game",
        },
        arguments,
    )
    if decision["blocked"]:
        fail(decision["reason"], error_type=decision["error_type"], toolId=tool["id"])
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认启动 Steam 游戏: {row['name']}（{row['appid']}）")

    res = ensure_success(
        await ctx.launcher_manager.run_action(
            tool["id"],
            action_id=row["actionId"],
            wait=arguments.get("wait"),
        )
    )
    payload.update(res)
    payload["executed"] = True
    return payload


def _launcher_action_row(source: str, tool_id: str, action: dict) -> dict:
    return {
        "source": source,
        "toolId": tool_id,
        "actionId": action.get("id"),
        "name": action.get("name") or "",
        "description": action.get("description") or "",
        "type": action.get("type") or "",
        "keywords": action.get("keywords") or [],
    }


async def _handle_search_actions(arguments: dict, ctx: ToolContext) -> dict:
    keyword = str(arguments.get("keyword") or "").strip()
    source = str(arguments.get("source") or "").strip() or None
    limit = _clamp_int(arguments.get("limit"), default=20, low=1, high=100)
    tools = _launcher_tools(ctx, source)

    rows: list[dict] = []
    errors: list[dict] = []
    searched: list[str] = []
    for tool in tools:
        adapter = str((tool.get("launcher") or {}).get("type") or "")
        searched.append(adapter)
        try:
            actions = ctx.launcher_manager.list_actions(
                tool["id"], keyword=keyword, limit=limit
            )
        except LauncherError as exc:
            errors.append({"toolId": tool["id"], "error": str(exc)})
            continue
        rows.extend(
            _launcher_action_row(adapter, tool["id"], action) for action in actions
        )

    rows = rows[:limit]
    payload: dict[str, Any] = {
        "success": True,
        "keyword": keyword,
        "count": len(rows),
        "searched": searched,
        "actions": rows,
    }
    if errors:
        payload["errors"] = errors
    if not rows:
        payload["note"] = (
            "没有匹配的动作。Quicker 的动作名来自其缓存，部分动作可能只有 ID；"
            "uTools 的清单来自 plugins/*.asar"
        )
    return payload


def _runner_tool_for_source(ctx: ToolContext, source: str | None) -> dict | None:
    tools = _launcher_tools(ctx, source)
    return tools[0] if len(tools) == 1 else None


def _match_actions(rows: list[dict], action_ref: str) -> list[dict]:
    needle = action_ref.strip().lower()
    exact = [
        row
        for row in rows
        if str(row.get("actionId") or "").lower() == needle
        or str(row.get("name") or "").lower() == needle
    ]
    if exact:
        return exact
    return [
        row
        for row in rows
        if needle in str(row.get("name") or "").lower()
        or needle in str(row.get("actionId") or "").lower()
        or needle in str(row.get("description") or "").lower()
        or any(needle in str(k).lower() for k in row.get("keywords") or [])
    ]


async def _handle_run_action(arguments: dict, ctx: ToolContext) -> dict:
    action_ref = str(arguments.get("action") or "").strip()
    source = str(arguments.get("source") or "").strip() or None
    execute = bool(arguments.get("execute"))
    tools = _launcher_tools(ctx, source)

    rows: list[dict] = []
    for tool in tools:
        adapter = str((tool.get("launcher") or {}).get("type") or "")
        try:
            actions = ctx.launcher_manager.list_actions(tool["id"])
        except LauncherError:
            continue
        rows.extend(
            _launcher_action_row(adapter, tool["id"], action) for action in actions
        )

    matches = _match_actions(rows, action_ref)
    if not matches and source:
        # 清单可能还没覆盖到刚创建的动作：来源明确时按原始 ID 直调
        tool = _runner_tool_for_source(ctx, source)
        if tool is None:
            fail(
                f"未找到启动器来源 '{source}' 的工具条目",
                error_type=ErrorType.NOT_FOUND,
                source=source,
            )
        adapter = str((tool.get("launcher") or {}).get("type") or "")
        matches = [
            {
                "source": adapter,
                "toolId": tool["id"],
                "actionId": action_ref,
                "name": "",
                "description": "",
                "type": "",
                "keywords": [],
                "direct": True,
            }
        ]
    if not matches:
        fail(
            f"没有匹配「{action_ref}」的动作。先用 search_actions 查可用动作；"
            "若已知动作 ID，可传 source 指定 quicker 或 utools",
            error_type=ErrorType.NOT_FOUND,
            action=action_ref,
        )
    if len(matches) > 1:
        if execute:
            fail(
                f"「{action_ref}」匹配到 {len(matches)} 个动作，未执行；请改用 action ID 或更精确的名称",
                error_type=ErrorType.INVALID_PARAMS,
                action=action_ref,
                candidates=matches[:20],
            )
        return {
            "success": True,
            "resolved": False,
            "action": action_ref,
            "count": len(matches),
            "candidates": matches[:20],
            "executed": False,
            "note": "匹配到多个动作，未执行；请改用 action ID 或更精确的名称",
        }

    target = matches[0]
    tool = ctx.registry.get_tool(target["toolId"])
    payload: dict[str, Any] = {
        "success": True,
        "resolved": True,
        "target": target,
        "executed": False,
        "_usageTarget": target["toolId"],
    }
    if not execute:
        payload["note"] = (
            "未执行：传 execute=true 且 allowDestructive=true 才会执行"
            "（三个启动器条目都标了 destructive）"
        )
        return payload

    decision = decide_destructive(
        "launcher",
        tool,
        {
            "action_id": target.get("actionId"),
            "action_name": target.get("name") or None,
            "action_type": target.get("type") or "",
        },
        arguments,
    )
    if decision["blocked"]:
        fail(decision["reason"], error_type=decision["error_type"], toolId=target["toolId"])
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行启动器动作: {target['toolId']}/{target.get('actionId')}")

    res = ensure_success(
        await ctx.launcher_manager.run_action(
            target["toolId"],
            action_id=target.get("actionId"),
            action_name=target.get("name") or None,
            param=arguments.get("param"),
            wait=arguments.get("wait"),
        )
    )
    payload.update(res)
    payload["executed"] = True
    return payload


async def _handle_search_winget_packages(arguments: dict, ctx: ToolContext) -> dict:
    keyword = str(arguments.get("keyword") or "").strip()
    backend_args: dict[str, Any] = {
        "upgradeable": bool(arguments.get("upgradeable")),
    }
    if keyword:
        backend_args["query"] = keyword
    await ensure_runtime(ctx, WINGET_TOOL_ID)
    payload = await _call_backend_json(
        ctx, WINGET_TOOL_ID, "find-winget-packages", backend_args
    )
    payload["keyword"] = keyword
    payload["upgradeable"] = backend_args["upgradeable"]
    return payload


async def _handle_install_winget_package(arguments: dict, ctx: ToolContext) -> dict:
    identifier = str(arguments.get("identifier") or "").strip()
    backend_args: dict[str, Any] = {
        "identifier": identifier,
        "upgradeOnly": bool(arguments.get("upgrade_only")),
    }
    if arguments.get("source"):
        backend_args["source"] = str(arguments.get("source")).strip()

    payload: dict[str, Any] = {
        "success": True,
        "backend": WINGET_TOOL_ID,
        "backendTool": "install-winget-package",
        "arguments": backend_args,
        "executed": False,
        "_usageTarget": WINGET_TOOL_ID,
    }
    if not arguments.get("execute"):
        payload["note"] = (
            "未执行：安装/升级会改动本机，需同时传 execute=true 与 allowDestructive=true"
        )
        return payload

    tool = ctx.registry.get_tool(WINGET_TOOL_ID)
    decision = decide_destructive(
        "mcp", tool, {"tool_name": "install-winget-package"}, arguments
    )
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=WINGET_TOOL_ID,
            toolName="install-winget-package",
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认 winget 安装/升级: {identifier}")

    await ensure_runtime(ctx, WINGET_TOOL_ID)
    result = await _call_backend_json(
        ctx, WINGET_TOOL_ID, "install-winget-package", backend_args
    )
    payload.update(result)
    payload["executed"] = True
    return payload


# 顺序即 tools/list 对外可见顺序（golden 按列表逐位锁定，保序即保行为）。
SERVER_TOOL_SPECS: tuple[ServerToolSpec, ...] = (
    ServerToolSpec(
        name="search_tools",
        handler=_handle_search_tools,
        description="搜索已注册的工具，按关键词匹配名称、描述、标签，可按类型、安装状态、发布者筛选",
        input_schema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词"
                },
                "kind": {
                    "type": "string",
                    # NE-5：driver 历史漂移已于 2026-10-17 修正为 launcher（§3.1 #20；golden 同步重新基线）
                    "description": "可选，按工具类型筛选（如 app、mcp、launcher）"
                },
                "status": {
                    "type": "string",
                    "enum": ["installed", "not_installed", "unknown"],
                    "description": "可选，按安装状态精确筛选（条目未写 status 时按 installed 计）"
                },
                "publisher": {
                    "type": "string",
                    "description": "可选，按发布者筛选（子串匹配、大小写不敏感；条目没写 publisher 一律不命中）"
                }
            },
            "required": ["keyword"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="list_tools",
        handler=_handle_list_tools,
        description="列出所有已注册的工具，可按类型、安装状态、发布者筛选",
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    # NE-5：driver 历史漂移已于 2026-10-17 修正为 launcher（§3.1 #20；golden 同步重新基线）
                    "description": "可选，按工具类型筛选（如 app、mcp、launcher）"
                },
                "status": {
                    "type": "string",
                    "enum": ["installed", "not_installed", "unknown"],
                    "description": "可选，按安装状态精确筛选（条目未写 status 时按 installed 计）"
                },
                "publisher": {
                    "type": "string",
                    "description": "可选，按发布者筛选（子串匹配、大小写不敏感；条目没写 publisher 一律不命中）"
                }
            }
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="get_tool_info",
        handler=_handle_get_tool_info,
        description="查看工具的详细信息",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID，如 everything、everything-search-mcp"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="launch_tool",
        handler=_handle_launch_tool,
        description="取指定工具的启动命令；传 execute=true 可真正执行启动（默认只返回命令，不执行）",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID"
                },
                "execute": {
                    "type": "boolean",
                    "description": "是否真正执行启动（默认 false：只返回命令、不 spawn 进程）"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，执行后等待早退的观察窗口秒数（默认 5，封顶 30）；窗口内退出即报退出码，超时仍运行则报 running=true"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "工具被标记为 destructive 时，执行启动需显式传 true"
                }
            },
            "required": ["tool_id"]
        },
        # execute=true 会真的 spawn 进程，与 call_capability 同款只标 openWorld
        annotations={"openWorldHint": True},
    ),
    ServerToolSpec(
        name="list_capabilities",
        handler=_handle_list_capabilities,
        description=(
            "列出某个工具提供的能力：MCP 子工具 / 启动器动作 / 应用的 launch，"
            "每条带完整 inputSchema；可按关键词与条数收窄"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID（如 everything-search-mcp、quicker、steam）"
                },
                "keyword": {
                    "type": "string",
                    "description": "可选，按能力名称/描述/元数据过滤"
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "可选，最多返回多少条"
                },
                "refresh": {
                    "type": "boolean",
                    "description": (
                        "可选，MCP 工具强制向后端刷新一次能力缓存"
                        "（默认读盘；启动器清单按自身的陈旧策略自动重扫）"
                    )
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="call_capability",
        handler=_handle_call_capability,
        description=(
            "调用某个工具的能力：把 arguments 交给 MCP 子工具 / 启动器动作 / 应用的 launch；"
            "默认只预览，execute=true 才产生副作用，destructive 能力还需 allowDestructive=true"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID（如 everything-search-mcp、quicker、steam）"
                },
                "capability_id": {
                    "type": "string",
                    "description": "能力 ID（取自 list_capabilities 的 capabilityId；应用的固定能力是 launch）"
                },
                "arguments": {
                    "type": "object",
                    "description": "能力自己的参数，结构见 list_capabilities 返回的 inputSchema；默认 {}",
                    "additionalProperties": True
                },
                "execute": {
                    "type": "boolean",
                    "description": "可选，是否真的执行；缺省时只读能力自动执行、写/破坏性能力只预览"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "能力被标记为 destructive 时，执行需显式传 true"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，启动器动作/应用启动的观察窗口秒数（默认取条目配置）"
                }
            },
            "required": ["tool_id", "capability_id"]
        },
        # 会访问外部世界（MCP 后端 / 启动器 / 进程），静态无法判定副作用
        annotations={"openWorldHint": True},
    ),
    ServerToolSpec(
        name="sync_capabilities",
        handler=_handle_sync_capabilities,
        description=(
            "同步某个工具的能力清单：MCP 会启动运行时并刷新子工具目录，"
            "启动器会重新扫描本地数据（应用没有可变清单）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"idempotentHint": True},
    ),
    ServerToolSpec(
        name="start_runtime",
        handler=_handle_start_runtime,
        description="启动 MCP 提供者的运行时并建立连接（非 mcp 工具没有运行时）",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"idempotentHint": True},
    ),
    ServerToolSpec(
        name="stop_runtime",
        handler=_handle_stop_runtime,
        description="停止 MCP 提供者的运行时",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"idempotentHint": True},
    ),
    ServerToolSpec(
        name="get_runtime_status",
        handler=_handle_get_runtime_status,
        description="获取 MCP 提供者的运行时状态",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID，不填则返回所有运行时状态"
                }
            }
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="get_runtime_logs",
        handler=_handle_get_runtime_logs,
        description="获取 MCP 提供者的运行时日志",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                },
                "level": {
                    "type": "string",
                    "description": "日志级别过滤（debug/info/warning/error）"
                },
                "limit": {
                    "type": "number",
                    "description": "返回条数上限，默认 50"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="set_runtime_log_level",
        handler=_handle_set_runtime_log_level,
        description="设置 MCP 提供者的运行时日志级别",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                },
                "level": {
                    "type": "string",
                    "description": "日志级别（debug/info/warning/error）"
                }
            },
            "required": ["tool_id", "level"]
        },
        # 历史条目即不挂注解，保留原样
        annotations={},
    ),
    ServerToolSpec(
        name="get_tool_help",
        handler=_handle_get_tool_help,
        description="取工具的帮助信息（文档链接 / 后端能力清单 / 启动器动作清单）；kind=app 传 run=true 可执行帮助命令并捕获输出",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "工具 ID"
                },
                "run": {
                    "type": "boolean",
                    "description": "是否真的执行帮助命令（默认 false：只返回文档链接、能力清单与命令预览，不 spawn）"
                },
                "timeout": {
                    "type": "number",
                    "description": "可选，执行帮助命令的超时秒数（覆盖条目 help.timeout；默认 5，封顶 30）"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "工具被标记为 destructive 时，执行帮助命令需显式传 true"
                }
            },
            "required": ["tool_id"]
        },
        # 默认路径是只读取数，但 run=true 会 spawn 子进程 → 与 launch_tool 同款只标 openWorld
        annotations={"openWorldHint": True},
    ),
    ServerToolSpec(
        name="get_usage_stats",
        handler=_handle_get_usage_stats,
        description="查询工具调用使用统计：按天、按对外工具、按目标 tool_id 聚合调用量、成功率与耗时（默认最近 7 天）",
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "可选，统计窗口天数（按日历日、含今天；默认 7，越界收敛到 1~365）",
                    "minimum": 1,
                    "maximum": 365
                },
                "tool_id": {
                    "type": "string",
                    "description": "可选，只看某个登记工具的调用（与调用记录里的 target 精确匹配）"
                }
            }
        },
        # 只读一份本机日志文件，不碰任何工具、不 spawn
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="discover_tools",
        handler=_handle_discover_tools,
        description=(
            "发现本机工具：按声明式候选清单做只读探测（路径存在 / 目录条目数 / 注册表登记值 / 命令是否在 PATH），"
            "对照已登记工具给出 installed / not_installed / unknown 结论（默认不执行任何命令）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "run_commands": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 才真正执行清单里的版本命令取版本号（会 spawn 子进程）；false 只报告命令是否在 PATH 上"
                },
                "status": {
                    "type": "string",
                    "enum": ["installed", "not_installed", "unknown"],
                    "description": "可选，只返回该结论的候选（summary 仍是整份清单的计数）"
                }
            }
        },
        # run_commands=true 会 spawn，按 mcp-conformance 的口径挂 openWorldHint
        # 且**不声明** readOnlyHint：默认路径确实只读，但工具的能力面包含执行，
        # 声明只读等于把「它会跑命令」对客户端藏起来（与 get_tool_help 同判例）
        annotations={"openWorldHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="list_packages",
        handler=_handle_list_packages,
        description=(
            "列出本机包管理器（当前支持 scoop）已安装的包：只读盘上的 apps 清单，"
            "不执行安装器命令；每个包给出 state（ok / current_broken / no_manifest）"
            "与是否可启动"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "enum": ["scoop"],
                    "description": "可选，默认 scoop"
                },
                "state": {
                    "type": "string",
                    "enum": ["ok", "current_broken", "no_manifest"],
                    "description": "可选，只返回该状态的包（count/states 仍是整份清单的口径）"
                }
            }
        },
        # 全程只读文件，不 spawn、不碰注册表写
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="search_packages",
        handler=_handle_search_packages,
        description=(
            "在包管理器已添加的仓库清单里搜可安装的包（只读，按包名匹配；"
            "命中为空只说明已添加的清单里没有，不代表装不了）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "enum": ["scoop"],
                    "description": "可选，默认 scoop"
                },
                "keyword": {
                    "type": "string",
                    "description": "包名关键词（匹配 bucket 清单里的包名）"
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "可选，最多返回多少条，默认 20"
                }
            },
            "required": ["keyword"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="launch_package",
        handler=_handle_launch_package,
        description=(
            "启动包管理器已安装的某个包：默认只解析可执行入口并预览（不 spawn），"
            "传 execute=true 才真的启动（入口按 快捷方式 → bin → shim 取，可用 entry 指定）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "enum": ["scoop"],
                    "description": "可选，默认 scoop"
                },
                "package": {
                    "type": "string",
                    "description": "包名（须已安装，如 7zip）"
                },
                "entry": {
                    "type": "string",
                    "description": "可选，指定入口的名字或文件名（如 7zFM.exe）；缺省按优先级自动挑"
                },
                "execute": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 才真的启动进程；false 只回预览"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，execute=true 时观察多少秒判定早退（默认 5，上限 30；<=0 不等待）"
                }
            },
            "required": ["package"]
        },
        # 能力面包含 spawn 进程，按 get_tool_help / discover_tools 的判例挂
        # openWorldHint 且不声明 readOnlyHint
        annotations={"openWorldHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="install_package",
        handler=_handle_install_package,
        description=(
            "用包管理器安装一个包：默认只回将要执行的命令预览，不改动本机；"
            "真要装必须同时传 execute=true 与 allowDestructive=true"
            "（安装会下载并运行仓库里的安装脚本，属破坏性操作）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "enum": ["scoop"],
                    "description": "可选，默认 scoop"
                },
                "package": {
                    "type": "string",
                    "description": "包名（只允许字母/数字/点/下划线/短横/加号，不接受路径分隔符）"
                },
                "execute": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 才真的执行安装命令"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "execute=true 时必须显式传 true，否则按破坏性门禁拒绝"
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 600,
                    "description": "可选，安装命令最长等待秒数，默认 120、上限 600"
                }
            },
            "required": ["package"]
        },
        annotations={"openWorldHint": True, "destructiveHint": True},
    ),
    # ---- default：高频意图前门（默认只预览，execute=true 才产生副作用）----
    ServerToolSpec(
        name="search_files",
        handler=_handle_search_files,
        description=(
            "用 Everything 索引全盘搜索文件与文件夹（按文件名或路径，支持通配符/正则）；"
            "后端未运行会自动启动"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词（Everything 语法，支持 * ? 通配符与 AND/OR/NOT）"
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "可选，最多返回多少条，默认 20"
                },
                "match_path": {
                    "type": "boolean",
                    "description": "可选，按完整路径而不是文件名匹配"
                },
                "match_case": {
                    "type": "boolean",
                    "description": "可选，区分大小写"
                },
                "regex": {
                    "type": "boolean",
                    "description": "可选，按正则表达式匹配"
                }
            },
            "required": ["keyword"]
        },
        # 只读索引但会访问全盘路径，且可能拉起后端进程
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("default", "full"),
    ),
    ServerToolSpec(
        name="open_path",
        handler=_handle_open_path,
        description=(
            "用系统默认程序打开一个文件/文件夹，或 reveal=true 在资源管理器中定位；"
            "默认只预览（exists/size 等），execute=true 才执行；可执行文件/脚本另需 "
            "allowDestructive=true"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件或文件夹路径（支持 %ENV% 与 ~ 展开；相对路径按当前目录）"
                },
                "reveal": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 只在资源管理器里选中该路径，不打开它"
                },
                "execute": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 才真的打开/定位"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "可选，execute=true 且目标是可执行文件/脚本时必须显式传 true"
                }
            },
            "required": ["path"]
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("default", "full"),
    ),
    ServerToolSpec(
        name="open_app",
        handler=_handle_open_app,
        description=(
            "按名称打开已登记的应用、Steam 客户端或 scoop 已安装程序；"
            "默认只解析候选并预览，execute=true 才启动；多个候选时只返回列表、不自动选择"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "应用名/工具 ID/显示名（大小写不敏感，支持子串匹配）"
                },
                "execute": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 才真的启动"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "可选，目标的策略要求确认时必须显式传 true"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，execute=true 时观察多少秒判定早退（默认 5，上限 30；<=0 不等待）"
                }
            },
            "required": ["name"]
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("default", "full"),
    ),
    ServerToolSpec(
        name="search_games",
        handler=_handle_search_games,
        description=(
            "搜索 Steam 已安装游戏（keyword 可匹配名称或 appid，省略则列出全部），"
            "返回 appid、安装目录、所属库、占用空间与最近游玩时间"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "可选，游戏名关键词或 appid；省略则列出全部已安装游戏"
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "可选，最多返回多少条，默认 50"
                }
            }
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
        exposure=("default", "full"),
    ),
    ServerToolSpec(
        name="open_game",
        handler=_handle_open_game,
        description=(
            "按游戏名或 appid 启动 Steam 已安装游戏；默认只预览命令与目标，"
            "execute=true 且 allowDestructive=true 才启动；多个候选时只返回列表、不自动选择"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "description": "游戏名（精确名优先，再做子串匹配）或纯数字 appid"
                },
                "execute": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 才真的启动游戏"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "execute=true 时必须显式传 true（Steam 条目标了 destructive）"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，启动后观察多少秒判定早退（默认 5，上限 30；<=0 不等待）"
                }
            },
            "required": ["game"]
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("default", "full"),
    ),
    ServerToolSpec(
        name="search_actions",
        handler=_handle_search_actions,
        description=(
            "跨 Quicker / uTools 搜索快捷动作（匹配名称、描述、关键词与动作 ID），"
            "返回 source、actionId、name；可用 source 限定单个启动器"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "动作名、描述、关键词或动作 ID 的子串"
                },
                "source": {
                    "type": "string",
                    "enum": ["quicker", "utools"],
                    "description": "可选，只搜某个启动器；省略则搜全部（不含 Steam）"
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "可选，最多返回多少条，默认 20"
                }
            },
            "required": ["keyword"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
        exposure=("default", "full"),
    ),
    ServerToolSpec(
        name="run_action",
        handler=_handle_run_action,
        description=(
            "按动作 ID 或名称执行 Quicker / uTools 快捷动作；默认只预览解析结果，"
            "execute=true 且 allowDestructive=true 才执行；多个候选时只返回列表、不自动选择"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "动作 ID 或名称（精确匹配优先，再做子串匹配）"
                },
                "source": {
                    "type": "string",
                    "enum": ["quicker", "utools"],
                    "description": "可选，限定启动器；已知原始动作 ID 时用它直调"
                },
                "param": {
                    "type": "string",
                    "description": "可选，传给动作的参数（Quicker 会拼成 ?param）"
                },
                "execute": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 才真的执行"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "execute=true 时必须显式传 true（启动器动作标了 destructive）"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，执行后观察多少秒（默认按启动器配置）"
                }
            },
            "required": ["action"]
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("default", "full"),
    ),
    # ---- full：低频补充 / 执行面工具 ----
    ServerToolSpec(
        name="get_file_info",
        handler=_handle_get_file_info,
        description=(
            "查询单个文件/文件夹的元数据（大小、创建/修改时间、属性）；"
            "路径不存在时返回 exists=false 而不是报错"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件或文件夹路径（支持 %ENV% 与 ~ 展开）"
                }
            },
            "required": ["path"]
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("full",),
    ),
    ServerToolSpec(
        name="get_everything_status",
        handler=_handle_get_everything_status,
        description=(
            "检查 Everything 搜索后端是否可用与索引状态（只读；不会自动启动后端）"
        ),
        input_schema={"type": "object", "properties": {}},
        annotations={"readOnlyHint": True, "destructiveHint": False},
        exposure=("full",),
    ),
    ServerToolSpec(
        name="search_winget_packages",
        handler=_handle_search_winget_packages,
        description=(
            "用 winget 搜索可安装或可升级的软件包（keyword 省略且 upgradeable=true "
            "时列出可升级项）；后端未运行会自动启动"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "可选，包名/ID 关键词；省略时配合 upgradeable 使用"
                },
                "upgradeable": {
                    "type": "boolean",
                    "description": "可选，只查有可升级版本的已安装包"
                }
            }
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("full",),
    ),
    ServerToolSpec(
        name="install_winget_package",
        handler=_handle_install_winget_package,
        description=(
            "用 winget 安装或升级一个软件包；默认只回将要执行的参数预览，"
            "execute=true 且 allowDestructive=true 才执行（会下载并运行安装器）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "winget 包标识（如 7zip.7zip；可从 search_winget_packages 的返回里取）"
                },
                "upgrade_only": {
                    "type": "boolean",
                    "description": "可选，只升级已安装的包，不新装"
                },
                "source": {
                    "type": "string",
                    "description": "可选，限定 winget 源（如 winget、msstore）"
                },
                "execute": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 才真的执行安装/升级"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "execute=true 时必须显式传 true"
                }
            },
            "required": ["identifier"]
        },
        annotations={"openWorldHint": True, "destructiveHint": True},
        exposure=("full",),
    ),
    ServerToolSpec(
        name="check_package_updates",
        handler=_handle_check_package_updates,
        description=(
            "查哪些包有可用更新；默认零 spawn（读 scoop 已装版本 vs 本地 bucket 索引），"
            "execute=true 才问 UniGetUI（可带 manager 查 winget/pip 等，需本机 UniGetUI 在运行）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "description": (
                        "可选，只查某个安装器（UniGetUI 的稳定 id，如 scoop / winget / pip / "
                        "npm / chocolatey / dotnet-tool）；纯读盘口径只覆盖 scoop"
                    )
                },
                "execute": {
                    "type": "boolean",
                    "description": (
                        "可选，默认 false。true 才 spawn UniGetUI 的只读查询"
                        "（探不到会话时自动退回读盘口径，不冷启动它的窗口）"
                    )
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "可选，UniGetUI 命令的超时秒数（默认 120，上限 600；纯读盘不跑命令）"
                    )
                },
            }
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
        exposure=("full",),
    ),
    ServerToolSpec(
        name="list_package_managers",
        handler=_handle_list_package_managers,
        description=(
            "列出本机 UniGetUI 能驱动的安装器及其就绪状态（scoop / winget / pip 等），"
            "可选带软件源清单；需要 UniGetUI 正在运行，只读，不冷启动 GUI"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_sources": {
                    "type": "boolean",
                    "description": "可选，默认 false。true 时附带各安装器的软件源清单"
                },
                "timeout": {
                    "type": "integer",
                    "description": "可选，UniGetUI 命令的超时秒数（默认 120，上限 600）"
                },
            }
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("full",),
    ),
    ServerToolSpec(
        name="search_uniget_packages",
        handler=_handle_search_uniget_packages,
        description=(
            "在指定安装器的远端索引里搜索可安装的包（只读）；manager 必填，"
            "需要 UniGetUI 正在运行"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "description": (
                        "必填，安装器稳定 id（如 scoop / winget / pip / npm / "
                        "chocolatey / dotnet-tool）"
                    )
                },
                "keyword": {
                    "type": "string",
                    "description": "必填，包名或 id 关键词"
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多返回多少条（默认 20，上限 100）"
                },
                "timeout": {
                    "type": "integer",
                    "description": "可选，UniGetUI 命令的超时秒数（默认 120，上限 600）"
                },
            },
            "required": ["manager", "keyword"],
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("full",),
    ),
    ServerToolSpec(
        name="list_uniget_packages",
        handler=_handle_list_uniget_packages,
        description=(
            "列出指定安装器里已安装的包及其版本（只读）；manager 必填，"
            "需要 UniGetUI 正在运行"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "description": (
                        "必填，安装器稳定 id（如 scoop / winget / pip / npm / "
                        "chocolatey / dotnet-tool）"
                    )
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多返回多少条（默认 50，上限 100）"
                },
                "timeout": {
                    "type": "integer",
                    "description": "可选，UniGetUI 命令的超时秒数（默认 120，上限 600）"
                },
            },
            "required": ["manager"],
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("full",),
    ),
    ServerToolSpec(
        name="get_package_details",
        handler=_handle_get_package_details,
        description=(
            "查单个包的详情（描述、主页、许可、安装位置等），默认附带版本历史（只读）；"
            "manager 与 package 必填，需要 UniGetUI 正在运行"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "manager": {
                    "type": "string",
                    "description": (
                        "必填，安装器稳定 id（如 scoop / winget / pip / npm / "
                        "chocolatey / dotnet-tool）"
                    )
                },
                "package": {
                    "type": "string",
                    "description": "必填，包 id（从 search_uniget_packages / list_uniget_packages 的 name 取）"
                },
                "include_versions": {
                    "type": "boolean",
                    "description": "可选，默认 true。false 时跳过版本历史查询"
                },
                "timeout": {
                    "type": "integer",
                    "description": "可选，UniGetUI 命令的超时秒数（默认 120，上限 600）"
                },
            },
            "required": ["manager", "package"],
        },
        annotations={"openWorldHint": True, "destructiveHint": False},
        exposure=("full",),
    ),
)

# ToolAnnotations 的合法键（MCP 协议 hints 全集）。validate_specs 用它拦
# 「注解键拼错 → 运行期才炸」。
_ANNOTATION_KEYS = frozenset(
    {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
)


def build_mcp_tools(mode: str = "lazy") -> list[Tool]:
    """由声明表统一构造**指定暴露模式下**的 `Tool` 列表。

    过滤只发生在导出层：`SERVER_TOOL_SPECS` 始终是完整声明表，policy /
    usage / dispatch 的门禁与埋点不因模式而分叉；未暴露的工具在
    `server.dispatch_tool` 侧同样会被拒（不能靠「名字没下发」当安全边界）。

    挂载逻辑与搬迁前 `list_tools()` 逐行等价（`if ToolAnnotations is not None
    and spec.annotations:` 的真值判断、`output_schema or SHARED_OUTPUT_SCHEMA`
    的回落都原样）；**有意的差异有两处**，都在「对外」这一侧：
    - `inputSchema` 过 `export_input_schema()`：带别名的工具多出别名属性与
      `anyOf`/`allOf` 任选组；
    - `description` 过 `export_description()`：末尾补上必填参数名与别名。
    声明表本身仍只写规范名，两处导出都是纯函数。
    """
    from exposure import normalize_mode

    mode = normalize_mode(mode)
    tools: list[Tool] = []
    for spec in SERVER_TOOL_SPECS:
        if not is_exposed(spec, mode):
            continue
        tool = Tool(
            name=spec.name,
            description=export_description(spec),
            inputSchema=export_input_schema(spec),
        )
        if ToolAnnotations is not None and spec.annotations:
            tool.annotations = ToolAnnotations(**spec.annotations)
        tool.output_schema = spec.output_schema or SHARED_OUTPUT_SCHEMA
        tools.append(tool)
    return tools


def get_spec(name: str) -> ServerToolSpec | None:
    """按工具名查声明；未命中返回 None（分发层转 `unknown_tool`）。"""
    return _SPECS_BY_NAME.get(name)


def validate_specs() -> None:
    """import 期自检：spec 表自身损坏时快速失败，而不是带病运行。

    检查五类问题：
    - 重名：名字索引会静默丢掉一条，tools/list 少一个工具且无人察觉；
    - 空 handler：新增工具忘了写 handler，要到运行期才暴露；
    - annotations 键非法：`ToolAnnotations(**{...})` 拼错键运行期才炸；
    - exposure 取值非法或为空：该工具会在任何模式下都不可见（或不存在的模式）；
    - 别名规范名拼错：`arg_aliases` 的键必须是本工具已声明的参数，否则那条
      覆盖永远不生效（别名白写、报错还归到「Agent 猜错」头上）；
    - 别名撞车 / 复活退役名：别名不得与本工具任何已声明参数同名（两个规范名
      抢一个别名时映射结果取决于 dict 顺序），也不得是 `mcp_tool_id` /
      `launcher_id` 这类已统一掉的退役名。

    对合法输入零差异——这是本设计「把一致性从约定变成强制」的核心机制。
    """
    from exposure import MODES

    seen: set[str] = set()
    for spec in SERVER_TOOL_SPECS:
        if spec.name in seen:
            raise RuntimeError(f"tool_specs: 工具名重复: {spec.name}")
        seen.add(spec.name)
        if spec.handler is None:
            raise RuntimeError(f"tool_specs: {spec.name} 缺少 handler")
        if not spec.exposure:
            raise RuntimeError(f"tool_specs: {spec.name} 的 exposure 不能为空")
        unknown_modes = set(spec.exposure) - set(MODES)
        if unknown_modes:
            raise RuntimeError(
                f"tool_specs: {spec.name} 的 exposure 含未知模式: "
                f"{sorted(unknown_modes)}（可选: {', '.join(MODES)}）"
            )
        if spec.annotations:
            unknown = set(spec.annotations) - _ANNOTATION_KEYS
            if unknown:
                raise RuntimeError(
                    f"tool_specs: {spec.name} 的 annotations 含非法键: "
                    f"{sorted(unknown)}"
                )
        declared = _declared_params(spec)
        alias_map = aliases_for(spec)
        for canon in spec.arg_aliases or {}:
            if canon not in declared:
                raise RuntimeError(
                    f"tool_specs: {spec.name} 的 arg_aliases 键 {canon} "
                    "不是已声明的参数（拼错了？）"
                )
        owners: dict[str, str] = {}
        for canon, aliases in alias_map.items():
            for alias in aliases:
                if alias in RETIRED_PARAM_NAMES:
                    raise RuntimeError(
                        f"tool_specs: {spec.name} 的别名 {alias} 是已退役参数名，"
                        "不得复活"
                    )
                if alias in declared:
                    raise RuntimeError(
                        f"tool_specs: {spec.name} 的别名 {alias}（{canon} 的别名）"
                        "与本工具已声明参数同名"
                    )
                if alias in owners:
                    raise RuntimeError(
                        f"tool_specs: {spec.name} 的别名 {alias} 同时是 "
                        f"{owners[alias]} 与 {canon} 的别名"
                    )
                owners[alias] = canon


# 名字 → spec 索引（validate_specs 保证无重名，建索引才有意义）
_SPECS_BY_NAME: dict[str, ServerToolSpec] = {
    spec.name: spec for spec in SERVER_TOOL_SPECS
}

# import 期自检（NE-3）：spec 表自身损坏（重名 / 空 handler / 非法注解键）时
# 快速失败，而不是带病运行。对合法输入零差异；M03 迁移期曾容忍 handler=None
# （脚手架回落旧分支），脚手架删除后（M04）起强制完整。
validate_specs()
