"""awam-tool-manager MCP Server

统一工具管理的 MCP 服务，提供工具搜索、列出、启动等能力。
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.types import Tool, TextContent
import mcp.server.stdio

# 添加 src 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from store import JsonStore
from registry import ToolRegistry
from backend_client import BackendManager

# 数据目录
DATA_DIR = Path(__file__).parent.parent / "data"

# 初始化
store = JsonStore(DATA_DIR)
registry = ToolRegistry(store)
backend_manager = BackendManager(store)

# 创建 MCP Server
app = Server("awam-tool-manager")


def log(msg: str):
    """日志输出到 stderr，不污染 stdout 协议通道"""
    print(f"[awam-tool-manager] {msg}", file=sys.stderr, flush=True)


@app.list_tools()
async def list_tools() -> list[Tool]:
    """列出所有可用工具"""
    log("收到 list_tools 请求")
    tools = [
        Tool(
            name="search_tools",
            description="搜索已注册的工具，按关键词匹配名称、描述、标签",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词"
                    },
                    "kind": {
                        "type": "string",
                        "description": "可选，按工具类型筛选（如 app、mcp、driver）"
                    }
                },
                "required": ["keyword"]
            }
        ),
        Tool(
            name="list_tools",
            description="列出所有已注册的工具，可按类型筛选",
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "可选，按工具类型筛选（如 app、mcp、driver）"
                    }
                }
            }
        ),
        Tool(
            name="get_tool_info",
            description="查看工具的详细信息",
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "工具 ID，如 everything、everything-search-mcp"
                    }
                },
                "required": ["tool_id"]
            }
        ),
        Tool(
            name="launch_tool",
            description="启动指定的工具，返回启动命令信息",
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "工具 ID"
                    }
                },
                "required": ["tool_id"]
            }
        ),
        Tool(
            name="call_backend_tool",
            description="路由调用：转发请求到指定 MCP 工具的后端 Server 执行具体操作",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_tool_id": {
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
                    }
                },
                "required": ["mcp_tool_id", "tool_name"]
            }
        ),
        Tool(
            name="list_backend_tools",
            description="列出指定 MCP 工具的后端提供的所有子工具（含完整参数 schema）",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_tool_id": {
                        "type": "string",
                        "description": "MCP 工具 ID"
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "description": "是否强制刷新缓存"
                    }
                },
                "required": ["mcp_tool_id"]
            }
        ),
        Tool(
            name="start_backend",
            description="启动指定的后端 MCP Server 并建立连接",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_tool_id": {
                        "type": "string",
                        "description": "MCP 工具 ID"
                    }
                },
                "required": ["mcp_tool_id"]
            }
        ),
        Tool(
            name="stop_backend",
            description="停止指定的后端 MCP Server",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_tool_id": {
                        "type": "string",
                        "description": "MCP 工具 ID"
                    }
                },
                "required": ["mcp_tool_id"]
            }
        ),
        Tool(
            name="get_backend_status",
            description="获取后端 MCP Server 的运行状态",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_tool_id": {
                        "type": "string",
                        "description": "MCP 工具 ID，不填则返回所有后端状态"
                    }
                }
            }
        ),
        Tool(
            name="get_backend_logs",
            description="获取后端 MCP Server 的运行日志",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_tool_id": {
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
                "required": ["mcp_tool_id"]
            }
        ),
        Tool(
            name="set_backend_log_level",
            description="设置后端 MCP Server 的日志级别",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_tool_id": {
                        "type": "string",
                        "description": "MCP 工具 ID"
                    },
                    "level": {
                        "type": "string",
                        "description": "日志级别（debug/info/warning/error）"
                    }
                },
                "required": ["mcp_tool_id", "level"]
            }
        ),
        Tool(
            name="sync_backend_tools",
            description="手动同步后端 MCP 的工具目录（自动获取所有工具的参数 schema 并保存）",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_tool_id": {
                        "type": "string",
                        "description": "MCP 工具 ID"
                    }
                },
                "required": ["mcp_tool_id"]
            }
        ),
    ]
    log(f"返回 {len(tools)} 个工具定义")
    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """处理工具调用"""
    log(f"收到工具调用: {name} | 参数: {arguments}")

    result = ""

    if name == "search_tools":
        keyword = arguments.get("keyword", "")
        kind = arguments.get("kind")
        tools = registry.search_tools(keyword, kind)
        result = json.dumps({
            "success": True,
            "keyword": keyword,
            "count": len(tools),
            "tools": [
                {
                    "id": t["id"],
                    "name": t.get("displayName", t["name"]),
                    "kind": t.get("kind", "unknown"),
                    "description": t.get("description", "")[:100],
                    "status": t.get("status", "installed")
                }
                for t in tools
            ]
        }, ensure_ascii=False, indent=2)

    elif name == "list_tools":
        kind = arguments.get("kind")
        tools = registry.list_all_tools(kind)
        result = json.dumps({
            "success": True,
            "count": len(tools),
            "tools": [
                {
                    "id": t["id"],
                    "name": t.get("displayName", t["name"]),
                    "kind": t.get("kind", "unknown"),
                    "description": t.get("description", "")[:100],
                    "status": t.get("status", "installed")
                }
                for t in tools
            ]
        }, ensure_ascii=False, indent=2)

    elif name == "get_tool_info":
        tool_id = arguments.get("tool_id", "")
        tool = registry.get_tool(tool_id)

        if not tool:
            result = json.dumps({
                "success": False,
                "error": f"未找到工具: {tool_id}"
            }, ensure_ascii=False, indent=2)
        else:
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
                backend_res = backend_manager.get_backend_status(tool_id)
                if backend_res.get("success"):
                    result_data["backend"] = backend_res.get("status", {})

                backend_tools = registry.list_backend_tools(tool_id)
                result_data["backendTools"] = [
                    {
                        "name": t["name"],
                        "description": t.get("description", "")
                    }
                    for t in backend_tools
                ]

            result = json.dumps(result_data, ensure_ascii=False, indent=2, default=str)

    elif name == "launch_tool":
        tool_id = arguments.get("tool_id", "")
        res = registry.launch_tool(tool_id)
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "call_backend_tool":
        mcp_tool_id = arguments.get("mcp_tool_id", "")
        tool_name = arguments.get("tool_name", "")
        backend_args = arguments.get("arguments", {})

        log(f"路由调用: {mcp_tool_id}/{tool_name}")
        res = await backend_manager.call_backend_tool(mcp_tool_id, tool_name, backend_args)
        result = json.dumps(res, ensure_ascii=False, indent=2, default=str)

    elif name == "list_backend_tools":
        mcp_tool_id = arguments.get("mcp_tool_id", "")
        force_refresh = arguments.get("force_refresh", False)
        res = await backend_manager.list_backend_tools(mcp_tool_id, force_refresh=force_refresh)
        result = json.dumps(res, ensure_ascii=False, indent=2, default=str)

    elif name == "start_backend":
        mcp_tool_id = arguments.get("mcp_tool_id", "")
        log(f"启动后端: {mcp_tool_id}")
        res = await backend_manager.start_backend(mcp_tool_id)
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "stop_backend":
        mcp_tool_id = arguments.get("mcp_tool_id", "")
        log(f"停止后端: {mcp_tool_id}")
        res = await backend_manager.stop_backend(mcp_tool_id)
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "get_backend_status":
        mcp_tool_id = arguments.get("mcp_tool_id")
        res = backend_manager.get_backend_status(mcp_tool_id)
        result = json.dumps(res, ensure_ascii=False, indent=2, default=str)

    elif name == "get_backend_logs":
        mcp_tool_id = arguments.get("mcp_tool_id", "")
        level = arguments.get("level")
        limit = arguments.get("limit", 50)
        res = backend_manager.get_backend_logs(mcp_tool_id, level, limit)
        result = json.dumps(res, ensure_ascii=False, indent=2, default=str)

    elif name == "set_backend_log_level":
        mcp_tool_id = arguments.get("mcp_tool_id", "")
        level = arguments.get("level", "")
        res = backend_manager.set_backend_log_level(mcp_tool_id, level)
        result = json.dumps(res, ensure_ascii=False, indent=2)

    elif name == "sync_backend_tools":
        mcp_tool_id = arguments.get("mcp_tool_id", "")
        log(f"手动同步后端工具目录: {mcp_tool_id}")
        res = await backend_manager.sync_tools_catalog(mcp_tool_id)
        result = json.dumps(res, ensure_ascii=False, indent=2)

    else:
        result = f"未知工具: {name}"

    log(f"工具 {name} 执行完成，返回 {len(result)} 字符")
    return [TextContent(type="text", text=result)]


# ============ Resources（资源） ============

from mcp.types import Resource, Prompt, PromptMessage, TextContent as MCPTextContent


@app.list_resources()
async def list_resources() -> list[Resource]:
    """列出所有可用资源"""
    log("收到 list_resources 请求")
    return [
        Resource(
            uri="awam://mcp-servers",
            name="MCP 服务器列表",
            description="所有已接入的 MCP 服务器",
            mimeType="application/json"
        ),
        Resource(
            uri="awam://tools",
            name="工具列表",
            description="所有已注册的工具",
            mimeType="application/json"
        ),
        Resource(
            uri="awam://backends/status",
            name="后端状态",
            description="所有后端 MCP Server 的运行状态",
            mimeType="application/json"
        ),
    ]


@app.read_resource()
async def read_resource(uri: str) -> list[MCPTextContent]:
    """读取资源内容"""
    log(f"收到 read_resource 请求: {uri}")

    if uri == "awam://mcp-servers":
        import json
        mcp_tools = registry.list_all_tools(kind="mcp")
        return [MCPTextContent(type="text", text=json.dumps(mcp_tools, ensure_ascii=False, indent=2, default=str))]

    elif uri == "awam://tools":
        import json
        tools = registry.list_all_tools()
        return [MCPTextContent(type="text", text=json.dumps(tools, ensure_ascii=False, indent=2, default=str))]

    elif uri == "awam://backends/status":
        import json
        res = backend_manager.get_backend_status()
        return [MCPTextContent(type="text", text=json.dumps(res, ensure_ascii=False, indent=2, default=str))]

    else:
        return [MCPTextContent(type="text", text=f"未知资源: {uri}")]


# ============ Prompts（提示模板） ============

@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    """列出所有可用提示模板"""
    log("收到 list_prompts 请求")
    return [
        Prompt(
            name="getting_started",
            description="awam-tool-manager 快速入门指南"
        ),
        Prompt(
            name="how_to_search",
            description="如何搜索和查找工具"
        ),
        Prompt(
            name="how_to_call_backend",
            description="如何调用后端 MCP 工具"
        ),
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None) -> list[PromptMessage]:
    """获取提示模板内容"""
    log(f"收到 get_prompt 请求: {name}")
    arguments = arguments or {}

    if name == "getting_started":
        text = """# awam-tool-manager 快速入门

