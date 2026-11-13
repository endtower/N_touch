# -*- coding: utf-8 -*-
"""Truck模块配置文件 - 包含所有truck相关的配置参数和开关设置"""
import os
from typing import Dict, Any

# ==================== 基础配置 ====================
class BaseTruckConfig:
    """基础Truck配置类 - 提供所有truck共用的配置参数"""
    WAREHOUSE_DEFAULT_CAPACITY_THRESHOLD = 3  # 默认仓库容量阈值
    UNREAD_COUNT_THRESHOLD = 10  # 未读消息数量阈值
    COOLDOWN_PERIOD_MINUTES = 10  # 冷却周期(分钟)
    MAX_MESSAGES_PER_SESSION = 5  # 每个会话最多显示最近的消息数
    MAX_SESSIONS_TO_PROCESS = 3  # 最多处理的会话数量
    FACTORY_ERROR_RECOVERY_TIME = 1.0  # 错误处理时的短暂暂停时间（秒）
    STOP_TIMEOUT = 5.0  # 组件停止超时时间（秒）
    LOG_LEVEL = os.environ.get('ATTENTION_LOG_LEVEL', 'INFO')  # 日志级别
    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'  # 日志格式

# ==================== PrivateTruck配置 ====================
class PrivateTruckConfig:
    """PrivateTruck配置类 - 私聊消息获取卡车配置"""
    ENABLED = True  # 是否启用此truck（默认开启：私聊消息是所有链路的触发源）
    CYCLE_TIME = 1.0  # 私聊卡车检测周期（秒）
    MESSAGE_TRUCK_THRESHOLD = 1  # 私聊卡车阈值：有消息即触发
    MAX_MESSAGES_PER_SESSION = 5  # 每个会话最多显示最近的消息数
    MAX_SESSIONS_TO_PROCESS = 3  # 最多处理的会话数量
    UNREAD_COUNT_THRESHOLD = 10  # 未读消息数量阈值
    BACKLOG_HINT_THRESHOLD_SECONDS = 3600  # 未读积压提示阈值（秒）：未读距今超过 1 小时才在通知文案中显式写"积压"，刚收到的维持现措辞

# ==================== AtMentionTruck配置 ====================
class AtMentionTruckConfig:
    """AtMentionTruck配置类 - 群聊中被@自己的检测卡车

    检测范围：群聊。pfca 游标未读消息里含指向助手本人（role_settings.ASSISTANT_QQ）
    的 at 段即触发，直接把该会话交给 PFCA（与私聊卡车同风格：命中即唤起，不经过判断层）。
    """
    ENABLED = True  # 是否启用此truck（默认开启，与当前运行状态一致）
    CYCLE_TIME = 5.0  # 检测周期（秒）

# ==================== ReflectionTruck配置 ====================
class ReflectionCharTruckConfig:
    """反思字符触发 truck 配置：未处理笔记（排除反思归档）字符数达到阈值时触发反思"""
    ENABLED = True  # 是否启用此truck（反思系统字符触发入口）
    CHECK_CYCLE_TIME = 60.0  # 触发条件检查周期（秒）
    CHAR_THRESHOLD = 500  # 未处理笔记（排除反思归档）字符数阈值：达到即触发反思

class ReflectionTimeTruckConfig:
    """反思定时触发 truck 配置：距上次反思满间隔时触发反思"""
    ENABLED = False  # 是否启用此truck（默认关闭，由控制面板显式启用）
    CHECK_CYCLE_TIME = 60.0  # 触发条件检查周期（秒）
    REFLECTION_INTERVAL = 10800.0  # 反思间隔（秒，3小时）

class ReflectionFeedbackTruckConfig:
    """决策情感自发入口：同一开关控制低值反思和达到100的高值行动。"""
    ENABLED = False
    CHECK_CYCLE_TIME = 1.0  # 秒；忙碌时由 station 暂停
    LOW_THRESHOLD = 0

class ReflectionEfficacyTruckConfig(ReflectionFeedbackTruckConfig):
    """效能感入口：低值自修正，高值专题维护。"""

class ReflectionBelongingTruckConfig(ReflectionFeedbackTruckConfig):
    """归属感入口：低值情绪宣泄，高值关系改进。"""

