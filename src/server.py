"""awam-tool-manager MCP Server

统一工具管理的 MCP 服务，提供工具搜索、列出、启动等能力。
"""

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

from mcp import MCPError
from mcp.server import Server
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    GetPromptRequestParams,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListToolsResult,
    Prompt,
    PromptMessage,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)
import mcp.server.stdio

# 添加 src 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from store import JsonStore
from runtime_paths import get_runtime_dir, migrate_legacy_runtime
from registry import ToolRegistry
# 注意：`ensure_npm_bin_on_path` 已从这里移除——启动预热不再无条件探测 npm
# 全局 bin（#28 续，见 `_prewarm_backends` 内注释）。它现在只由
# `BackendClient.ensure_installed()` 的慢路径调用，server 不再需要这个名字。
from backend_client import BackendClient, BackendManager
from launcher_client import LauncherManager
from schema import schema_backend_available
# P1-04：业务错误三件套搬到 errors.py（tool_specs 的 handlers 要用，留在
# server.py 会形成 server → tool_specs → server 循环 import），这里继续
# re-export，`from server import ToolError` 等外部引用拿到的仍是同一个类对象。
from errors import ErrorType, ToolError, fail, ensure_success
# P1-04：对外工具声明式注册表——list_tools 的定义源与 dispatch 的查表目标。
# SHARED_OUTPUT_SCHEMA 已随常量搬到 tool_specs（连注释一起），这里 re-export
# 保护外部引用。ARC-P1-05 Phase 1 起破坏性判定收敛到 policy.decide_destructive
# （原 _needs_destructive_confirm 已删除，无外部引用）。
from tool_specs import (
    SHARED_OUTPUT_SCHEMA,  # noqa: F401
    ToolContext,
    build_mcp_tools,
    get_spec,
    is_exposed,
    normalize_arguments,
)
# ARC-P1-05 Phase 2：部署策略（data/policy.json）的 dispatch 前置检查。
# policy 在 import 期加载并校验策略（坏策略快速失败，不在这里处理）。
from policy import check_call
# 暴露模式（lazy / default / full）：进程启动时解析一次，决定 tools/list 的
# 切片与 initialize.instructions 的内容。dispatch 侧用同一份模式做兜底门禁
# ——「名字没下发」不能当成安全边界。
import exposure
# P3 使用统计：dispatch 出口记一行。**按模块引用**（不是 import 函数名），
# 单测才能 monkeypatch `usage.USAGE_LOG` 把统计写进 tmp。
import usage
# 可选 Streamable HTTP transport：默认 stdio，`--transport http` 才启用。
# 模块本身不 import uvicorn/starlette（在 serve_http 内延迟导入），stdio
# 路径不会因为少装 HTTP 依赖而启动失败。
import http_transport

# 数据目录：DATA_DIR 放人写的声明（进版本库），RUNTIME_DIR 放程序生成的
# 运行时数据（本机应用数据目录）。两者此前混在 data/ 下，导致活实例改写
# lastSynced 时工作区永远脏。见 runtime_paths.py。
DATA_DIR = Path(__file__).parent.parent / "data"
RUNTIME_DIR = get_runtime_dir()

# 初始化
store = JsonStore(DATA_DIR, runtime_dir=RUNTIME_DIR)
registry = ToolRegistry(store)
backend_manager = BackendManager(store)
launcher_manager = LauncherManager(registry, store)

# 暴露模式：环境变量 > data/exposure.json > lazy（非法配置在 import 期快速失败）
EXPOSURE_MODE = exposure.resolve_exposure_mode()

# 网关自身版本：写入 MCP serverInfo 与 HTTP /health。
# mcp 1.x 在未传 version 时会回落到 SDK 包版本（如 1.30.0），
# 会让下游 gatewayCompat 误把 SDK 版本当成网关版本。
GATEWAY_VERSION = "0.1.0"

# v2 起 lowlevel Server 的 handler 通过构造参数 on_* 注册，server 实例须在
# 所有 handler 定义之后统一构造（见本文件 _on_get_prompt 之后）。


