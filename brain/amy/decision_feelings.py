"""决策情感的暂用文案。只描述感受，不指定行动；后续可单独替换本文件。"""

# recognition / competence 沿用旧标识，保证既有状态和历史记录兼容。
AXIS_NAMES = {"recognition": "归属感", "competence": "效能感"}

FEELINGS = {
    "competence": (
        (20, "对自己的处理方式有些挫败，缺少把握。"),
        (40, "对自己的处理方式还有些怀疑，不太有把握。"),
        (60, "对自己处理事情的能力感觉平稳。"),
        (80, "对自己的处理方式更有信心。"),
        (100, "对自己处理事情的能力感到踏实、有把握。"),
    ),
    "recognition": (
        (20, "有些孤单、缺少归属，想得到回应。"),
        (40, "对自己与他人的联系还有些不安，心里有些空落。"),
        (60, "对自己与他人的联系感觉平稳。"),
        (80, "对自己与他人的联系感到安心，更愿意参与其中。"),
        (100, "对与他人的联系感到踏实、亲近，心里很满足。"),
    ),
}


def build_decision_feedback_prompt(state: dict) -> str:
    """每轴一段：先现状感受，再该轴最近五条变动；不含 NE/DA，也不强制采取补偿行动。

    格式（每轴：现状一行，其后该轴变动每行一条）：
        【当前情绪】{轴名}：{值}/100。{感受}
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
