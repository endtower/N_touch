#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统常量与数据结构

去甲肾上腺素与多巴胺双值系统

ACT 表设计：
- ExternalInteractionAct: 外部互动类（他人行为直接刺激）
- EvaluationAct: 评价类（他人对我的能力/表现评价）
- SelfActionAct: 自身行为类（我主动选择做什么）
- PfcaRegulationAct: PFCA 专属情绪调节类（PFCA 主动调节自身情绪）
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

    铁律：本表完全专属 NE（话语的尖锐程度），所有条目 da_delta 一律为 0——
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
        所有 thought 贡献加性累加，最后 clamp 到 [0, 100]。

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
            "name": "被夸奖/被善意支持/被希望",
            "ne_delta": -15, "da_delta": 0,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "嗯……谢谢。",
        },
        # 警报彻底解除：可堆叠；单次足以彻底放下戒心
        {
            "name": "被真诚道歉/对方妥协安抚/被祈求",
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

    铁律：本表完全专属表达侧 DA（当前心情与容错感），所有条目 ne_delta 一律为 0。
    决策侧效能感另由 TaskResultAct 结算，不与本表联动。
    多档细腻的阶段成败梯度，废除粗暴的 ±30 断崖式跳跃：
    单次普通成功(+15)只让 DA 走到活跃档，只有连续出色推进或超预期大爆发才能堆上 80+。

    字段格式与 ExternalInteractionAct.items 一致。
    """
    success_items: List[dict] = [
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
    ]
    failure_items: List[dict] = [
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
        # 心情重创：可堆叠
        {
            "name": "严重失败/全盘搞砸",
            "ne_delta": 0, "da_delta": -25,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "全完了……",
        },
    ]

    items = success_items + failure_items

    @classmethod
    def is_failure(cls, name: str) -> bool:
        """按词表所属分区识别失败，不匹配中文字样或推断分数正负。"""
        return name in [item["name"] for item in cls.failure_items]

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


# === PfcaRegulationAct：PFCA 专属情绪调节类（PFCA 主动调节自身情绪） ===
class PfcaRegulationAct:
    """PFCA 决策层主动调节自身情绪的 act（PFCA 专属，不对 conversation /
    social_chat 的 nature / emotional_judgment 开放）。

    与 SelfActionAct 的区别：SelfActionAct 锚定「我说了什么做了什么」，
    本表只锚定「我对自己情绪做了什么处理」，由 PFCA 的调节工具单独触发。

    字段格式与 ExternalInteractionAct.items 一致。
    """
    items: List[dict] = [
        # 松下来：心情与容错感（DA）回落，语气降温；可堆叠
        {
            "name": "冷静下来",
            "ne_delta": 0, "da_delta": -25,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "",
        },
        # 站到对方立场：话语的尖锐度（NE）消退，戒心降低；可堆叠
        {
            "name": "共情对方",
            "ne_delta": -25, "da_delta": 0,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "",
        },
        # 提起劲：心情与容错感（DA）回升，语气活起来；与「冷静下来」对称
        {
            "name": "提起劲头",
            "ne_delta": 0, "da_delta": 25,
            "max_stack": 3, "stack_coefficient": 0,
            "duration": 600,  # 10 分钟
            "desc_extra": "",
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
    """ExternalInteractionAct + EvaluationAct + SelfActionAct + PfcaRegulationAct
    + SystemBuiltInAct 的合并视图。
    amy 内部所有 act 查询都通过这里；同时支持按"分类标签"识别 act 属于哪个类。

    顶层导出 ACT_EFFECTS 由此推导得到。

    category 取值：
    - "external": 外部互动类（ExternalInteractionAct）
    - "evaluation": 评价类（EvaluationAct）
    - "self_action": 自身行为类（SelfActionAct）
    - "pfca_regulation": PFCA 专属情绪调节类（PfcaRegulationAct）
    - "system": 系统内置类（SystemBuiltInAct，启动时由系统注入，不由 LLM 触发）
    """
    @classmethod
    def _all_entries(cls) -> List[tuple]:
        """返回 [(act_dict, category_label), ...] 列表。"""
        return (
            [(item, "external") for item in ExternalInteractionAct.entries()] +
            [(item, "evaluation") for item in EvaluationAct.entries()] +
            [(item, "self_action") for item in SelfActionAct.entries()] +
            [(item, "pfca_regulation") for item in PfcaRegulationAct.entries()] +
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
        返回值："external" / "evaluation" / "self_action" / "pfca_regulation" /
        "system" / None（act 不存在时）。
        """
        for item, category in cls._all_entries():
            if item["name"] == act:
                return category
        return None


# === FinalEvaluationAct：PFCA 收尾最终评价词表 ===
class FinalEvaluationAct:
    """外部社交评价五档；旧四档名称仅作兼容别名，不再暴露给新工具。"""
    DEFAULT_FINAL_EVALUATION = "无评价"
    success_items: List[dict] = [
        {"name": "明确好评价", "feedback_delta": 20},
        {"name": "含蓄好评价", "feedback_delta": 10},
    ]
    failure_items: List[dict] = [
        {"name": "含蓄坏评价", "feedback_delta": -10},
        {"name": "明确坏评价", "feedback_delta": -20},
    ]
    neutral_items: List[dict] = [
        {"name": "无评价", "feedback_delta": -5},
    ]
    items = success_items + failure_items + neutral_items
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

    @classmethod
    def is_failure(cls, name: str) -> bool:
        return cls.normalize(name) in [item["name"] for item in cls.failure_items]


class TaskResultAct:
    """PFCA 按实际效益结算效能感：只有对方明确认可价值才加，其余按结果扣减。"""
    BENEFIT = "对方明确说自己有用/产生了实际价值/明确使对方收益"
    UNCERTAIN = "可能产生收益"
    DISSATISFIED = "结果不满"
    DELAYED = "任务延期"
    NEGATIVE = "产生负面收益"
    HARMFUL = "帮倒忙"
    deltas = {BENEFIT: 10, UNCERTAIN: 0, DISSATISFIED: -5,
              DELAYED: -10, NEGATIVE: -20, HARMFUL: -30}
    DEFAULT = UNCERTAIN
    # 保留旧效益标签的读取能力，不推断受益对象，也不重新结算旧评分。
    legacy_results = ("帮了大忙，且反馈中收益明显", "帮了小事，反馈中有明确的较小收益",
                      "可能有用，不太清楚", "结果未知/推迟了任务/没有明确反馈",
                      "确认没有产生效益", "帮倒忙，实际使事情变差",
                      "对主人产生了实际效益", "对自己带来了帮助、收获或体验改善",
                      "结果未明", "产生实际效益", "未产生实际效益")

    @classmethod
    def is_beneficial(cls, result: str) -> bool:
        return result in (cls.BENEFIT, "帮了大忙，且反馈中收益明显",
                          "帮了小事，反馈中有明确的较小收益", "产生实际效益")

    @classmethod
    def is_failure(cls, result: str) -> bool:
        return result in (cls.DISSATISFIED, cls.DELAYED, cls.NEGATIVE, cls.HARMFUL)

    @classmethod
    def names(cls) -> List[str]:
        return list(cls.deltas)

    @classmethod
    def validate(cls, result: str) -> None:
        if not isinstance(result, str) or result not in cls.deltas:
            raise ValueError(f"未知效益评价：{result}；请选择{'、'.join(cls.names())}，不能仅按任务成败判断")



# === 顶层常量（推导得到）===

# Amy 内部全量 act 表（含全部三个分类的完整 dict）
ACT_EFFECTS = ACTEffects.all()

# 全部 act 条目，附分类标签。[(act_dict, category_label), ...]
ALL_ACT_ENTRIES = ACTEffects._all_entries()

# 按分类组织的字典：{"external": [...], "evaluation": [...], "self_action": [...],
# "pfca_regulation": [...], "system": [...]}
ACT_EFFECTS_BY_CATEGORY = {
    "external": [item for item, cat in ALL_ACT_ENTRIES if cat == "external"],
    "evaluation": [item for item, cat in ALL_ACT_ENTRIES if cat == "evaluation"],
    "self_action": [item for item, cat in ALL_ACT_ENTRIES if cat == "self_action"],
    "pfca_regulation": [item for item, cat in ALL_ACT_ENTRIES if cat == "pfca_regulation"],
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

# NE/DA 单向量程 0–100（2026-10-20 由 ±100 收敛，跨度 200 → 100）。
# ACT 表中的负增量保留原值，仍可抵消已累积的值，但结果不会低于 0。
BASE_VALUE = 0.0
MIN_VALUE = 0.0
MAX_VALUE = 100.0

# === 反馈维度参数（0-100 仅为容器量程，实际按格子离散步进结算）===
# 与 AMY 原设计的冲突点，暂以替代办法实现，随时可能推翻（见 docs/未来改进.md 第一节 1.1）。
FEEDBACK_DEFAULT = 50.0   # 中性基线
FEEDBACK_MIN = 0.0
FEEDBACK_MAX = 100.0

THOUGHT_DURATION = 300

# === DA 曲线平滑 ===
# 计算快照后，DA 按比例朝中性锚点收敛，压低极端档位的跳变（NE 不平滑）。
DA_NEUTRAL = 50.0
DA_SMOOTH_RATIO = 0.2

CAP_SCALE = 2.0

AFFINITY_ALPHA = 0.3
AFFINITY_BASE_DURATION = 3 * 24 * 3600


# === 描述表 ===

DOPAMINE_DESCRIPTIONS = [
    # 表现侧：当前心情，以及"自己还剩多少容错"（越高越松，越低越紧）。
    (90, 100, "心情极好，觉得自己怎么都对，容错很宽。说话轻快随意，想说什么说什么，不设防。"),
    (80, 90, "心情很好，心里笃定，容错宽。说话从容、敢说，也开得起玩笑。"),
    (70, 80, "心情明亮，底气足。说话放松，愿意主动开口。"),
    (60, 70, "心情不错，状态松。说话自然，有来有回。"),
    (50, 60, "心情平稳，不紧不松。正常应对。"),
    (40, 50, "心情偏淡，容错一般，不太主动。"),
    (30, 40, "有点低落，容错变窄。话少，语气发闷。"),
    (20, 30, "情绪低，容错窄，容易觉得自己做错。"),
    (10, 20, "很难过，心里发紧，容错很窄，怕说错。"),
    (0, 10, "情绪沉到底，绷得很紧，觉得自己没有余量。"),
]

NOREPINEPHRINE_DESCRIPTIONS = [
    # 表现侧：话语的尖锐程度（越高越冲，越低越软）。
    (90, 100, "话语极尖，攻击全开，直接伤人，不留情面。"),
    (80, 90, "很尖锐，冷嘲热讽，专戳痛处。"),
    (70, 80, "带刺，直揭短处，不给台阶。"),
    (60, 70, "不耐烦，语气冲，会顶回去。"),
    (50, 60, "话语正常，平和，不刺人也不讨好。"),
    (40, 50, "温和，话软，不设防。"),
    (30, 40, "说话很软，习惯性缓和，带语气词。"),
    (20, 30, "顺着对方说，基本不反驳。"),
    (10, 20, "极软，带着小心，只敢小声应。"),
    (0, 10, "完全不设防，只会顺着应，不反驳也不追问。"),
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
