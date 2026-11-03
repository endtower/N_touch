"""全新安装只建立必要身份，不导入开发者的群、经验或个人印象。"""
from datetime import datetime
class DefaultInitializer:
    @staticmethod
    async def initialize_all():
        from brain.hc.database.core import get_memory_db
        from brain.hc.database import entity_identity
        from config.role_settings import role_config
        conn = await get_memory_db()._ensure_connection()
        now = datetime.now().isoformat()
        identities = [("00000000", role_config.ASSISTANT_NAME, "")]
        if role_config.MASTER_ID not in ("", "0"):
            identities.append(("00000001", role_config.MASTER_NAME, "private_" + role_config.MASTER_ID))
        for uuid, name, session_id in identities:
            await conn.execute("INSERT OR IGNORE INTO characters (uuid,usage_count,last_used_at,created_at) VALUES (?,0,?,?)", (uuid, now, now))
            await entity_identity.set_primary_name(conn, entity_identity.CHARACTER, uuid, name)
            if session_id:
                await entity_identity.add_contact(conn, entity_identity.CHARACTER, uuid, session_id)
        await conn.commit()
