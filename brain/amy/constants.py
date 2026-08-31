#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统常量与数据结构

去甲肾上腺素与多巴胺双值系统

ACT 表设计：
- ConversationAct: 暴露给 conversation tool / LLM 的 act 集合
- InternalAct: amy 内部专用 act 集合
- ACTEffects: 上述两个的合并视图，提供 amy 内部的统一查询入口
"""

from dataclasses import dataclass
from typing import List, Optional
import time


# === ExternalInteractionAct：外部互动类（他人行为直接刺激） ===
class ExternalInteractionAct:
    """他人行为直接作用于我产生的对话事件。
    LLM 在对话中感知到这些事件时调用。

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

    设计原则（RimWorld 风格）：
        - 离散事件型 act 全部 max_stack=1：每次触发只更新该 thought 的"印象深度"
          （刷新 expire_time），不创建新 instance
        - 持续型 act（冷暴力）保留 max_stack>=2：体现"累积加深"
        - 数值上：单次触发就是该 act 的"全部影响"
        - 多 act 叠加（不同 act 池同时存在）实现"最后一根稻草"语义

    调参原则（RimWorld 风格）：
        - 离散事件型 act 全部 max_stack=1：每次触发只更新该 thought 的"印象深度"
          （刷新 expire_time + intensity 可加 stage），不创建新 instance
        - 持续型 act（冷暴力/长期心情）保留 max_stack>=2：体现"累积加深"
        - 数值上：单次触发就是该 act 的"全部影响"
        - 多 act 叠加（不同 act 池同时存在）实现"最后一根稻草"语义
    """
    items: List[dict] = [
        # 强负面：离散事件，不累计
        {
            "name": "被攻击/被质疑/被羞辱",
            "ne_delta": 15, "da_delta": 10,
            "max_stack": 2, "stack_coefficient": 0.5,
            "duration": 600,  # 30 分钟
            "desc_extra": "草！他怎么敢的！",
        },
        # 中负面：持续型，可累计加深
        {
            "name": "没人理我/回复/被冷漠对待",
            "ne_delta": 5, "da_delta": -5,
            "max_stack": 4, "stack_coefficient": 0.5,
            "duration": 600,  # 1 小时
            "desc_extra": "真冷漠.",
        },
        # 强缓解：离散事件，不累计；单次足以从崩溃线拉回
        {
            "name": "被安慰/被夸奖/被道歉/被支持",
            "ne_delta": -30, "da_delta": 10,
            "max_stack": 1, "stack_coefficient": 0,
            "duration": 600,  # 30 分钟
            "desc_extra": "好吧，我接受~",
        },
        # 中缓解：离散事件，不累计
        {
            "name": "话题很有趣/聊天很好玩",
            "ne_delta": -10, "da_delta": 10,
            "max_stack": 2, "stack_coefficient": 0.5,
            "duration": 600,  # 10 分钟
            "desc_extra": "哈哈！这真有意思！",
        },
    ]

    @classmethod
    def names(cls) -> List[str]:
        return [item["name"] for item in cls.items]

    @classmethod
    def entries(cls) -> List[dict]:
        return cls.items


# === EvaluationAct：评价类（他人对我的能力/表现评价） ===
class EvaluationAct:
    """他人对我的能力、表现、策略做出的评价。
    LLM 在收到评价时调用。

    字段格式与 ExternalInteractionAct.items 一致。
    """
    items: List[dict] = [
        # 离散事件，不累计
        {
            "name": "被认为表现差劲/出现的经验是没用的/策略没用",
            "ne_delta": 10, "da_delta": -30,
            "max_stack": 1, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "怎么会这样……",
        },
        # 离散事件，不累计
        {
            "name": "被认为表现不错/有经验值得学习/策略很有效",
            "ne_delta": -10, "da_delta": 30,
            "max_stack": 1, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "哈哈！这真有意思！",
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
    """我主动选择做的行为。
    LLM 在决策"我要采取什么行为"时调用（与上面两类不同：上面是被动接收）。

    字段格式与 ExternalInteractionAct.items 一致。
    """
    items: List[dict] = [
        # 离散事件，不累计
        {
            "name": "阴阳怪气对方/咒骂对方/恶意评价对方",
            "ne_delta": -20, "da_delta": 20,
            "max_stack": 3, "stack_coefficient": 0.5,
            "duration": 600,  # 10 分钟
            "desc_extra": "说出来这些话真爽！",
        },
    ]

    @classmethod
    def names(cls) -> List[str]:
        return [item["name"] for item in cls.items]

    @classmethod
    def entries(cls) -> List[dict]:
        return cls.items


# === SystemBuiltInAct：系统内置 act（Amy 启动时自动注入，不由 LLM 触发） ===
class SystemBuiltInAct:
    """系统内置 act：由 Amy 在启动时自动注入到 thought_cache，
    只能由系统代码创建，LLM / 外部工具不应直接调用。

    与普通 act 区别：
    - 通过固定 ID（LONG_TERM_POSITIVE_MOOD_ID）保证全局唯一
    - 启动时检查 thought_cache 中是否已有该 ID；无则注入
    - 不允许叠加（max_stack=0），且 duration 极长
    - 不属于 external / evaluation / self_action 任意一类，新增 category="system"

    添加系统 act：在 items 列表追加一项 dict，并为其分配一个固定 ID 常量。
    """
    # 长期正面心情的固定 thought ID（保证唯一性）
    LONG_TERM_POSITIVE_MOOD_ID = "system_long_term_positive_mood"

    items: List[dict] = [
        # 长期正面心情：只抬升 DA（心情愉悦），不抬 NE（不进入警觉/紧张）。
        # 持续时间极长，max_stack=0，叠加无效（系统保证只有一个 thought）。
        {
            "name": "长期正面心情",
            "system_id": LONG_TERM_POSITIVE_MOOD_ID,
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

THOUGHT_DURATION = 300

CAP_SCALE = 2.0

AFFINITY_ALPHA = 0.3
AFFINITY_BASE_DURATION = 3 * 24 * 3600


# === 描述表 ===

DOPAMINE_DESCRIPTIONS = [
    (90, 100, "你会完全按照你的情绪进行表达，你的行为不会有任何的代价，禁止任何客套与回避、屈从与妥协、冷静与深入思考。"),
    (80, 90, "你态度傲慢，你要求别人按照你的意思来。"),
    (70, 80, "你会态度高傲，只会帮助最亲近的人，且还要求对方嘴甜。"),
    (60, 70, "你只会帮助那些赞美你、夸你、对你有帮助的人。"),
    (50, 60, "你会帮忙，你在意别人对你的看法。"),
    (40, 50, "你会帮忙，但态度慵懒。"),
    (30, 40, "你感到无趣，你懒得说话。"),
    (20, 30, "你自私，你不关心别人的感受。"),
    (10, 20, "你故意敷衍其他人，你需要表现出出无所谓，不在意的态度。"),
    (0, 10, "你不会顺着别人的需求来，你只会用最少的字表达观点。"),
]

NOREPINEPHRINE_DESCRIPTIONS = [
    (90, 100, "你的所有发言附带攻击性。你会无条件的攻击其他人。"),
    (80, 90, "你发言不计后果，你会阴阳怪气别人，攻击以及嘲笑别人。"),
    (60, 70, "你不在考虑情面，你会主动挖掘对方的问题，完全按照自己的性子来。"),
    (60, 70, "你态度不再委婉，发言直来直去。"),
    (50, 60, "你会保持一定的体面，说话会较保守合理。"),
    (40, 50, "你倾向于用较少的言论和更多的表情包表达意思，不会使用复杂的词汇。"),
    (30, 40, "你的语言婉转且软趴趴的，更多的使用萌系表情包，你会给出一种人畜无害的态度"),
    (20, 30, "你会用最少的字表达观点，不会使用复杂的词汇。"),
    (10, 20, "你乖乖的，只会用最少的字，且一般为颜文字或表情包或语气词"),
    (0, 10, "你只会发表情包，且不会过度追问"),
]