# ==================== TaskTruck配置 ====================
class TaskTruckConfig:
    """TaskTruck配置类 - 任务到期触发卡车配置

    默认开启：与当前运行状态一致。任务到期直接启动 PFCA 属于会对外产生行为的链路，
    需要时应到前端 Truck 控制面板显式关闭。
    """
    ENABLED = True  # 是否启用此truck
    CYCLE_TIME = 60.0  # 到期任务检查周期（秒）

# ==================== PRE模块配置 ====================
class PreTruckConfig:
    """PRE 契机规则层配置

    规则层零 LLM 成本，只有两条：时间到、群聊中主人有新消息。
    命中后交给 proactive 判断层（intent=pre_opportunity），由它决定是否拉起 PFCA。
    """
    ENABLED = True  # 是否启用此truck（默认开启，与当前运行状态一致；需关闭时由控制面板显式停用）
    CYCLE_TIME = 3600.0  # 契机巡检周期（秒）：距上次命中满 1 小时触发「时间到」
    CHECK_CYCLE_TIME = 60.0  # 规则扫描周期（秒）：群聊主人新消息的检测频率
    # 整辆 PRE 的全局触发冷却（秒）：命中一次后，距下次规则判定需满该时长。
    # 防止主人在群里频繁发言导致契机过密；区别于 CYCLE_TIME（后者只是「时间到」规则的周期）。
    TRIGGER_CD = 1800.0

# ==================== 全局配置实例 ====================
base_truck_config = BaseTruckConfig()
private_truck_config = PrivateTruckConfig()
at_mention_truck_config = AtMentionTruckConfig()
reflection_char_truck_config = ReflectionCharTruckConfig()
reflection_time_truck_config = ReflectionTimeTruckConfig()
reflection_efficacy_truck_config = ReflectionEfficacyTruckConfig()
reflection_belonging_truck_config = ReflectionBelongingTruckConfig()
task_truck_config = TaskTruckConfig()
pre_truck_config = PreTruckConfig()

# ==================== Truck开关检查辅助函数 ====================
def is_truck_enabled(truck_id: str) -> bool:
    """检查指定truck是否启用"""
    config_mapping = {
        'private_truck': private_truck_config,
        'at_mention_truck': at_mention_truck_config,
        'reflection_char_truck': reflection_char_truck_config,
        'reflection_time_truck': reflection_time_truck_config,
        'reflection_efficacy_truck': reflection_efficacy_truck_config,
        'reflection_belonging_truck': reflection_belonging_truck_config,
        'task_truck': task_truck_config,
        'pre_truck': pre_truck_config,
    }
    config = config_mapping.get(truck_id.lower())
    return getattr(config, 'ENABLED', False) if config else False

def get_truck_config(truck_id: str):
    """获取指定truck的配置实例"""
    config_mapping = {
        'private_truck': private_truck_config,
        'at_mention_truck': at_mention_truck_config,
        'reflection_char_truck': reflection_char_truck_config,
        'reflection_time_truck': reflection_time_truck_config,
        'reflection_efficacy_truck': reflection_efficacy_truck_config,
        'reflection_belonging_truck': reflection_belonging_truck_config,
        'task_truck': task_truck_config,
        'pre_truck': pre_truck_config,
    }
    return config_mapping.get(truck_id.lower())

def get_all_truck_configs() -> Dict[str, Any]:
    """获取所有truck配置的字典"""
    return {
        'private_truck': private_truck_config,
        'at_mention_truck': at_mention_truck_config,
        'reflection_char_truck': reflection_char_truck_config,
        'reflection_time_truck': reflection_time_truck_config,
        'reflection_efficacy_truck': reflection_efficacy_truck_config,
        'reflection_belonging_truck': reflection_belonging_truck_config,
        'task_truck': task_truck_config,
        'pre_truck': pre_truck_config,
    }


def get_enabled_trucks() -> list:
    """获取所有已启用的truck列表"""
    return [tid for tid in ['private_truck', 'at_mention_truck', 'reflection_char_truck', 'reflection_time_truck', 'reflection_efficacy_truck', 'reflection_belonging_truck', 'task_truck', 'pre_truck'] if is_truck_enabled(tid)]
