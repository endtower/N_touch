from config.env_tools import env_str
class RoleConfig:
    ASSISTANT_NAME = env_str("ASSISTANT_NAME", "助手")
    ASSISTANT_NICK_NAME = env_str("ASSISTANT_NICK_NAME", ASSISTANT_NAME)
    ASSISTANT_ID = "system"
    ASSISTANT_NAME_DESCRIPTION = ASSISTANT_NAME
    MASTER_NAME = env_str("MASTER_NAME", "主人")
    MASTER_ID = env_str("MASTER_ID", "0")
    ASSISTANT_QQ = env_str("ASSISTANT_QQ", "0")
role_config = RoleConfig()
