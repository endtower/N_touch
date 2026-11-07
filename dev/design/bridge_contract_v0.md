# 通用桥接接入契约 v0：主程序侧定案与微信侧变更

日期：2026-10-16（Asia/Shanghai）。范围：私聊、群聊的纯文字入站与出站。

> 用户后续补充的平台前缀与微信界面调度规则见文末「用户补充：平台身份和发送期间的界面调度」。
> 该节优先于本文早期方案。现有部分后端修改仍在开发、未完成整体验收，也未部署。

本文基于用户转交的《接入契约 v0：微信桥 ↔ 主程序》。下述新增字段和能力是主程序侧提出的联调要求，尚未得到 Qwen 确认，不能当作微信板端已经提供。旧板端的裸 ID、无 generation、无鉴权协议不能直接启用自动收发。

## 四项定案

| 问题 | 定案 | 负责方 |
| --- | --- | --- |
| 时间戳 | `timestamp` 为非负 int Unix 秒，`timestamp_source:"observed_at"`；不是微信实际发送时间。v0 严格拒绝 ISO 字符串，避免混用。 | Qwen 转换；主程序验证 |
| 异步发送 | 接受两阶段。先返回 `request_id/send_id/status`，只有 `observed` 才附 `message_id`。受理号不冒充平台消息 ID；`actioned` 不是对端收到。 | 主程序升级路由返回值与调用者；Qwen 提供状态查询 |
| 平台身份 | `platform:"wechat"` 为独立字段，主程序另存用户配置项的 `connection_id`。微信原有 `private_wc…/group_wc…/unknown_wc…` 原样保留，不要求 Qwen 为了去前缀重建缓存。路由只用保存的连接身份，不用字符串猜平台。 | 两侧 |
| 请求身份 | 主程序生成 UUID 请求 ID，连同逻辑发送操作 ID、连接、generation、收件会话、正文、回执一起先落盘。重启恢复同一请求。游标与请求日志在同一主程序状态数据库。 | 主程序 |

第一阶段每种平台最多启用一个实际连接；`connection_id` 是配置项身份，不代表已经支持微信多账号。桥接端专用一个微信账号；切换账号必须生成新 generation，不能沿用旧会话映射。

## 共同边界

- 接口是 HTTP/JSON，默认 loopback。所有接口（包括 health/poll/status/focus）统一 `Authorization: Bearer <token>`；token 缺失或错误返回 401，不回显 token。局域网绑定前先完成鉴权；跨不可信网络使用 HTTPS 或受控隧道。
- 桥接桌面线程失败即进程退出非零；主程序不能替它自动重启。主程序可显示离线并查询恢复状态，绝不因网络错误自动重复发送。
- 不保证历史完整性；`arrival_confirmed:false` 必须保留。群成员未知保持空字符串及 `speaker_source:"none"`，不编造姓名、ID 和时间。
- 同名会话会冲突、改名会换 ID；标题哈希不是微信稳定身份。发生歧义必须拒绝发送，由用户确认目标，禁止模糊匹配到另一个会话。未知 scope 的消息留在隔离收件箱，不能自动发送。
- 本契约不引入插件市场、远程执行命令或 LLM 修改接入设置的工具。用户配置 SnowLuma 或通用桥接；微信是第一个通用桥接实现。

## 健康检查与能力协商（Qwen 需补）

`GET /health` 成功返回：

```json
{
  "protocol_version": "0",
  "platform": "wechat",
  "generation": "opaque-cache-and-account-generation",
  "ready": true,
  "capabilities": ["text.receive", "text.send", "send.idempotency.persistent", "message.direction.final"],
  "mode": "patrol",
  "focus": [],
  "pinned_missing": [],
  "unclaimed_sends": 0
}
```

generation 是不透明身份字符串；与缓存及微信账号绑定，正常进程重启不改变。缓存重建、换库、账号切换必须改变；侧车丢失不能继续承诺旧 generation。生成与读取 generation 必须和对应数据库一致。缺少任何必需能力时，主程序拒绝启用自动收发，不能默认为“旧版也许能用”。

## 入站与游标

`GET /poll?after=<nonnegative-int>` 成功返回：

