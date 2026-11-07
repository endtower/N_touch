"""PRE 处理模块（pre_truck）—— 契机规则层。

定位：低消耗、偶尔看一眼电脑屏幕的感觉。不派发任务，只负责发现「可以上场」的契机，
把契机推给 proactive 判断层（ProactiveSession，intent=pre_opportunity），
由判断层决定要不要拉起 PFCA。

规则层只有两条（零 LLM 成本）：
1. 时间：距上次契机巡检满 CYCLE_TIME（默认 1 小时）→ 命中。
2. 群聊中主人有新消息：扫描群聊未读（与 PFCA 共用 consumer="pfca" 游标），
   未读里含 user_id == MASTER_ID 的消息 → 命中。

触发冷却：命中一次后，距下次规则判定需满 TRIGGER_CD（默认 30 分钟），
冷却期内整辆 truck 不判定任何规则，避免主人频繁发言导致契机过密。

规则命中 → 判断层 → launch_pfca（PFCA 行动，可能只是发言/露面）或
skip_proactive（静默，等下一轮）。

游标契约：PRE 与 PFCA 共用 consumer_id="pfca" 游标。PRE 是 PFCA 的前置过滤层——
它先看一遍未读，只有构成契机（群聊里主人有新发言）才交给判断层/PFCA；没有主人新
消息的群聊由 PRE 直接推进游标替 PFCA 标记已读，PFCA 不再为这批闲聊付阅读成本。
因此本轮扫描到的消息无论最终 launch_pfca 还是 skip_proactive，判断结束后都统一把
pfca 游标推进到扫描快照，避免同一批消息重复触发；判断期间新到的消息留在快照之后，
交给下一轮处理。at_mention_truck 与 PFCA 工具共用同一游标，被 PRE 过滤掉的消息它们
也不会再读到。

持续任务（kind=ongoing）是判断层的背景：ProactiveSession 会拉全部 active 任务
（含 ongoing），此处无需单独传递。持续任务没有 target_session_id，也没有到期时间。
"""

import os
import time
import traceback
from typing import Any, Dict, List, Optional

from utils.logger import get_logger
from utils.chain_context import get_current_chain_id
from .base_truck import BaseTruck

logger = get_logger('pre_truck', 'INFO')

# 与 PFCA 共用的消息游标消费者：PRE 过滤完的消息直接替 PFCA 标记已读
CONSUMER_ID = "pfca"
# 单次最多读取的未读消息数（数据层上限 MAX_UNREAD_FETCH_LIMIT = 50）
MAX_SHOW_UNREAD = 20
# 判断层需要的语境条数下限（未读不足时从游标之前补历史）
MIN_SHOW_MESSAGES = 10
# 一次判断最多附带几个群聊的上下文
MAX_SESSIONS_IN_REASON = 3


