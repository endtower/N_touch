"""人工维护统计及统一工具发布的维护申请，只读展示申请内的失败原因。"""
from aiohttp import web
from brain.hc.maintenance import MaintenanceService
from brain.base.persona.persona_registry import list_personas, update_persona_from_human
from user_prompt_settings import list_user_system_personas, update_user_system_persona


async def handle_maintenance(request):
    return web.json_response(await MaintenanceService().query())


async def handle_maintenance_items(request):
    items = await MaintenanceService().metadata(request.query.get("kind"))
    item_id = request.query.get("item_id")
    if item_id:
        items = [item for item in items if item["item_id"] == item_id]
    return web.json_response({"items": items})


async def handle_review(request):
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"item_id"}:
            raise ValueError("请指定要记录人工维护的经验编号")
        await MaintenanceService().review(body["item_id"])
        return web.json_response({"reviewed": True})
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)


async def handle_review_all(request):
    try:
        body = await request.json()
        if body != {}:
            raise ValueError("该接口不接受参数")
        return web.json_response({"reviewed": await MaintenanceService().review_all()})
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)


async def handle_archive(request):
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) not in ({"action_id"}, {"action_id", "archived"}):
            raise ValueError("请指定要归档的维护申请编号")
        archived = body.get("archived", True)
        if not isinstance(archived, bool):
            raise ValueError("archived 必须为布尔值")
        await MaintenanceService().archive(body["action_id"], archived)
        return web.json_response({"archived": archived})
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)


async def handle_personas(request):
    # 系统人格只在人工页面中追加，不进入任何 LLM 可用的人格注册表。
    return web.json_response({"personas": [p.to_dict() for p in list_personas()] + list_user_system_personas()})


async def handle_update_persona(request):
    try:
        name = request.match_info["persona_name"]
        changes = await request.json()
        if name.startswith("system:"):
            return web.json_response({"persona": await update_user_system_persona(name, changes)})
        persona = await update_persona_from_human(name, changes)
        return web.json_response({"persona": persona.to_dict()})
    except (ValueError, TypeError, KeyError) as error:
        return web.json_response({"error": str(error)}, status=400)