```json
{
  "protocol_version": "0",
  "platform": "wechat",
  "generation": "opaque-cache-and-account-generation",
  "cursor": 12,
  "arrival_confirmed": false,
  "mode": "focus",
  "focus": ["private_wc0123456789ab"],
  "sessions": [{
    "session_info": {"session_id": "private_wc0123456789ab", "session_name": "示例会话", "remark": "private"},
    "message_list": [{
      "sequence": 12,
      "message_id": "private_wc0123456789ab-12",
      "user_id": "wc0123456789ab",
      "user_name": "示例用户",
      "timestamp": 1792108800,
      "timestamp_source": "observed_at",
      "content_text": "示例文字",
      "direction": "inbound",
      "is_own": false,
      "news_source": "diff"
    }]
  }]
}
```

要求：

1. `sequence` 显式提供，不让通用客户端解析微信特定 message_id。它是整个缓存共用序列；每页消息满足 `after < sequence <= cursor`，不能重复序列。按 sequence 分页后再分组到 sessions；不能先各会话截断、再推进到全局最大序列而漏掉别的会话。
2. cursor 只越过本页已返回的消息及明确丢弃的非消息行；空页也可因时间分隔行推进。非消息行之外，不能越过下一页尚未返回的消息。generation、cursor、行列表来自同一数据库快照。
3. 主程序把 `(connection_id,generation,cursor)` 配对持久保存。检测 generation 改变时丢弃旧游标响应，用 `after=0` 重读；重读期间又换代则报错，不提交任何游标。
4. 主程序在一个事务中写入原始消息收件箱和新游标，后续标准化、写主消息库另行处理。重启可重放，不会因已经推进桥游标而丢失尚未处理的消息。
5. inbox 去重键为 `(connection_id,generation,session_id,message_id)`。同一个 generation 的已发布行不可变；发现同 ID 内容或方向改变时报错，不能当作新入站。换 generation 后重新编号不会被旧数据误去重。
6. `news_source` 必须逐条保留。`baseline/unquantified` 以及未知方向/未知 scope 原文持久保存并隔离，默认不触发 LLM；不能通过内容哈希删除“相同文字”，否则会吞掉真人重复发言。`diff/badge` 仍然不代表绝无重复。

### 方向发布屏障：当前契约的阻塞点（Qwen 需改）

“普通行先出现在 poll，之后补 is_own”不可用：主程序游标推进后不会重取旧行，并可能已回复自己的话。

要求在该轮新行对 HTTP 可见前完成方向判断与发送认领，事务提交后才发布。`direction` 固定为 `inbound/outbound/unknown`；`is_own` 固定等于 direction 是否为 outbound；unknown 不等于已确认 inbound。若 AT-SPI 不能确定左右气泡/自己的身份，就标 unknown，不能用“没有匹配待发送文本”推断 inbound。手动在微信客户端发出的消息同样要识别方向。

同文本 + 发送前水位只能作为界面证据，不能证明对端送达，也可能误认领同文本手动发送。桥接回执需保留水位、证据来源；无法区分时维持 unknown/actioned，主程序不重发。

## 出站和回执

`POST /send`：

```json
{
  "request_id": "main-generated-uuid",
  "expected_generation": "opaque-cache-and-account-generation",
  "conversation": "private_wc0123456789ab",
  "text": "示例文字"
}
```

首次受理 `202`；重复相同请求返回 `200`，同一回执且 `replayed:true`，不得再次入队。

```json
{"request_id":"main-generated-uuid","send_id":"bridge-send-id","status":"queued","message_id":null}
```

查询沿用 `GET /send/<send_id>`，**新增** `GET /send/by-request/<request_id>`；后者解决 POST 已执行但 HTTP 响应丢失、主程序不知道 send_id 的窗口。

| status | 语义 | 必需字段 |
| --- | --- | --- |
| queued | 持久受理，尚未确认桌面动作 | request_id、send_id |
| actioned | 写入输入框并点过一次发送 | 同上，`report.clicked:true`，无 message_id |
| observed | 在界面观察到相应发出行 | 同上，非空 message_id；不是对端送达 |
| rejected | 明确没有执行，例如无目标、目标歧义、generation 不符 | 同上，reason |
| failed | 有明确失败证据，不能据此声称没有发送 | 同上，reason；主程序不自动重发 |
| unknown | 进程/网络中断后无法判断是否执行 | 同上；主程序不自动重发 |