class PreTruck(BaseTruck):
    """PRE 处理模块 - 契机规则层（时间 + 群聊中主人有没有消息）"""

    def __init__(self):
        super().__init__("pre_truck")
        # 上次契机巡检时间（时间规则的基准；启动时重新计时，避免刚启动就触发）
        self._last_trigger_at = time.time()
        # 本次规则命中的内容（_main_loop 设置，station 调 get_truck_messages 时消费）
        self._pending: Optional[Dict[str, Any]] = None

    # ==================== 规则层 ====================

    def _rule_hit_time(self) -> bool:
        """时间规则：距上次契机巡检满 CYCLE_TIME 即命中。"""
        from config.truck_settings import pre_truck_config
        return (time.time() - self._last_trigger_at) >= pre_truck_config.CYCLE_TIME

    @staticmethod
    def _master_identity() -> tuple:
        """主人身份 (user_id, user_name)，来自 role_settings。"""
        from config.role_settings import role_config
        return str(role_config.MASTER_ID).strip(), role_config.MASTER_NAME

    @classmethod
    def _is_master_message(cls, msg: Dict[str, Any]) -> bool:
        """是否为主人发出的消息（群聊消息的 user_id 即发言人 QQ 号）。"""
        master_id, _ = cls._master_identity()
        return str(msg.get("user_id", "")).strip() == master_id

    async def _rule_hit_master_message(self) -> List[Dict[str, Any]]:
        """群聊规则：pfca 游标之后的群聊未读里，是否有主人的新消息。

        游标闭环（与 PFCA 共用 pfca 游标）：
        - 首次看到某会话（游标不存在）只建立基线，不把历史消息当成新契机；
        - 未读里没有主人消息的会话直接推进 pfca 游标消费掉，替 PFCA 过滤掉这批闲聊。

        Returns:
            List[Dict]: 命中的群聊（按主人最新发言的 _seq 倒序），每项含会话信息、
                最近消息列表（未读不足时已从游标前补历史）、主人的新消息、
                扫描时的最新 _seq（判断结束后按它推进游标，不吞掉判断期间的新消息）。
            空列表表示未命中。
        """
        from memory.message_advanced import get_message_advanced_manager
        from memory.message_repository import get_message_store

        message_advanced_manager = get_message_advanced_manager()
        store = get_message_store()

        unread_sessions = await message_advanced_manager.get_unread_sessions_by_type(
            "group", consumer_id=CONSUMER_ID
        )

        triggered: List[Dict[str, Any]] = []
        for session in unread_sessions:
            session_id = session.get("session_id", "")
            if not session_id:
                continue

            current_cursor = await store.get_cursor(session_id, CONSUMER_ID)
            unread_messages = await store.get_unread_for(
                session_id, CONSUMER_ID, limit=MAX_SHOW_UNREAD, newest=True
            )
            if not unread_messages:
                continue
            # newest=True 升序返回，末条即扫描时的会话最新消息
            latest_seq = unread_messages[-1].get("_seq", current_cursor)

            if current_cursor < 0:
                # 第一次看到该会话：建立游标基线，历史消息不当作新契机
                await store.advance_cursor(session_id, CONSUMER_ID, latest_seq)
                logger.debug(f"PRE 建立 pfca 游标基线 {session_id} -> {latest_seq}")
                continue

            master_messages = [m for m in unread_messages if self._is_master_message(m)]
            if not master_messages:
                # 没有主人的新消息：不构成 PRE 契机，直接推进 pfca 游标替 PFCA 过滤掉
                if latest_seq > current_cursor:
                    await store.advance_cursor(session_id, CONSUMER_ID, latest_seq)
                continue

            filler_messages: List[Dict[str, Any]] = []
            if len(unread_messages) < MIN_SHOW_MESSAGES:
                filler_messages = await store.get_messages_before_cursor(
                    session_id, CONSUMER_ID,
                    limit=MIN_SHOW_MESSAGES - len(unread_messages),
                )

            triggered.append({
                "session_id": session_id,
                "session_type": "group",
                "session_name": session.get("session_name", ""),
                "remark": session.get("remark", ""),
                "unread_count": session.get("unread_count", 0),
                "unread_messages": unread_messages,
                "master_messages": master_messages,
                "message_list": filler_messages + unread_messages,
                "max_seq": latest_seq,
            })

        triggered.sort(
            key=lambda s: s["master_messages"][-1].get("_seq", 0),
            reverse=True,
        )
        return triggered

    # ==================== 主循环 ====================

    async def cycle_control(self):
        from config.truck_settings import pre_truck_config
        await super().cycle_control(pre_truck_config.CHECK_CYCLE_TIME)

    async def _main_loop(self):
        """主循环 - 先检测再等待：规则命中才提醒 station（规则层零 LLM 成本）。"""
        from config.truck_settings import pre_truck_config

        logger.info(
            f"PreTruck 已启动, check_interval={pre_truck_config.CHECK_CYCLE_TIME}s, "
            f"cycle_time={pre_truck_config.CYCLE_TIME}s"
        )

        while self.state != "stopped":
            try:
                # 冷却闸门：距上次触发不足 TRIGGER_CD 时整辆 truck 不判定任何规则。
                # 必须先 await cycle_control 保持 60s 轮询节奏，否则会变成忙等空转。
                if time.time() - self._last_trigger_at < pre_truck_config.TRIGGER_CD:
                    await self.cycle_control()
                    continue

                # 优先级：主人新消息（更及时的契机）> 时间到
                sessions = await self._rule_hit_master_message()
                if sessions:
                    self._pending = {
                        "rule": "master_message",
                        "sessions": sessions,
                    }
                    logger.info(
                        f"PRE 规则命中（群聊中主人有新消息）: "
                        f"{[s['session_id'] for s in sessions[:MAX_SESSIONS_IN_REASON]]}"
                    )
                elif self._rule_hit_time():
                    self._pending = {"rule": "time", "sessions": []}
                    logger.info("PRE 规则命中（时间到）")

                if self._pending:
                    self._last_trigger_at = time.time()
                    await self.remind_station()
                await self.cycle_control()
            except Exception as e:
                # 硬约定：关键环节失败不允许继续运行，直接崩溃阻断
                logger.critical(
                    f"PreTruck 主循环发生致命错误，强制退出进程: "
                    f"{e}\n{traceback.format_exc()}"
                )
                for h in getattr(logger, 'handlers', []):
                    try:
                        h.flush()
                    except Exception:
                        pass
                os._exit(1)

    # ==================== 判断层输入 ====================

    @staticmethod
    def _format_sessions(sessions: List[Dict[str, Any]]) -> str:
        """把命中的群聊整理成判断层可读的上下文（主人新消息 + 最近消息）。"""
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        _, master_name = PreTruck._master_identity()
        history = get_global_session_history()
        blocks: List[str] = []

        for session in sessions[:MAX_SESSIONS_IN_REASON]:
            session_name = session.get("session_name", "") or session["session_id"]
            block = [
                f"群聊「{session_name}」（{session['session_id']}）中，"
                f"主人 {master_name} 有新发言。"
            ]
            show_lines, _ = history.format_messages(
                session.get("message_list", []),
                FetchOptions(
                    message_format=MessageFormatOptions(
                        show_new_marker=True,
                        show_timestamp=True,
                    )
                ),
            )
            block.extend(show_lines)
            blocks.append("\n".join(block))

        return "\n\n".join(blocks)

    def _build_wakeup_reason(self, pending: Dict[str, Any]) -> str:
        """构造判断层的「本次主动唤醒原因」。"""
        rule = pending.get("rule")
        if rule == "master_message":
            return (
                "规则命中：群聊中主人有新消息。这是可以上场的契机——"
                "请判断此刻是否值得做点什么（参与话题、露面、回应主人、发发牢骚），"
                "还是继续静默。\n\n"
                + self._format_sessions(pending.get("sessions") or [])
            )
        return (
            "规则命中：时间到。距离上次契机巡检已满一个周期，"
            "请看一眼当前有没有值得主动做点什么的机会；没有就跳过，等下一轮。"
        )

    # ==================== 自身上下文落盘 ====================

    @staticmethod
    def _record_pre_context(
        pending: Dict[str, Any],
        wakeup_reason: str,
        decision_result: Optional[Dict[str, Any]],
    ) -> None:
        """把本次契机写入 PRE 独立上下文（system / user / assistant 三段）。

        PRE 是零 LLM 的规则层，但事件链面板需要看到它自己的判断过程，
        而不是只有它唤醒 PFCA 后留下的 proactive 上下文。本方法运行在 station
        的 set_chain_context 作用域内，直接落位
        event_chains/{chain_id}/pre_{HHMMSS}.json，链尾随链统一归档。
        """
        from brain.att.trucks.pre_history_manager import get_pre_history_manager

        sessions = pending.get("sessions") or []
        if pending.get("rule") == "master_message":
            rule_desc = "群聊中主人有新消息"
            scan_lines = [
                f"- {s.get('session_name') or s['session_id']}（{s['session_id']}）"
                f"：未读 {len(s.get('unread_messages') or [])} 条，"
                f"主人新发言 {len(s.get('master_messages') or [])} 条，"
                f"扫描快照 _seq={s.get('max_seq')}"
                for s in sessions[:MAX_SESSIONS_IN_REASON]
            ]
        else:
            rule_desc = "距上次契机巡检已满一个周期"
            scan_lines = ["- 无（时间规则不扫描群聊）"]

        system_content = "\n".join([
            "【PRE 契机规则层】",
            f"规则命中: {pending.get('rule')}（{rule_desc}）",
            f"共用游标消费者: {CONSUMER_ID}（与 PFCA 共用，过滤后直接替 PFCA 标记已读）",
            "扫描范围:",
            *scan_lines,
        ])

        if decision_result:
            assistant_content = f"判断层决策: {decision_result.get('tool_name') or 'unknown'}"
            tool_result = decision_result.get("tool_result")
            if isinstance(tool_result, str) and tool_result.strip():
                assistant_content += f"\n\n{tool_result}"
        else:
            assistant_content = "判断层未返回决策（本轮不拉起 PFCA）"

        manager = get_pre_history_manager()
        manager.append({"role": "system", "content": system_content})
        manager.append({"role": "user", "content": wakeup_reason})
        manager.append({"role": "assistant", "content": assistant_content})

    # ==================== 交付 ====================

    async def _advance_shared_cursor(self, sessions: List[Dict[str, Any]]) -> None:
        """判断结束后把 pfca 游标推进到扫描快照（只进不退）。

        PRE 与 PFCA 共用游标：本轮已交给判断层处理过的消息，PFCA 不应再读一遍。
        """
        from memory.message_repository import get_message_store

        store = get_message_store()
        for session in sessions:
            session_id = session.get("session_id", "")
            if not session_id:
                continue
            current_cursor = await store.get_cursor(session_id, CONSUMER_ID)
            # 按扫描时的快照推进：判断期间新到的消息留给下一轮，不在这里吞掉
            latest_seq = session.get("max_seq")
            if latest_seq is None:
                latest = await store.get_latest_session_messages(session_id, 1)
                latest_messages = latest.get("message_list", [])
                if not latest_messages:
                    continue
                latest_seq = latest_messages[0]["_seq"]
            if latest_seq > current_cursor:
                await store.advance_cursor(session_id, CONSUMER_ID, latest_seq)
                logger.info(
                    f"PreTruck 判断后推进 pfca 游标 "
                    f"{current_cursor} -> {latest_seq} ({session_id})"
                )

    async def get_truck_messages(self) -> Optional[Dict[str, Any]]:
        """station 调度时调用：把规则命中的契机交给 proactive 判断层。

        判断层决定 launch_pfca 才返回 dict（station 调 PFCA）；skip / 无决策返回 None。
        """
        pending = self._pending
        self._pending = None
        if not pending:
            return None

        sessions = pending.get("sessions") or []
        try:
            import json
            from brain.att.proactive.proactive_core import get_proactive_core

            core = get_proactive_core()
            if not core.is_running:
                await core.start()
                logger.info("proactive core 启动 (lazy)")

            # 契机的完整文本：既交给判断层，也写入 PRE 自身上下文
            wakeup_reason = self._build_wakeup_reason(pending)

            task_draft = {
                "intent": "pre_opportunity",
                "reason": f"PRE 契机规则命中: {pending.get('rule')}",
                "wakeup_reason": wakeup_reason,
                # 持续任务没有 target_session_id；目标会话由判断层决定
                "target_session_id": None,
                # station 出队即生成的任务链 ID（contextvar 不传播到 proactive
                # 主循环任务，需显式携带，主循环内重新 set）
                "chain_id": get_current_chain_id(),
                "payload": {
                    "source": "pre_truck",
                    "rule": pending.get("rule"),
                    "session_ids": [s["session_id"] for s in sessions],
                },
            }
            logger.info(
                f"pre_truck 投递契机构会到 proactive 判断层, rule={pending.get('rule')}"
            )
            # 阻塞到 proactive_session 退出
            await core.feed_proactive_task(task_draft)

            decision_result = task_draft.get("_decision_result")
            logger.info(
                f"pre_truck 拿到 proactive 判断: "
                f"{decision_result.get('tool_name') if decision_result else 'None'}"
            )

            # PRE 自身也留一份上下文（事件链面板可见 PRE，而不只是它唤醒的 PFCA）
            self._record_pre_context(pending, wakeup_reason, decision_result)

            # 无论判断结果如何，本轮契机已消费完毕：推进共用的 pfca 游标
            await self._advance_shared_cursor(sessions)

            if not decision_result:
                return None
            if decision_result.get("skipped"):
                logger.info("proactive 判断后决定静默（skip_proactive）")
                return None
            if decision_result.get("tool_name") != "launch_pfca":
                logger.info(
                    f"proactive 判断为 {decision_result.get('tool_name')!r}, 非 launch_pfca"
                )
                return None

            # === launch_pfca：构造 PFCA user content ===
            tool_result = decision_result.get("tool_result")
            formatted_message = ""
            target_session_id = ""
            target_task_title = ""
            if isinstance(tool_result, str) and tool_result.strip():
                try:
                    queued = json.loads(tool_result)
                    if isinstance(queued, dict) and queued.get("status") == "queued":
                        task_info = (queued.get("task_info") or "").strip()
                        target_session_id = (queued.get("target_session_id") or "").strip()
                        target_task_title = (
                            (queued.get("target_task_title") or "").strip()
                        )
                        if task_info:
                            formatted_message = f"【PRE 契机】\n{task_info}"
                except (ValueError, TypeError):
                    logger.info("proactive tool_result 非 JSON, 按空消息处理")

            if not formatted_message:
                formatted_message = "（PRE 契机构会触发，但判断层未提供 task_info）"

            # 判断层没指定目标会话时，回落规则命中的群聊（主人发言的现场）
            if not target_session_id and sessions:
                target_session_id = sessions[0]["session_id"]

            if target_session_id:
                formatted_message = f"{formatted_message}\n\n目标会话: {target_session_id}"

            # 判断层若引用了任务表条目：一次性任务交付即过期；持续任务永远不在此过期
            if target_task_title:
                from brain.hc.database.task import (
                    TASK_KIND_ONGOING,
                    TaskOperations,
                )
                task_ops = TaskOperations()
                task_id = await task_ops.get_task_id_by_title(target_task_title)
                if task_id:
                    rec = await task_ops.get_task_by_id(task_id)
                    if rec and rec.get("kind") != TASK_KIND_ONGOING:
                        await task_ops.expire_task(task_id)
                        logger.info(f"PreTruck 交付一次性任务并置过期: {target_task_title}")
                    else:
                        logger.info(f"PreTruck 引用持续任务，保持挂起: {target_task_title}")

            logger.info("pre_truck 判断 launch_pfca, station 将调 PFCA")

            raw_sessions = None
            if target_session_id:
                raw_sessions = {"session_id": target_session_id}

            return {
                "formatted_message": formatted_message,
                "raw_sessions": raw_sessions,
                "decision": decision_result,
            }
        except Exception as e:
            logger.critical(
                f"pre_truck 投递判断层发生致命错误，强制退出进程: "
                f"{e}\n{traceback.format_exc()}"
            )
            for h in getattr(logger, 'handlers', []):
                try:
                    h.flush()
                except Exception:
                    pass
            os._exit(1)
