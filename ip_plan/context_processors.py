"""Данные, которые нужны каждому шаблону.

Пока здесь только версия сборки: её показывает уголок внизу страницы, и по
ней видно, доехал ли деплой. Файл `VERSION` читается один раз при старте
процесса — значит после пересборки контейнера номер обновится сам, а лишних
обращений к диску на каждый запрос не будет.
"""
from django.conf import settings
from django.core.cache import cache

from .services import remote_gateway, ssh_access


def app_version(request):
    return {"app_version": settings.APP_VERSION}


# ── Оболочка: площадка как глобальный контекст и заголовок раздела ─────────
#
# Площадка выбирается в боковом меню один раз и действует на «Адреса» и
# «ACL» (решение 01.10.2026; «Блокировки» общие для всех площадок). Хранится
# в cookie IP_PLAN_SITE_COOKIE: 'all' — все, '__none__' — адреса без площадки,
# иначе slug площадки NetBox. Список площадок берётся из NetBox через
# sync_config.site_options() и кэшируется на 5 минут: без кэша каждый запрос
# страницы ходил бы в NetBox API ради одного списка.

SITE_COOKIE = "ip_plan_site"
SITE_ALL = "all"
SITE_NONE = "__none__"

_SECTION_TITLES = (
    ("ip_list", "Адреса"),
    ("acl_", "ACL"),
    ("regulator_", "Блокировки"),
    ("map_", "Карта сети"),
    ("settings", "Настройки"),
    ("password_change", "Смена пароля"),
)


def site_options_cached():
    """[{slug, name}] площадок NetBox; пустой список, если NetBox недоступен."""
    cached = cache.get("shell_site_options")
    if cached is not None:
        return cached
    from ip_plan.services import sync_config as sc
    try:
        raw = sc.site_options()
    except Exception:  # noqa: BLE001 — меню площадок не должно ронять страницу
        raw = []
    options = []
    for item in raw or []:
        if isinstance(item, dict):
            slug, name = item.get("slug"), item.get("name") or item.get("slug")
        else:
            slug, name = item[0], item[1] if len(item) > 1 else item[0]
        if slug:
            options.append({"slug": slug, "name": name})
    cache.set("shell_site_options", options, 300)
    return options


def current_site(request):
    """Выбранная площадка из cookie: 'all' | '__none__' | slug. Неизвестный
    slug (площадку удалили) считается 'all'."""
    value = (request.COOKIES.get(SITE_COOKIE) or SITE_ALL).strip()
    if value in (SITE_ALL, SITE_NONE):
        return value
    if any(o["slug"] == value for o in site_options_cached()):
        return value
    return SITE_ALL


def shell(request):
    match = getattr(request, "resolver_match", None)
    url_name = (match.url_name if match else "") or ""
    title = ""
    for prefix, label in _SECTION_TITLES:
        if url_name.startswith(prefix):
            title = label
            break
    site = current_site(request)
    options = site_options_cached()
    label = {SITE_ALL: "Все площадки", SITE_NONE: "Без площадки"}.get(site)
    if label is None:
        label = next((o["name"] for o in options if o["slug"] == site), site)
    menu = [{"slug": SITE_ALL, "name": "Все площадки", "sub": "", "sep": False}]
    menu += [{"slug": o["slug"], "name": o["name"], "sub": o["slug"], "sep": False} for o in options]
    menu.append({"slug": SITE_NONE, "name": "Без площадки",
                 "sub": "не привязаны к устройству и подсети", "sep": True})
    gateway = remote_gateway.allowed_for(getattr(request, "user", None))
    return {
        "shell_section_title": title,
        "shell_site": site,
        "shell_site_label": label,
        "shell_sites": menu,
        # Пункты «В браузере» меню строки адресов (шлюз Guacamole).
        "shell_gateway": gateway,
        # …и пункт «SSH по ключу», если ключ задан в «Настройках».
        "shell_gateway_key": gateway and ssh_access.configured(),
    }
