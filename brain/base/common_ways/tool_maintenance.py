"""共治查询及反思专属维护申请；不提供 LLM 人工确认或处理申请的入口。"""
from brain.hc.maintenance import MaintenanceService


class ToolMaintenance:
    @staticmethod
    def get_query_maintenance_tool_schema():
        return {"type": "function", "function": {
            "name": "query_maintenance",
            "description": "查看最近人工维护、尚未处理的专题申请、暂缓/拒绝结论、可选条目编号及使用机会统计。申请维护前必须查询；不要重复催促主人。人工确认不提高检索权重，返回次数不代表实际采用。",
            "parameters": {"type": "object", "properties": {"topic": {"type": "string", "description": "可选，按专题筛选最近记录；不影响全局申请限制。"}}}}}

    @staticmethod
    def get_request_maintenance_tool_schema():
        return {"type": "function", "function": {
            "name": "request_maintenance",
            "description": "效能感达到100且近期任务结果较好时，围绕一个具体专题邀请主人维护1至5条经验、人格或锦囊。说明实际依据和需要主人判断的问题。成功登记后效能感重置为50，归属感不变；未登记不重置。申请不是修改授权，不自动改内容，不重复申请或催促。",
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "单个具体专题，最多80字"},
                "reason": {"type": "string", "description": "真实事例、现有内容不足和希望主人检查的具体问题"},
                "items": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["experience", "persona", "kit"]},
                    "item_id": {"type": "string", "description": "查询工具返回的准确编号"}}, "required": ["kind", "item_id"], "additionalProperties": False}}},
                "required": ["topic", "reason", "items"]}}}

    @staticmethod
    async def query_maintenance(topic=""):
        if not isinstance(topic, str):
            raise ValueError("topic 必须为文字")
        service = MaintenanceService()
        result = await service.query(topic.strip())
        catalog = await service.metadata()
        result["items"] = [{k: item[k] for k in ("kind", "item_id", "title", "maintenance", "usage")} for item in catalog[:100]]
        result["total_items"] = len(catalog)
        return result

    @staticmethod
    async def request_maintenance(topic, reason, items):
        return await MaintenanceService().request(topic, reason, items)
