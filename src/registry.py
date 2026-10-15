"""工具注册中心：管理所有工具的查询"""

from typing import Any

from store import JsonStore


class ToolRegistry:
    """统一工具注册中心"""

    def __init__(self, store: JsonStore):
        self.store = store

    # ---- 工具查询 ----

    def list_all_tools(self, kind: str | None = None) -> list[dict[str, Any]]:
        """列出所有工具，可按类型筛选"""
        tools = self.store.list_tools()
        if kind:
            tools = [t for t in tools if t.get("kind") == kind]
        return tools

    def search_tools(self, keyword: str, kind: str | None = None) -> list[dict[str, Any]]:
        """按关键词搜索工具"""
        all_tools = self.list_all_tools(kind)
        keyword_lower = keyword.lower()

        results = []
        for tool in all_tools:
            # 匹配名称、描述、标签
            searchable = " ".join([
                tool.get("name", ""),
                tool.get("displayName", ""),
                tool.get("description", ""),
                " ".join(tool.get("tags", [])),
            ]).lower()
            if keyword_lower in searchable:
                results.append(tool)
        return results

    def get_tool(self, tool_id: str) -> dict[str, Any] | None:
        """根据 ID 获取工具详情"""
        return self.store.load_tool(tool_id)

    # ---- 后端 MCP 子工具 ----

    def list_backend_tools(self, parent_id: str) -> list[dict[str, Any]]:
        """列出某个 MCP 工具的所有子工具"""
        return self.store.load_backend_tools(parent_id)

    def get_backend_tool(self, parent_id: str, tool_name: str) -> dict[str, Any] | None:
        """获取某个 MCP 工具的具体子工具"""
        for tool in self.store.load_backend_tools(parent_id):
            if tool.get("name") == tool_name:
                return tool
        return None

    # ---- 启动 ----

    def launch_tool(self, tool_id: str) -> dict[str, Any]:
        """启动工具（返回启动信息，实际执行由调用方完成）"""
        tool = self.get_tool(tool_id)
        if not tool:
            return {"success": False, "error": f"Tool not found: {tool_id}"}

        launch = tool.get("launch", {})
        if not launch:
            return {"success": False, "error": f"Tool has no launch config: {tool_id}"}

        return {
            "success": True,
            "toolId": tool_id,
            "toolName": tool.get("displayName", tool.get("name")),
            "launch": launch,
            "message": f"准备启动: {tool.get('displayName', tool.get('name'))}"
        }
