"""工具注册中心：管理所有工具的查询"""

import sys
from typing import Any

from store import JsonStore

try:  # 可选依赖，缺失时 schema.py 会自动降级为基础校验
    from schema import validate_tool_entry
except ImportError:  # pragma: no cover
    validate_tool_entry = None


class ToolRegistry:
    """统一工具注册中心"""

    def __init__(self, store: JsonStore):
        self.store = store
        # 校验不通过的条目只告警一次，避免每次查询都刷屏
        self._invalid_reported: set[str] = set()

    # ---- 工具查询 ----

    def _report_invalid(self, tool: dict, error: str):
        """记录不合规条目（stderr，不污染 stdout 协议通道）"""
        tool_id = tool.get("id") or tool.get("name") or "<无 id>"
        if tool_id in self._invalid_reported:
            return
        self._invalid_reported.add(tool_id)
        print(
            f"[awam:registry][warning] 工具条目校验失败，已跳过: {tool_id} —— {error}",
            file=sys.stderr,
            flush=True,
        )

    def _valid_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """过滤掉不合规条目

        不这么做的话，缺 `id` 的条目会在 server.py 的 `t["id"]` 处直接 KeyError，
        一条脏数据就能让 tools/list 整体失败。
        """
        if validate_tool_entry is None:
            return tools

        valid = []
        for tool in tools:
            error = validate_tool_entry(tool)
            if error:
                self._report_invalid(tool, error)
                continue
            valid.append(tool)
        return valid

    def list_all_tools(self, kind: str | None = None) -> list[dict[str, Any]]:
        """列出所有工具，可按类型筛选"""
        tools = self._valid_tools(self.store.list_tools())
        if kind:
            tools = [t for t in tools if t.get("kind") == kind]
        return tools

    def search_tools(self, keyword: str, kind: str | None = None) -> list[dict[str, Any]]:
        """按关键词搜索工具

        匹配范围包含 `id`：`tool_id` 是文档中让用户传的主要标识
        （`everything-search-mcp` 这种 ID 里带关键词的情况很常见），
        只匹配 name/description/tags 会导致"搜到了工具却搜不到它的 ID"。
        """
        all_tools = self.list_all_tools(kind)
        keyword_lower = keyword.lower()

        results = []
        for tool in all_tools:
            # 匹配 id / 名称 / 描述 / 标签
            searchable = " ".join([
                tool.get("id", ""),
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
        tool = self.store.load_tool(tool_id)
        if not tool:
            return None

        if validate_tool_entry is not None:
            error = validate_tool_entry(tool)
            if error:
                self._report_invalid(tool, error)
                return None
        return tool

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
        """取工具的启动信息（只返回命令，实际执行由调用方完成）

        失败同样带上 `errorType`，与 server.py 的错误契约保持一致。
        """
        tool = self.get_tool(tool_id)
        if not tool:
            return {
                "success": False,
                "errorType": "not_found",
                "error": f"未找到工具: {tool_id}",
            }

        launch = tool.get("launch", {})
        if not launch:
            return {
                "success": False,
                "errorType": "config_error",
                "error": f"工具 '{tool_id}' 未配置 launch（本工具只返回命令，不实际执行启动）",
            }

        return {
            "success": True,
            "toolId": tool_id,
            "toolName": tool.get("displayName", tool.get("name")),
            "launch": launch,
            "message": f"准备启动: {tool.get('displayName', tool.get('name'))}"
        }
