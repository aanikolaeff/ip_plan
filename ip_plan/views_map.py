"""Раздел «Карта сети»: страница с React-островом (static_src/topo_map) и
JSON-эндпоинты данных и раскладки. Данные собирает services/topology_map.py,
раскладка — модель MapLayout, своя у каждого пользователя.

Карта пока только для просмотра. Ручные связи (создать/удалить кабель) —
следующий шаг: им понадобятся POST/DELETE рядом с data.json, а на фронтенде
у кабеля уже есть owner (ours/edited/manual), по которому решается, что
можно трогать.
"""
import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .context_processors import SITE_ALL, SITE_NONE, current_site, site_options_cached
from .models import MapLayout
from .services import topology_map
from .services.netbox_client import NetBoxAPIError

logger = logging.getLogger(__name__)

LAYERS = ("l2",)


def _site_param(request):
    """site из запроса, иначе площадка оболочки; «все» и «без площадки» → None."""
    site = (request.GET.get("site") or "").strip() or current_site(request)
    if site in (SITE_ALL, SITE_NONE, "*"):
        return None
    return site


def map_view(request):
    site = _site_param(request)
    if site is None:
        # Карта всех площадок разом нечитаема: по умолчанию первая площадка,
        # «Все площадки» остаётся выбором внутри карты.
        opts = site_options_cached()
        if request.GET.get("site") not in ("*",) and opts:
            site = opts[0]["slug"]
    boot = {
        "site": site or "*",
        "sites": [{"slug": o["slug"], "name": o.get("name") or o["slug"]} for o in site_options_cached()],
        "dataUrl": _url("map_data"),
        "layoutUrl": _url("map_layout"),
        "user": request.user.get_username(),
    }
    return render(request, "ip_plan/map.html", {"map_boot": boot})


def _url(name):
    from django.urls import reverse
    return reverse(f"ip_plan:{name}")


def map_data(request):
    site = _site_param(request)
    if request.GET.get("site") == "*":
        site = None
    try:
        data = topology_map.build_map(site)
    except NetBoxAPIError as exc:
        logger.warning("Карта: NetBox недоступен: %s", exc)
        return JsonResponse({"error": f"NetBox недоступен: {exc}"}, status=502)
    return JsonResponse(data, json_dumps_params={"ensure_ascii": False})


@require_http_methods(["GET", "PUT", "DELETE"])
def map_layout(request):
    site = (request.GET.get("site") or "*").strip() or "*"
    layer = request.GET.get("layer") or "l2"
    if layer not in LAYERS:
        return JsonResponse({"error": "неизвестный слой"}, status=400)
    if request.method == "GET":
        obj = MapLayout.objects.filter(user=request.user, site=site, layer=layer).first()
        if not obj:
            return JsonResponse({"positions": {}, "expanded": [], "locked": False, "updated_at": None})
        return JsonResponse({"positions": obj.positions, "expanded": obj.expanded, "locked": obj.locked,
                             "updated_at": obj.updated_at.isoformat()})
    if request.method == "DELETE":
        MapLayout.objects.filter(user=request.user, site=site, layer=layer).delete()
        return JsonResponse({"ok": True})
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "неверный JSON"}, status=400)
    positions = body.get("positions", {})
    expanded = body.get("expanded", [])
    if not isinstance(positions, dict) or not isinstance(expanded, list):
        return JsonResponse({"error": "positions — объект, expanded — список"}, status=400)
    clean = {}
    for k, v in positions.items():
        if isinstance(k, str) and len(k) <= 120 and isinstance(v, (list, tuple)) and len(v) == 2 \
                and all(isinstance(x, (int, float)) for x in v):
            clean[k] = [round(float(v[0]), 1), round(float(v[1]), 1)]
    if len(clean) > 5000:
        return JsonResponse({"error": "слишком много узлов"}, status=400)
    obj, _ = MapLayout.objects.update_or_create(
        user=request.user, site=site, layer=layer,
        defaults={"positions": clean, "expanded": [str(x)[:120] for x in expanded][:500],
                  "locked": bool(body.get("locked"))})
    return JsonResponse({"ok": True, "updated_at": obj.updated_at.isoformat()})
