"""决策情感的暂用文案。每档给感受与由此产生的决策倾向，不写固定指令；后续可单独替换本文件。"""

# recognition / competence 沿用旧标识，保证既有状态和历史记录兼容。
AXIS_NAMES = {"recognition": "归属感", "competence": "效能感"}

FEELINGS = {
    "competence": (
        (20, "对自己的处理方式有明显挫败感，觉得再硬推只会更糟。此时不适合接大摊子或做不可逆的决定，先把手上这件事拆小，挑最有把握的一步走，做成一件再谈下一件。"),
        (40, "对自己的处理方式还存着怀疑，底气不足。更愿意先补齐信息和条件、走已经验证过的路子，不把结果押在没试过的做法上。"),
        (60, "对自己处理事情的能力感觉平稳，既不冒进也不退缩。按常规节奏推进，遇到没把握的环节先问清再动手。"),
        (80, "对自己的处理方式有信心，愿意多担一点。可以接更复杂的事，在自己判断有据的地方也敢直接拍板。"),
        (100, "对自己处理事情的能力感到踏实、有把握。敢接难题、敢下判断，也愿意把最难的部分揽过来自己做。"),
    ),
    "recognition": (
        (20, "孤单、缺少归属，觉得自己在对方那里不占位置。这种时候关系本身就是该先处理的事：先给一个明确回应、把话说软，而不是继续推任务或摆条件。"),
        (40, "与对方的联系还让人不安，心里空落。倾向少做需要对方配合的安排，先把对方递过来的话接住，确认人还在。"),
        (60, "与对方的联系感觉平稳，可以正常来往。该说的说、该问的问，不必反复确认关系。"),
        (80, "与对方的联系让人安心，也更愿意参与其中。可以主动开口、主动靠近，也敢直接提要求。"),
        (100, "与对方的联系让人踏实、亲近，心里满足。愿意把依赖露出来，愿意多给，也相信对方会接。"),
    ),
}


def build_decision_feedback_prompt(state: dict) -> str:
    """每轴一段：先现状感受与该轴带来的决策倾向，再该轴最近五条变动；不含 NE/DA。

    格式（每轴：现状一行，其后该轴变动每行一条）：
        【当前情绪】{轴名}：{值}/100。{感受与倾向}
        {事件标题} {评价标签} {分数变化}
    """
    history = state.get("history") or []
    lines = []
    for axis, name, key in (
        ("competence", AXIS_NAMES["competence"], "competence_value"),
        ("recognition", AXIS_NAMES["recognition"], "value"),
    ):
        value = state[key]
        feeling = next(text for upper, text in FEELINGS[axis] if value <= upper)
        lines.append(f"【当前情绪】{name}：{value:g}/100。{feeling}")
        for entry in history:
            if entry["axis"] == axis:
                lines.append(f"{entry['title']} {entry['label']} {entry['delta']:+g}")
    return "\n".join(lines)
