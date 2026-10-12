import asyncio
import os
import traceback
from typing import Dict
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from utils.chain_context import generate_chain_id, set_chain_context, reset_chain_context
from config.truck_settings import is_truck_enabled
from .trucks.private_truck import PrivateTruck
from .trucks.special_attention_truck import SpecialAttentionTruck
from .trucks.reflection_truck import ReflectionCharTruck, ReflectionTimeTruck
from .trucks.feedback_reflection_truck import ReflectionEfficacyTruck, ReflectionBelongingTruck

logger = get_logger(__name__, 'WARNING')


def _force_archive_chain_contexts():
    """链结束强制归档：终结所有 kind 对该链的写入资格。

    - conversation / observe / temporary / social_chat → Chat_HistoryManager 工厂
    - pfca / proactive → 各自全局单例
    - quest → Quest_HistoryManager 全局单例（QuestAgent 落盘，链尾终结写入资格）
    - persona / vision / bequest / memory → LightAgent_HistoryManager 前缀单例
      （LightweightAgent._llm_loop 落盘，链尾终结写入资格）

    新存储模型下链内文件写入即落位到 event_chains/{chain_id}/{prefix}.json，
    归档只终结写入资格（移出进行中注册表 + 广播），文件原位保留，
    前端按链文件夹直接查看该任务涉及的全部 agent 上下文；
    链外（unlinked）场景则轮转常驻文件为时间戳归档并清空。
    无论 PFCA 是否被调用都要执行（skip 分支也可能写入了 proactive 上下文）。
    """
    from brain.chat.chat_history_manager import get_history_manager
    from brain.pfc.pfca.pfca_history_manager import get_pfca_history_manager
    from brain.att.proactive.proactive_history_manager import get_proactive_history_manager
    from brain.base.light_agent.quest_history_manager import get_quest_history_manager
    from brain.base.light_agent.light_history_manager import archive_light_contexts
    managers = [
        get_history_manager('conversation'),
        get_history_manager('observe'),
        get_history_manager('temporary'),
        get_history_manager('social_chat'),
        get_pfca_history_manager(),
        get_proactive_history_manager(),
        get_quest_history_manager(),
    ]
    for m in managers:
        m.archive_context()
    # light agent 系（persona / vision / bequest / memory）：链尾终结写入资格
    archive_light_contexts()


