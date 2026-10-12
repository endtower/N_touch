"""人机共治的人工入口：查询、确认版本、处理申请及人格编辑。"""
from aiohttp import web
from brain.hc.maintenance import MaintenanceService
from brain.base.persona.persona_registry import list_personas, update_persona_from_human


async def handle_maintenance(request):
    return web.json_response(await MaintenanceService().query(request.query.get("topic", "")))


async def handle_maintenance_items(request):
    try:
        items = await MaintenanceService().metadata(request.query.get("kind"))
    except ValueError as error:
        return web.json_response({"error": str(error)}, status=400)
    item_id = request.query.get("item_id")
    if item_id:
        items = [item for item in items if item["item_id"] == item_id]
    return web.json_response({"items": items})


async def handle_review(request):
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"kind", "item_id", "revision", "topic"}:
            raise ValueError("人工确认参数无效")
        await MaintenanceService().review(body.get("kind"), body.get("item_id"), body.get("revision"), body.get("topic", "日常维护"))
        return web.json_response({"reviewed": True})
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)


async def handle_maintenance_request(request):
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"status", "conclusion", "defer_hours"}:
            raise ValueError("维护处理参数无效")
        result = await MaintenanceService().handle_request(request.match_info["request_id"], **body)
        return web.json_response(result)
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)


async def handle_personas(request):
    return web.json_response({"personas": [p.to_dict() for p in list_personas()]})


async def handle_update_persona(request):
    name = request.match_info["persona_name"]
    try:
        persona = await update_persona_from_human(name, await request.json())
        service = MaintenanceService()
        item = await service.item("persona", name)
        await service.review("persona", name, item["revision"], action="edit")
        return web.json_response({"persona": persona.to_dict()})
    except (ValueError, TypeError, KeyError) as error:
        return web.json_response({"error": str(error)}, status=400)
