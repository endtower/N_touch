from .private_truck import PrivateTruck
from .at_mention_truck import AtMentionTruck
from .reflection_truck import ReflectionCharTruck, ReflectionTimeTruck
from .feedback_reflection_truck import ReflectionEfficacyTruck, ReflectionBelongingTruck
from .task_truck import TaskTruck
from .pre_truck import PreTruck

__all__ = ['PrivateTruck', 'AtMentionTruck', 'ReflectionCharTruck', 'ReflectionTimeTruck', 'ReflectionEfficacyTruck', 'ReflectionBelongingTruck', 'TaskTruck', 'PreTruck']

# 注册所有truck
truck_registry = {
    'private_truck': PrivateTruck,
    'at_mention_truck': AtMentionTruck,
    'reflection_char_truck': ReflectionCharTruck,
    'reflection_time_truck': ReflectionTimeTruck,
    'reflection_efficacy_truck': ReflectionEfficacyTruck,
    'reflection_belonging_truck': ReflectionBelongingTruck,
    'task_truck': TaskTruck,
    'pre_truck': PreTruck
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
