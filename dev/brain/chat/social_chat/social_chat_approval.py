"""Social Chat 自由发送申请的主人回复格式。"""

from dataclasses import dataclass
import re

from brain.hc.event_chain_permissions import MAX_DURATION_MINUTES


@dataclass(frozen=True)
class FreeSendOptions:
    duration_minutes: int | None
    notify: bool


def parse_free_send_reply(content: str, defaults: FreeSendOptions) -> FreeSendOptions | None:
    """回复以「同意」开头，选项可任意排序；未知或重复选项拒绝整条授权。"""
    words = content.split()
    if not words or words[0] != "同意":
        return None

    duration = defaults.duration_minutes
    notify = defaults.notify
    seen = set()
    for word in words[1:]:
        interval = re.fullmatch(r"([0-9]{1,4})(分钟|小时)", word)
        if word == "本链" or interval:
            option = "duration"
            duration = None if interval is None else int(interval[1]) * (60 if interval[2] == "小时" else 1)
            if duration is not None and not 1 <= duration <= MAX_DURATION_MINUTES:
                return None
        elif word in ("知会", "不知会"):
            option = "notify"
            notify = word == "知会"
        else:
            return None
        if option in seen:
            return None
        seen.add(option)
    return FreeSendOptions(duration, notify)
