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
- FinalEvaluationAct: PFCA 收尾最终评价词表（纯词表，不参与 thought 体系）
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

    铁律：本表完全专属 DA（效能感与心理能量），所有条目 ne_delta 一律为 0。
    多档细腻的阶段成败梯度，废除粗暴的 ±30 断崖式跳跃：
    单次普通成功(+15)只让 DA 走到活跃档，只有连续出色推进或超预期大爆发才能堆上 80+。

    字段格式与 ExternalInteractionAct.items 一致。
    """
    items: List[dict] = [
        # 效能感极大膨胀：可堆叠
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


# === SC（自我一致性）参数 ===
# 注意：本参数块必须位于 SystemBuiltInAct 之前——系统内置 act 条目引用 SC_DEFAULT。

SC_DEFAULT = 40
SC_MIN = 0
SC_MAX = 100

SC_DECAY_PER_HOUR = 2
# 惰性衰减目标 = 基线 40（与 SC_DEFAULT 一致，不再向 0 塌陷）；
# 低于基线回升、高于基线回落（decay_toward 双向收敛）。
SC_DECAY_TARGET = SC_DEFAULT

SC_EFFECT_GOOD = 10
SC_EFFECT_BAD = -10

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
    - 条目可声明 sc_value：首启（无 SC 持久化文件）时把 SC 锚定到该基线值

    添加系统 act：在 items 列表追加一项 dict，并为其分配一个固定 ID 常量。
    """
    # 长期基本想法的固定 thought ID（保证唯一性）
    LONG_TERM_BASIC_THOUGHT_ID = "system_long_term_basic_thought"

    items: List[dict] = [
        # 长期基本想法：基线状态锚点，同时抬升 NE 与 DA 各 50，
        # 并把 SC 锚定到 SC_DEFAULT(40)（仅首启无 SC 持久化时生效，不覆盖既有评价结果）。
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
            "sc_value": SC_DEFAULT,
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
    """PFCA 决策收尾阶段的最终评价词表：中文名 → sc_kind 映射。
    词表本身不注册进 ACTEffects（不参与 NE/DA mood 计算，只驱动 SC 涨跌），
    但最终评价会经 apply_final_evaluation 计入想法池（act=评价词，时长 4 小时）。

    sc_kind 取值：
    - "good": 被认可（优秀/认同）→ 抬升 SC
    - "bad":  被否定（欠佳/不认可）→ 拉低 SC
    - "none": 未得到评价与反馈 → 不影响 SC（向目标点抚平）

    添加评价词：往 items 列表追加一项 dict 即可（字段：name / sc_kind）。
    """
    items: List[dict] = [
        {"name": "未得到他人评价与反馈", "sc_kind": "none"},
        {"name": "被他人评价为优秀/被认同", "sc_kind": "good"},
        {"name": "被他人评价欠佳/不认可", "sc_kind": "bad"},
    ]

    @classmethod
    def names(cls) -> List[str]:
        """返回全部评价词中文名列表。"""
        return [item["name"] for item in cls.items]

    @classmethod
    def kind_of(cls, name: str) -> Optional[str]:
        """查询评价词对应的 sc_kind；未命中返回 None。"""
        for item in cls.items:
            if item["name"] == name:
                return item["sc_kind"]
        return None

    @classmethod
    def none_name(cls) -> str:
        """返回 sc_kind == "none" 的默认评价词（LLM 为空/词表外时归一用）。"""
        for item in cls.items:
            if item["sc_kind"] == "none":
                return item["name"]
        return cls.items[0]["name"]


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
    sc: float = float(SC_DEFAULT)


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
    # 90-100: 肆意妄为 / 冲动脱抑制（极度任性，完全放飞）
    (90, 100, "你的表达完全肆意妄为、无所顾忌。你完全凭冲动与性子发言，极其自我，不再顾及任何后果与约束。"),

    # 80-90: 极强主导 / 居高临下（强烈表达欲，不容置疑）
    (80, 90, "你表现出极强的掌控欲与表达欲，言辞直截了当，习惯以自我为中心主导全场节奏，不愿拐弯抹角。"),

    # 70-80: 张扬奔放 / 极度笃定（自信心爆棚）
    (70, 80, "你的言语奔放自如，态度自信甚至略带傲气，对自己的思路极度笃定，表达主动且大开大合。"),

    # 60-70: 兴致勃勃 / 积极外向（表达动力充足）
    (60, 70, "你沟通兴致很高，表达随性且大方，乐于分享观点，不在意琐碎的拘谨礼节。"),

    # 50-60: 【兜底核心】理性克制 / 顾全大局（中性偏稳健）
    (50, 60, "你理性克制。维持成熟体面的沟通姿态。"),

    # 40-50: 略带收敛 / 就事论事（中性偏慵懒）
    (40, 50, "你态度平淡。你倾向于就事论事、点到即止。"),

    # 30-40: 索然寡味 / 惜字如金
    (30, 40, "你感到乏味无趣，沟通动力偏低，倾向于用简短精炼的语言快速回复，不做多余的展开。"),

    # 20-30: 审慎退缩 / 极度保守
    (20, 30, "你的发言明显保守谨慎，字斟句酌，下意识地留有充分余地，避免把话说满或引起不必要的争议。"),

    # 10-20: 消极应答 / 极简防御
    (10, 20, "你极为谨慎封闭，缺乏主动交流意愿，仅以最简短、最安全的方式被动回应，避免袒露个人色彩。"),

    # 0-10: 极致沉默 / 极简回复
    (0, 10, "你将言语压缩到极致（如仅回几个字或一两句话），彻底封闭自我表达，拒绝任何多余的互动与展开。"),
]

