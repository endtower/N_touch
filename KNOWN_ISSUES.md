# Known Issues

> 本文件记录尚未修复、但已造成实际影响的问题。仅作为提醒，不替代代码修复。

---

## Issue #1 · Bequest 经验库产生"自我标注应删除"的噪音节点

**日期**: 2026-08-13
**严重程度**: 中（污染向量召回 + 占用 ID + 影响体验整洁度）
**状态**: 待修复

### 现象描述

话术经验登记链路在写入 Neo4j `Bequest` 节点时，会出现 LLM 自我标注"无需保留/已删除"的条目，但实际仍然落库。共清理 4 条，均已手动从数据库删除：

| bequest_id | 标题 | 内容 | 创建时间 |
|---|---|---|---|
| `Bequest_20260812024753824` | 话术经验登记无更新-已删除 | 此条不再适用，已删除。 | 2026-08-12T02:47:53 |
| `Bequest_20260812024759375` | 无更新标记-已合并删除 | 经分析本无需另行登记，此条目已由'指导说话态度与干脆否决'涵盖，予以删除以保持经验库整洁。 | 2026-08-12T02:47:59 |
| `Bequest_20260711175859707` | 话术经验登记无更新 | 此条已不再适用，删除。 | 2026-07-11T17:58:59 |
| `Bequest_20260812024746242` | 无更新标记 | 经分析，本次发言内容过于常规平淡，无值得登记的新词汇句式结构。 | 2026-08-12T02:47:46 |

清理前后数据库规模：98 → 94 条 Bequest。

### 修复方向（待规划）

- 在 `BequestOperations.create_bequest` 调用前增加 title 黑名单/正则拦截（如：`已删除|无更新|不再适用|合并删除`）。
- 或在写入路径增加 LLM 输出后置清洗：检测到"无需登记"语义时直接 abort 写入。
- 关联模块：话术经验登记链路（conversation / social_chat）。

---

## Issue #2 · PFCA 会话陷入"重复调用 query_history"死循环

**日期**: 2026-08-14
**严重程度**: 高（死循环阻塞会话、无限消耗 token、日志刷屏）
**状态**: 待修复

### 现象描述

PFCA 会话在群聊中等待"大湿"回复时，LLM 反复调用 `query_history` 工具，且每次调用参数完全一致、reasoning 内容完全相同，形成死循环：

```
08-14 21:38:14 [INFO] PFCATools | 处理工具请求：工具名称=query_history，参数={'search_range': 'last_8', 'session_id': 'group_424674224'}
08-14 21:38:16 [INFO] PFCATools | 处理工具请求：工具名称=query_history，参数={'search_range': 'last_8', 'session_id': 'group_424674224'}
08-14 21:38:18 [INFO] PFCATools | 处理工具请求：工具名称=query_history，参数={'search_range': 'last_8', 'session_id': 'group_424674224'}
...
reasoning: 大湿还没有回复我的三条追问。群里的对话暂停在我发的三条问题上。我需要继续等待观察。让我再等等看大湿是否回复。
```

每次 `query_history` 均正常返回（标记 8 条消息为已读），因此会话循环现有的两个退出计数都不增长：

- `no_tool_call_count`（LLM 未使用工具）→ 不触发，因为 LLM 一直在调用工具。
- `tool_error_count`（工具参数错误）→ 不触发，因为工具调用每次都成功。

### 根因分析

`PFCASession.process_cycle` 的退出防护仅覆盖两种异常情形：

1. LLM 累计 3 次未使用工具（`no_tool_call_count >= max_no_tool_call`）。
2. 工具调用累计 3 次参数错误（`tool_error_count >= max_tool_error`）。

但对于"LLM 反复调用同一工具且参数相同、工具成功返回但无状态推进、reasoning 内容不变"这种**成功但无进展**的情形，没有任何去重 / 循环检测 / 轮次上限机制，导致 `while self.running` 循环无法退出。

### 修复方向（待规划）

- 增加工具调用去重检测：记录最近 N 次工具调用（`name + arguments`），连续重复超过阈值时强制 `raise` 或 `break`。
- 增加单次会话的最大 cycle 上限（如 `max_cycles`），超出即停止会话并记录退出原因。
- 或针对 `query_history` 等"只读查询"类工具，限制其在相邻轮次中的重复调用次数。
- 关联模块：`brain/pfc/pfca/pfca_session.py`（`process_cycle` / `_handle_tool_call`）。