def log(msg: str):
    """日志输出到 stderr，不污染 stdout 协议通道"""
    print(f"[awam-tool-manager] {msg}", file=sys.stderr, flush=True)


# P1-04：handler 依赖注入容器。tool_specs 的 handlers 不 import 任何 manager，
# 只从 ctx 取；测试 monkeypatch 的是 manager 对象属性，ctx 持有同一引用，
# patch 照样生效。
_TOOL_CONTEXT = ToolContext(
    registry=registry,
    backend_manager=backend_manager,
    launcher_manager=launcher_manager,
    store=store,
    log=log,
)


async def list_tools(_ctx, _params) -> ListToolsResult:
    """列出所有可用工具"""
    log("收到 list_tools 请求")
    # 定义由 tool_specs 声明表统一产出（annotations / output_schema 在
    # build_mcp_tools() 内挂载），此处只保留两行日志。
    tools = build_mcp_tools(EXPOSURE_MODE)

    log(f"返回 {len(tools)} 个工具定义")
    return ListToolsResult(tools=tools)


def _result_to_structured(result: str) -> dict | None:
    """把 dispatch_tool 返回的 JSON 文本解析成结构化结果（dict）。

    返回 None 表示无法解析（理论上不会发生：dispatch_tool 永远 json.dumps）。
    此时调用方应跳过 structuredContent，避免把非法结构透传给客户端。
    """
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    # 个别工具万一返回非对象（不应发生），用 {"value": ...} 包一层，保证结构化可用
    return {"value": parsed}


async def call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    """MCP tools/call 入口（v2）：成功与失败都返回完整 CallToolResult。

    v2 不再把 handler 抛出的异常自动转成 isError 结果——未捕获异常会变成
    协议层 JSON-RPC error，模型读不到 message。因此这里显式接管 v1 的语义：
    业务错误（ToolError）与其他异常都在此转成 CallToolResult(is_error=True)，
    正文仍是给客户端解析的 JSON，保持对外 wire 与 mcp 1.x 完全一致
    （AFM 的 mcp_client.rs 读 isError / structuredContent 不受影响）。
    """
    name = params.name
    arguments = params.arguments or {}
    log(f"收到工具调用: {name} | 参数: {arguments}")

    try:
        result = await dispatch_tool(name, arguments)
    except ToolError as e:
        # 业务错误：转成 is_error 结果，正文为 handler 产出的 JSON 载荷
        log(f"工具 {name} 业务失败: {e}")
        structured = _result_to_structured(str(e))
        content = [TextContent(type="text", text=str(e))]
        if structured is None:
            return CallToolResult(content=content, is_error=True)
        return CallToolResult(
            content=content, structured_content=structured, is_error=True
        )
    except Exception as e:
        log(f"工具 {name} 执行异常: {type(e).__name__}: {e}")
        payload = json.dumps(
            {
                "success": False,
                "errorType": ErrorType.INTERNAL_ERROR,
                "error": f"内部错误: {type(e).__name__}: {e}",
            },
            ensure_ascii=False,
        )
        return CallToolResult(
            content=[TextContent(type="text", text=payload)], is_error=True
        )

    structured = _result_to_structured(result)
    if structured is None:
        # 兜底：极少数情况下解析失败，退化为纯文本（不挂结构化，避免触发 output_schema 校验失败）
        log(f"⚠️ 工具 {name} 的返回无法解析为 JSON，仅返回文本")
        return CallToolResult(
            content=[TextContent(type="text", text=result)], is_error=False
        )

    log(f"工具 {name} 执行完成，返回 {len(result)} 字符")
    return CallToolResult(
        content=[TextContent(type="text", text=result)],
        structured_content=structured,
        is_error=False,
    )


