"""空闲时触发反思：四种临界状态分别进入统一维护工具。

触发本身不改变情感值，只有行动实际登记后重置对应值。
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
        from config import maintenance_settings as settings
        from brain.hc.critical_actions import ACTION_STATES, available_actions
        state = get_feedback_state().snapshot()
        actions = [a for a in available_actions(state) if ACTION_STATES[a][0] == self.axis]
        if not actions:
            return ""
        if not force and time.monotonic() - self._last_high_check < settings.CHECK_INTERVAL_SECONDS:
            return ""
        self._last_high_check = time.monotonic()
        return f"{AXIS_NAMES[self.axis]} {state[self.value_key]:g}/100。可以使用 maintenance_action 登记{actions[0]}，填写相应内容和时间；登记后仅对应值重置为50。"

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
    """归属感独立自发入口：低值宣泄，高值关系改进。"""
    axis = "recognition"
    value_key = "value"

    def __init__(self):
        super().__init__("reflection_belonging_truck")

    def _get_config(self):
        from config.truck_settings import reflection_belonging_truck_config
        return reflection_belonging_truck_config