awam-tool-manager 是一个统一的工具管理器，聚合各种 MCP 工具和应用。

## 核心能力

1. **搜索工具**：用 `search_tools` 按关键词搜索已注册的工具
2. **查看详情**：用 `get_tool_info` 查看工具的详细信息
3. **列出工具**：用 `list_tools` 查看所有已注册的工具（可按类型筛选）
4. **调用后端**：用 `call_backend_tool` 路由调用后端 MCP 的工具
5. **管理后端**：用 `start_backend` / `stop_backend` / `get_backend_status` 管理后端连接

## 快速流程

1. 先 `list_tools` 看看有哪些工具
2. 然后 `list_backend_tools` 看看某个 MCP 工具下有什么子工具
3. 最后 `call_backend_tool` 调用具体工具
"""

    elif name == "how_to_search":
        text = """# 如何搜索工具

## 基本搜索

调用 `search_tools`，传入关键词：
- 关键词会匹配工具名称、描述、标签
- 可以用 `source` 参数按启动器筛选

## 列出所有工具

调用 `list_tools`，可以按来源筛选。

## 查看工具详情

调用 `get_tool_info`，传入工具 ID（格式：`启动器ID:工具名`）。

## 示例

1. 搜索"文件"相关工具：`search_tools(keyword="文件")`
2. 只看 Everything 来源：`list_tools(source="everything")`
3. 查看某个工具详情：`get_tool_info(tool_id="everything:main")`
"""

    elif name == "how_to_call_backend":
        text = """# 如何调用后端 MCP 工具

