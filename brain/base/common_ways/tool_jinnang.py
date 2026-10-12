"""LLM 只读锦囊工具；人通过记忆页维护条目。"""
from brain.hc.database.decision_kit import DecisionKitOperations
from brain.hc.shortlight.observation_search import search_observations
from utils.logger import get_logger

logger = get_logger(__name__, "INFO")


class ToolJinnang:
    @staticmethod
    def get_query_jinnang_tool_schema():
        return {
            "type": "function",
            "function": {
                "name": "query_jinnang",
                "description": (
                    "遇到疑难杂项问题、缺少可行思路时，查阅人类手工维护的锦囊。"
                    "输入当前遇到的困境，一次返回全部锦囊（建议正文、作用困境、困境分类），"
                    "并附全部见闻按与当前困境的向量相似度排序的结果；库存足够时至少5条，"
                    "不足时只返回已有记录。锦囊是供你结合实际资源判断的建议，见闻是观察与推测，"
                    "不得把相似度当作事实可信度；向量停用时会明确提示，锦囊仍完整返回。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "描述你当前遇到的困难、信息缺口或不知道如何推进的问题。"}},
                    "required": ["query"],
                },
            },
        }

    @staticmethod
    async def query_jinnang(query: str):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 必须描述当前遇到的困境")
        kits = await DecisionKitOperations().list_all()
        from brain.hc.maintenance import MaintenanceService
        await MaintenanceService().record_usage("kit", [item["kit_id"] for item in kits], "returned")
        try:
            observations = await search_observations(query)
        except Exception:
            logger.exception("锦囊查询的关联见闻检索失败")
            observations = {"status": "error", "count": 0, "observations": [],
                            "hint": "见闻向量检索失败，锦囊已完整返回；可用 list_notes 查看见闻录，或检查嵌入服务后重试。"}
        return {
            "query": query.strip(), "count": len(kits), "kits": kits,
            "observation_status": observations["status"],
            "observation_count": observations["count"],
            "observations": observations["observations"],
            "hint": observations["hint"],
        }
