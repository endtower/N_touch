# -*- coding: utf-8 -*-
"""LLM 配置总表 —— 全系统 LLM 相关配置的唯一入口

本模块集中所有"与 LLM 有关"的配置，与 config/settings.py（安全开关 / 向量 / 卡车 /
各业务开关）分离，改模型、换接入点只动这里：

- `LLMSettings`：全局模型注册表（模型等级 → 模型名 + 各等级专属接入点 + 思考开关）
- `LLMConfig`  ：LLM 通用运行参数（循环上限 / 无工具调用上限 / 重试 / 系统提示 / token 追踪）
- `VisionConfig`：视觉位配置（模型名取自 `LLMSettings.VISION` + 图片预处理）

等级 → 接入点路由规则（消费方见 utils/llm_client_manager.py 的 LLMBridge）：
- flash / vision / pro：主接入点（火山方舟北京 coding 接入点 deepseek-v4.1-flash；未配置时回退 opencode 统一接入点）
- conversation：主接入点（专属接入点配置为空即回退主接入点）
- persona：主接入点（专属接入点配置为空即回退主接入点）
- embedding：火山方舟 API，见 config/settings.py 的 VectorConfig

密钥管理：**所有 API key 只存项目根 .env.dev（永不入库）**，本文件的 key 字段
默认值为空串、仅作占位文档；接入点 URL 与模型名支持环境变量覆盖（.env.dev
可写），字面量为默认值。换机器 / 打包 exe 时只需带 .env.dev。
"""

from config.env_tools import env_str


# ==================================================
# 全局模型注册表 —— 系统所有 LLM 的唯一模型配置入口
# ==================================================
class LLMSettings:
    """全局模型注册表：以后更换模型只需修改本类，全系统自动生效。

    等级划分: flash / conversation / persona / pro / vision / embedding。
    - flash 走主力专属接入点 火山方舟（北京）coding 接入点 deepseek-v4.1-flash（未配置时回退 opencode 统一接入点）
    - conversation 走主接入点（当前模型 deepseek-v4.1-flash，与 flash 同级）
    - persona 走主接入点（当前模型 deepseek-v4.1-flash，与 flash 同级）
    - vision 位即 flash 等级模型（deepseek-v4.1-flash）
    - embedding 走火山方舟 API（见 config/settings.py 的 VectorConfig）
    """

    # ===================== opencode 统一接入点 =====================
    # key 只存 .env.dev（OPENCODE_API_KEY=...），不进 git
    API_KEY = env_str("OPENCODE_API_KEY", "")
    BASE_URL = env_str("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")

    # ===================== 主力模型（flash）专属接入点 =====================
    # 【套餐·主力】火山方舟（北京）coding 接入点（OpenAI 兼容，2026-09-30 新增并切为主力）；
    # 国内直连、免梯子，实测 /models 1.7s、/chat/completions 1.1s
    # （对比 commandcode 中转 2.5~30s）；模型 deepseek-v4.1-flash
    # （端点内部解析为 deepseek-v4-1-flash）；flash / vision / pro 等级使用；
    # 置空则回退 opencode 统一接入点。key 只存 .env.dev（FLASH_API_KEY=...）
    FLASH_API_KEY = env_str("FLASH_API_KEY", "")
    FLASH_BASE_URL = env_str("FLASH_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3")

    # 【套餐·停用】commandcode 中转（境外 Cloudflare，链路慢，2026-09-30 停用改走方舟）；
    # 需要回退时把 key/url 写进 .env.dev 的 FLASH_API_KEY / FLASH_BASE_URL 即可
    # （注意该套餐模型名带 deepseek/ 前缀；旧 key 备份在 .env.dev 注释里）

    # ===================== conversation 专属接入点 =====================
    # 仅 conversation 等级使用（social_chat 也走本等级）；置空则回退主接入点（火山方舟 deepseek-v4.1-flash）。
    # 2026-09-23 临时置空：原中转的 gemini adapter 在「思考 + function calling」组合下会
    # 间歇性吞掉模型输出（HTTP 200、content 为空、tool_calls 字段缺失，却照计 21 个
    # candidate token），使 conversation 连续 3 次被判「未使用工具」而直接崩溃。
    # （旧接入点 croissant：key/url 备份在 .env.dev 注释里）
    CONVERSATION_API_KEY = env_str("CONVERSATION_API_KEY", "")
    CONVERSATION_BASE_URL = env_str("CONVERSATION_BASE_URL", "")

    # ===================== persona（内心人格）专属接入点 =====================
    # 人格召唤的 LLM 独立接入点，与 conversation 分开配置（避免借用会话等级语义）；
    # 置空则回退主接入点（火山方舟 deepseek-v4.1-flash）。
    # 2026-09-23 临时置空：智谱 GLM 出字太慢，人格召唤又是 PFCA 每次决策前的必经步骤，
    # 延迟被放大；改走主接入点（与 flash 同级模型）。
    # （旧接入点智谱 GLM：key/url 备份在 .env.dev 注释里）
    PERSONA_API_KEY = env_str("PERSONA_API_KEY", "")
    PERSONA_BASE_URL = env_str("PERSONA_BASE_URL", "")

    # ===================== 模型等级 =====================
    FLASH = env_str("LLM_FLASH", "deepseek-v4.1-flash")   # 主力模型（火山方舟北京 coding 接入点，当前所有主力 LLM 调用统一使用）
    CONVERSATION = env_str("LLM_CONVERSATION", "deepseek-v4.1-flash")  # conversation 会话模型（2026-09-23 临时切回主接入点，与 flash 同级；原 gemini-3.8-flash）
    PERSONA = env_str("LLM_PERSONA", "deepseek-v4.1-flash")       # 内心人格模型（2026-09-23 临时切回主接入点，与 flash 同级；原 glm-5.3-flash）
    PRO = env_str("LLM_PRO", "")                                  # 高阶模型，暂不设置
    # 视觉位：deepseek-v4.1-flash 原生支持图像理解，与主力同接入点
    VISION = env_str("LLM_VISION", "deepseek-v4.1-flash")
    EMBEDDING = env_str("LLM_EMBEDDING", "doubao-embedding-vision")     # 向量嵌入模型（火山方舟 API 接入，2048 维）

    # ===================== 思考模式 =====================
    # 统一控制主力与视觉模型的思考开关（仅 opencode 接入点注入 thinking extra_body；
    # 火山方舟 / commandcode 接入点走其默认值——默认即思考模式）
    THINKING = True


