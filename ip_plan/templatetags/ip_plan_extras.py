import json
import re
from pathlib import Path

from django import template
from django.urls import reverse
from django.utils.html import escape
from django.conf import settings
from django.templatetags.static import static as static_url
from django.utils.safestring import mark_safe

register = template.Library()

_VITE_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "static" / "ip_plan" / "dist" / ".vite" / "manifest.json"


def _collect_css(manifest, key, seen):
    """CSS собственного чанка + всех чанков, на которые он ссылается через
    "imports" — рекурсивно. Нужно с тех пор, как появился второй Vite-вход
    (regulator_table, см. vite.config.js): Rollup вынес общий для обоих
    входов код (react-dom, @tanstack/react-table, table.css) в отдельный
    shared-чанк, и CSS теперь висит на ЭТОМ чанке, а не на самой записи
    входа в manifest.json — chunk.get("css") у самого входа стал пустым.
    До этого (один вход) CSS всегда лежал прямо в записи входа, отдельного
    обхода "imports" не требовалось. seen — защита от повторного обхода
    одного и того же чанка (и на всякий случай от цикла), не от дублей в
    итоговом списке (дедуп — в самом vite_asset)."""
    if key in seen:
        return []
    seen.add(key)
    chunk = manifest.get(key) or {}
    css_files = list(chunk.get("css", []))
    for imported_key in chunk.get("imports", []):
        css_files.extend(_collect_css(manifest, imported_key, seen))
    return css_files


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
    css_files = list(dict.fromkeys(_collect_css(manifest, entry, set())))  # dedup, сохраняя порядок
    for css_file in css_files:
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
def ru_plural_zapis(n):
    """Русское склонение "запись/записи/записей" по числу n — Django
    встроенный pluralize не умеет 3-формное склонение (mod10/mod100), а
    используется тут только в тексте confirm-диалога удаления письма (см.
    regulator_document_delete)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "записей"
    n_mod10, n_mod100 = n % 10, n % 100
    if n_mod10 == 1 and n_mod100 != 11:
        return "запись"
    if n_mod10 in (2, 3, 4) and n_mod100 not in (12, 13, 14):
        return "записи"
    return "записей"


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


# ── ACL: операнд правила со ссылками на объекты ─────────────────────────

_OBJ_TOKEN_RE = re.compile(r"(@?)([^\s,@]+)")


@register.simple_tag
def acl_endpoint(rule, side, device):
    """HTML операнда (source/destination) правила: текст как в снимке, а имена
    объектов (ViPNet «@BLOCK_BY_IP_2», Cisco «object-group Proxy») — ссылки
    на карточку объекта устройства. Какие токены считать объектами, берём из
    самого операнда: Endpoint.object_groups (все объекты, включая смешанные
    операнды) и object_group. Всё экранируется, ссылки собираются через reverse."""
    endpoint = getattr(rule, side, None)
    if endpoint is None:
        return "—"
    names = set(endpoint.object_groups or ())
    if endpoint.object_group:
        names.add(endpoint.object_group)
    raw = endpoint.raw or ""
    nodes = _vpn_nodes_map((rule.extra or {}).get(f"{side}_vpn_nodes"))
    if not names and not nodes:
        return escape(raw)
    # Cisco: «object-group Proxy»; ViPNet: «@Proxy» (пробелы в именах — через lookup без пробелов)
    compact = {n.replace(" ", ""): n for n in names}

    def link(name, shown):
        url = reverse("ip_plan:acl_object_group_detail", kwargs={"device": device, "name": name})
        return f'<a href="{escape(url)}" title="объект {escape(name)}">{escape(shown)}</a>'

    if raw.startswith("object-group "):
        name = raw[len("object-group "):].strip()
        if name in names:
            return mark_safe("object-group " + link(name, name))
        return escape(raw)

    out = []
    pos = 0
    for m in _OBJ_TOKEN_RE.finditer(raw):
        out.append(escape(raw[pos:m.start()]))
        at, tok = m.group(1), m.group(2)
        key = tok.replace(" ", "")
        if at and key in compact:
            out.append(link(compact[key], "@" + tok))
        elif not at and tok.lower() in nodes:
            out.append(_node_html(tok, nodes[tok.lower()]))
        else:
            out.append(escape(at + tok))
        pos = m.end()
    out.append(escape(raw[pos:]))
    return mark_safe("".join(out))


# «0x4f260032 Видов А. С. VPN-20262 (virt 11.0.0.210); 0x4f260046 …» —
# тексты узлов из vipnet_firewall (extra[«source_vpn_nodes»]). Внутри скобок
# тоже бывают «;», поэтому режем не по «; », а по следующему «; 0x…».
_VPN_NODE_RE = re.compile(r"(0x[0-9a-fA-F]{4,})\s+(.*?)(?=;\s*0x[0-9a-fA-F]{4,}\s|$)", re.S)
# имя может содержать скобки («АП1 (Самолётов …)»), поэтому режем по метке
# сети VPN-NNNN, а без неё — по последней « (» перед адресами
_VPN_NAME_RE = re.compile(r"^(.*?)\s+(VPN-\d+)\s*(?:\((.*)\))?\s*$", re.S)


def _vpn_nodes_map(text):
    """{id в нижнем регистре: (имя, сеть, адреса)} из строки extra."""
    result = {}
    if not text:
        return result
    for m in _VPN_NODE_RE.finditer(text):
        node_id, rest = m.group(1), m.group(2).strip().rstrip(";").strip()
        nm = _VPN_NAME_RE.match(rest)
        if nm:
            name, network, addrs = nm.group(1).strip(), nm.group(2), (nm.group(3) or "").strip()
        else:
            i = rest.rfind(" (")
            name = rest[:i].strip() if i > 0 else rest
            addrs = rest[i + 2:].rstrip(")").strip() if i > 0 else ""
            network = ""
        result[node_id.lower()] = (name or rest, network, addrs)
    return result


def _node_html(node_id, info):
    """Имя узла ViPNet вместо голого идентификатора; сам идентификатор —
    мелко рядом, адреса и сеть — во всплывающей подсказке."""
    name, network, addrs = info
    if name.startswith("(нет в iplir"):
        return f'<span class="acl-node acl-node-unknown" title="узел не найден в iplir show config">{escape(node_id)}</span>'
    title = " · ".join(x for x in (node_id, network, addrs) if x)
    return (f'<span class="acl-node" title="{escape(title)}">{escape(name)}'
            f'<span class="acl-node-id">{escape(node_id)}</span></span>')


# ── ACL v2: склонения и числа для шаблонов раздела ──────────────────────

@register.filter
def plural(n, forms):
    """Русское склонение по числу: {{ n|plural:"правило,правила,правил" }} →
    «правил». Три формы через запятую — для 1, для 2–4 и для 5–20/0; 11–14
    всегда третья форма. Общий вариант ru_plural_zapis выше, который
    завязан на одно конкретное слово. При нечисловом n — третья форма."""
    parts = [p.strip() for p in str(forms).split(",")]
    if len(parts) != 3:
        return forms
    one, few, many = parts
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return many
    n_mod10, n_mod100 = n % 10, n % 100
    if n_mod10 == 1 and n_mod100 != 11:
        return one
    if n_mod10 in (2, 3, 4) and n_mod100 not in (12, 13, 14):
        return few
    return many


@register.filter
def ru_number(n):
    """Число в формате ru-RU с неразрывным тонким пробелом (U+202F) между
    разрядами: 2340 → «2 340». Тонкий, а не обычный пробел — чтобы «2 340
    правил» не разрывалось при переносе и читалось как одно число, как в
    макете (toLocaleString('ru-RU'))."""
    try:
        value = int(n)
    except (TypeError, ValueError):
        return n
    return f"{value:,}".replace(",", " ")
