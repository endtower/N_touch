# 脑功能模块层（brain/）

本目录是 N_Touch 的脑功能层，采用神经科学术语命名来对应不同功能分区。
各模块间**不通过消息总线通信**：消息统一落在 `memory/` 的消息库（`chat_history.db`），
下游按各自的 **consumer 游标拉取**未读；注意力层（`att/`）再驱动 PFCA 决策层（`pfc/`）。

> 工程全貌见 [../docs/工程总览.md](../docs/工程总览.md)；各模块接口见
> [../docs/api/README.md](../docs/api/README.md)。

## 目录结构

- **att/**：注意力层。AttentionStation 是唯一仲裁者，配 **7 类 truck** 感知器
  （private_truck / ongoing_truck / special_attention_truck / scheduled_proactive_truck /
  reflection_char_truck / reflection_time_truck / task_truck；注册不代表默认启用）与
  proactive 后台主动任务循环。见 [../docs/api/attention.md](../docs/api/attention.md)。
- **pfc/**：决策层。PFCA（`pfc/pfca/`）是唯一调度者，把任务分发给对话代理；
  另有反思子系统（`pfc/reflection/`）。见 [../docs/api/pfca.md](../docs/api/pfca.md)。
- **chat/**：对话代理。conversation（熟人对话）、social_chat（对外社交，审批后发送），
  以及共享的 `chat_history_manager.py`。见 [../docs/api/conversation.md](../docs/api/conversation.md)、
  [../docs/api/social_chat.md](../docs/api/social_chat.md)。
- **amy/**：情绪系统。所有模块只经 `EmotionalInterface` 门面访问，当前为 **DA / NE 双轴**
  （SC 轴已于 2026-09-27 剔除）。见 [../docs/api/amy.md](../docs/api/amy.md)。
- **hc/**：全局状态与记忆库。`GlobalSessionState` + SQLite 长期记忆库（`memory.db`）+
  临时记忆 + shortlight 记事本。见 [../docs/api/hc.md](../docs/api/hc.md)、
  [../docs/api/temporary_memory.md](../docs/api/temporary_memory.md)。
- **base/**：框架基类与通用能力。`heavy_agent/`（HeavyCore / HeavySession / ToolManager）、
  `light_agent/`（LightweightAgent 及 memory/quest/vision/persona agent）、
  `common_ways/`（跨模块复用的工具实现）、`persona/`（人格注册表与人格定义）。
- **memory/**：消息/会话历史/状态 API 层（`message_handler.py`、`session_history.py`、
  `state_api.py` 等）。见 [../docs/api/message_store.md](../docs/api/message_store.md)。
- **tools/**：运维/迁移脚本（如 `backup_chat_history.py`、`migrate_image_refs.py`）。
- **utils/**：brain 内部使用的工具函数（logger、错误处理、历史管理、LLM 客户端等）。
- **attachments/**：附件前置校验与解析（`preflight.py`、`resolver.py`）。

## 调用链（简）

```
memory/ 消息库（游标）
    → att/ truck 轮询 → station 仲裁 → pfc/ PFCA 决策
    → chat/ 对话代理（conversation / social_chat）
    → amy/ 情绪 + hc/ 状态与记忆
    → adapt/ 出站路由
```

更细的数据流见各模块的 API 文档与 [../docs/工程总览.md](../docs/工程总览.md)。