async def dispatch_tool(name: str, arguments: dict) -> str:
    """按工具名分发，返回 JSON 文本"""
    # P1-04：查表分发，未命中即 unknown_tool（与旧 if/elif 全链落空等价，
    # 错误消息与 errorType 逐字一致）。handler 返回载荷对象（dict），
    # 序列化统一在这里做一处（NE-1：default=str 由「5 个工具有」统一为
    # 「15 个都有」——可达输入上的 no-op，方向是更不容易失败）。
    #
    # P3 使用统计埋点也放在这里：dispatch_tool 是「协议入口」和「单测直调」
    # 共同的唯一漏斗，成功/失败/被策略拦下都只在这一处出口，计数才不会漏。
    # 埋点是旁路（record_call 自己吞掉落盘错误），返回值与异常语义一字未改；
    # 副作用面只有运行时目录里多一行 usage.log（TASKS 申报 NE-9）。
    started = time.perf_counter()

    def _record(
        ok: bool,
        error_type: str | None = None,
        target: str | None = None,
        capability: str | None = None,
    ) -> None:
        usage.record_call(
            name,
            arguments,
            ok=ok,
            error_type=error_type,
            duration_ms=int((time.perf_counter() - started) * 1000),
            target=target,
            capability=capability,
        )

    spec = get_spec(name)
    if spec is None:
        _record(False, ErrorType.UNKNOWN_TOOL)
        fail(f"未知工具: {name}", error_type=ErrorType.UNKNOWN_TOOL, toolName=name)
    # 暴露模式兜底：未下发的工具在协议面等同于不存在（不泄露「有这个工具但
    # 没开」），与未知工具走同一条错误/埋点路径。
    if not is_exposed(spec, EXPOSURE_MODE):
        _record(False, ErrorType.UNKNOWN_TOOL)
        fail(f"未知工具: {name}", error_type=ErrorType.UNKNOWN_TOOL, toolName=name)
    # ARC-P1-05 Phase 2：策略层先于 handler——deny 名单 / 白名单命中即拒绝
    # （permission_denied），调用参数（含 allowDestructive）一律不看。
    denial = check_call(name)
    if denial is not None:
        _record(False, ErrorType.PERMISSION_DENIED)
        fail(denial["reason"], error_type=ErrorType.PERMISSION_DENIED, toolName=name)
    try:
        # 入参别名归一（`{"id": "everything"}` → `{"tool_id": "everything"}`）+
        # 必填缺失的自描述错误。放在 handler 之前、`_record` 之后：统计记的是
        # **归一后**的参数（`get_usage_stats` 的 tool_id 维度才不因别名漏计），
        # 归一失败的 errorType 也照样进台账。
        arguments = normalize_arguments(spec, arguments)
        payload = await spec.handler(arguments, _TOOL_CONTEXT)
    except ToolError as e:
        # errorType 从异常正文里取——那份 JSON 就是客户端看到的载荷，同源不另编
        _record(False, usage.error_type_from(e))
        raise
    except Exception:
        # 原样再抛：交给 call_tool 转成 internal_error（与埋点前的控制流一致）
        _record(False, ErrorType.INTERNAL_ERROR)
        raise
    # 意图工具用 `_usageTarget` 标注真实目标（后端 / scoop 包 / 启动器），
    # call_capability 再加 `_usageCapability` 标注具体能力；两个字段都只进
    # 统计，不进对外载荷。
    usage_target = None
    usage_capability = None
    if isinstance(payload, dict):
        usage_target = payload.pop("_usageTarget", None)
        usage_capability = payload.pop("_usageCapability", None)
    _record(True, target=usage_target, capability=usage_capability)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ============ Resources（资源） ============


async def list_resources(_ctx, _params) -> ListResourcesResult:
    """列出所有可用资源"""
    log("收到 list_resources 请求")
    return ListResourcesResult(
        resources=[
            Resource(
                uri="awam://mcp-servers",
                name="MCP 服务器列表",
                description="所有已接入的 MCP 服务器",
                mime_type="application/json",
            ),
            Resource(
                uri="awam://tools",
                name="工具列表",
                description="所有已注册的工具",
                mime_type="application/json",
            ),
            Resource(
                uri="awam://backends/status",
                name="后端状态",
                description="所有后端 MCP Server 的运行状态",
                mime_type="application/json",
            ),
        ]
    )


