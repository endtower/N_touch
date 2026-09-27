from .private_truck import PrivateTruck
from .ongoing_truck import OngoingTruck
from .special_attention_truck import SpecialAttentionTruck
from .scheduled_proactive_truck import ScheduledProactiveTruck
from .reflection_truck import ReflectionCharTruck, ReflectionTimeTruck
from .task_truck import TaskTruck

__all__ = ['PrivateTruck', 'OngoingTruck', 'SpecialAttentionTruck', 'ScheduledProactiveTruck', 'ReflectionCharTruck', 'ReflectionTimeTruck', 'TaskTruck']

# 注册所有truck
truck_registry = {
    'private_truck': PrivateTruck,
    'ongoing_truck': OngoingTruck,
    'special_attention_truck': SpecialAttentionTruck,
    'scheduled_proactive_truck': ScheduledProactiveTruck,
    'reflection_char_truck': ReflectionCharTruck,
    'reflection_time_truck': ReflectionTimeTruck,
    'task_truck': TaskTruck
}

# 获取truck实例的工厂函数
def get_truck(truck_id):
    """根据truck_id获取对应的truck实例"""
    if truck_id in truck_registry:
        return truck_registry[truck_id]()
    return None

# 获取所有可用的truck_id列表
def get_available_trucks():
    """获取所有可用的truck_id列表"""
    return list(truck_registry.keys())
