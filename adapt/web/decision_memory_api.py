"""记忆页：人工锦囊与见闻录增删改查、见闻向量查询。"""
from datetime import datetime

from aiohttp import web

from brain.hc.database.decision_kit import DecisionKitOperations
from brain.hc.database.core import get_memory_db
from brain.hc.shortlight.notepad import get_shortlight_notepad, SLOT_EVENT
from brain.hc.shortlight.observation_search import search_observations
from utils.logger import get_logger

logger = get_logger(__name__, "INFO")

# 见闻录列表的默认条数与硬上限（全量返回会一次性铺满前端，2026-10-14 收敛）
DEFAULT_OBSERVATION_LIMIT = 20
MAX_OBSERVATION_LIMIT = 200


async def handle_get_kits(request):
    kits = await DecisionKitOperations().list_all()
    return web.json_response({"kits": kits, "total": len(kits)})


async def _kit_write(request, create):
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"error": "body 必须是 JSON 对象"}, status=400)
    try:
        ops = DecisionKitOperations()
        kit = await ops.create(data) if create else await ops.update(request.match_info["kit_id"], data)
    except ValueError as error:
        return web.json_response({"error": str(error)}, status=400)
    if kit is None:
        return web.json_response({"error": "锦囊不存在，可能已被删除"}, status=404)
    return web.json_response({"kit": kit}, status=201 if create else 200)


async def handle_create_kit(request):
    return await _kit_write(request, True)


async def handle_update_kit(request):
    return await _kit_write(request, False)


async def handle_delete_kit(request):
    kit_id = request.match_info["kit_id"]
    if not await DecisionKitOperations().delete(kit_id):
        return web.json_response({"error": "锦囊不存在，可能已被删除"}, status=404)
    return web.json_response({"deleted": True, "kit_id": kit_id})


def _parse_limit(raw: str) -> int:
    """解析 limit 查询参数（缺省取默认值，并夹到 [1, 硬上限]）。非法值抛 ValueError。"""
    if raw is None or raw == "":
        return DEFAULT_OBSERVATION_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("limit 必须是整数")
    return max(1, min(value, MAX_OBSERVATION_LIMIT))


def _parse_time(raw: str) -> datetime:
    """解析 since / until（ISO 8601）；带时区的值归一到本地朴素时间。非法值抛 ValueError。"""
    if raw is None or raw == "":
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("时间段必须是 ISO 8601 时间（如 2026-10-10T00:00:00）")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _within_range(record_time: str, since: datetime, until: datetime) -> bool:
    """按 record_time 判断是否落在 [since, until] 区间内；无法解析的记录视为不匹配。"""
    try:
        recorded = datetime.fromisoformat((record_time or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if recorded.tzinfo is not None:
        recorded = recorded.astimezone().replace(tzinfo=None)
    if since is not None and recorded < since:
        return False
    if until is not None and recorded > until:
        return False
    return True


async def handle_get_observations(request):
    """见闻录列表：默认返回最近 20 条，支持 limit 与 since/until 时间段过滤。

    observations 为最新在前的一页；total 为过滤后的总条数（不受 limit 影响）。
    """
    try:
        limit = _parse_limit(request.query.get("limit"))
        since = _parse_time(request.query.get("since"))
        until = _parse_time(request.query.get("until"))
    except ValueError as error:
        return web.json_response({"error": str(error)}, status=400)

    notes = await get_shortlight_notepad().get_slot_notes(SLOT_EVENT)
    if since is not None or until is not None:
        notes = [n for n in notes if _within_range(n.get("record_time", ""), since, until)]
    total = len(notes)
    return web.json_response({"observations": list(reversed(notes))[:limit], "total": total})


async def _observation_write(request, create):
    try:
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"text"}:
            raise ValueError("见闻只接受 text 正文字段")
        notepad = get_shortlight_notepad()
        if create:
            note_id = await notepad.record_observation(data["text"], source="human")
            notes = await notepad.get_slot_notes(SLOT_EVENT)
            note = next(item for item in notes if item["note_id"] == note_id)
        else:
            note = await notepad.update_observation(request.match_info["note_id"], data["text"])
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)
    if note is None:
        return web.json_response({"error": "见闻不存在，可能已被删除"}, status=404)
    return web.json_response({"observation": note}, status=201 if create else 200)


async def handle_create_observation(request):
    return await _observation_write(request, True)


async def handle_update_observation(request):
    return await _observation_write(request, False)


async def handle_delete_observation(request):
    note_id = request.match_info["note_id"]
    if not await get_shortlight_notepad().delete_observation(note_id):
        return web.json_response({"error": "见闻不存在，可能已被删除"}, status=404)
    conn = await get_memory_db()._ensure_connection()
    await conn.execute("DELETE FROM observation_vectors WHERE note_id=?", (note_id,))
    await conn.commit()
    return web.json_response({"deleted": True, "note_id": note_id})


async def handle_search_observations(request):
    query = request.query.get("query", "").strip()
    if not query:
        return web.json_response({"error": "请填写当前困境"}, status=400)
    try:
        return web.json_response(await search_observations(query))
    except Exception:
        logger.exception("见闻向量查询失败")
        return web.json_response({"error": "见闻向量查询失败，请检查嵌入服务后重试"}, status=502)