async def read_resource(_ctx, params: ReadResourceRequestParams) -> ReadResourceResult:
    """读取资源内容（v2）

    v2 里 `params.uri` 已是 str，无需再 str() 归一化（v1 的 `AnyUrl` 比较陷阱
    不复存在）。资源不存在时抛业务错误（ToolError），统一转成 JSON-RPC 协议
    错误（-32602 INVALID_PARAMS），正文保留可读原因。AFM 不消费 resources，
    仅 MCP agent 客户端可见。
    """
    uri = params.uri
    log(f"收到 read_resource 请求: {uri}")

    try:
        if uri == "awam://mcp-servers":
            payload = registry.list_all_tools(kind="mcp")
        elif uri == "awam://tools":
            payload = registry.list_all_tools()
        elif uri == "awam://backends/status":
            payload = backend_manager.get_backend_status()
        else:
            fail(
                f"未知资源: {uri}",
                error_type=ErrorType.UNKNOWN_RESOURCE,
                uri=uri,
            )
    except ToolError as e:
        raise MCPError(INVALID_PARAMS, str(e)) from e

    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return ReadResourceResult(
        contents=[
            TextResourceContents(uri=uri, mime_type="application/json", text=text)
        ]
    )


# ============ Prompts（提示模板） ============

async def list_prompts(_ctx, _params) -> ListPromptsResult:
    """列出所有可用提示模板"""
    log("收到 list_prompts 请求")
    return ListPromptsResult(
        prompts=[
            Prompt(name="getting_started", description="awam-tool-manager 快速入门指南"),
            Prompt(name="how_to_search", description="如何搜索和查找工具"),
            Prompt(name="how_to_call_capability", description="如何列出并调用工具的能力"),
        ]
    )


