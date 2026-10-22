# 统一 Agent 实验分支

`experiment/unified-agent`。目标：验证「去掉决策层与说话层的分工」——能不能既提连贯性、
又不让她说话带着算盘。

- 设计说明：[spec.md](spec.md)（含实验二：**让 Conversation 消失**）
- 原型：`state.py` / `tools.py` / `prompt.py` / `model.py` / `loop.py` / `history.py` / `replay.py`

## 实验二做了什么

`conversation_agent` 没了。它原来干两件事，各自有了新归处：

| 原来的职责 | 现在 |
|---|---|
| 派一个人去说 | 她自己说（`speech` 字段直接发出） |
| 把会话消息汇报回来 | `Feed` 每拍自动拉，不需要申请 |
| 遇到不会处理的局面 | `recall_experience` 取真经验与锦囊 |

工具 8 → 2，两个 `decision` 型动作消失，只剩 `recall`（capability）和 `end_episode`（decision）。

## 实验三：工具精简 6 → 2

| 原工具 | 处理 | 原因 |
|---|---|---|
| `look_up` / `look_up_group` | 合成 `recall` | 人不会先判断查的是做法还是人 |
| `wait` | 删除 | 消息每拍自动到，没有可等的，留着只空转 |
| `note` | 删除 | 写进没人读的列表，有出口没去处 |

剩下 `recall`（能力）+ `end_episode`（定性）。不合成一个，因为那会把定性权力藏进参数里。

## 跑一下

```bash
python -m unified.selftest                              # 13 项硬规则
python -m unified.replay --demo                          # 离线假数据
python -m unified.replay --session group_1095799561 --limit 20   # 真会话 + 真经验库
python -m unified.export_history --sessions              # 看有哪些会话
```

后两条直接读 `E:\ABYSS\N_Touch\memory\` 下的两个库，**只读，不写**。
产物落在 `unified/_run/`：`events.jsonl`（每次生成、每次工具调用、每次状态事务）、
`outbox.jsonl`（真正说出去的话，单独一份，方便和 inner 对照）。

## 真数据

- 消息库：18 个会话 / 112,525 条
- 经验库：`bequests` 126 条 + `decision_kits` 3 条
- 实测取到的最高频经验被检索过 487 次

## 和 main 的关系

| | main | 这里 |
|---|---|---|
| 循环 | 5 个 HeavySession + pfc_handoff 接力 | 1 个循环 |
| 说话 | conversation_agent 派出去说 | 自己说 |
| 现场 | 靠汇报摘要 | 每拍自动拉 |
| 意图传递 | `purpose` ≤120 字 | 无接缝 |
| 工具 | 按 agent 切分 | 全开，分 capability / decision |
| 隔离 | 进程边界 | 字段边界（`inner` 不可输出） |
| 状态 | `GlobalSessionState._data` | 显式 `State` + 命名事务 |

## 下一步（要真模型才有意义）

1. 把 `MockModel` 换成真模型：实现 `complete(messages, tools) -> str` 即可。
2. 同一批真实消息跑两遍（main 与这里），人读 20 条，只判断一件事：哪边更像她。
3. 看 `leak_check` 的报警率。它高就说明"字段隔离"挡不住，接缝那 120 字其实是过滤器。

## 已知会输的地方

长对话一致性（没有离线反思沉淀）、可解释性（只能回答"那一轮她心里写了什么"）、
无行为验收。另加实验二新增的两条：文风失去缓冲、容易变成"会用工具的人"。
