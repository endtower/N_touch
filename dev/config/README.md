# 配置文件

本目录包含Abyss Core系统的配置文件，用于设置系统参数和环境变量。

## 文件结构

- **llm_settings.py**：LLM 配置唯一入口。`LLMSettings` 全局模型注册表（模型等级 flash / conversation / persona / pro / vision / embedding → 模型名 + 各等级专属接入点 + 思考开关）、`LLMConfig`（循环上限 / 重试 / 系统提示 / token 追踪）、`VisionConfig`（视觉位配置）。换模型、换接入点只改这里。
- **settings.py**：非 LLM 系统配置。安全开关（`SafetyConfig`）、向量嵌入（`VectorConfig`）、注意力（`AttentionConfig`）、会话业务开关（`ChatConfig`）等；另保留旧版按模块 LLM 存根类（`PfcaConfig` / `PfcdConfig` / `ReflectionConfig` / `HCConfig` / `AgentConfig`，模型名一律取 `LLMSettings.FLASH`）。
- **adapt_settings.py**：平台适配开关（OneBot / Web）。
- **role_settings.py**：角色相关配置（主人名、助手名、ID 等）。
- **truck_settings.py**：感知器（truck）配置。
- **__init__.py**：`from .llm_settings / .settings / .truck_settings import *` 统一导出。

## 模型等级与接入点

`utils/llm_client_manager.py` 的 `call_llm(level, ...)` 是唯一 LLM 网关，等级同时决定接入点：

| 等级 | 默认模型 | 接入点 |
|---|---|---|
| flash | `DeepSeek-V4.1-Flash` | 火山方舟（北京）套餐接入点 `/api/plan/v3`（`FLASH_API_KEY` / `FLASH_BASE_URL`，空则回退 opencode） |
| vision | `DeepSeek-V4.1-Flash` | 同上（DeepSeek-V4.1-Flash 原生支持图像理解） |
| conversation | `DeepSeek-V4.1-Flash` | 同上（`CONVERSATION_*` 置空即回退主接入点） |
| persona | `DeepSeek-V4.1-Flash` | 同上（`PERSONA_*` 置空即回退主接入点） |
| pro | 未设置 | 未设置时禁止降级调用（直接报错阻断） |
| embedding | `doubao-embedding-vision` | 火山方舟 API（见 `settings.py` 的 `VectorConfig`，`VECTOR_BASE_URL` 同为 `/api/plan/v3`） |

## 主要功能

- 定义系统运行参数
- 管理环境变量
- 提供配置读取和验证功能

## 配置管理

- API Key 保存在用户数据目录的 `.env.dev`，通过启动器或 Web 系统设置编辑；源码与公开组件包不包含个人密钥。
- 首次启动的账号、名称与模型接入由配置向导保存；人格、行为等框架逻辑仍在 Python 源码中维护。
- 配置参数应当有合理的默认值
- 定期备份重要配置
- 注意配置文件的权限控制
