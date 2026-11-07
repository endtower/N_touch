"""
系统设置配置文件
包含Abyss Core系统的各种配置参数
"""
import os
from pickle import TRUE
from typing import Dict, Any, Optional
from config.env_tools import env_str

class AttentionConfig:
    """注意力模块配置类"""
    WAREHOUSE_DEFAULT_CAPACITY_THRESHOLD = 3
    UNREAD_COUNT_THRESHOLD = 10
    COOLDOWN_PERIOD_MINUTES = 10
    PRIVATE_MESSAGE_TRUCK_THRESHOLD = 1
    GROUP_TRUCK_CYCLE_TIME = 5.0
    PRIVATE_TRUCK_CYCLE_TIME = 1.0
    MAX_MESSAGES_PER_SESSION = 5
    MAX_SESSIONS_TO_PROCESS = 3
    FACTORY_ERROR_RECOVERY_TIME = 1.0
    PRE_PROCESSING_TIME_SECONDS = 15
    PRE_TIMEOUT_MINUTES = 1
    LOG_LEVEL = os.environ.get('ATTENTION_LOG_LEVEL', 'INFO')
    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    STOP_TIMEOUT = 5.0

    @staticmethod
    def get_truck_thresholds() -> Dict[str, int]:
        """获取不同卡车的阈值配置

        Returns:
            Dict[str, int]: 卡车ID到阈值的映射
        """
        return {'private_message_truck': AttentionConfig.PRIVATE_MESSAGE_TRUCK_THRESHOLD}
from config.llm_settings import LLMSettings

class ChatConfig:
    """Chat模块配置类，包含chat模块的所有LLM相关配置"""
    API_KEY = LLMSettings.API_KEY
    BASE_URL = LLMSettings.BASE_URL
    MODEL_NAME = LLMSettings.FLASH
    SOCIAL_CHAT_SEND_MODE = 'approval'
    SOCIAL_CHAT_UNRESTRICTED_NOTIFY = True
    SOCIAL_CHAT_DUAL_PUSH = True

class PfcaConfig:
    """PFCA模块配置类，包含pfca模块和pfca_att子模块的所有配置"""
    API_KEY = LLMSettings.API_KEY
    BASE_URL = LLMSettings.BASE_URL
    MODEL_NAME = LLMSettings.FLASH

class PfcdConfig:
    """PFCD模块配置类，包含pfcd模块的所有配置"""
    API_KEY = LLMSettings.API_KEY
    BASE_URL = LLMSettings.BASE_URL
    MODEL_NAME = LLMSettings.FLASH

class ReflectionConfig:
    """Reflection（反思）模块配置类，包含reflection模块的所有配置"""
    API_KEY = LLMSettings.API_KEY
    BASE_URL = LLMSettings.BASE_URL
    MODEL_NAME = LLMSettings.FLASH

class HCConfig:
    """HC模块配置类，包含hc模块的所有LLM相关配置"""
    API_KEY = LLMSettings.API_KEY
    BASE_URL = LLMSettings.BASE_URL
    MODEL_NAME = LLMSettings.FLASH

class AgentConfig:
    """Agent模块配置类，包含agent相关的所有配置"""
    API_KEY = LLMSettings.API_KEY
    BASE_URL = LLMSettings.BASE_URL
    MODEL_NAME = LLMSettings.FLASH

class SafetyConfig:
    """敏感/安全设置集中管控类。

    承载直接影响"对外真实行为"的关键安全开关，集中在一处便于审查与紧急切换，
    不散落在各业务配置类中：
    - SIMULATED_SEND：模拟发送开关——是否真实向外部用户发送消息
    - CONVERSATION_PROTECTION_MODE：conversation 保护模式——是否仅允许对主人对话
    """
    SIMULATED_SEND = False
    CONVERSATION_PROTECTION_MODE = True
    CONVERSATION_ALLOWED_SESSIONS = []

class VectorConfig:
    """向量嵌入模块配置类，包含向量嵌入相关的所有配置"""
    ENABLE_VECTOR_EMBEDDING = True
    ONLY_BEQUEST_VECTORIZATION = True
    API_KEY = env_str('VECTOR_API_KEY', '') or LLMSettings.FLASH_API_KEY or LLMSettings.API_KEY
    BASE_URL = env_str('VECTOR_BASE_URL', '') or LLMSettings.FLASH_BASE_URL or LLMSettings.BASE_URL
    MODEL_NAME = LLMSettings.EMBEDDING
    VECTOR_DIMENSIONS = 2048
    BATCH_SIZE = 10
    TIMEOUT = 30
from config.truck_settings import base_truck_config, private_truck_config, pre_truck_config, is_truck_enabled, get_truck_config, get_all_truck_configs, get_enabled_trucks
attention_config = AttentionConfig()
chat_config = ChatConfig()
pfca_config = PfcaConfig()
pfcd_config = PfcdConfig()
reflection_config = ReflectionConfig()
api_config = None
hc_config = HCConfig()
agent_config = AgentConfig()
safety_config = SafetyConfig()
vector_config = VectorConfig()
