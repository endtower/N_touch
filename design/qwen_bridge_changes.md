# 转交 Qwen：桥接契约 v0 主程序侧定案

2026-10-16。请在 WeChatBridge 工作区修改；不要修改 N_Touch，不要因本文件自动部署板端、操作联系人或发送真实消息。遵守用户在你所在会话中已给出的测试与部署限制。

完整约定：本仓库 `design/bridge_contract_v0.md`。这是主程序侧的新增要求，尚待你确认实现，不是对板端现状的描述。

四项答案：时间戳统一 int Unix 秒；支持结构化异步回执（受理号不是消息 ID）；platform 独立保存但保留现有 private_wc/group_wc ID；request_id 由主程序生成、与游标和正文一起落盘。

### 用户最新补充（优先执行）

- 主程序统一使用 `qq_private_<id>` / `wechat_private_<id>` 等平台前缀，桥原始 session_id 无需因此修改；由主程序负责映射及旧数据迁移。
- 桥负责尽最大努力采集真实消息，不负责 Social Agent 决策。Core 的主人线与工作线同时读已入库缓存，不要求桥物理同时打开两个微信会话。
- 用户最新修正：发送 A 时，桥内部自行锁住 A **1–2 分钟**（建议默认 90 秒），不允许巡检或其他目标的发送切走；可在同一桌面线程的安全间隙只读侧栏，记 B/C 未读线索但不点击、不编造正文。当前 A 消息采集及 HTTP 缓存读取继续。窗口到期、当前发送动作安全结束后自主解锁，恢复轮询并尽力补读。
- 主程序继续逐条 POST/send，不再要求批量 parts 或批量回执。A 的后续单条在保留窗口内依次执行，每条独立 request_id、幂等日志和发送回执；其他目标的请求持久排队，不丢弃，不冒称已执行。
- 建议从取得锁时计时，不因同会话每条发送而无限续期；请说明桥侧实际默认值和计时边界。到期不能中断当前输入/点击，也不能为了等待 observed 无限持锁。异常遵守已有失败退出规则，结果不明不自动重发。该时间窗不等于 Core 整个事件链结束。
- 本阶段不要求 Core 发 lock/unlock/focus 租约、不做抢占或复杂优先级调度。已有 focus 可保留为人手动使用的功能，主程序本阶段不主动调用。
- 未读线索不等于消息已采集；读不到侧栏时标未知。无法在发送 A 期间及时取得 B 正文是第一阶段接受的限制，不能宣称历史缓存为最新消息。
- 可额外保留 observed_at_ms 等精细读取时间，不要求为了兼容秒级 timestamp 丢掉已有精度；它仍不是微信真实发送时间。

请优先完成以下更改：

1. 所有端点统一 Bearer token；局域网暴露前完成鉴权。health/poll 加 `protocol_version:"0"`、`platform:"wechat"`；health 加 ready 和能力列表 `text.receive/text.send/send.idempotency.persistent/message.direction.final`。
2. poll 每行给显式 sequence、int timestamp、timestamp_source、news_source、direction；is_own 与 direction 一致。先在同一发布事务中判断方向并认领自己的发送，再让 HTTP 看到行。无法确认方向标 unknown，不能默认 inbound；手动发送也需识别。已发布行不再补改。
3. generation/cursor/消息来自同一快照；分页先按全局 sequence 切，再按会话分组。缓存与账号切换必须换 generation；正常桥重启不换。
4. 幂等记录先落盘再碰桌面，跨进程重启保留；最近 200 条只作为展示窗口。当前 generation 的旧 request_id 保留精简墓碑，禁止超窗后重新执行。同 ID 不同内容返回 409。中断时不重跑可能已执行的任务。
5. POST/send 接受 expected_generation，不符拒绝执行。保留原 GET/send/<id>，增加 GET/send/by-request/<request_id>；所有回执返回 request_id/send_id/status。只在 observed 状态附 message_id；actioned 仍需 report.clicked=true。by-request 404/410 只表示未知/回收，不授权重发。
6. 发送目标未知/同名歧义要拒绝，不能用模糊匹配兜底。群成员未知仍为空，arrival_confirmed 始终 false。

请反馈：可接受/需调整的条款、实际响应 JSON、改动文件、本地验证情况和板端是否升级。主程序侧已经开始写严格客户端与模拟验收，不要求你迁就旧 router.send 必须立刻返回 message_id 的限制。