async def get_prompt(_ctx, params: GetPromptRequestParams) -> GetPromptResult:
    """获取提示模板内容"""
    name = params.name
    arguments = params.arguments or {}
    log(f"收到 get_prompt 请求: {name}")

    if name == "getting_started":
        text = """# awam-tool-manager 快速入门

awam-tool-manager 是一个统一的工具管理器，聚合各种 MCP 工具和应用。

## 核心能力

1. **搜索工具**：用 `search_tools` 按关键词搜索已注册的工具
2. **查看详情**：用 `get_tool_info` 查看工具的详细信息
3. **列出工具**：用 `list_tools` 查看所有已注册的工具（可按类型筛选）
4. **列出能力**：用 `list_capabilities` 查看某个工具提供的能力与参数 schema
5. **调用能力**：用 `call_capability` 执行 MCP 子工具 / 启动器动作 / 应用启动
6. **管理运行时**：用 `start_runtime` / `stop_runtime` / `get_runtime_status` 管理 MCP 运行时

## 快速流程

1. 先 `list_tools` 看看有哪些工具
2. 然后 `list_capabilities(tool_id=...)` 看看该工具有哪些能力、每个能力要什么参数
3. 最后 `call_capability(tool_id=..., capability_id=..., arguments={...})` 调用
"""

    elif name == "how_to_search":
        text = """# 如何搜索工具

## 基本搜索

调用 `search_tools`，传入关键词：
- 关键词会匹配工具的名称、显示名、描述、标签
- 可以用 `kind` 参数按类型筛选（app = 普通应用，mcp = MCP 工具）

## 列出所有工具

调用 `list_tools`，可按同样的 `kind` 筛选。

## 查看工具详情

调用 `get_tool_info`，传入工具 ID（即 `data/tools/` 下的文件名去掉 .json）。

## 示例

1. 搜索"文件"相关工具：`search_tools(keyword="文件")`
2. 只看 MCP 类工具：`list_tools(kind="mcp")`
3. 查看某个工具详情：`get_tool_info(tool_id="everything")`
"""

    elif name == "how_to_call_capability":
        text = """# 如何调用工具的能力

每个工具条目提供若干能力：MCP 工具是它的子工具，启动器是本地动作，
普通应用是固定的 `launch`。

## 步骤

1. **列出能力**：`list_capabilities(tool_id="everything-search-mcp")`
   - 每条能力给出 `capabilityId`、`risk`、`execution` 与完整 `inputSchema`
2. **组织参数**：`arguments` 必须符合该能力的 `inputSchema`（它放在嵌套对象里，
   不和 `execute` / `allowDestructive` 这些门禁参数混在一起）
3. **调用能力**：
   `call_capability(tool_id="everything-search-mcp", capability_id="search_files", arguments={"query": "测试"})`

MCP 运行时未启动时 `call_capability` 会自动拉起；需要预热或排查时再用
`start_runtime` / `get_runtime_status` / `get_runtime_logs`。

## 注意事项

- 能力 ID 必须和 `list_capabilities` 返回的 `capabilityId` 一致（注意大小写）
- 只读能力默认直接执行；写/破坏性能力默认只预览，`execute=true` 才执行
- 被标记为 destructive 的能力还需要显式 `allowDestructive=true`
- 启动器动作与启动应用还可能返回 `fireAndForget` / `deliveryOnly`：目标没有结果回传
- 能力缓存为空时先 `sync_capabilities(tool_id=...)`

## 错误处理

调用失败时本服务会返回 MCP 协议层的错误（isError），文本部分是 JSON：

- `connection_error`：运行时启动失败；用 `start_runtime` 重试并看日志
- `runtime_error`：参数错误或能力执行失败，对照 `inputSchema` 检查
- `config_error`：工具缺少 connection 配置，或 kind 不支持能力路由
- `not_found`：工具 ID 或能力 ID 不存在
- `unknown_tool`：工具名不存在
"""

    else:
        raise MCPError(
            INVALID_PARAMS,
            json.dumps(
                {
                    "success": False,
                    "errorType": ErrorType.UNKNOWN_PROMPT,
                    "error": f"未知提示模板: {name}",
                    "promptName": name,
                },
                ensure_ascii=False,
            ),
        )

    messages = [PromptMessage(role="user", content=TextContent(type="text", text=text))]
    return GetPromptResult(description=f"{name} 提示模板", messages=messages)


# 创建 MCP Server（v2：handler 通过 on_* 构造参数注册，instructions 按模式
# 生成——能力清单是「主动告知」的唯一通道）。放在所有 handler 定义之后。
app = Server(
    "awam-tool-manager",
    version=GATEWAY_VERSION,
    instructions=exposure.build_instructions(EXPOSURE_MODE),
    on_list_tools=list_tools,
    on_call_tool=call_tool,
    on_list_resources=list_resources,
    on_read_resource=read_resource,
    on_list_prompts=list_prompts,
    on_get_prompt=get_prompt,
)


