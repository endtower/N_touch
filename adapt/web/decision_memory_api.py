"""记忆页：人工锦囊与见闻录增删改查、见闻向量查询。"""
from aiohttp import web

from brain.hc.database.decision_kit import DecisionKitOperations
from brain.hc.database.core import get_memory_db
from brain.hc.shortlight.notepad import get_shortlight_notepad, SLOT_EVENT
from brain.hc.shortlight.observation_search import search_observations
from utils.logger import get_logger

logger = get_logger(__name__, "INFO")


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
    from brain.hc.maintenance import MaintenanceService
    service = MaintenanceService()
    item = await service.item("kit", kit["kit_id"])
    await service.review("kit", kit["kit_id"], item["revision"], action="edit")
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


async def handle_get_observations(request):
    notes = await get_shortlight_notepad().get_slot_notes(SLOT_EVENT)
    return web.json_response({"observations": list(reversed(notes)), "total": len(notes)})


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
