"""Раздел «Поиск MAC/IP» (U11 плана хранилища наблюдений): где в сети виден
MAC или IP — по истории наблюдений netbox-sync в базе netobs.

Доступен любому вошедшему пользователю (вход требует
LoginRequiredMiddleware). Ищет по всем площадкам сразу — переключатель
площадки в шапке здесь не действует: адрес ищут по всей сети. Вся работа с
базой — в ip_plan/services/netobs_search.py; недоступная или не настроенная
netobs даёт сообщение на странице, а не 500 (R12)."""
from django.shortcuts import render

from .services import netobs_search

# Примеры на начальном экране: один MAC во всех четырёх записях.
MAC_EXAMPLES = ("90:54:b7:34:06:c0", "9054.b734.06c0", "90-54-B7-34-06-C0", "9054b73406c0")


def obs_search(request):
    """?q=<MAC | часть MAC от 4 hex | IP>. Без q — начальный экран с
    примерами форматов и состоянием хранилища."""
    q = (request.GET.get("q") or "").strip()
    ctx = {"q": q, "limit": netobs_search.LIMIT, "result": None, "status": None,
           "error": None, "unavailable": None, "not_configured": False,
           "mac_examples": MAC_EXAMPLES}
    try:
        if q:
            try:
                ctx["result"] = netobs_search.search(q)
            except netobs_search.QueryError as exc:
                ctx["error"] = str(exc)
        else:
            ctx["status"] = netobs_search.status()
    except netobs_search.Unavailable as exc:
        ctx["unavailable"] = str(exc)
        ctx["not_configured"] = not exc.configured
    return render(request, "ip_plan/obs_search.html", ctx)