async def _prewarm_backends():
    """启动预热：补 PATH + 后台检测各后端依赖，不阻塞协议服务就绪

    依赖自装（BackendClient.ensure_installed）保证「start_backend 时缺
    什么装什么」，但安装是重操作（npm i -g 可达数十秒），第一次调用就要
    现场等。这里在服务启动时后台预热一遍，把安装成本提前到启动阶段。

    这里**不再**无条件做 PATH 自愈（`ensure_npm_bin_on_path`）：该自愈已
    下放到 `BackendClient.ensure_installed()` 的慢路径（#28 续），「需要补」
    与「补得起」现在是同一个判断，预热阶段再补一遍是纯浪费。详见函数体内
    try 块开头的注释（含残余风险评估）。

    只检不启、异常全吞：预热失败不能拖垮服务本体——最坏情况退化为
    「首次 start_backend 时按需处理」，与没有预热时完全一致。

    **P0 之后预热默认不执行安装**：install.command 来自进版本库的数据文件，
    旧逻辑让它在服务启动时被无条件执行（无门禁）。现在默认只做检测，
    把「谁缺依赖、缺什么」如实写进启动日志；要恢复启动时自动安装，由
    部署者显式设 `AWAM_AUTO_INSTALL=1`（或逐条目配 `install.auto=true`）。
    """
    try:
        # ── 这里**不再**无条件 `await ensure_npm_bin_on_path()`（#28 续）────
        # 旧逻辑在预热前先补一遍 npm 全局 bin，理由是「上游网关可能用裁剪过
        # 的环境启动本服务（实测 toolport 传入的 PATH 不含 npm 全局 bin），
        # 不补的话 which 会把已装好的包误判成缺失」。这个理由在 #28 之后已经
        # 过期：PATH 自愈下放到了 `BackendClient.ensure_installed()` 的慢路径
        # ——先按当前 PATH 判一次，没判到才补 npm 全局 bin，补完再判一次。
        # 于是「需要补」与「补得起」变成同一个判断：
        #   - 启动命令已在 PATH 上 → 它本来就解析得到，补不补都不影响 spawn；
        #   - 启动命令不在 PATH 上 → 慢路径会补，判定结果与旧逻辑一致。
        # 实测（e2e 场景③，真实 PATH）：预热这条无条件调用是本进程**唯一**
        # 真正用到的 npm 探测，去掉后 `npm prefix -g` 由 1 次降到 0 次，
        # 场景③ 全链路再省 ≈1.8s（冷机该命令实测可达 6.5s）。
        #
        # **残余风险（已评估，接受）**：启动命令在 PATH 上、但后端**运行时**
        # 还需要 npm 全局 bin 里的**其他**命令时，PATH 不再被提前补齐。为什么
        # 可接受：这些后端本身是 npm 全局安装的 CLI，它要调用的兄弟命令都在
        # **自己的包内**（node_modules/.bin 或包内相对路径），不依赖 PATH 里
        # 的兄弟 bin；真正需要 PATH 解析的是「启动命令自己」，而那正是慢路径
        # 覆盖的部分。
        result = await backend_manager.ensure_all_installed()
        if result.get("installed"):
            log(
                f"启动预热：已自动安装 {result['installed']} 个后端依赖"
                f"（共 {result['total']} 个 MCP 工具，其余已就绪）"
            )
        elif result.get("blocked"):
            missing = [
                d["tool_id"]
                for d in result.get("details", [])
                if d.get("installBlocked")
            ]
            log(
                f"启动预热：{len(missing)} 个 MCP 工具依赖缺失但未授权自动安装"
                f"（{', '.join(missing)}）——本次不执行任何安装命令；"
                f"如需启动时自动安装，设环境变量 {BackendClient.INSTALL_ENV_VAR}=1"
            )
        elif result.get("total"):
            log(f"启动预热：{result['total']} 个 MCP 工具依赖均已就绪")
        else:
            log("启动预热：没有需要处理的 MCP 工具")
    except Exception as e:
        log(f"启动预热失败（不影响服务，将退化为首次调用时按需安装）: {type(e).__name__}: {e}")


async def _restore_backends():
    """状态恢复：把上次明确启动过的后端重新拉起来（A10）

    背景：toolport 网关周期性整池回收它拉起的 server 进程（实测约每几分钟
    一批）。每次回收后新 server 世代内存状态清零，所有后端回到 stopped——
    用户视角就是「启动后保不住，每次都得重新初始化」。start_backend 成功时
    会把启动意图写进 backend_desired_state.json，这里在每个新世代启动时
    据此恢复。

    与预热的分工：预热只装不启（所有 MCP 工具）；恢复只启 desired=running
    的（用户明确要过的）。恢复失败同样不阻断服务——最坏是用户下次调用时
    看到 stopped，再手启一次（意图仍在，下代还会尝试）。
    """
    try:
        result = await backend_manager.restore_desired_backends()
        if result:
            ok = sum(1 for r in result.values() if r.get("success"))
            log(f"状态恢复：{ok}/{len(result)} 个 desired 后端已重新拉起")
        else:
            log("状态恢复：没有需要恢复的后端")
    except Exception as e:
        log(f"状态恢复失败（不影响服务）: {type(e).__name__}: {e}")


