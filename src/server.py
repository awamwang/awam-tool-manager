"""awam-tool-manager MCP Server

统一工具管理的 MCP 服务，提供工具搜索、列出、启动等能力。
"""

import asyncio
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
                    "source": {
                        "type": "string",
                        "description": "可选，按启动器来源筛选（如 everything、steam）"
                    }
                },
                "required": ["keyword"]
            }
        ),
        Tool(
            name="list_tools",
            description="列出所有已注册的工具，可按来源筛选",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "可选，按启动器来源筛选"
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
                        "description": "工具 ID，格式如 everything:main"
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
            name="list_launchers",
            description="列出所有已接入的启动器（工具集）",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="get_launcher_info",
            description="查看启动器的详细信息和推荐的 MCP",
            inputSchema={
                "type": "object",
                "properties": {
                    "launcher_id": {
                        "type": "string",
                        "description": "启动器 ID"
                    }
                },
                "required": ["launcher_id"]
            }
        ),
        Tool(
            name="call_backend_tool",
            description="路由调用：转发请求到指定启动器的后端 MCP Server 执行具体操作",
            inputSchema={
                "type": "object",
                "properties": {
                    "launcher_id": {
                        "type": "string",
                        "description": "启动器 ID（如 everything、steam）"
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
                "required": ["launcher_id", "tool_name"]
            }
        ),
        Tool(
            name="list_backend_tools",
            description="列出指定启动器的后端 MCP 提供的所有工具",
            inputSchema={
                "type": "object",
                "properties": {
                    "launcher_id": {
                        "type": "string",
                        "description": "启动器 ID"
                    }
                },
                "required": ["launcher_id"]
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
        source = arguments.get("source")
        tools = registry.search_tools(keyword, source)
        result = f"找到 {len(tools)} 个工具：\n"
        for t in tools:
            result += f"- [{t['id']}] {t.get('displayName', t['name'])}: {t.get('description', '')[:50]}\n"

    elif name == "list_tools":
        source = arguments.get("source")
        tools = registry.list_all_tools(source)
        result = f"共 {len(tools)} 个工具：\n"
        for t in tools:
            result += f"- [{t['id']}] {t.get('displayName', t['name'])}\n"

    elif name == "get_tool_info":
        tool_id = arguments.get("tool_id", "")
        tool = registry.get_tool(tool_id)
        if tool:
            result = f"工具详情：\n"
            result += f"- ID: {tool['id']}\n"
            result += f"- 名称: {tool.get('displayName', tool['name'])}\n"
            result += f"- 类型: {tool.get('kind', 'unknown')}\n"
            result += f"- 支持操作: {', '.join(tool.get('actions', []))}\n"
            result += f"- 描述: {tool.get('description', '')}\n"
            result += f"- 版本: {tool.get('version', '未知')}\n"
            result += f"- 来源: {tool.get('source', '')}\n"
            result += f"- 标签: {', '.join(tool.get('tags', []))}\n"
            if tool.get("launch"):
                result += f"- 启动: {tool['launch'].get('command', '')}\n"
        else:
            result = f"未找到工具: {tool_id}"

    elif name == "launch_tool":
        tool_id = arguments.get("tool_id", "")
        res = registry.launch_tool(tool_id)
        if res.get("success"):
            result = f"✅ {res['message']}\n"
            result += f"命令: {res['launch'].get('command', '')}\n"
        else:
            result = f"❌ {res.get('error', '未知错误')}"

    elif name == "list_launchers":
        launchers = registry.list_launchers()
        result = f"已接入 {len(launchers)} 个启动器：\n"
        for l in launchers:
            status = "✅ 已安装" if l.get("installed") else "❌ 未安装"
            result += f"- [{l['id']}] {l.get('displayName', l['name'])} ({l.get('type')}) {status}\n"

    elif name == "get_launcher_info":
        launcher_id = arguments.get("launcher_id", "")
        info = registry.get_launcher(launcher_id)
        if info:
            result = f"启动器详情：\n"
            result += f"- ID: {info['id']}\n"
            result += f"- 名称: {info.get('displayName', info['name'])}\n"
            result += f"- 类型: {info.get('type')}\n"
            result += f"- 描述: {info.get('description', '')}\n"
            result += f"- 已安装: {info.get('installed', False)}\n"
            mcp = info.get("mcp", {})
            if mcp:
                result += f"- 推荐 MCP: {mcp.get('serverName', '')}\n"
                result += f"- 安装命令: {mcp.get('installCommand', '')}\n"
        else:
            result = f"未找到启动器: {launcher_id}"

    elif name == "call_backend_tool":
        launcher_id = arguments.get("launcher_id", "")
        tool_name = arguments.get("tool_name", "")
        backend_args = arguments.get("arguments", {})

        log(f"路由调用: {launcher_id}/{tool_name}")
        res = await backend_manager.call_backend_tool(launcher_id, tool_name, backend_args)
        if res.get("success"):
            result = f"✅ 调用成功 ({launcher_id}/{tool_name})\n\n"
            result += res.get("result", "")
        else:
            error_type = res.get("errorType", "unknown")
            error_msg = res.get("error", "未知错误")
            result = f"❌ 调用失败\n"
            result += f"  类型: {error_type}\n"
            result += f"  错误: {error_msg}\n"
            # 常见错误的提示
            if error_type == "connection_error":
                result += f"  提示: 请确认后端软件（{launcher_id}）是否已启动"
            elif error_type == "runtime_error":
                result += f"  提示: 请检查工具名 '{tool_name}' 是否正确，参数是否匹配"

    elif name == "list_backend_tools":
        launcher_id = arguments.get("launcher_id", "")
        res = await backend_manager.list_backend_tools(launcher_id)
        if res.get("success"):
            tools = res.get("tools", [])
            result = f"启动器 {launcher_id} 的后端 MCP 提供 {len(tools)} 个工具：\n"
            for t in tools:
                result += f"- {t['name']}: {t.get('description', '')[:60]}\n"
            # 日志里打印详细内容
            log(f"后端工具列表: {[t['name'] for t in tools]}")
        else:
            result = f"❌ 获取失败: {res.get('error', '未知错误')}"

    else:
        result = f"未知工具: {name}"

    log(f"工具 {name} 执行完成，返回 {len(result)} 字符")
    return [TextContent(type="text", text=result)]


async def main():
    """启动 MCP Server"""
    log("=" * 50)
    log("awam-tool-manager MCP Server 启动中...")
    log(f"数据目录: {DATA_DIR}")
    launchers = store.list_launchers()
    log(f"已加载 {len(launchers)} 个启动器: {', '.join(launchers)}")
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
