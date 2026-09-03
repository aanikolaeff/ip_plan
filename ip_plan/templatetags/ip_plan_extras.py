import json
from pathlib import Path

from django import template
from django.conf import settings
from django.templatetags.static import static as static_url
from django.utils.safestring import mark_safe

register = template.Library()

_VITE_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "static" / "ip_plan" / "dist" / ".vite" / "manifest.json"


@register.simple_tag
def vite_asset(entry):
    """<script type="module">/<link> для собранного Vite-бандла (см.
    docs/plans/2026-09-03-ip-table-react-design.md, раздел "Сборка и
    деплой"). Читает manifest.json заново на каждый вызов (не кэшируется в
    памяти процесса, в отличие от COLUMN_INFO_JSON в views.py) — это
    крошечный файл, а не кэшировать удобнее в активной разработке: правки
    после `npm run build` подхватываются без перезапуска контейнера.
    Отсутствие файла (сборка ещё не запускалась) — не 500, а комментарий в
    HTML, чтобы страница не падала целиком."""
    try:
        manifest = json.loads(_VITE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return mark_safe(f"<!-- vite manifest not found/invalid — run `npm run build` ({entry}) -->")
    chunk = manifest.get(entry)
    if not chunk:
        return mark_safe(f"<!-- vite entry '{entry}' not in manifest — run `npm run build` -->")
    tags = [f'<script type="module" src="{static_url("ip_plan/dist/" + chunk["file"])}"></script>']
    for css_file in chunk.get("css", []):
        tags.append(f'<link rel="stylesheet" href="{static_url("ip_plan/dist/" + css_file)}">')
    return mark_safe("\n".join(tags))


@register.simple_tag
def csrf_cookie_name():
    """Имя cookie CSRF (см. settings.CSRF_COOKIE_NAME) — нужно в JS, чтобы
    читать ту же cookie, которую реально выставляет сервер (см. base.html:
    window.CSRF_COOKIE_NAME)."""
    return settings.CSRF_COOKIE_NAME


@register.filter
def ip_only(value):
    """Адрес без маски подсети: "10.0.0.1/24" -> "10.0.0.1"."""
    return (value or "").split("/")[0]


@register.filter
def assignment_role(ip):
    """Роль устройства/ВМ, на которую назначен адрес — заранее посчитана в
    Python и положена в ip["_role"] (см. views.py: _attach_role/
    _attach_roles_bulk). В самом ответе /api/ipam/ip-addresses/ её нет:
    assigned_object.device/.virtual_machine там — краткая (brief)
    репрезентация NetBox без поля role, нужен отдельный запрос к
    устройству/ВМ."""
    return ip.get("_role")


@register.filter
def guest_os(ip):
    """Guest OS ВМ, на которую назначен адрес — заранее посчитано в Python
    и положено в ip["_guest_os"] (см. views.py: _attach_role/
    _attach_roles_bulk); как и с assignment_role, Django-шаблоны не дают
    обращаться к "ip._guest_os" напрямую (ведущее подчёркивание)."""
    return ip.get("_guest_os")


def _is_vm(ip):
    role = ip.get("_role")
    return bool(role and role.get("slug") == "vm")


@register.filter
def effective_description(ip):
    """Значение для показа/копирования и для предзаполнения инлайн-правки
    в колонке "Описание": для роли VM — описание самой ВМ (поле read-only,
    реально заполняется внешней синхронизацией); во всех остальных случаях
    (устройство или без привязки) — собственное description IP-адреса,
    которое и редактируется инлайн."""
    if _is_vm(ip):
        return ip.get("_parent_description") or ""
    return ip.get("description") or ""


@register.filter
def description_hint(ip):
    """Текст тултипа (атрибут title). Для VM — описание ВМ + пометка про
    автозаполнение. Для устройства с непустым description — "устройство ›
    IP" (показываем оба, хотя редактируется и совпадает с отображаемым
    только описание IP). Иначе (устройство без description, либо адрес без
    привязки вообще) — просто собственное описание IP-адреса."""
    own = ip.get("description") or ""
    if _is_vm(ip):
        vm_desc = ip.get("_parent_description") or ""
        return f"{vm_desc}\n*заполняется автоматически" if vm_desc else vm_desc
    parent_desc = ip.get("_parent_description")
    if parent_desc:
        return f"{parent_desc} › {own}"
    return own