async def main(
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
):
    """启动 MCP Server（默认 stdio；可切 Streamable HTTP）"""
    transport = http_transport.normalize_transport(transport)
    http_config = None
    if transport == "http":
        # 配置在启动日志之前解析：非法 host/port/token 直接失败，不打印
        # 一份「看起来要启动」的日志再崩。
        http_config = http_transport.resolve_http_config(RUNTIME_DIR, host=host, port=port)

    log("=" * 50)
    log("awam-tool-manager MCP Server 启动中...")
    log(f"数据目录: {DATA_DIR}")
    log(f"运行时目录: {RUNTIME_DIR}")
    log(
        f"暴露模式: {EXPOSURE_MODE}"
        f"（本次下发 {len(exposure.exposed_tool_names(EXPOSURE_MODE))} 个工具；"
        f"可选: {', '.join(exposure.MODES)}）"
    )
    if http_config:
        log(
            "传输方式: Streamable HTTP"
            f"（{http_config.host}:{http_config.port}{http_transport.MCP_PATH}，"
            f"token 来源: {http_config.token_source}）"
        )
    else:
        log("传输方式: stdio")

    # 旧版本把运行时数据写在仓库里（data/backend-tools、data/launcher-actions、
    # 根目录的 backend_desired_state.json），这里搬到运行时目录。只搬目标不存在的，
    # 失败不阻断启动。
    moved = migrate_legacy_runtime(DATA_DIR, RUNTIME_DIR, log=log)
    if moved:
        log(f"已迁移旧运行时数据到 {RUNTIME_DIR}: {', '.join(moved)}")

    all_tools = registry.list_all_tools()
    mcp_tools = [t for t in all_tools if t.get("kind") == "mcp"]
    log(f"已加载 {len(all_tools)} 个工具: {', '.join([t['id'] for t in all_tools])}")
    log(f"其中 MCP 类工具: {len(mcp_tools)} 个")
    log(
        "条目校验: "
        + ("jsonschema 完整校验" if schema_backend_available() else "基础校验（未安装 jsonschema）")
    )
    log("等待客户端连接...")
    log("=" * 50)

    # 后台预热后端依赖：安装是重操作，不能阻塞协议服务就绪
    prewarm_task = asyncio.create_task(_prewarm_backends())
    # 后台恢复上次启动过的后端：网关回收 server 进程后状态自愈（A10）
    restore_task = asyncio.create_task(_restore_backends())

    try:
        if http_config:
            await http_transport.serve_http(app, http_config, log=log)
        else:
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                await app.run(
                    read_stream,
                    write_stream,
                    app.create_initialization_options()
                )
    finally:
        for task in (prewarm_task, restore_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log("服务退出，关闭所有后端连接...")
        await backend_manager.close_all()
        log("清理完成")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="awam-tool-manager MCP Server（默认 stdio，可选 Streamable HTTP）",
    )
    parser.add_argument(
        "--transport",
        choices=http_transport.TRANSPORTS,
        default=None,
        help="传输方式；未指定时读 AWAM_TRANSPORT，再缺省为 stdio",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP 监听地址（默认读 AWAM_HTTP_HOST，再缺省 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP 监听端口（默认读 AWAM_HTTP_PORT，再缺省 18778）",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="打印 HTTP transport 使用的 token 后退出（不启动服务）",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    try:
        if args.print_token:
            transport = http_transport.normalize_transport(args.transport)
            if transport != "http":
                raise http_transport.HttpConfigError(
                    "--print-token 只适用于 HTTP transport（加 --transport http）"
                )
            config = http_transport.resolve_http_config(
                RUNTIME_DIR, host=args.host, port=args.port
            )
            print(config.token)
        else:
            asyncio.run(
                main(transport=args.transport, host=args.host, port=args.port)
            )
    except http_transport.HttpConfigError as e:
        log(f"配置错误: {e}")
        sys.exit(2)
    except http_transport.HttpTransportError as e:
        log(f"启动失败: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"服务异常退出: {e}")
        raise
