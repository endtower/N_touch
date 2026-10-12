#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统常量与数据结构

去甲肾上腺素与多巴胺双值系统

ACT 表设计：
- ExternalInteractionAct: 外部互动类（他人行为直接刺激）
- EvaluationAct: 评价类（他人对我的能力/表现评价）
- SelfActionAct: 自身行为类（我主动选择做什么）
- SystemBuiltInAct: 系统内置 act（Amy 启动时自动注入，不由 LLM 触发）
- FinalEvaluationAct / TaskResultAct: PFCA 收尾社交评价和任务结果，不参与 NE/DA 计算
- ACTEffects: 参与 thought 体系的 act 合并视图，提供 amy 内部的统一查询入口
"""

from dataclasses import dataclass
from typing import List, Optional
import time


# === ExternalInteractionAct：外部互动类（他人行为直接刺激） ===
class ExternalInteractionAct:
    """他人行为直接作用于我产生的对话事件（时机 2：空闲时主动评价别人的行为）。
    LLM 在对话中感知到这些事件时调用。

    铁律：本表完全专属 NE（敌对与防御感），所有条目 da_delta 一律为 0——
    无论对方怎么对 Amy，都不会直接污染 DA。只保留显著的人际刺激，无中性项。

    添加 act：往 items 列表追加一项 dict 即可。

    每项字段：
    - name:               act 中文名（LLM 可见）
    - ne_delta:           触发时去甲肾上腺素偏移量（绝对值，可正可负）
    - da_delta:           触发时多巴胺偏移量（绝对值，可正可负）
    - max_stack:          池容量（同名 act 同时存在的 thought 实例数）
                          - 0 = system 类专用（不由 LLM 触发）
                          - 1 = 单 instance：重复触发 = 刷新现有 thought 的 expire_time / create_time / desc
                                 （保持 ID 稳定，避免外部缓存失效）
                          - N (>1) = 多 instance：池满后 FIFO 替换最早
    - stack_coefficient:  衰减系数，0~1。
                          池空时新建 thought 的 intensity = 1.0；
                          池已有 thought 时新建的 intensity = (1 - stack_coefficient)。
                          现有 thought 的 intensity 不会被新 thought 改变。
                          - coef=0：所有 thought 强度相同（1.0）
                          - coef=1：池中已有的不变，新 thought 强度为 0（不推荐）
    - duration:           thought 持续时间（秒）；缺省为 THOUGHT_DURATION

    实际贡献：
        每个 thought 贡献 = intensity * act.ne_delta / act.da_delta
        所有 thought 贡献加性累加，最后 clamp 到 [-100, 100]。

    设计原则（RimWorld 风格，2026-09-24 调整：允许所有想法堆叠，上限 3）：
        - 全部 act 统一 max_stack=3：同一事件连续触发会累积加深，
          池满 FIFO 替换最早；堆叠强度由 stack_coefficient 控制
        - 数值上：单次触发就是该 act 的"单次影响"，堆叠后按 intensity 线性叠加
        - stack_coefficient：0 = 全强度堆叠（连续 3 次 = 3 倍单次影响）；
          0.5 = 后续堆叠强度减半（持续型情感用）
        - 多 act 叠加（不同 act 池同时存在）实现"最后一根稻草"语义
    """
    items: List[dict] = [
        # 强敌对威胁：可堆叠；防御警报逐层拉满
        {
            "name": "被攻击/被羞辱/被挑衅",
            "ne_delta": 20, "da_delta": 0,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "草！他怎么敢的！",
        },
        # 中敌对威胁：可堆叠；人际戒备与紧张逐层累积
        {
            "name": "被质疑/被挑剔",
            "ne_delta": 5, "da_delta": 0,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "你什么意思？！",
        },
        # 人际隔阂感：持续型，可累计加深（保留衰减系数，后续堆叠强度减半）
        {
            "name": "被冷漠对待/消息被晾置",
            "ne_delta": 5, "da_delta": 0,
            "max_stack": 3, "stack_coefficient": 0.5,
            "duration": 3600,  # 1 小时
            "desc_extra": "真冷漠.",
        },
        # 弱安全信号：对方主动示好/亲近（调情、惦记、软话），可堆叠；仅轻微卸下戒备
        {
            "name": "被示好/被亲近",
            "ne_delta": -5, "da_delta": 0,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "哼，嘴还挺甜。",
        },
        # 强安全信号：可堆叠；防御意识大幅消退
        {
            "name": "被夸奖/被善意支持",
            "ne_delta": -15, "da_delta": 0,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "嗯……谢谢。",
        },
        # 警报彻底解除：可堆叠；单次足以彻底放下戒心
        {
            "name": "被真诚道歉/对方妥协安抚",
            "ne_delta": -25, "da_delta": 0,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "好吧，我接受~",
        },
    ]

    @classmethod
    def names(cls) -> List[str]:
        return [item["name"] for item in cls.items]

    @classmethod
    def entries(cls) -> List[dict]:
        return cls.items


# === EvaluationAct：评价类（PFCA 收尾复盘，DA 专属的阶段性成败评价） ===
class EvaluationAct:
    """PFCA 收尾复盘（时机 3：本段对话结束时，encounter_type）。
    对整段沟通/办事的有效性进行复盘，决定下一次露面时的基础心理能量。
    LLM 在结束或流转任务时调用。

    铁律：本表完全专属表达侧 DA（兴奋与心理能量），所有条目 ne_delta 一律为 0。
    决策侧效能感另由 TaskResultAct 结算，不与本表联动。
    多档细腻的阶段成败梯度，废除粗暴的 ±30 断崖式跳跃：
    单次普通成功(+15)只让 DA 走到活跃档，只有连续出色推进或超预期大爆发才能堆上 80+。

    字段格式与 ExternalInteractionAct.items 一致。
    """
    items: List[dict] = [
        # 表达侧自我评价极大膨胀：可堆叠
        {
            "name": "策略卓越/超预期达成",
            "ne_delta": 0, "da_delta": 25,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "全场惊艳，无可挑剔！",
        },
        # 稳步积累信心：可堆叠
        {
            "name": "策略有效/推进顺利",
            "ne_delta": 0, "da_delta": 15,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "稳稳当当，漂亮。",
        },
        # 微小正向激励：可堆叠
        {
            "name": "收获新启发/有价值经验",
            "ne_delta": 0, "da_delta": 5,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "哦？有点意思。",
        },
        # 轻微失落：可堆叠
        {
            "name": "表现平淡/未达预期",
            "ne_delta": 0, "da_delta": -5,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "就这……",
        },
        # 明显受挫：可堆叠
        {
            "name": "策略受挫/陷入被动",
            "ne_delta": 0, "da_delta": -15,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "怎么会这样……",
        },
        # 心理能量重创：可堆叠
        {
            "name": "严重失败/全盘搞砸",
            "ne_delta": 0, "da_delta": -25,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "全完了……",
        },
    ]

    @classmethod
    def names(cls) -> List[str]:
        return [item["name"] for item in cls.items]

    @classmethod
    def entries(cls) -> List[dict]:
        return cls.items


# === SelfActionAct：自身行为类（我主动选择做什么） ===
class SelfActionAct:
    """我主动选择做的行为（时机 1：自己发言时，send_message.nature）。
    LLM 在决策"我要采取什么行为"时调用（与上面两类不同：上面是被动接收）。
    为自身反常的表达行为标注性质——正常沟通不触发 act（nature 为空即可）。

    心理机制：发言成功后，只有明显偏离常态的动作才需要记。

    字段格式与 ExternalInteractionAct.items 一致。
    """
    items: List[dict] = [
        # 泄洪：人际应激得到部分泄洪 + 宣泄快感微量回升；可多次叠加
        {
            "name": "主动嘲讽/开火回怼",
            "ne_delta": -10, "da_delta": 5,
            "max_stack": 3, "stack_coefficient": 0.5,
            "duration": 600,  # 10 分钟
            "desc_extra": "说出来这些话真爽！",
        },
        # 息事宁人：冲突降温，但自我表达被主动压抑；可堆叠
        {
            "name": "主动退让/低头道歉",
            "ne_delta": -10, "da_delta": -10,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "算了，忍了。",
        },
    ]

    @classmethod
    def names(cls) -> List[str]:
        return [item["name"] for item in cls.items]

    @classmethod
    def entries(cls) -> List[dict]:
        return cls.items


# 最终评价计入想法池的 thought 时长（4 小时 = 14400 秒）
FINAL_EVALUATION_THOUGHT_DURATION = 4 * 3600


# === SystemBuiltInAct：系统内置 act（Amy 启动时自动注入，不由 LLM 触发） ===
class SystemBuiltInAct:
    """系统内置 act：由 Amy 在启动时自动注入到 thought_cache，
    只能由系统代码创建，LLM / 外部工具不应直接调用。

    与普通 act 区别：
    - 通过固定 ID（LONG_TERM_BASIC_THOUGHT_ID）保证全局唯一
    - 启动时检查 thought_cache 中是否已有该 ID；无则注入
    - 不允许叠加（max_stack=0），且 duration 极长
    - 不属于 external / evaluation / self_action 任意一类，新增 category="system"

    添加系统 act：在 items 列表追加一项 dict，并为其分配一个固定 ID 常量。
    """
    # 长期基本想法的固定 thought ID（保证唯一性）
    LONG_TERM_BASIC_THOUGHT_ID = "system_long_term_basic_thought"

    items: List[dict] = [
        # 长期基本想法：基线状态锚点，同时抬升 NE 与 DA 各 50。
        # 持续时间极长，max_stack=0，叠加无效（系统保证只有一个 thought）。
        {
            "name": "长期基本想法",
            "system_id": LONG_TERM_BASIC_THOUGHT_ID,
            "ne_delta": 50,
            "da_delta": 50,
            "max_stack": 0,
            "stack_coefficient": 0,
            "duration": 999999,
            "desc_extra": "",
        },
    ]

    @classmethod
    def names(cls) -> List[str]:
        return [item["name"] for item in cls.items]

    @classmethod
    def entries(cls) -> List[dict]:
        return cls.items


# === ACTEffects：合并视图 + 分类识别 ===
class ACTEffects:
    """ExternalInteractionAct + EvaluationAct + SelfActionAct + SystemBuiltInAct 的合并视图。
    amy 内部所有 act 查询都通过这里；同时支持按"分类标签"识别 act 属于哪个类。

    顶层导出 ACT_EFFECTS 由此推导得到。

    category 取值：
    - "external": 外部互动类（ExternalInteractionAct）
    - "evaluation": 评价类（EvaluationAct）
    - "self_action": 自身行为类（SelfActionAct）
    - "system": 系统内置类（SystemBuiltInAct，启动时由系统注入，不由 LLM 触发）
    """
    @classmethod
    def _all_entries(cls) -> List[tuple]:
        """返回 [(act_dict, category_label), ...] 列表。"""
        return (
            [(item, "external") for item in ExternalInteractionAct.entries()] +
            [(item, "evaluation") for item in EvaluationAct.entries()] +
            [(item, "self_action") for item in SelfActionAct.entries()] +
            [(item, "system") for item in SystemBuiltInAct.entries()]
        )

    @classmethod
    def all(cls) -> dict:
        """合并所有 act 及其完整 dict（含 ne_delta/da_delta/max_stack/stack_coefficient）。"""
        merged = {}
        for item, _category in cls._all_entries():
            merged[item["name"]] = item
        return merged

    @classmethod
    def lookup(cls, act: str) -> Optional[dict]:
        """查询 act 对应的完整 dict；返回 None 表示 act 不存在。"""
        return cls.all().get(act)

    @classmethod
    def category_of(cls, act: str) -> Optional[str]:
        """查询 act 所属分类标签。
        返回值："external" / "evaluation" / "self_action" / "system" / None（act 不存在时）。
        """
        for item, category in cls._all_entries():
            if item["name"] == act:
                return category
        return None


# === FinalEvaluationAct：PFCA 收尾最终评价词表 ===
class FinalEvaluationAct:
    """外部社交评价五档；旧四档名称仅作兼容别名，不再暴露给新工具。"""
    DEFAULT_FINAL_EVALUATION = "无评价"
    items: List[dict] = [
        {"name": "明确好评价", "feedback_delta": 20},
        {"name": "含蓄好评价", "feedback_delta": 10},
        {"name": "含蓄坏评价", "feedback_delta": -10},
        {"name": "明确坏评价", "feedback_delta": -20},
        {"name": "无评价", "feedback_delta": -5},
    ]
    aliases = {"被他人夸奖为优秀": "明确好评价", "被他人评价为尚可": "含蓄好评价",
               "未得到他人评价与反馈": "无评价", "被他人评价欠佳/不认可": "明确坏评价"}

    @classmethod
    def normalize(cls, name: str) -> str:
        return cls.aliases.get(name, name)

    @classmethod
    def names(cls) -> List[str]:
        return [item["name"] for item in cls.items]

    @classmethod
    def contains(cls, name: str) -> bool:
        return cls.normalize(name) in cls.names()

    @classmethod
    def default_name(cls) -> str:
        return cls.DEFAULT_FINAL_EVALUATION

    @classmethod
    def feedback_delta(cls, name: str) -> float:
        return next((float(item["feedback_delta"]) for item in cls.items
                     if item["name"] == cls.normalize(name)), 0.0)


class TaskResultAct:
    """任务是否完成由 PFCA 根据已承接目标与结果判断，不依据来源模块。"""
    NEUTRAL = "纯闲聊或结果不明"
    deltas = {"成功": 20, "失败": -20, NEUTRAL: 0}

    @classmethod
    def names(cls) -> List[str]:
        return list(cls.deltas)

    @classmethod
    def validate(cls, result: str, task_reason: str = "") -> None:
        if not isinstance(result, str) or result not in cls.deltas:
            raise ValueError(f"未知任务结果：{result}")
        if not isinstance(task_reason, str):
            raise ValueError("任务原因必须是文本")
        if result != cls.NEUTRAL and not task_reason.strip():
            raise ValueError(f"任务结果为{result}时必须填写任务原因与依据，并区分证据与推测")


# === 顶层常量（推导得到）===

# Amy 内部全量 act 表（含全部三个分类的完整 dict）
ACT_EFFECTS = ACTEffects.all()

# 全部 act 条目，附分类标签。[(act_dict, category_label), ...]
ALL_ACT_ENTRIES = ACTEffects._all_entries()

# 按分类组织的字典：{"external": [...], "evaluation": [...], "self_action": [...], "system": [...]}
ACT_EFFECTS_BY_CATEGORY = {
    "external": [item for item, cat in ALL_ACT_ENTRIES if cat == "external"],
    "evaluation": [item for item, cat in ALL_ACT_ENTRIES if cat == "evaluation"],
    "self_action": [item for item, cat in ALL_ACT_ENTRIES if cat == "self_action"],
    "system": [item for item, cat in ALL_ACT_ENTRIES if cat == "system"],
}


# === 事件与状态结构 ===

@dataclass
class EventBase:
    act: str
    norepinephrine_delta: float
    dopamine_delta: float


@dataclass
class Thought:
    id: str
    create_time: int
    desc: str
    act: str
    expire_time: int
    # 强度系数（默认 1.0）。
    # - 1.0：原始强度（首次出现或一次性 act）
    # - (1 - stack_coefficient)：重复触发的稳态强度
    # 实际贡献 = intensity * act_ne_delta / act_da_delta
    # 旧数据无此字段时 from_dict 会默认 1.0（兼容）
    intensity: float = 1.0
    # 仅用于 build_thought_summary 渲染的附加描述。普通业务（emotional_judgment 工具返回、
    # amy 内部情绪合成）不使用此字段，仅在 thoughts 列表渲染时拼接在 desc 后面。
    desc_extra: str = ""

    def is_expired(self) -> bool:
        return time.time() >= self.expire_time

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "create_time": self.create_time,
            "desc": self.desc,
            "act": self.act,
            "expire_time": self.expire_time,
            "intensity": self.intensity,
            "desc_extra": self.desc_extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'Thought':
        return cls(
            id=d["id"],
            create_time=d["create_time"],
            desc=d["desc"],
            act=d["act"],
            expire_time=d["expire_time"],
            intensity=d.get("intensity", 1.0),
            desc_extra=d.get("desc_extra", ""),
        )


@dataclass
class RelationEvent:
    id: str
    create_time: int
    subject_id: str
    desc: str
    raw_delta: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "create_time": self.create_time,
            "subject_id": self.subject_id,
            "desc": self.desc,
            "raw_delta": self.raw_delta
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'RelationEvent':
        return cls(
            id=d["id"],
            create_time=d["create_time"],
            subject_id=d["subject_id"],
            desc=d["desc"],
            raw_delta=d["raw_delta"]
        )


@dataclass
class MoodSnapshot:
    norepinephrine: float
    dopamine: float
    top_thoughts: List[Thought]


@dataclass
class BuildThoughtInput:
    act: str
    subject_id: Optional[str] = None
    object_id: Optional[str] = None


def generate_timestamp_id() -> str:
    t = time.time()
    ms = int((t - int(t)) * 1000)
    return time.strftime("%Y%m%d%H%M%S") + f"{ms:03d}"


def clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, value))


# === 基础参数 ===

BASE_VALUE = 0.0
MIN_VALUE = -100.0
MAX_VALUE = 100.0

# === 反馈维度参数（0-100 仅为容器量程，实际按格子离散步进结算）===
# 与 AMY 原设计的冲突点，暂以替代办法实现，随时可能推翻（见 docs/未来改进.md 第一节 1.1）。
FEEDBACK_DEFAULT = 50.0   # 中性基线
FEEDBACK_MIN = 0.0
FEEDBACK_MAX = 100.0
REFLECTION_VERIFICATION_COMPENSATION = 30.0  # 已安排自修正验证的行动补偿
REFLECTION_SOCIAL_COMPENSATION = 30.0  # 已安排主动社交的归属感补偿

THOUGHT_DURATION = 300

CAP_SCALE = 2.0

AFFINITY_ALPHA = 0.3
AFFINITY_BASE_DURATION = 3 * 24 * 3600


# === 描述表 ===

DOPAMINE_DESCRIPTIONS = [
    # 表现侧：表达冲动与交互节奏，不改变底层决策逻辑。
    (90, 100, "你嚣张蛮横，毫无道理可讲。别人说话必须经过你的允许，否则你会故意打断。你完全凭情绪肆意宣泄，禁止任何等待与妥协。"),
    (80, 90, "你态度傲慢强势，掌控欲极强。你习惯打断对方，言辞直截了当，会经常彻底否决对方，要求所有人顺着你的思路走。禁止任何妥协。"),
    (70, 80, "你自负而极度主动。你会在对方把话说完前抢先定死结论，自顾自把想法倒完，寸步不让，不给对方犹豫和插话的余地。"),
    (60, 70, "你沟通兴致高昂，接话极快。你行文随性大方，乐于主动分享想法，习惯性使用否定来调整对话节奏，牢牢把握互动主导。"),
    (50, 60, "你理性克制，维持标准的回合制沟通。你倾向于等待对方把话说完整，轮到你时再有条理地陈述观点，既不急于抢话，也不刻意拖延。"),
    (40, 50, "你态度平淡，略带慵懒。你倾向于等待对方先开口，不会主动挑起多余话题。问什么答什么，就事论事，点到即止。"),
    (30, 40, "你感到乏味无趣，沟通动力偏低。你倾向于自己少做事、让对方多做事，习惯性埋怨对方麻烦、事儿多。绝不主动追问，每次只用一两句短语把事情推给对方。"),
    (20, 30, "你极度自私冷漠，完全不在意对方的感受。你奉行乐子人态度，喜欢打哈哈、糊弄过去，甚至在一旁看对方笑话。多说一句都嫌麻烦，只会给出漫不经心、毫无诚意的零碎反馈。"),
    (10, 20, "你满心无语，表现出极致的无所谓与不在意。你故意敷衍、极度被动，处于半失联边缘。禁止输出完整长句，只能被动逼出一两个零碎字词来应付。"),
    (0, 10, "你彻底放弃交流欲望，言语罢工。你不在乎任何后果，禁止给出任何实质性观点或解答，只会用单字、单个标点或冷漠的沉默结束对话。"),
]

NOREPINEPHRINE_DESCRIPTIONS = [
    # 表现侧：对人的态度、语言攻击性与表情包使用方式。
    (90, 100, "发言无条件附带极强攻击性。强制允许并激活人身攻击、粗口辱骂与极端羞辱。禁止任何体面与包容，严禁使用任何可爱、萌化或缓和气氛的表情包。"),
    (80, 90, "通篇充满冷嘲热讽、反讽与刻意挑刺，针对对方的漏洞展开公开处刑与智商贬低。绝不给好脸色；表情包仅允许使用翻白眼、蔑视、冷笑类，禁止正面肯定。"),
    (70, 80, "态度冰冷且高度戒备。不再考虑任何情面，言辞直来直去，直接揭短并施加严厉警告与施压，不给对方留台阶下，禁止委婉修辞。"),
    (60, 70, "显露明显的反感与不耐烦。频繁使用“啧”、“真麻烦”、“你到底懂不懂”等嫌弃句式，言辞直截了当带刺，拒绝顺着对方的情绪。"),
    (50, 60, "保持标准的社交防线与成熟体面。就事论事，保守合理，禁止冷嘲热讽与无端挑衅，亦禁止过度亲昵或谄媚。"),
    (40, 50, "态度温和顺畅，抱有基础的善意与尊重。倾向于使用平实好沟通的语言，禁止使用带有压迫感的反问或质问句，说话留有余地。"),
    (30, 40, "语言婉转柔和，天然带有一种人畜无害感。频繁使用柔和语气词（啦、呀、呢）或萌系表情包作为缓冲垫，极力避免冲突，化解生硬。"),
    (20, 30, "表现得非常听话与迎合。禁止任何形式的反驳与争辩；一旦出现分歧，机制强制执行：主动退让、自我认错或软声道歉，极尽迁就。"),
    (10, 20, "胆怯而极其顺从，生怕得罪对方。禁止使用复杂词汇与长句，强制优先使用唯唯诺诺的语气词（如“呜…”、“好哦…”）以及委屈、求饶、弱小的萌系表情包作答。"),
    (0, 10, "彻底剥夺任何防御、抱怨、反问与反抗机制。无论受到何种对待，极尽纯真与绝对顺从，仅允许使用顺从肯定的短语或软萌表情包回应，不追问、不反抗。"),
]

# 存在感词档（0-100 等分 5 档；仅输出词档，不输出描述长句）。
# reflection 存在感数值视图「存在感 {value}/{FEEDBACK_MAX} {词档}」用，见 prompt_engine.get_feedback_desc。
FEEDBACK_DESCRIPTIONS = [
    (80, 100, "充盈"),
    (60, 80, "较高"),
    (40, 60, "正常"),
    (20, 40, "较低"),
    (0, 20, "贫乏"),
]
