# -*- coding: utf-8 -*-
"""审批队列（给主人用的，不是给模型用的）。

  python -m unified.approvals                 查看待批
  python -m unified.approvals approve <会话>   批准：长期可听可说
  python -m unified.approvals reject  <会话>   驳回
"""

from __future__ import annotations

import sys

from . import permits


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("list", "ls"):
        rows = permits.pending()
        if not rows:
            print("没有待批的申请。")
            return 0
        print(f"待批 {len(rows)} 条：")
        for r in rows:
            print(f"  {r['session_id']}  {r.get('session_name') or ''}")
            if r.get("reason"):
                print(f"     理由：{r['reason']}")
            if r.get("blocked_text"):
                print(f"     被拦下的话：{r['blocked_text']}")
        return 0

    action, sid = argv[0], (argv[1] if len(argv) > 1 else "")
    if not sid:
        print("要给 session_id")
        return 2
    if action == "approve":
        print(permits.approve(sid))
        return 0
    if action == "reject":
        print(permits.reject(sid))
        return 0
    print(f"不认识的动作：{action}（list / approve / reject）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