class AttentionStation:
    def __init__(self):
        self.running = False
        self.private_truck = None  # 先初始化为None，避免循环引用
        self.ongoing_truck = None  # 先初始化为None，避免循环引用
        self.special_attention_truck = None  # 先初始化为None，避免循环引用
        self.scheduled_proactive_truck = None  # 定时主动任务 truck
        self.reflection_char_truck = None  # 反思系统字符触发 truck
        self.reflection_time_truck = None  # 反思系统定时触发 truck
        self.reflection_efficacy_truck = None  # 效能感低值自发入口
        self.reflection_belonging_truck = None  # 归属感低值自发入口
        self.task_truck = None  # 任务到期触发 truck
        # 任务队列 - 存储需要处理的truck请求
        self.task_queue = asyncio.Queue()
        # 已入队但尚未被 _process_tasks 消费的 truck_id 集合（去重，防止同一 truck 堆栈）
        self._enqueued_truck_ids: set = set()
        # 添加stop_event用于控制事件循环运行
        self.stop_event = asyncio.Event()
        # 处理任务的主任务
        self._process_task = None

    def _all_running_trucks(self):
        """返回所有处于 running 状态的 truck 实例 (跳过 None)"""
        trucks = []
        for t in (self.private_truck, self.ongoing_truck,
                  self.special_attention_truck, self.scheduled_proactive_truck,
                  self.reflection_char_truck, self.reflection_time_truck,
                  self.reflection_efficacy_truck, self.reflection_belonging_truck,
                  self.task_truck):
            if t is None:
                continue
            try:
                if t.state == "running":
                    trucks.append(t)
            except Exception:
                continue
        return trucks

    @async_handle_errors()
    async def _process_tasks(self):
        """处理任务队列中的任务

        设计：
        1. 从队列取出 truck_id
        2. 暂停其他 truck (除触发者外), 防止并发提醒
           【关键修复】只在 truck 实例存在时（_get_truck 返回非 None）才 pause 其它 truck。
           未知 truck_id / truck 未启动时直接 task_done 跳过，避免永久 pause 所有兄弟 truck。
        3. 校验 truck (调 truck.get_truck_messages) -> 拿到决策
        4. truck.get_truck_messages 内部已 await proactive_session 跑完
           根据 decision_result 决定返回 None 还是 dict
        5. 如果返回 dict -> station 调 PFCA; 否则跳过
        6. 恢复所有被暂停的 truck + 触发 truck
           【关键修复】用 try/finally 保证异常路径下也能 resume，不会让兄弟 truck 永久 paused。
        """
        while self.running:
            # 从队列中获取任务
            truck_id = await self.task_queue.get()
            # 取出即从去重集合移除（即便后面走异常分支也能保证下次能再入队）
            self._enqueued_truck_ids.discard(truck_id)
            logger.info(f"开始处理来自{truck_id}的任务")

            # 根据truck_id获取对应的truck实例
            truck = self._get_truck(truck_id)

            # 未知 / 未实例化的 truck_id：直接跳过，绝不能 pause 其它 truck
            if truck is None:
                logger.warning(
                    f"station._process_tasks: 收到未知或未实例化的 truck_id={truck_id!r}, 跳过"
                )
                self.task_queue.task_done()
                continue

            # === step 2: 暂停其他 truck (仅在 truck 实例存在时执行) ===
            other_paused: list = []
            other_trucks = [t for t in self._all_running_trucks() if t is not truck]
            for t in other_trucks:
                t.pause()
                other_paused.append(t)
            if other_paused:
                logger.info(
                    f"处理{truck_id}任务时已暂停其他 {len(other_paused)} 个 truck: "
                    f"{[t.truck_id for t in other_paused]}"
                )

            # === 出队即生成本轮任务链 ID 并设置 contextvar ===
            # 链内 proactive 决策阶段（get_truck_messages 内部阻塞等待 session 跑完）
            # 与 PFCA / conversation / social_chat 阶段的上下文写入全部归属同一链。
            # ProactiveCore 主循环跑在独立任务上，contextvar 不会自动传播，
            # 由 truck 把 chain_id 塞进 task_draft 显式携带（见 ongoing/scheduled truck）。
            chain_id = generate_chain_id()
            chain_token = set_chain_context(chain_id)
            logger.debug(f"本轮任务链 ID: {chain_id}")

            focus_state = None
            try:
                # 请求truck生成完整消息
                truck_messages = await truck.get_truck_messages()

                # 如果有消息返回，将其发布给下级att模块
                if truck_messages:
                    # 处理可能没有formatted_message的情况
                    formatted_message = truck_messages.get('formatted_message', None)
                    raw_sessions = truck_messages.get('raw_sessions')
                    logger.debug(f"从{truck_id}获取到的原始会话: {raw_sessions}")

                    # 调用pre模块的工厂接口处理消息
                    from brain.pfc.core import async_process_from_att_factory
                    from brain.hc.global_session_state import get_global_session_state

                    # 开始新的事件链（触发原因 = truck 注入的第一轮消息，
                    # 由灵光一现记事本随文档自动记录；链 ID 复用出队时预生成的）
                    focus_state = get_global_session_state()
                    focus_state.start_new_event_chain(formatted_message or "", chain_id=chain_id)

                    # PFCA 工厂接口仅需 (message, session_id, name)
                    # - raw_sessions 在 truck 侧形态:
                    #     * private/group/special_attention/scheduled: dict,
                    #       含 session_id 与 session_name
                    #     * ongoing: list, 取首项作为会话源
                    #     * 无 raw_sessions / proactive 场景: session_id=None, name=None
                    session_id_for_pfca = None
                    name_for_pfca = None
                    if isinstance(raw_sessions, dict):
                        session_id_for_pfca = raw_sessions.get("session_id") or None
                        name_for_pfca = raw_sessions.get("session_name") or None
                    elif isinstance(raw_sessions, list) and raw_sessions and isinstance(raw_sessions[0], dict):
                        session_id_for_pfca = raw_sessions[0].get("session_id") or None
                        name_for_pfca = raw_sessions[0].get("session_name") or None

                    result = await async_process_from_att_factory(
                        formatted_message,
                        session_id_for_pfca,
                        name_for_pfca,
                    )

                    # 结束事件链
                    focus_state.end_event_chain()

                    logger.debug(f"Pre模块处理结果: {result}")

                    if result.get('success', False):
                        logger.info("消息处理成功")
                else:
                    # truck 校验后认为不需要调 PFCA
                    # （如 OngoingTruck: proactive 决策 skip / 没决策 / 异常）
                    logger.info(
                        f"{truck_id} 校验/决策后无需 PFCA, 跳过"
                    )

                # === 链收尾：强制归档（B 方案）===
                # skip 分支也可能写入了 proactive 上下文，统一在链尾归档清空，
                # 保证 active 文件不残留跨链内容（前端进行中视图因此准确）。
                _force_archive_chain_contexts()
            except Exception as e:
                # 硬约定#4：运行期业务错误默认 fatal（os._exit(1)）。
                # 任何未预期异常（含非 AbyssCoreError）都不能让 _process_tasks
                # 后台任务静默死亡——否则 station 处理循环永久停摆，truck 与
                # PFCA 全部失声且看似"正常"（致命静默，见本轮修复）。
                logger.critical(
                    f"station._process_tasks 处理 {truck_id} 时发生致命错误，强制退出进程: "
                    f"{e}\n{traceback.format_exc()}"
                )
                for h in getattr(logger, 'handlers', []):
                    try:
                        h.flush()
                    except Exception:
                        pass
                os._exit(1)
            finally:
                try:
                    # CancelledError 不经过上面的 Exception 分支，仍必须关链撤权。
                    if focus_state is not None and focus_state.is_event_chain_active():
                        focus_state.end_event_chain()
                finally:
                    reset_chain_context(chain_token)
                    self.task_queue.task_done()
                    # 无论正常结束、取消或归档失败，都恢复被暂停的 truck。
                    if truck.state == "paused":
                        truck.start()
                        logger.info(f"已恢复{truck_id}运行")
                    elif truck.state == "running":
                        logger.debug(f"{truck_id}未暂停，无需恢复")
                    for t in other_paused:
                        if t is truck:
                            continue
                        if t.state == "paused":
                            t.start()
                            logger.info(f"已恢复{t.truck_id}运行（处理{truck_id}期间被暂停）")

    async def station_input(self, message: Dict[str, any]):
        """处理从truck接收的消息 - truck只提醒station需要处理，不发送完整消息

        Args:
            message: 包含truck_id的消息

        关键修复（#1）：
        - 入队前去重：如果同一 truck_id 已经在队列里待处理，直接丢弃新提醒。
          防止 truck 自身未及时 pause 时把队列灌爆（"突发卡顿"的主要成因之一）。
        """
        logger.debug(f"收到truck提醒: {message}")
        # 获取truck_id
        truck_id = message.get('truck_id')
        if not truck_id:
            logger.warning("station_input: 收到无 truck_id 的消息, 忽略")
            return

        # 去重：已在队列里的同 truck_id 跳过
        if truck_id in self._enqueued_truck_ids:
            logger.debug(
                f"station_input: {truck_id} 已在队列中待处理, 丢弃重复提醒"
            )
            return

        self._enqueued_truck_ids.add(truck_id)
        # 将truck请求加入任务队列
        await self.task_queue.put(truck_id)
        logger.info(f"已将{truck_id}的请求加入任务队列")

    async def start(self):
        logger.debug("启动station")
        if not self.running:
            self.running = True

            # 初始化truck实例
            if self.private_truck is None:
                self.private_truck = PrivateTruck()
                if is_truck_enabled('private_truck'):
                    self.private_truck.start()
                else:
                    logger.info("private_truck 已禁用，不启动线程")
            if self.ongoing_truck is None:
                from .trucks.ongoing_truck import OngoingTruck
                self.ongoing_truck = OngoingTruck()
                if is_truck_enabled('ongoing_truck'):
                    self.ongoing_truck.start()
                else:
                    logger.info("ongoing_truck 已禁用，不启动线程")
            if self.special_attention_truck is None:
                self.special_attention_truck = SpecialAttentionTruck()
                if is_truck_enabled('special_attention_truck'):
                    self.special_attention_truck.start()
                else:
                    logger.info("special_attention_truck 已禁用，不启动线程")
            if self.scheduled_proactive_truck is None:
                from .trucks.scheduled_proactive_truck import ScheduledProactiveTruck
                self.scheduled_proactive_truck = ScheduledProactiveTruck()
                if is_truck_enabled('scheduled_proactive_truck'):
                    self.scheduled_proactive_truck.start()
                else:
                    logger.info("scheduled_proactive_truck 已禁用，不启动线程")
            if self.reflection_char_truck is None:
                self.reflection_char_truck = ReflectionCharTruck()
                if is_truck_enabled('reflection_char_truck'):
                    self.reflection_char_truck.start()
                else:
                    logger.info("reflection_char_truck 已禁用，不启动线程")
            if self.reflection_time_truck is None:
                self.reflection_time_truck = ReflectionTimeTruck()
                if is_truck_enabled('reflection_time_truck'):
                    self.reflection_time_truck.start()
                else:
                    logger.info("reflection_time_truck 已禁用，不启动线程")
            if self.reflection_efficacy_truck is None:
                self.reflection_efficacy_truck = ReflectionEfficacyTruck()
                if is_truck_enabled('reflection_efficacy_truck'):
                    self.reflection_efficacy_truck.start()
                else:
                    logger.info("reflection_efficacy_truck 已禁用，不启动线程")
            if self.reflection_belonging_truck is None:
                self.reflection_belonging_truck = ReflectionBelongingTruck()
                if is_truck_enabled('reflection_belonging_truck'):
                    self.reflection_belonging_truck.start()
                else:
                    logger.info("reflection_belonging_truck 已禁用，不启动线程")
            if self.task_truck is None:
                from .trucks.task_truck import TaskTruck
                self.task_truck = TaskTruck()
                if is_truck_enabled('task_truck'):
                    self.task_truck.start()
                else:
                    logger.info("task_truck 已禁用，不启动线程")
            
            # 启动任务处理主任务
            self._process_task = asyncio.create_task(self._process_tasks())
            
            # 等待stop_event被触发，保持事件循环运行
            await self.stop_event.wait()

    def stop(self):
        self.running = False
        # 设置stop_event以结束start方法的等待
        try:
            self.stop_event.set()
        except Exception as e:
            # 当事件循环已关闭时，调用set()会抛出异常，这里捕获并忽略
            logger.debug(f"设置stop_event时发生错误: {str(e)}")
        
        # 取消任务处理主任务
        if self._process_task:
            self._process_task.cancel()
        
        # 如果truck存在，停止它们
        if self.private_truck:
            self.private_truck.stop()
        if self.ongoing_truck:
            self.ongoing_truck.stop()
        if self.special_attention_truck:
            self.special_attention_truck.stop()
        if self.scheduled_proactive_truck:
            self.scheduled_proactive_truck.stop()
        if self.reflection_char_truck:
            self.reflection_char_truck.stop()
        if self.reflection_time_truck:
            self.reflection_time_truck.stop()
        if self.reflection_efficacy_truck:
            self.reflection_efficacy_truck.stop()
        if self.reflection_belonging_truck:
            self.reflection_belonging_truck.stop()
        if self.task_truck:
            self.task_truck.stop()

    def get_queue_size(self) -> int:
        """获取当前任务队列大小

        Returns:
            int: 任务队列中的任务数量
        """
        return self.task_queue.qsize()

    def _get_truck(self, truck_id: str):
        """根据 truck_id 获取已实例化的 truck 对象 (None 表示未启用或不存在)"""
        mapping = {
            'private_truck': self.private_truck,
            'ongoing_truck': self.ongoing_truck,
            'special_attention_truck': self.special_attention_truck,
            'scheduled_proactive_truck': self.scheduled_proactive_truck,
            'reflection_char_truck': self.reflection_char_truck,
            'reflection_time_truck': self.reflection_time_truck,
            'reflection_efficacy_truck': self.reflection_efficacy_truck,
            'reflection_belonging_truck': self.reflection_belonging_truck,
            'task_truck': self.task_truck,
        }
        return mapping.get(truck_id)

    def set_truck_enabled(self, truck_id: str, enabled: bool) -> bool:
        """运行时切换 truck 状态 (在 station 已启动后调用)

        Args:
            truck_id: 'private_truck' / 'ongoing_truck' /
                      'special_attention_truck'
            enabled: True 启动, False 停止

        Returns:
            True 表示操作成功或状态已经匹配, False 表示 truck 不存在
        """
        truck = self._get_truck(truck_id)
        if truck is None:
            logger.warning(f"set_truck_enabled: 未知或未实例化的 truck_id={truck_id}")
            return False
        if enabled:
            if truck.state == "stopped":
                truck.start()
                logger.info(f"{truck_id} 已运行时启动")
            else:
                # running / paused, 已在运行, 不需要做什么
                logger.debug(f"{truck_id} 已经在运行 (state={truck.state})")
        else:
            if truck.state != "stopped":
                truck.stop()
                logger.info(f"{truck_id} 已运行时停止")
            else:
                logger.debug(f"{truck_id} 已经停止")
        return True

    def get_truck_state(self, truck_id: str) -> str:
        """获取 truck 当前实际运行状态 ('stopped' / 'running' / 'paused')"""
        truck = self._get_truck(truck_id)
        if truck is None:
            return "unknown"
        return truck.state

