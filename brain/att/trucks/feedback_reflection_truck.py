"""空闲时触发反思：低于20修正；达到100且共治条件满足时考虑维护/关系建设。

低值路径不设置冷却、不改变情感值；高值查询有检查间隔并遵循维护申请限制。
"""
from brain.amy.decision_feelings import AXIS_NAMES
from brain.amy.feedback_state import get_feedback_state
from .reflection_truck import _BaseReflectionTruck
import time


class _FeedbackReflectionTruck(_BaseReflectionTruck):
    axis = ""
    value_key = ""
    _last_high_check = 0.0

    async def cycle_control(self):
        await super().cycle_control()
        await self.pause_event.wait()  # 沿用 station 的忙碌暂停

    async def _check_trigger(self, config, force=False):
        if not config.ENABLED:
            return ""
        value = get_feedback_state().snapshot()[self.value_key]
        if value < config.LOW_THRESHOLD:
            return f"{AXIS_NAMES[self.axis]} {value:g}/100，低于 {config.LOW_THRESHOLD:g}。具体依据见当前情绪与变动记录。"
        from config import maintenance_settings as settings
        if value >= settings.HIGH_THRESHOLD and (force or time.monotonic() - self._last_high_check >= settings.CHECK_INTERVAL_SECONDS):
            self._last_high_check = time.monotonic()
            from brain.hc.maintenance import MaintenanceService
            options = (await MaintenanceService().query())["eligibility"]
            kind = "maintenance" if self.axis == "competence" else "social_growth"
            if options[kind]["allowed"]:
                purpose = "围绕一个专题邀请主人维护经验、人格或锦囊" if kind == "maintenance" else "择时安排具体的关系建设，积累后续合作条件"
                return f"{AXIS_NAMES[self.axis]}达到100。可以考虑{purpose}；先查询维护记录与已有安排，成功登记后仅对应值重置为50，不领取低值补偿。"
        return ""

    async def get_truck_messages(self):
        # 执行前核对当前数值，避免排队期间已恢复或关闭仍然启动。
        reason = await self._check_trigger(self._get_config(), force=True)
        if not reason:
            self._trigger_reason = ""
            return None
        self._trigger_reason = reason
        return await super().get_truck_messages()


class ReflectionEfficacyTruck(_FeedbackReflectionTruck):
    """效能感独立自发入口：低值修正，高值专题维护。"""
    axis = "competence"
    value_key = "competence_value"

    def __init__(self):
        super().__init__("reflection_efficacy_truck")

    def _get_config(self):
        from config.truck_settings import reflection_efficacy_truck_config
        return reflection_efficacy_truck_config


class ReflectionBelongingTruck(_FeedbackReflectionTruck):
    """归属感独立自发入口：低值补偿，高值关系建设。"""
    axis = "recognition"
    value_key = "value"

    def __init__(self):
        super().__init__("reflection_belonging_truck")

    def _get_config(self):
        from config.truck_settings import reflection_belonging_truck_config
        return reflection_belonging_truck_config