# ==================================================
# LLM 通用运行参数
# ==================================================
class LLMConfig:
    """LLM 运行参数：循环上限 / 重试 / 系统提示 / token 追踪。"""

    # ===================== 会话配置 =====================
    MAX_SELF_LOOPS = 10  # 最大自我循环次数
    RESPONSE_INTERVAL_MIN = 2  # 最小响应间隔（秒）
    MAX_NO_TOOL_CALLS = 3  # 最大无工具调用次数
    ERROR_RETRY_MAX = 3  # 错误重试次数
    ERROR_RETRY_INTERVAL = 0.0  # 错误重试间隔（秒）

    # ===================== 系统提示配置 =====================
    SYSTEM_PROMPT = "你是深度思考人格，你需要结合自身的特点，自主决策并制定行动计划。你必须使用工具来处理请求，否则会出现系统级错误。注意，同时应对多件事务是很难的，所以一次只能使用一个工具。"  # 系统提示词

    # ===================== Token追踪配置 =====================
    ENABLE_TOKEN_TRACE = False  # 是否启用token消耗追踪记录


# ==================================================
# 视觉位配置
# ==================================================
class VisionConfig:
    """视觉位配置 —— 模型名取自 LLMSettings.VISION（当前 = deepseek-v4.1-flash，火山方舟北京 coding 接入点）。

    模型名与接入点统一由 LLMSettings 管理，本类只补充视觉位特有的参数。
    """

    MODEL_NAME = LLMSettings.VISION

    # ===================== 图片预处理配置 =====================
    # 压缩到最长边 max_side 像素，降低 token 消耗
    IMAGE_MAX_SIDE = 1024
    IMAGE_JPEG_QUALITY = 80


llm_config = LLMConfig()
vision_config = VisionConfig()


# ==================================================
# API 提供商（家）标签 —— token 统计按接入点归集的唯一映射
# ==================================================
# key 为接入点 host 子串，value 为展示用提供商名；新增接入点只需在此追加一行。
PROVIDER_LABELS = {
    "ark.cn-beijing.volces.com": "火山方舟",
    "opencode.ai": "opencode",
    "api.commandcode.ai": "commandcode",
    "open.bigmodel.cn": "智谱GLM",
    "croissant.521911.xyz": "croissant",
    "api.deepseek.com": "DeepSeek官方",
}


def resolve_provider_label(base_url: str) -> str:
    """把接入点 base_url 归类为提供商标签（未知 host 回退为 host 本身）。"""
    url = (base_url or "").strip()
    for host, label in PROVIDER_LABELS.items():
        if host in url:
            return label
    if not url:
        return "未知接入点"
    from urllib.parse import urlparse
    return urlparse(url).hostname or url