## 步骤

1. **查看后端状态**：`get_backend_status(launcher_id="everything")`
2. **如果未运行，先启动**：`start_backend(launcher_id="everything")`
3. **列出后端工具**：`list_backend_tools(launcher_id="everything")`
   - 这会返回每个工具的名称、描述、参数 schema
4. **调用工具**：`call_backend_tool(launcher_id="everything", tool_name="everything_search", arguments={"query": "测试"})`

## 注意事项

- 工具名必须和 `list_backend_tools` 返回的一致（注意大小写）
- 参数必须匹配 inputSchema 的定义，必填参数不能少
- 如果连接失败，先 `get_backend_logs` 看看日志
- 可以用 `set_backend_log_level` 调整日志级别

## 错误处理

- `connection_error`：后端未运行，用 `start_backend` 启动
- `runtime_error`：参数错误或工具执行失败，检查参数
- `config_error`：启动器配置有问题
"""

    else:
        text = f"未知提示模板: {name}"

    return [PromptMessage(role="user", content=TextContent(type="text", text=text))]


async def main():
    """启动 MCP Server"""
    log("=" * 50)
    log("awam-tool-manager MCP Server 启动中...")
    log(f"数据目录: {DATA_DIR}")
    all_tools = registry.list_all_tools()
    mcp_tools = [t for t in all_tools if t.get("kind") == "mcp"]
    log(f"已加载 {len(all_tools)} 个工具: {', '.join([t['id'] for t in all_tools])}")
    log(f"其中 MCP 类工具: {len(mcp_tools)} 个")
    log("等待客户端连接...")
    log("=" * 50)

    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options()
            )
    finally:
        log("服务退出，关闭所有后端连接...")
        await backend_manager.close_all()
        log("清理完成")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log(f"服务异常退出: {e}")
        raise
