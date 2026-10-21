"""通用维护工具：四种临界行动统一登记。"""
from brain.hc.critical_actions import ACTION_STATES, CriticalActions


class ToolMaintenance:
    @staticmethod
    def get_tool_schema():
        descriptions = {
            "action": "效能感100：申请维护；效能感为0：尝试自修复；归属感为0：情绪宣泄；归属感100：发布关系改进任务。",
            "title": "本次行动标题",
            "reason": "维护理由、自身错误、不满缘由或关系现状，依据实际经历填写。",
            "plan": "维护改进需求、期望修改方向或尝试、宣泄打算、关系改进办法。",
            "next_occurrence": "实施事件或后续实践验证的时间：ISO 8601 文本或尽快。",
            "notify_master": "申请维护时必填：如何提醒、如何向主人解释这次维护需求和实施时间。",
            "verification": "尝试自修复时必填：如何实践验证修改或尝试是否有效。",
            "event": "情绪宣泄时必填：引发不满的具体事件。",
            "target": "情绪宣泄或关系改进时必填：对象或关系目标。",
            "expectation": "关系改进时必填：希望关系产生什么变化。",
        }
        properties = {key: {"type": "string", "description": value} for key, value in descriptions.items()}
        properties["action"]["enum"] = list(ACTION_STATES)
        return {"type": "function", "function": {
            "name": "maintenance_action",
            "description": "统一维护入口，四类行动严格对应四种情感临界状态。申请维护只发布申请，并把本批负面结束评价的 failure_reason 归入申请后清空缓存；其余三类自动转交 Quest 登记对应待办。成功登记后只将对应情感值重置为50，不给+30或其他补偿；登记失败不重置。",
            "parameters": {"type": "object", "properties": properties,
                           "required": ["action", "title", "reason", "plan", "next_occurrence"]}}}

    @staticmethod
    async def maintenance_action(**kwargs):
        return await CriticalActions().register(**kwargs)
