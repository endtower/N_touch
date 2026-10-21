"""全新安装只建立必要身份，不导入开发者的群、经验或个人印象。"""
import json
from datetime import datetime
class DefaultInitializer:
    @staticmethod
    async def initialize_all():
        from brain.hc.database.core import get_memory_db
        from config.role_settings import role_config
        conn = await get_memory_db()._ensure_connection()
        now = datetime.now().isoformat()
        identities = [("00000000", role_config.ASSISTANT_NAME, [])]
        if role_config.MASTER_ID not in ("", "0"):
            identities.append(("00000001", role_config.MASTER_NAME, [{"session_id": "private_" + role_config.MASTER_ID, "usage_count": 0}]))
        for uuid, name, contacts in identities:
            await conn.execute("INSERT OR IGNORE INTO characters (uuid,name,aliases,contacts,usage_count,last_used_at,created_at) VALUES (?,?,?,?,0,?,?)", (uuid,name,json.dumps([name],ensure_ascii=False),json.dumps(contacts),now,now))
        await conn.commit()