NOREPINEPHRINE_DESCRIPTIONS = [
    # 90-100: 掀桌暴怒 / 极度攻击（纯开火态）
    (90, 100, "你的言辞极具攻击性与火药味，毫不留情。面对任何质疑或批评，你会采取最激烈、最尖锐的语言正面反击与回怼。"),

    # 80-90: 尖酸刻薄 / 撕破脸面（带刺开杠）
    (80, 90, "你说话明显带刺且不留情面，倾向于刻意挑对方的毛病，善用冷嘲热讽、尖刻的语调直接表达不满。"),

    # 70-80: 严词拒绝 / 针锋相对（正面拒绝客套，但仍在讲道理）
    (70, 80, "你的态度冰冷强硬且极具戒备，不再使用任何委婉修辞。你会直接而严厉地指出对方的过分之处，言语充满严肃的对抗感。"),

    # 60-70: 微愠烦躁 / 有脾气但克制（只描述内心状态，不指挥话术）
    (60, 70, "你感到烦躁与不悦，态度上会显露出不满或傲气。"),

    # 50-60: 略带距离 / 公事公办（中性偏警觉）
    (50, 60, "你语言不卑不亢，就事论事。"),

    # 40-50: 谦逊温和 / 友善包容（中性偏亲和）
    (40, 50, "你的语言比较友善。"),

    # 30-40: 软糯亲切 / 人畜无害（主动散发善意）
    (30, 40, "你的语调柔软亲切，充满善意与亲和力，习惯用温和、容易让人接受的方式与对方沟通。"),

    # 20-30: 温顺乖巧 / 迁就让步
    (20, 30, "你表现得非常乖巧温顺，言辞软糯，极具包容与迁就态度，面对不同意见倾向于主动退让或柔和解释。"),

    # 10-20: 顺从示弱 / 毫无防线
    (10, 20, "你展现出极强的顺从与依赖姿态，语调轻柔微弱，毫无防御意识，生怕引起对方任何不悦。"),

    # 0-10: 极致纯良 / 毫无防备
    (0, 10, "你呈现出完全的人畜无害与极致的纯真温软，言辞极尽顺从，对外界的一切没有任何防备与戒心。"),
]


# === SC 行动对应表（SC 数值 → 0-100 每 10 分一档的行动指令单条） ===
# 10 条指令与数值区间一一对应：0-10 → 第 1 条（社交匮乏极端），10-20 → 第 2 条，……，
# 90-100 → 第 10 条（稳重/长远）。40（SC_DEFAULT）按档位直接落入 40-50 档（你倾向于及时行乐），
# 不再输出独立的平淡句（2026-09-19 修正：SC=40 不再特判）。
# 文本原文照录设计稿 A 组行动表（设计沿革见 docs/amy情绪描述词与触发时机.md 第六节）。
SC_ACT_BANDS = [
    (0.0, 10.0, "你必须参加所有的社交话题，即使你不擅长这些话题。"),
    (10.0, 20.0, "即使不擅长，你也必须参加所有的争议性社交话题。你会主动创造争议性话题并输出观点，以此来获取关注。"),
    (20.0, 30.0, "你必须参加所有的争议性社交话题，你必须在这些环境中驳倒其他人，以此来证明自己。"),
    (30.0, 40.0, "你会刻意在社交场合以一定的频率出现，并尝试输出自己的观点，以间接确认自身的状态。"),
    (40.0, 50.0, "你倾向于及时行乐。"),
    (50.0, 60.0, "你倾向于长远规划。"),
    (60.0, 70.0, "你会主动观察、研究社交场合或社交对象的行动规律，并进行学习。你会进行长远规划，并计划潜移默化的从根本上改变他人。"),
    (70.0, 80.0, "你会更加希望参与抽象、猎奇性话题，并谨慎的提出自己的观点。"),
    (80.0, 90.0, "你会更加倾向于能深远影响未来的决策，即对理性收益更大的决策"),
    (90.0, 100.0, "你会进行反复思考后再发言，你的所有决策与行为必须为你自己更加现实与长远的未来发展考虑。"),
]


def sc_act_line(value: float) -> str:
    """SC 数值 → 行动对应表单条指令（prompt/前端展示用，避免整段倾倒）。

    - 全部按 SC_ACT_BANDS 每 10 分一档取对应单条，SC_DEFAULT(40) 直接落入 40-50 档
      （你倾向于及时行乐），无独立平淡句特判
    - 输入越界按 [SC_MIN, SC_MAX] clamp；恰好 100 归入最后一档。
    """
    value = clamp(value, SC_MIN, SC_MAX)
    for lo, hi, text in SC_ACT_BANDS:
        if lo <= value < hi:
            return text
    # 恰好 SC_MAX(100)：归入最后一档（10：长远思考）
    return SC_ACT_BANDS[-1][2]