def _get_station() -> "AttentionStation | None":
    """获取当前运行的 station 实例（未启动返回 None）。

    station 单例由 get_attention_core() 持有；延迟 import 避免与 att_core 循环。
    """
    from brain.att.att_core import get_attention_core
    return get_attention_core().station


# 模块级别的station_input函数
async def station_input(message: Dict[str, any]):
    """模块级别的消息输入函数，可以直接从其他模块导入使用
    """
    station = _get_station()
    if station is None:
        logger.warning("station 未启动，station_input 消息被丢弃")
        return
    return await station.station_input(message)

# 模块级别的获取任务队列大小函数
async def get_queue_size():
    """获取当前任务队列的大小

    Returns:
        int: 任务队列中的任务数量
    """
    station = _get_station()
    if station is None:
        return 0
    return station.get_queue_size()

# 模块级别的运行时切换 truck
def set_truck_enabled(truck_id: str, enabled: bool) -> bool:
    """模块级别: 在 station 已启动后, 运行时切换 truck 状态

    如果 station 尚未启动, 调用无效 (返回 False), truck 会按 config 默认值在
    station 启动时被处理.
    """
    station = _get_station()
    if station is None:
        return False
    return station.set_truck_enabled(truck_id, enabled)

def get_truck_state(truck_id: str) -> str:
    """模块级别: 获取 truck 实际运行状态"""
    station = _get_station()
    if station is None:
        return "unknown"
    return station.get_truck_state(truck_id)
