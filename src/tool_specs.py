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
本模块管辖**对外 MCP 工具**（协议层暴露的 22 个 `tools/call` 入口）。
两者字段名有重叠但语义不同，互不 import、互不引用。
"""

import json
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from mcp.types import Tool

try:  # Tool annotations 同样是 mcp>=1.9 才引入的能力
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    ToolAnnotations = None

from errors import ErrorType, ToolError, fail, ensure_success  # noqa: F401（handler 侧经本模块转手）
from app_launcher import launch_app
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
      复现搬迁前现状：`set_backend_log_level` 的注解就是 `{}`，而原
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
    # 本条工具专属的「规范名 → 别名」覆盖。未列出的规范名回落
    # `DEFAULT_ARG_ALIASES`；列成空元组表示该规范名显式不接别名。
    arg_aliases: dict[str, tuple[str, ...]] | None = None


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
    return out


# ---- handlers（M03 分批搬迁）----
# 分支体从 server.dispatch_tool 逐字搬运，只做五类机械替换：
#   registry. → ctx.registry.、backend_manager. → ctx.backend_manager.、
#   launcher_manager. → ctx.launcher_manager.、store. → ctx.store.、log( → ctx.log(
# handler 返回**载荷对象**（dict）而非 JSON 文本——序列化统一由
# server.dispatch_tool 负责。本模块禁止出现裸 manager 名（R1：漏做替换
# 必报 NameError，第一批测试即红）。


def _lookup_action_type(
    ctx: ToolContext, tool_id: str, action_id: str | None, action_name: str | None
) -> str:
    """从动作清单缓存里取动作的 `type`（id 优先，其次 name）；找不到返回空串。

    `type` 只用于 `destructiveActions` 的 glob 匹配；动作清单是 partial 时
    （只有 ID、没有名称/类型）返回空串，匹配自然落空——附录 A 的既定语义：
    宁可漏拦（未命中）也不误放，部署者需按实际清单核对模式。
    缓存缺失 / 损坏不应影响门禁主流程，吞掉异常按空处理。
    """
    try:
        catalog = ctx.store.load_launcher_catalog(tool_id) or {}
    except Exception:  # noqa: BLE001 —— 缓存问题不阻断门禁
        return ""
    for action in catalog.get("actions") or []:
        if action_id and action.get("id") == action_id:
            return action.get("type") or ""
        if action_name and action.get("name") == action_name:
            return action.get("type") or ""
    return ""


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
    # policy（与 call_backend_tool / run_launcher_action 同一判定入口）；
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
                "后端子工具缓存为空：先 start_backend 或 sync_backend_tools 再取帮助"
            )
    elif kind == "launcher":
        catalog = ctx.store.load_launcher_catalog(tool_id) or {}
        actions = catalog.get("actions") or []
        payload["actions"] = launcher_summary(actions)
        payload["actionCount"] = len(actions)
        payload["lastSynced"] = catalog.get("lastSynced")
        payload["source"] = catalog.get("source")
        if not actions:
            payload["note"] = "动作清单缓存为空：先 sync_launcher_actions 再取帮助"

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


async def _handle_get_backend_status(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id")
    res = ensure_success(ctx.backend_manager.get_backend_status(tool_id))
    return res


async def _handle_call_backend_tool(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    tool_name = arguments.get("tool_name", "")
    backend_args = arguments.get("arguments") or {}

    # A2/A12 → ARC-P1-05 Phase 1：判定收敛到 policy.decide_destructive
    # （判定表与审计见 src/policy.py）。mcp 未配置 destructiveTools 时整后端拦，
    # 与接入前逐字等价；判定只算一次，转发后的 warning 复用同一结果。
    target_tool = ctx.registry.get_tool(tool_id)
    decision = decide_destructive(
        "mcp", target_tool, {"tool_name": tool_name}, arguments
    )
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
            toolName=tool_name,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认转发到 destructive 工具: {tool_id}/{tool_name}")

    ctx.log(f"路由调用: {tool_id}/{tool_name}")
    res = ensure_success(
        await ctx.backend_manager.call_backend_tool(tool_id, tool_name, backend_args)
    )
    # 与上面的拦截判定保持同源：免确认的子工具不该被标注成 destructive
    if decision["required_confirm"]:
        res["warning"] = f"目标工具 '{tool_id}/{tool_name}' 被判定为 destructive"
    return res


async def _handle_list_backend_tools(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    force_refresh = arguments.get("force_refresh", False)
    res = ensure_success(
        await ctx.backend_manager.list_backend_tools(tool_id, force_refresh=force_refresh)
    )
    return res


async def _handle_start_backend(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"启动后端: {tool_id}")
    res = ensure_success(await ctx.backend_manager.start_backend(tool_id))
    return res


async def _handle_stop_backend(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"停止后端: {tool_id}")
    res = ensure_success(await ctx.backend_manager.stop_backend(tool_id))
    return res


async def _handle_get_backend_logs(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    level = arguments.get("level")
    limit = arguments.get("limit", 50)
    res = ensure_success(ctx.backend_manager.get_backend_logs(tool_id, level, limit))
    return res


async def _handle_set_backend_log_level(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    level = arguments.get("level", "")
    res = ensure_success(ctx.backend_manager.set_backend_log_level(tool_id, level))
    return res


async def _handle_sync_backend_tools(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"手动同步后端工具目录: {tool_id}")
    res = ensure_success(await ctx.backend_manager.sync_tools_catalog(tool_id))
    return res


async def _handle_sync_launcher_actions(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    ctx.log(f"手动同步启动器动作清单: {tool_id}")
    res = ensure_success(ctx.launcher_manager.sync(tool_id))
    return res


async def _handle_list_launcher_actions(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    keyword = arguments.get("keyword")
    limit = arguments.get("limit")
    # A13：默认读缓存（陈旧会自动重扫）；refresh=true 强制重扫
    refresh = bool(arguments.get("refresh"))
    # list_actions 返回的是数组而非结果字典，不能用 ensure_success 包；
    # 启动器自己的错误转成带 errorType 的失败，而不是落到内部异常兜底
    try:
        actions = ctx.launcher_manager.list_actions(
            tool_id, keyword=keyword, limit=limit, refresh=refresh
        )
    except LauncherError as exc:
        fail(str(exc), error_type=ErrorType.LAUNCHER_ERROR, toolId=tool_id)
    # 带上 lastSynced / source：调用方要能判断这份清单有多旧、是否完整，
    # 否则「自动刷新」只是把不透明变得更不透明。
    catalog = ctx.store.load_launcher_catalog(tool_id) or {}
    return {
        "success": True,
        "toolId": tool_id,
        "count": len(actions),
        "lastSynced": catalog.get("lastSynced"),
        "source": catalog.get("source"),
        "actions": actions,
    }


async def _handle_run_launcher_action(arguments: dict, ctx: ToolContext) -> dict:
    tool_id = arguments.get("tool_id", "")
    target_tool = ctx.registry.get_tool(tool_id)
    # 执行动作是真的会跑起来（Quicker 动作可以是任何操作）：
    # 条目未标 destructive → 放行；配了 destructiveActions → 只拦命中动作
    # （glob）；未配 → 回退条目级全拦（与接入前逐字等价）。
    action_id = arguments.get("action_id")
    action_name = arguments.get("action_name")
    sub_op = {"action_id": action_id, "action_name": action_name, "action_type": ""}
    # 动作 type 只服务 glob 匹配；未配置 destructiveActions 时不引入额外 IO
    if target_tool and target_tool.get("destructiveActions") is not None:
        sub_op["action_type"] = _lookup_action_type(ctx, tool_id, action_id, action_name)
    decision = decide_destructive("launcher", target_tool, sub_op, arguments)
    if decision["blocked"]:
        fail(
            decision["reason"],
            error_type=decision["error_type"],
            toolId=tool_id,
        )
    if decision["required_confirm"]:
        ctx.log(f"⚠️ 已确认执行 destructive 启动器动作: {tool_id}")
    res = ensure_success(
        await ctx.launcher_manager.run_action(
            tool_id,
            action_id=action_id,
            action_name=action_name,
            param=arguments.get("param"),
            wait=arguments.get("wait"),
        )
    )
    return res


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
        # execute=true 会真的 spawn 进程，与 run_launcher_action 同款只标 openWorld
        annotations={"openWorldHint": True},
    ),
    ServerToolSpec(
        name="call_backend_tool",
        handler=_handle_call_backend_tool,
        description="路由调用：转发请求到指定 MCP 工具的后端 Server 执行具体操作",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID（如 everything-search-mcp）"
                },
                "tool_name": {
                    "type": "string",
                    "description": "要调用的后端工具名"
                },
                "arguments": {
                    "type": "object",
                    "description": "调用参数"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "目标工具被标记为 destructive 时，需显式传 true 才会转发"
                }
            },
            "required": ["tool_id", "tool_name"]
        },
        # 这里的 `name` 指的是**后端子工具**名，不是本服务的工具 ID——
        # 所以不能用默认表（那条会把 name 映射到 tool_id，语义正好反）。
        # `tool_id` 仍吃默认表的 `id`，两个名字在本工具里不打架。
        arg_aliases={
            "tool_name": ("name",),
        },
        # 转发到后端 MCP，属于"与外部世界交互"但静态无法判定副作用
        annotations={"openWorldHint": True},
    ),
    ServerToolSpec(
        name="list_backend_tools",
        handler=_handle_list_backend_tools,
        description="列出指定 MCP 工具的后端提供的所有子工具（含完整参数 schema）",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID"
                },
                "force_refresh": {
                    "type": "boolean",
                    "description": "是否强制刷新缓存"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="start_backend",
        handler=_handle_start_backend,
        description="启动指定的后端 MCP Server 并建立连接",
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
        name="stop_backend",
        handler=_handle_stop_backend,
        description="停止指定的后端 MCP Server",
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
        name="get_backend_status",
        handler=_handle_get_backend_status,
        description="获取后端 MCP Server 的运行状态",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "MCP 工具 ID，不填则返回所有后端状态"
                }
            }
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="get_backend_logs",
        handler=_handle_get_backend_logs,
        description="获取后端 MCP Server 的运行日志",
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
        name="set_backend_log_level",
        handler=_handle_set_backend_log_level,
        description="设置后端 MCP Server 的日志级别",
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
        # 搬迁前即不挂注解（TOOL_ANNOTATION_HINTS 里是 {}），保留原样
        annotations={},
    ),
    ServerToolSpec(
        name="sync_backend_tools",
        handler=_handle_sync_backend_tools,
        description="手动同步后端 MCP 的工具目录（自动获取所有工具的参数 schema 并保存）",
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
        name="sync_launcher_actions",
        handler=_handle_sync_launcher_actions,
        description="手动同步启动器的动作清单（从启动器本地数据扫描，Quicker 的动作名依赖其缓存，可能只拿到 ID）",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "启动器工具 ID"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"idempotentHint": True},
    ),
    ServerToolSpec(
        name="list_launcher_actions",
        handler=_handle_list_launcher_actions,
        description="列出/搜索启动器（如 Quicker）的动作：按名称、描述、所属程序或 ID 关键词过滤",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "启动器工具 ID"
                },
                "keyword": {
                    "type": "string",
                    "description": "可选，按名称/描述/所属程序/ID 过滤"
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多返回多少条"
                },
                "refresh": {
                    "type": "boolean",
                    "description": "可选，强制重新扫描启动器本地数据（默认读缓存，缓存超过 5 分钟会自动重扫）"
                }
            },
            "required": ["tool_id"]
        },
        annotations={"readOnlyHint": True, "destructiveHint": False},
    ),
    ServerToolSpec(
        name="run_launcher_action",
        handler=_handle_run_launcher_action,
        description="调用启动器的动作（如执行一个 Quicker 动作），可传参并等待返回结果",
        input_schema={
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "启动器工具 ID"
                },
                "action_id": {
                    "type": "string",
                    "description": "动作 ID（推荐：稳定不变；二选一，与 action_name 至少给一个）"
                },
                "action_name": {
                    "type": "string",
                    "description": "动作名称（Quicker 要求名称不重复且不含空格）"
                },
                "param": {
                    "type": "string",
                    "description": "可选，传给动作的参数（Quicker 用 ? 分隔符，不支持换行）"
                },
                "wait": {
                    "type": "number",
                    "description": "可选，最长等待秒数（默认取 launcher.timeout，缺省 20）"
                },
                "allowDestructive": {
                    "type": "boolean",
                    "description": "启动器被标记为 destructive 时，需显式传 true 才会执行"
                }
            },
            "required": ["tool_id"]
        },
        # 本工具的 `id` / `name` 天然指**动作**（调用方通常已把 tool_id 写全），
        # 不能让默认表把它们当成 tool_id 的别名——那会把动作名折成工具 ID。
        # 而且这里 tool_id 与动作是**两个都要给**的参数：`id` 若同时归 tool_id，
        # `{"tool_id":"quicker","id":"<动作>"}` 会撞成「同一参数取值不同」直接报错。
        # 所以 `id`/`name` 让给 action_id/action_name，tool_id 在本工具显式不接别名。
        arg_aliases={
            "tool_id": (),
            "action_id": ("id",),
            "action_name": ("name",),
        },
        # 执行动作会真的跑起来，只标 openWorld
        annotations={"openWorldHint": True},
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
)

# ToolAnnotations 的合法键（MCP 协议 hints 全集）。validate_specs 用它拦
# 「注解键拼错 → 运行期才炸」。
_ANNOTATION_KEYS = frozenset(
    {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
)


def build_mcp_tools() -> list[Tool]:
    """由声明表统一构造 `Tool` 列表（annotations / outputSchema 在此挂载）。

    挂载逻辑与搬迁前 `list_tools()` 逐行等价（`if ToolAnnotations is not None
    and spec.annotations:` 的真值判断、`output_schema or SHARED_OUTPUT_SCHEMA`
    的回落都原样）；**有意的差异有两处**，都在「对外」这一侧：
    - `inputSchema` 过 `export_input_schema()`：带别名的工具多出别名属性与
      `anyOf`/`allOf` 任选组；
    - `description` 过 `export_description()`：末尾补上必填参数名与别名。
    声明表本身仍只写规范名，两处导出都是纯函数。
    """
    tools: list[Tool] = []
    for spec in SERVER_TOOL_SPECS:
        tool = Tool(
            name=spec.name,
            description=export_description(spec),
            inputSchema=export_input_schema(spec),
        )
        if ToolAnnotations is not None and spec.annotations:
            tool.annotations = ToolAnnotations(**spec.annotations)
        tool.outputSchema = spec.output_schema or SHARED_OUTPUT_SCHEMA
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
    - 别名规范名拼错：`arg_aliases` 的键必须是本工具已声明的参数，否则那条
      覆盖永远不生效（别名白写、报错还归到「Agent 猜错」头上）；
    - 别名撞车 / 复活退役名：别名不得与本工具任何已声明参数同名（两个规范名
      抢一个别名时映射结果取决于 dict 顺序），也不得是 `mcp_tool_id` /
      `launcher_id` 这类已统一掉的退役名。

    对合法输入零差异——这是本设计「把一致性从约定变成强制」的核心机制。
    """
    seen: set[str] = set()
    for spec in SERVER_TOOL_SPECS:
        if spec.name in seen:
            raise RuntimeError(f"tool_specs: 工具名重复: {spec.name}")
        seen.add(spec.name)
        if spec.handler is None:
            raise RuntimeError(f"tool_specs: {spec.name} 缺少 handler")
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
