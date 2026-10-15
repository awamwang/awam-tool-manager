"""工具注册中心：管理所有启动器和工具的查询"""

from typing import Any

from store import JsonStore


class ToolRegistry:
    """统一工具注册中心"""

    def __init__(self, store: JsonStore):
        self.store = store

    # ---- 启动器 ----

    def list_launchers(self) -> list[dict[str, Any]]:
        """列出所有启动器概览"""
        result = []
        for lid in self.store.list_launchers():
            data = self.store.load_launcher(lid)
            if data:
                result.append({
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "displayName": data.get("displayName"),
                    "type": data.get("type"),
                    "installed": data.get("installed", False),
                    "description": data.get("description", ""),
                })
        return result

    def get_launcher(self, launcher_id: str) -> dict[str, Any] | None:
        return self.store.load_launcher(launcher_id)

    # ---- 工具 ----

    def list_all_tools(self, source: str | None = None) -> list[dict[str, Any]]:
        """列出所有工具，可按来源筛选"""
        tools = []
        sources = [source] if source else self.store.list_launchers()
        for src in sources:
            tools.extend(self.store.load_tools(src))
        return tools

    def search_tools(self, keyword: str, source: str | None = None) -> list[dict[str, Any]]:
        """按关键词搜索工具"""
        all_tools = self.list_all_tools(source)
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
        # ID 格式：source:name
        if ":" not in tool_id:
            return None
        source, _ = tool_id.split(":", 1)
        for tool in self.store.load_tools(source):
            if tool.get("id") == tool_id:
                return tool
        return None

    def launch_tool(self, tool_id: str) -> dict[str, Any]:
        """启动工具（返回启动信息，实际执行由调用方完成）"""
        tool = self.get_tool(tool_id)
        if not tool:
            return {"success": False, "error": f"Tool not found: {tool_id}"}

        launch = tool.get("launch", {})
        return {
            "success": True,
            "toolId": tool_id,
            "toolName": tool.get("displayName", tool.get("name")),
            "launch": launch,
            "message": f"准备启动: {tool.get('displayName', tool.get('name'))}"
        }
