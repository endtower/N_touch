# 统一 Agent 实验分支

`experiment/unified-agent`。目标：验证"去掉决策层与说话层的分工，改成一个循环 + 内心/正文双产物"
能不能既提连贯性、又不让她说话带着算盘。

- 设计说明：[spec.md](spec.md)
- 原型：本目录下的 `state.py` / `tools.py` / `prompt.py` / `model.py` / `loop.py` / `replay.py`

## 跑一下

```bash
python -m unified.replay --demo
```

不需要网络：`MockModel` 按剧本吐出协议输出，完整链路（状态事务、工具、出话、账本）都会走一遍。
产物落在 `unified/_run/`：

- `events.jsonl`：每次状态事务、每次生成、每次工具调用
- `outbox.jsonl`：真正说出去的话（单独一份，方便和 inner 对照）

## 和 main 的关系

| | main | 这里 |
|---|---|---|
| 循环 | 5 个 HeavySession + pfc_handoff 接力 | 1 个循环 |
| 意图传递 | `purpose` ≤120 字 | 无接缝 |
| 工具 | 按 agent 切分 | 全开，但分 capability / decision |
| 隔离 | 进程边界 | 字段边界（`inner` 不可输出） |
| 状态 | `GlobalSessionState._data` | 显式 `State` + 命名事务 |

## 下一步（需要网络恢复才能做）

1. 把 `MockModel` 换成真模型：实现 `complete(messages, tools) -> str` 即可，其余不动。
2. 从 `memory/chat_history.db` 导出一段真实群聊为 JSONL，`--jsonl` 回放。
3. 同一批消息跑两遍（main 与这里），人读 20 条，只判断一件事：哪边更像她。
4. 看 `leak_check` 的报警率。它高就说明"字段隔离"挡不住，接缝那 120 字其实是过滤器。

## 已知会输的地方

长对话一致性（没有离线反思沉淀）、可解释性（只能回答"那一轮她心里写了什么"）、
无行为验收。这三条写在 [spec.md](spec.md) 里，别事后归因。