仅 observed 可提供 message_id。明确的参数/鉴权错误可返回 4xx；不确定是否发送的请求不能通过 5xx 暗示“可安全重试”。

### 幂等日志：200 条窗口需要修改（Qwen 需改）

- 接受 request_id 的记录必须在任何桌面动作之前事务落盘。正常桥进程重启保留日志；重启时不能重新执行处于“可能已经执行”的任务，必须置 unknown 或仅查询界面证据。
- 最近 200 条可以是展示窗口，不能成为幂等边界。第一阶段最简单的做法：当前 generation 内持久保留 request_id 的精简记录/墓碑。回执正文可回收，但旧 ID 不得重新入队。
- 同一 request_id 携带不同 conversation、text 或 expected_generation 返回 409，不执行；检查参数一致性应发生在返回重放回执之前。
- generation 不匹配返回 409、不执行；换账号/换缓存后旧请求不能落到新目标。已知请求先按持久记录返回，不得重新执行；全新请求必须核对 expected_generation。
- 查询未知请求返回 404；只剩墓碑、详细回执已回收可返回 410。二者均不授权重发；主程序保留 unknown，用户如明确决定再发才创建新的逻辑操作和新请求 ID。
- 主程序先落本地 outbox，再把状态改为 submitting 并提交，最多主动 POST 一次；超时/断电/取消都只查 by-request。桥的幂等能力仍是必要防线，不能用 HTTP 只能调用一次推导“网络绝不重复”。

## 分工与验收门槛

主程序侧（本仓库）：

1. 定义并验证本契约，提供模拟 HTTP 桥、本地持久收件箱、游标与发送日志。
2. 后续连接独立 ingress，把桥消息映射至主消息库；持久保存来源连接/generation，去除发送工具硬编码 QQ，升级路由为结构化回执。未完成前不得把受理号塞进旧 message_id 返回值。
3. 再提供 SnowLuma/通用桥接的用户设置、检查连接、状态提示，以及隔离消息和不明确发送的人工处理入口。不得在 LLM 工具中暴露 token 和接入设置。

Qwen（WeChatBridge）：

1. 完成 token、协议版本和能力字段、整数时间戳、逐行 sequence/news_source/final direction。
2. 实现持久幂等日志、by-request 查询、generation 发送屏障、先认领后发布，并用本地合成测试验证。
3. 回传真实 JSON 样例和测试结果，再安排板端升级。板端升级/登录/真实收发由桥接侧和用户控制；本仓库测试不访问树莓派，不操作真实联系人。

必须通过：同 generation 重启继续读取；换 generation 游标归零且旧 ID 不冲突；非消息行推进；分页不漏行；未知方向隔离；自发消息不被当真人输入；POST 响应丢失后仅查询且不重复发送；重启后请求 ID 不变；超过 200 条仍拒绝重执行；同 ID 不同正文拒绝；401/版本不符不启用；桥未上新版则连接明确失败。

## 当前落地状态

- 已写：`adapt/bridge/contract.py` 严格协议验证，`state.py` 持久状态，`client.py` 异步 HTTP 客户端；本地模拟验收脚本 `scripts/acceptance_bridge_contract.py`。
- 源码已接入独立 ingress、Core 的桥接代理、按持久来源选平台的路由和平台配置 UI；已用模拟桥验收，尚未连接真实微信，未重新打包或部署在用程序。该客户端主动要求新能力，不能直接连接当前旧版微信桥。
- 新微信会话已使用平台前缀及连接/代次隔离。旧 QQ ID 仍保留，完整历史迁移未执行；`scripts/audit_session_prefixes.py` 提供只读统计，没有 apply 模式。
- 旧 Windows 自动化探索见 `design/wechat_bridge_handoff.md`，仅作为历史记录；最新传输约定以本文和 Qwen 后续确认结果为准。

## 用户补充：平台身份和发送期间的界面调度

本节记录用户后续明确的要求，优先于前文早期方案；当前代码尚未全部实现。

### 平台前缀与存储

