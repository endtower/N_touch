# brain/pfc — 决策层（PFCA / 反思）

本目录是 N_Touch 的决策层。PFCA 是**唯一调度者**：注意力层（`brain/att`）把唤醒消息
交给 PFCA，PFCA 经工具调用把任务分发给对话代理（conversation / social_chat）等。

> 完整接口与工具表见 [../../docs/api/pfca.md](../../docs/api/pfca.md)；
> 工程全貌见 [../../docs/工程总览.md](../../docs/工程总览.md)。

## 目录结构

```
pfc/
├── core.py                  PFCA 入口工厂（async_process_from_att_factory）等
├── pfca/                    PFCA 决策主体
│   ├── pfca_core.py         PFCACore
│   ├── pfca_session.py      PFCASession(HeavySession) —— LLM 循环
│   ├── pfca_input_manager.py  首条/增量输入组装（含人格总览注入）
│   ├── pfca_history_manager.py 上下文 JSON 归档
│   ├── pfca_prompt.py       系统提示词
│   ├── pfca_tools.py        LLM 工具表（conversation_agent / social_chat_agent /
│   │                        wait_and_observe / terminate 等）
│   ├── pfca_bequest.py      经验（bequest）通道
│   └── agents/              PFCA 直连的子 agent
├── reflection/              反思子系统（独立 Core/Session + 工具 + 提示词）
└── inspiration/             预留（当前仅 __init__.py）
```

## 调用链（简）

```
brain/att station ──async_process_from_att_factory──> PFCA
    PFCA 工具调用 ──> conversation_agent / social_chat_agent / 记忆 / 任务等
    PFCA terminate ──（可选）──> reflection 反思会话（协调器内部串行接力）
```

各 Core/Session 的基类关系不完全一致：`PFCASession` / `ReflectionSession` 继承
`HeavySession`，而 `PFCACore` / `ReflectionCore` 并非都继承 `HeavyCore`——具体以源码为准，
不要据基类注释推断全部模块。

> 历史备注：本目录曾用名 `pre/`（前额叶），早期设计稿中的 PreCore / 模块注册表 /
> 工作流引擎等结构在当前代码中**已不存在**，本文件原 Mermaid 图已随之作废。