# -*- coding: utf-8 -*-
"""adapt 平台适配层配置文件

配置 adapt 层（QQ / 微信 / ...）的连接、录制、发送等行为。
重启 Core 生效（不需要改任何代码）。

切换调试 / 生产模式：改 OneBotConfig.RECORD_RAW_EVENTS 即可。

部署参数（端口 / host / 鉴权 token / 平台开关）支持环境变量覆盖：
同名变量（通常写在项目根 .env.dev，本文件导入时自动加载）优先于下方默认值，
字面量仅作默认值与文档。人格/行为类配置不走环境变量。
"""

from config.env_tools import env_bool, env_int, env_opt, env_str



# ==================================================
# OneBot 平台配置（QQ via NapCat）
# ==================================================
class OneBotConfig:
    """OneBot v11 平台配置 - 适配 NapCat / go-cqhttp / Lagrange 等实现"""

    # ===================== 开关 =====================
    ENABLED = env_bool("ONEBOT_ENABLED", True)  # 是否启用 OneBot 平台

    # ===================== 反向 WebSocket 服务端（Core 起在 host:port，SnowLuma/NapCat 来连）=====================
    WS_HOST = env_str("ONEBOT_WS_HOST", "127.0.0.1")      # 监听地址（0.0.0.0 = 接受所有网卡）
    WS_PORT = env_int("ONEBOT_WS_PORT", 5001)             # 监听端口
    WS_PATH = env_str("ONEBOT_WS_PATH", "/onebot/v11/ws") # OneBot v11 协议路径约定

    # ===================== 鉴权 =====================
    # 必须跟 SnowLuma/NapCat onebot.json 里 websocketClients[i].accessToken 完全一致
    # 建议把真 token 放 .env.dev（ONEBOT_ACCESS_TOKEN=...），不要提交进 git
    ACCESS_TOKEN = env_str("ONEBOT_ACCESS_TOKEN", "test-token-123")

    # ===================== 录制模式（调试 / 生产切换）=====================
    # 调试期：开 → 所有 NapCat 推送的原始事件落盘到 logs/onebot_raw_events/
    # 生产期：关 → 不写盘
    RECORD_RAW_EVENTS = True
    RAW_EVENTS_DIR = "logs/onebot_raw_events"
    MAX_RAW_EVENTS_PER_DAY = 5000

    # ===================== 发送行为 =====================
    # True  = NapCat 离线时 send 立即抛 NetworkError（不缓冲、不等、不重试）
    # False = 等待 reconnect_grace 秒后报错（保留旧行为，建议 False 仅在测试用）
    FAIL_FAST_ON_DISCONNECT = True

    # ===================== 媒体本地下载（图片 / 表情包）=====================
    # 收到图片/表情包消息时，自动从 QQ CDN 下载到本地，避免 rkey 过期后无法访问
    # 落盘路径：<MEDIA_BASE_DIR>/{stickers|images}/<YYYY-MM-DD>/<file_id>.<ext>
    # 注意：会自动保留所有用户发的图片，**有隐私风险**，需要定期清理（TODO）
    MEDIA_DOWNLOAD_ENABLED = True
    MEDIA_BASE_DIR = "memory/chat_history/media"     # 媒体根目录
    MEDIA_DOWNLOAD_TIMEOUT = 30                      # 单个文件下载超时（秒）
    MEDIA_DOWNLOAD_MAX_SIZE_MB = 50                  # 单个文件大小上限（MB），超限跳过
    MEDIA_DOWNLOAD_RETRIES = 2                       # 失败重试次数


# ==================================================
# 微信平台配置（经通用 HTTP 桥接，默认关闭）
# ==================================================
class WeChatConfig:
    """兼容配置入口；实际连接验证与收发由 adapt.bridge 完成。"""

    ENABLED = env_bool("BRIDGE_ENABLED", False)


# ==================================================
# Web 平台配置（浏览器前端，替代 NapCat 用）
# ==================================================
class WebConfig:
    """Web 平台配置 - 浏览器 WebSocket 前端，充当消息通道

    用法：
    - 默认开启（ENABLED=True），跟 OneBot 可以并存
    - 不想用就改 ENABLED=False 重启即可
    - 只在你不想跑 NapCat 的时候用 web 当唯一通道
    """

    # ===================== 开关 =====================
    ENABLED = env_bool("WEB_ENABLED", True)

    # ===================== 反向 WebSocket 服务端 =====================
    WS_HOST = env_str("WEB_WS_HOST", "127.0.0.1")
    WS_PORT = env_int("WEB_WS_PORT", 5002)
    WS_PATH = env_str("WEB_WS_PATH", "/web/ws")

    # ===================== 鉴权 =====================
    # None = 不鉴权（开发期推荐）
    # 设个非空字符串后，前端连 WS 必须带 ?access_token=xxx 或 Authorization: Bearer xxx
    ACCESS_TOKEN = env_opt("WEB_ACCESS_TOKEN", None)

    # ===================== 会话配置 =====================
    USER_ID = "web_user"
    USER_NAME = "Web User"
    SESSION_ID = "private_web_user"

    # ===================== 媒体 =====================
    MEDIA_BASE_DIR = "memory/chat_history/media"
    MAX_IMAGE_BYTES = 8 * 1024 * 1024    # 8MB
    MAX_IMAGES_PER_MESSAGE = 9


# ==================================================
# 全局配置实例
# ==================================================
onebot_config = OneBotConfig()
wechat_config = WeChatConfig()
web_config = WebConfig()


# ==================================================
# 辅助函数
# ==================================================
def is_onebot_enabled() -> bool:
    """检查 OneBot 平台是否启用"""
    return onebot_config.ENABLED


def is_wechat_enabled() -> bool:
    """检查微信平台是否启用"""
    return wechat_config.ENABLED


def is_web_enabled() -> bool:
    """检查 Web 平台是否启用"""
    return web_config.ENABLED


def get_onebot_config() -> OneBotConfig:
    """获取 OneBot 配置实例"""
    return onebot_config


def get_wechat_config() -> WeChatConfig:
    """获取微信配置实例"""
    return wechat_config


def get_web_config() -> WebConfig:
    """获取 Web 平台配置实例"""
    return web_config
