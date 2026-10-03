#!/usr/bin/env python3
"""剩余两处编辑：manager_core 异步停止 + 服务 shutdown 调用。"""
from pathlib import Path

p = Path("manager_core.py")
t = p.read_text(encoding="utf-8")
old = """    def stop_everything(self) -> list[str]:
        msgs = []
        for c in sorted(self.controllers.values(), key=lambda c: c.spec.order, reverse=True):
            msgs.append(f"[{c.spec.name}] " + c.submit("stop").get("message", ""))
        return msgs"""
new = old + """

    async def stop_everything_async(self) -> list[str]:
        return await asyncio.to_thread(self.stop_everything)"""
assert old in t, "manager_core anchor miss"
t = t.replace(old, new, 1)
p.write_text(t, encoding="utf-8", newline="\n")
print("manager_core ✓")

p = Path("manager_service.py")
t = p.read_text(encoding="utf-8")
old = "    msgs = _service.stop_everything()"
new = "    msgs = await _service.stop_everything_async()"
assert old in t, "service anchor miss"
p.write_text(t.replace(old, new, 1), encoding="utf-8", newline="\n")
print("manager_service shutdown ✓")
