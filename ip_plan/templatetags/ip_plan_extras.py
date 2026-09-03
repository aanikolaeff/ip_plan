from django import template
from django.conf import settings

register = template.Library()


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