- 主程序会话 ID 统一平台前缀：`qq_private_<id>`、`qq_group_<id>`、`wechat_private_<id>`、`wechat_group_<id>`；Web 同样独立。微信 `<id>` 为主程序映射后的不透明标识，须保留连接/缓存代次隔离，远端原始 ID 在映射中原样保存。平台和 scope 仍是独立字段，不在各处临时切字符串猜目标。
- QQ 与微信的标准消息进入同一个主程序消息库、同一套消费者游标和 Social Agent；平台前缀及来源映射隔离身份。桥接程序自己的 SQLite 是界面采集缓存与发送日志，不是另建一套 Core 记忆系统。
- 前缀变更属于数据迁移：需要同步历史会话、消息、消费者游标、路由、主人会话派生、其他会话引用与前端选择。迁移前留备份；不得只改新消息 ID 而把历史割裂。实际迁移另行验证，不在运行服务上直接执行。

### 并发模型

Core 可以同时读取和处理多个微信会话**已采集并持久化**的消息。一个微信 UI 同一时刻只操作一个会话，两个事实不冲突。Social Agent 的主人线/工作线沿用同一缓存库，不为“检查另一条线”直接命令桥切换窗口。

桥侧一个桌面执行线程调度所有 UI 动作；HTTP 继续只读已提交缓存，不触碰 UI，也不因一条发送任务持有数据库锁而长期阻塞。

发送 A 会话时（按用户最新修正采用 1–2 分钟的桥内保留窗口）：

1. 桥核验目标、进入 A，本地锁定 A 约 1–2 分钟；建议默认 90 秒，由桥自行计时。期间不允许巡检或其他发送任务切去 B。同一 A 的后续单条发送可在窗口内依次执行，不要求主程序合并批量请求。
2. 可以在同一桌面线程的安全间隙只读侧栏，记录 B/C 的未读角标等线索；不能点击，也不能据此构造 B/C 消息正文。若侧栏读不到，则明确未知，不保证持续或完整发现。
3. 保留窗口到期后，等待当前已开始的发送动作安全结束，由桥自身解除锁定、恢复轮询，尽力补读有未读线索的会话，再提交真实消息。窗口不能在一次输入/点击动作中途强制切走。
4. 任务执行失败按既有规则终止/上报；结果不明不重发。不为追求 observed 回执而无限锁住 A，执行结束与后续读回确认是两个阶段。锁不阻塞当前 A 的采集、侧栏只读观察或 HTTP 对已提交缓存的读取；其他会话的出站请求可持久排队，不得因 UI 锁而丢弃或冒称已执行。

锁的调度单位是桥内的**1–2 分钟会话保留窗口**，不是整个 LLM 事件链。没有 Core 发起的 lock/unlock/focus 租约、优先级抢占、跨会话状态机控制。建议从本次取得会话锁计时、不随每条单发无限续期，避免持续发言使其他会话永久无法补读；具体默认值及边界由 Qwen 在桥侧实现时说明。该窗口不证明 Core 已经完成整个会话，仅是第一阶段的简单调度策略。

### 逐条发送，取消批量任务要求（用户最新修正）

主程序保留当前 ToolConversation 将一次工具调用拆成多条文字、逐条提交及打字延迟的方式。桥的会话保留窗口覆盖这些独立请求之间的间隙；不需要主程序另外发送“这一批开始/结束”的信号。

不再要求 Qwen 增加 parts 数组或批量回执。继续 `POST /send {request_id, expected_generation, conversation, text}`，每条请求独立持久记录与去重，每条分别提供回执。保留窗口内不同目标的发送排队；已受理与实际执行必须区分。

桥负责保留窗口、锁与轮询，N_Touch 只提交目标和待发送正文。窗口到期后的后续请求交给桥正常调度；不承诺任意长的 Core 事件链都不会被轮询打断。

### 明确接受的限制

锁住 A 的 1–2 分钟内，Core 可能已经知道 B 有未读线索，但还拿不到 B 的新正文；也可能连线索都暂时读不到。使用的是最后一次成功采集的记录，不能把历史缓存描述为 B 的最新实时状态。窗口结束后补读仍是尽力而为，不承诺滚出视口的消息都能恢复。

Qwen 的职责是尽最大努力采集并交付可证实的消息，以及串行、安全地执行发送。主程序负责 AI 决策、主人/工作线协调和发言限制；本阶段不要求桥猜测 Core 的工作流。
