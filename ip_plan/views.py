import ipaddress
import json
import logging
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.utils import timezone
from django.conf import settings
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import PasswordChangeForm
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .forms import (
    STATUS_LABELS,
    CUSTOM_FIELD_KEYS, STATUS_CHOICES, IPAddressForm,
    RegulatorDocumentNoteForm, RegulatorEntryForm,
)
from .models import FirewallObjectName, RegulatorDocument, RegulatorEntry
from .services import (
    acl_search, acl_store, firewall_blocklist, ioc_parser, os_catalog, regulator_sync, remote_gateway, ssh_access,
    sync_config, sync_log, vm_console,
)
from .services.netbox_client import NetBoxAPIError, NetBoxClient
from .services.network_tools import NetworkToolError, ping_check, resolve_ptr, scan_ports
from .templatetags.ip_plan_extras import description_hint, effective_description

logger = logging.getLogger(__name__)

# Описания колонок таблицы (column_info.json в корне проекта).
# Читается один раз при старте процесса; изменения требуют пересборки образа.
_COLUMN_INFO_PATH = Path(__file__).parent.parent / "column_info.json"
try:
    COLUMN_INFO_JSON = _COLUMN_INFO_PATH.read_text(encoding="utf-8")
except FileNotFoundError:
    COLUMN_INFO_JSON = "{}"

# Поля, доступные для inline-редактирования (двойной клик по ячейке).
# Тэги, IP-адрес и Интерфейс редактируются только в модалке. CPE/Open
# Ports/OS Guess — только для чтения везде, их заполняет только
# сканирование/импорт. Описание и Комментарии — редактируются инлайн (как
# однострочное поле; полный многострочный текст комментариев — всё ещё в
# модалке). Доступность описания в UI зависит от роли назначения: для VM —
# только для чтения, см. data-editable в ip_row.html, его описание
# заполняется автоматически синхронизацией и инлайн-правка там скрыта).
INLINE_CUSTOM_FIELDS = {"os_installed", "purpose"}
INLINE_TOP_LEVEL_FIELDS = {"dns_name", "status", "description", "comments"}
PATCHABLE_FIELDS = INLINE_TOP_LEVEL_FIELDS | INLINE_CUSTOM_FIELDS


def _build_payload(cleaned_data, tag_ids):
    return {
        "address": cleaned_data["address"],
        "status": cleaned_data["status"],
        "dns_name": cleaned_data.get("dns_name", ""),
        "description": cleaned_data.get("description", ""),
        "comments": cleaned_data.get("comments", ""),
        "tags": [{"id": tag_id} for tag_id in tag_ids],
        "custom_fields": {key: cleaned_data.get(key) or None for key in CUSTOM_FIELD_KEYS},
    }


def _ip_to_initial(ip):
    custom_fields = ip.get("custom_fields") or {}
    initial = {
        "address": ip.get("address", ""),
        "status": (ip.get("status") or {}).get("value", "active"),
        "dns_name": ip.get("dns_name") or "",
        "description": ip.get("description") or "",
        "comments": ip.get("comments") or "",
        "tags": ", ".join(tag["name"] for tag in ip.get("tags") or []),
    }
    for key in CUSTOM_FIELD_KEYS:
        initial[key] = custom_fields.get(key) or ""
    return initial


def _assigned_parent_ref(ip):
    """(id, is_device) устройства/ВМ, на которые назначен адрес, либо None —
    assigned_object.device и .virtual_machine взаимоисключающие в NetBox."""
    assigned = ip.get("assigned_object") or {}
    device = assigned.get("device")
    if device:
        return device["id"], True
    vm = assigned.get("virtual_machine")
    if vm:
        return vm["id"], False
    return None


def _attach_role(client, ip):
    """Точечно (1 запрос) дополняет ip-дикт полями "_role", "_parent_description"
    и "_guest_os" для одной строки — используется после create/update, когда
    у нас уже есть конкретный ip. "_parent_description" — описание устройства/ВМ
    из назначения: для роли VM это и есть то, что показывается/копируется в
    колонке "Описание" (поле read-only, реально синхронизируется снаружи); для
    устройства — только обогащение тултипа, сама ячейка работает с собственным
    description IP-адреса (см. ip_plan_extras.py: effective_description/
    description_hint). "_guest_os" — custom field guest_os самой ВМ (у
    устройств его не бывает, NetBox ограничивает content type этого поля
    virtualization.virtualmachine)."""
    ref = _assigned_parent_ref(ip)
    if ref is None:
        ip["_role"] = None
        ip["_parent_description"] = None
        ip["_guest_os"] = None
        ip["_site"] = None
        return ip
    parent_id, is_device = ref
    parent = client.get_device(parent_id) if is_device else client.get_virtual_machine(parent_id)
    ip["_role"] = parent.get("role")
    ip["_parent_description"] = parent.get("description") or None
    ip["_guest_os"] = None if is_device else (parent.get("custom_fields") or {}).get("guest_os") or None
    ip["_site"] = _site_ref(parent.get("site"))
    return ip


def _site_ref(site):
    """{"slug", "name"} площадки из вложенного объекта NetBox либо None."""
    if not site:
        return None
    return {"slug": site.get("slug"), "name": site.get("name") or site.get("display") or site.get("slug")}


def _attach_roles_bulk(client, ip_addresses):
    """Массово (2 запроса, не N+1) дополняет каждый ip-дикт полями "_role",
    "_parent_description" и "_guest_os" — используется для всего списка на
    главной странице (см. _attach_role)."""
    devices = {d["id"]: d for d in client.list_devices()}
    vms = {v["id"]: v for v in client.list_virtual_machines()}
    for ip in ip_addresses:
        ref = _assigned_parent_ref(ip)
        if ref is None:
            ip["_role"] = None
            ip["_parent_description"] = None
            ip["_guest_os"] = None
            ip["_site"] = None
            continue
        parent_id, is_device = ref
        parent = (devices if is_device else vms).get(parent_id) or {}
        ip["_role"] = parent.get("role")
        ip["_parent_description"] = parent.get("description") or None
        ip["_guest_os"] = None if is_device else (parent.get("custom_fields") or {}).get("guest_os") or None
        ip["_site"] = _site_ref(parent.get("site"))
    return ip_addresses


def _attach_sites_from_prefixes(ip_addresses, prefixes):
    """Площадка для адресов без назначения — по самому специфичному
    префиксу NetBox, содержащему адрес и приписанному к площадке
    (scope_type=dcim.site; префиксы получают её от устройства, с которого
    прочитаны, см. netbox_network_setup.py). У самого IP-адреса поля site
    в NetBox нет: площадка у него всегда производная — от устройства/ВМ,
    на которые он назначен, иначе от подсети."""
    scoped = []
    for prefix in prefixes:
        if prefix.get("scope_type") != "dcim.site" or not prefix.get("scope"):
            continue
        try:
            scoped.append((ipaddress.ip_network(prefix["prefix"], strict=False), _site_ref(prefix["scope"])))
        except ValueError:
            continue
    scoped.sort(key=lambda pair: pair[0].prefixlen, reverse=True)
    for ip in ip_addresses:
        if ip.get("_site"):
            continue
        try:
            addr = ipaddress.ip_interface(ip["address"]).ip
        except (KeyError, ValueError):
            continue
        ip["_site"] = next((site for net, site in scoped if addr in net), None)
    return ip_addresses


def _site_choices(client):
    """Список (slug, name) всех площадок NetBox — для опций фильтра
    «Площадка» в тулбаре. Именно справочник целиком, а не «встречающиеся
    среди адресов», как у фильтра «Роль»: площадка — ключевой разрез
    инфраструктуры, и список должен совпадать с тем, что в настройках
    устройств; площадка без адресов (новая RDC) в фильтре тоже нужна —
    пустой результат по ней сам по себе информация."""
    return [(s["slug"], s["name"]) for s in client.list_sites()]


def _distinct_roles(ip_addresses):
    """Список (slug, name) ролей, реально встречающихся среди адресов с
    назначением — для опций фильтра "Роль" в тулбаре."""
    seen = {ip["_role"]["slug"]: ip["_role"]["name"] for ip in ip_addresses if ip.get("_role")}
    return sorted(seen.items(), key=lambda pair: pair[1].lower())




def _sorted_prefixes(prefixes):
    def sort_key(prefix):
        try:
            return ipaddress.ip_network(prefix["prefix"], strict=False)
        except ValueError:
            return ipaddress.ip_network("0.0.0.0/0")

    return sorted(prefixes, key=sort_key)


def _saved_response(request, client, ip, oob, message, close="close-modal"):
    """Ответ на успешное сохранение: закрыть модалку/панель (close —
    «close-modal» или «close-drawer»), показать тост и сообщить React-таблице
    адресов, что данные изменились (событие ip-addresses-changed → она
    перезагружает /ip/list.json). Раньше сюда рендерилась OOB-строка старой
    DataTables-таблицы — в React-странице её некуда вставлять, параметр oob
    оставлен только ради сигнатуры вызовов."""
    triggers = {"show-toast": {"message": message, "kind": "success"},
                "ip-addresses-changed": True}
    if close:
        triggers[close] = None
    response = HttpResponse("")
    response["HX-Trigger"] = json.dumps(triggers)
    response["HX-Reswap"] = "none"
    return response


def _prefix_context(client, address, prefixes=None):
    """Подсеть адреса для подзаголовков форм: самый специфичный префикс
    NetBox, содержащий адрес, с VLAN (если задан). None — не нашли."""
    try:
        ip_obj = ipaddress.ip_interface(address).ip
    except ValueError:
        return None
    best = None
    for p in (prefixes if prefixes is not None else client.list_prefixes()):
        try:
            net = ipaddress.ip_network(p["prefix"], strict=False)
        except (ValueError, KeyError):
            continue
        if ip_obj in net and (best is None or net.prefixlen > best[0].prefixlen):
            best = (net, p)
    if best is None:
        return None
    vlan = best[1].get("vlan") or {}
    return {"prefix": str(best[0]), "vlan_vid": vlan.get("vid"), "vlan_name": vlan.get("name") or "",
            "description": best[1].get("description") or ""}


def _assignment_display(ip):
    """Текст колонки "Принадлежность": "устройство/ВМ › интерфейс", либо ''
    без назначения — то же самое, что собирает шаблон ip_row.html, но как
    функция для экспорта в Excel."""
    assigned = ip.get("assigned_object")
    if not assigned:
        return ""
    parent = assigned.get("device") or assigned.get("virtual_machine")
    parent_name = f"{parent['name']} › " if parent else ""
    return f"{parent_name}{assigned.get('name', '')}"


EXPORT_COLUMNS = [
    ("Адрес (IP)", lambda ip: ip.get("address", "")),
    ("Статус", lambda ip: (ip.get("status") or {}).get("label", "")),
    ("DNS-имя", lambda ip: ip.get("dns_name") or ""),
    ("Принадлежность", _assignment_display),
    ("Роль", lambda ip: (ip.get("_role") or {}).get("name", "")),
    ("Описание", effective_description),
    ("Тэги", lambda ip: ", ".join(tag["name"] for tag in ip.get("tags") or [])),
    ("CPE", lambda ip: (ip.get("custom_fields") or {}).get("cpe") or ""),
    ("Open Ports", lambda ip: (ip.get("custom_fields") or {}).get("open_ports") or ""),
    ("Guest OS", lambda ip: ip.get("_guest_os") or ""),
    ("OS Guess", lambda ip: (ip.get("custom_fields") or {}).get("os_guess") or ""),
    ("Операционная система", lambda ip: (ip.get("custom_fields") or {}).get("os_installed") or ""),
    ("Назначение", lambda ip: (ip.get("custom_fields") or {}).get("purpose") or ""),
    ("Комментарии", lambda ip: ip.get("comments") or ""),
]


def serialize_ip(ip):
    """Один IP-адрес для /ip/list.json (React-таблица) — плоский JSON-
    объект на строку, поля 1:1 с колонками ip_row.html (data-col-key).
    Переносит на бэкенд то, что раньше считалось в Jinja-фильтрах
    (ip_plan_extras.py) при рендере <td> — React не должен дублировать эту
    бизнес-логику на клиенте. ip уже должен быть пропущен через
    _attach_role/_attach_roles_bulk (иначе "_role"/"_guest_os"/
    "_parent_description" отсутствуют)."""
    role = ip.get("_role")
    custom_fields = ip.get("custom_fields") or {}
    is_vm = bool(role and role.get("slug") == "vm")
    return {
        "id": ip.get("id"),
        "address": ip.get("address", ""),
        "status": {
            "value": (ip.get("status") or {}).get("value", "active"),
            "label": STATUS_LABELS.get((ip.get("status") or {}).get("value"),
                                       (ip.get("status") or {}).get("label", "")),
        },
        "dns_name": ip.get("dns_name") or "",
        "dns_records": custom_fields.get("dns_records") or "",
        "assignment": _assignment_display(ip),
        "role": role,  # {"slug": ..., "name": ...} либо null
        "site": ip.get("_site"),  # {"slug": ..., "name": ...} либо null — производная от устройства/ВМ/подсети
        # "Описание" редактируется инлайн только не-VM (для VM — read-only,
        # заполняется внешней синхронизацией) — см. effective_description/
        # description_hint в ip_plan_extras.py про то же разделение.
        "description": ip.get("description") or "",
        "effective_description": effective_description(ip),
        "description_hint": description_hint(ip),
        "description_editable": not is_vm,
        "tags": [tag["name"] for tag in ip.get("tags") or []],
        "cpe": custom_fields.get("cpe") or "",
        "open_ports": custom_fields.get("open_ports") or "",
        "guest_os": ip.get("_guest_os") or "",
        "os_guess": custom_fields.get("os_guess") or "",
        "os_installed": custom_fields.get("os_installed") or "",
        "purpose": custom_fields.get("purpose") or "",
        "comments": ip.get("comments") or "",
    }


def _sheet_title(prefix_cidr):
    """Имя листа Excel из CIDR: "/" недопустим в именах листов, плюс лимит
    31 символ (с запасом для любого префикса IPv4/IPv6)."""
    return prefix_cidr.replace("/", "_")[:31]


def _group_by_prefix(ip_addresses, prefixes):
    """({prefix_cidr: [ip, ...]}, [ip без подходящего префикса, ...]) —
    префиксы в этом NetBox не перекрываются (проверено), поэтому у каждого
    адреса не больше одного совпадения."""
    networks = [
        (prefix["prefix"], ipaddress.ip_network(prefix["prefix"], strict=False)) for prefix in prefixes
    ]
    grouped = {cidr: [] for cidr, _ in networks}
    unmatched = []
    for ip in ip_addresses:
        address = (ip.get("address") or "").split("/")[0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            unmatched.append(ip)
            continue
        for cidr, network in networks:
            if parsed in network:
                grouped[cidr].append(ip)
                break
        else:
            unmatched.append(ip)
    return grouped, unmatched


def _write_sheet(sheet, ip_addresses):
    headers = [title for title, _ in EXPORT_COLUMNS]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for ip in ip_addresses:
        sheet.append([getter(ip) for _, getter in EXPORT_COLUMNS])

    for column_cells in sheet.columns:
        length = max((len(str(cell.value)) for cell in column_cells if cell.value), default=8)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 60)


def export_excel(request):
    """Выгрузка всех IP-адресов со всеми полями (независимо от текущей
    видимости столбцов в таблице) в .xlsx — отдельный лист на каждый
    префикс (подсеть), плюс лист "Без подсети" для адресов, не попавших ни
    в один известный префикс."""
    try:
        client = NetBoxClient()
        ip_addresses = client.list_ip_addresses()
        prefixes = _sorted_prefixes(client.list_prefixes())
        _attach_roles_bulk(client, ip_addresses)
    except NetBoxAPIError as exc:
        logger.error("Не удалось получить список IP-адресов для экспорта: %s", exc)
        return HttpResponse(f"Не удалось получить данные из NetBox: {exc}", status=502)

    grouped, unmatched = _group_by_prefix(ip_addresses, prefixes)

    workbook = Workbook()
    first = True
    for prefix in prefixes:
        cidr = prefix["prefix"]
        rows = grouped[cidr]
        if not rows:
            continue
        sheet = workbook.active if first else workbook.create_sheet()
        first = False
        sheet.title = _sheet_title(cidr)
        _write_sheet(sheet, rows)

    if unmatched:
        sheet = workbook.active if first else workbook.create_sheet()
        first = False
        sheet.title = "Без подсети"
        _write_sheet(sheet, unmatched)

    if first:
        # Совсем нет данных — лист по умолчанию остаётся пустым, но хотя бы
        # с заголовками, чтобы файл не выглядел сломанным.
        _write_sheet(workbook.active, [])

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    filename = f"ip_plan_{timezone.now():%Y-%m-%d}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    workbook.save(response)
    return response


def set_site(request):
    """Выбор площадки в боковом меню: кладёт slug в cookie и возвращает на
    страницу, с которой пришли (?next=, только относительный путь)."""
    from ip_plan.context_processors import SITE_COOKIE, SITE_ALL, SITE_NONE, site_options_cached
    value = (request.GET.get("site") or SITE_ALL).strip()
    if value not in (SITE_ALL, SITE_NONE) and not any(o["slug"] == value for o in site_options_cached()):
        value = SITE_ALL
    nxt = request.GET.get("next") or ""
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = reverse("ip_plan:ip_list")
    response = redirect(nxt)
    response.set_cookie(SITE_COOKIE, value, max_age=365 * 24 * 3600, samesite="Lax")
    return response


def _acl_devices_for_site(request):
    """Множество имён устройств выбранной площадки (по meta.json снимков) или
    None, если выбраны все площадки."""
    from ip_plan.context_processors import current_site, SITE_ALL, SITE_NONE
    site = current_site(request)
    if site == SITE_ALL:
        return None
    rows = acl_store.snapshot_info()
    if site == SITE_NONE:
        return {r["device"] for r in rows if not r.get("site")}
    return {r["device"] for r in rows if r.get("site") == site}


def _acl_site_context(request):
    """(devices, site_labels, show_site_headers) для раздела ACL: устройства
    выбранной в оболочке площадки (None — все), подписи площадок для меню
    устройств и нужно ли рисовать заголовки площадок (только при «Все»)."""
    from ip_plan.context_processors import current_site, SITE_ALL, site_options_cached
    devices = _acl_devices_for_site(request)
    labels = {}
    for opt in site_options_cached() or []:
        if isinstance(opt, dict) and opt.get("slug"):
            labels[opt["slug"]] = opt.get("name") or opt["slug"]
    return devices, labels, current_site(request) == SITE_ALL


def acl_lookup(request):
    """Главная раздела «ACL» (макет test_des/AclIndex): вопрос «где
    встречается адрес или подсеть» с выбором устройств и каталог списков
    доступа и групп адресов по всем устройствам площадки. ?tab=lists|groups —
    вкладка каталога, ?mode=traffic — вкладка-заглушка «Проверить трафик».
    Данные — только из снимков netbox-sync/config_backups (acl_store,
    acl_search); в NetBox страница не ходит."""
    devices, labels, headers = _acl_site_context(request)
    mode = "traffic" if request.GET.get("mode") == "traffic" else "find"
    tab = "groups" if request.GET.get("tab") == "groups" else "lists"
    # ?device= — выбор устройств переживает переключение вкладок каталога
    # (вкладки — ссылки; aclCatalog дописывает выбор в их href).
    selected = [d for d in request.GET.getlist("device") if d]
    return render(request, "ip_plan/acl_home.html", {
        "question": acl_search.question(devices, "", selected, headers, labels, mode),
        "catalog": acl_search.catalog(devices),
        "snapshot_summary": acl_search.snapshot_summary(devices),
        "tab": tab,
    })


def acl_where(request):
    """Результат «Где встречается адрес» (макет AclWhere): ?q=<ip|cidr>,
    ?device=<имя> (повторяемый; пусто — все устройства площадки). Категории
    совпадения, цепочки «почему совпало» и свёртки широких/any считает
    acl_search.search; фильтры роли и категории — на клиенте."""
    devices, labels, headers = _acl_site_context(request)
    q = (request.GET.get("q") or "").strip()
    if not q:
        return redirect("ip_plan:acl_lookup")
    selected = [d for d in request.GET.getlist("device") if d]
    result, error = None, None
    try:
        result = acl_search.search(q, devices=devices, selected=selected)
    except acl_search.QueryError as exc:
        error = str(exc)
    return render(request, "ip_plan/acl_where.html", {
        "question": acl_search.question(devices, q, selected, headers, labels),
        "result": result,
        "error": error,
        "snapshot_summary": acl_search.snapshot_summary(devices),
    })


def acl_detail(request, device, acl_name):
    """Список правил одного ACL в порядке конфига (макет AclList): ?q=<ip|cidr>
    подсвечивает совпадения, сворачивает блоки без совпадений и даёт
    навигацию «1 из N». Правило отдаётся фрагментами (text/any/group/hit),
    чтобы группы были кликабельны, а совпавшие части подсвечены."""
    listing = acl_search.listing(device, acl_name, request.GET.get("q"))
    if listing is None:
        raise Http404(f"ACL «{acl_name}» на устройстве «{device}» не найден в config_backups")
    devices, labels, headers = _acl_site_context(request)
    return render(request, "ip_plan/acl_list.html", {
        "listing": listing,
        "question": acl_search.question(devices, listing["query"], [device], headers, labels),
        "snapshot_summary": acl_search.snapshot_summary(devices),
    })


def acl_rule_nodes_panel(request, device, acl_name, rule_id, side):
    """HTMX-фрагмент панели узлов ViPNet, перечисленных прямо в правиле
    (side = source|destination) — тот же шаблон, что у панели группы."""
    panel = acl_search.rule_nodes_panel(device, acl_name, rule_id, side, request.GET.get("q"))
    if panel is None:
        raise Http404(f"Правило {rule_id} ({side}) в «{acl_name}» на {device} не найдено")
    return render(request, "ip_plan/_acl_group_panel.html", {"panel": panel})


def acl_group_panel(request, device, name):
    """HTMX-фрагмент правой панели группы адресов (в #drawer-content):
    члены группы, содержащие ?q=, идут первыми."""
    panel = acl_search.group_panel(device, name, request.GET.get("q"))
    if panel is None:
        raise Http404(f"object-group «{name}» не найден на {device}")
    return render(request, "ip_plan/_acl_group_panel.html", {"panel": panel})


def acl_object_group_detail(request, device, name):
    """Полное содержимое одной object-group network (все адреса/подсети,
    которые объединяет группа) + список ACL, где она используется. Та же
    DataTables-таблица + built-in поиск, что и на acl_detail — эти группы
    тоже бывают на тысячи записей (RusNets — ~9200 на каждом ASR)."""
    result = acl_store.get_object_group(device, name)
    if result is None:
        raise Http404(f"object-group «{name}» не найден на {device}")
    return render(request, "ip_plan/acl_object_group_detail.html", result)


def acl_history(request, device):
    """История снимков устройства (все файлы <dev>_<ts>.* из config_backups)
    и diff выбранного снимка с предыдущим того же имени — «что изменилось в
    политике». ?file=<имя файла> выбирает снимок; без него — самый свежий.
    Строки с секретами в diff маскируются (acl_store._scrub)."""
    if acl_store.get_device(device) is None and not acl_store.list_history(device):
        raise Http404(f"Устройство «{device}» не найдено в config_backups")
    history = acl_store.list_history(device)
    selected = request.GET.get("file") or (history[0]["name"] if history else None)
    diff = acl_store.diff_snapshot(device, selected) if selected else None
    return render(request, "ip_plan/acl_history.html", {
        "device": device,
        "history": history,
        "selected": selected,
        "diff": diff,
        "snapshot_rows": acl_store.snapshot_info(device_name=device),
    })


# --- Реестр блокировок регулятора (models.py) ---
# В отличие от ACL Lookup выше — настоящие Django-модели (см. models.py:
# записи накапливаются во времени, нужна история/статусы, а не просто
# зеркало текущего файла на диске). Страница/фильтры — тот же htmx-паттерн,
# что у acl_lookup (один URL на обычный GET и на HX-запрос, hx-push-url).
# Записи (RegulatorEntry) везде, где их вообще показывают — и вкладка
# «Записи» общего списка, и таблица на детальной странице письма —
# React/TanStack island (static_src/regulator_table, один и тот же
# TableRoot с разными параметрами, см. main.jsx/regulator_entries_json):
# один fetch целиком, вся фильтрация/сортировка/пагинация клиентские.
# Мутации (revoke/reactivate/delete) — свои JSON-эндпоинты (_api ниже),
# без модалок и OOB-рендера списка сервером — раньше (до перевода детальной
# страницы письма на этот же компонент) тут был ещё server-rendered путь
# для неё же, с HTML-модалкой снятия и OOB-обновлением всей таблицы;
# убран целиком как дублирующий (см. коммит).

def _regulator_letters_context(request):
    """Контекст вкладки «Письма» — только список писем, без фильтра (см.
    задачу: "там не нужен фильтр, просто список писем"), с сортировкой по
    дате (?sort=document_date|-document_date, по умолчанию свежие сверху).
    Записи сюда не попадают вообще — они переехали в отдельную вкладку
    «Записи» на React-таблице (regulator_entries_json ниже)."""
    sort = request.GET.get("sort", "-document_date")
    if sort not in ("document_date", "-document_date"):
        sort = "-document_date"
    tie_break = "-id" if sort.startswith("-") else "id"
    documents = RegulatorDocument.objects.annotate(
        entries_count=Count("entries"),
        active_count=Count("entries", filter=Q(entries__status="active")),
    ).order_by(sort, tie_break)
    return {"documents": documents, "sort": sort}


def _reg_snapshot_public(snap):
    """Снимок xFW без объектов — для шаблонов."""
    return {k: v for k, v in snap.items() if k not in ("objects", "names")} | {
        "objects": [{"kind": k, "name": o.name, "wanted": o.wanted, "found": o.found, "fallback": o.fallback,
                     "count": len(o.raw)} for k, o in snap["objects"].items()]}


def regulator_list(request):
    """Главная «Блокировки» (макет test_des/Reg): сводные карточки «всё ли
    применено», проверка одного значения (?check=), вкладки ?tab=letters |
    entries | sync. Сверка со снимком объектов xFW — services/regulator_sync."""
    snap = regulator_sync.snapshot()
    index = regulator_sync._value_index()
    lists = regulator_sync.sync_lists(snap, index)
    tab = request.GET.get("tab") if request.GET.get("tab") in ("letters", "entries", "sync") else "letters"
    check_q = (request.GET.get("check") or "").strip()
    context = {
        "tab": tab,
        "snapshot": _reg_snapshot_public(snap),
        "summary": regulator_sync.summary(snap, index, lists),
        "letters": regulator_sync.letters(snap, index),
        "sync": lists,
        "check_query": check_q,
        "check": regulator_sync.check(check_q, snap, index) if check_q else None,
    }
    if tab == "entries":
        entries = RegulatorEntry.objects.select_related("document").order_by("-created_at")
        context["entries"] = regulator_sync.annotate(entries, snap, index)
    return render(request, "ip_plan/reg_home.html", context)


def regulator_commands(request):
    """Мастер «Команды для xFW» (макет RegCmd): что изменится по каждому
    объекту, команды, правила ViPNet, где менять списки, «Отметить, что
    применили»."""
    snap = regulator_sync.snapshot()
    return render(request, "ip_plan/reg_commands.html", {
        "snapshot": _reg_snapshot_public(snap),
        "cmd": regulator_sync.commands(snap),
        "applied": request.GET.get("applied") == "1",
    })


def _regulator_list_legacy(request):
    """Страница «Блокировки регулятора» — тот же принцип, что у acl_lookup:
    один URL на обычный GET и на HTMX (переключение сортировки бьёт сюда же,
    hx-push-url кладёт query-string в адресную строку). HTMX-ветка отдаёт
    только вкладку «Письма» (единственное, что там переключает сортировку) —
    «Записи» подгружает свои данные отдельным JSON-запросом
    (regulator_entries_json, статический React-island), а «Команды для
    xFW» пересчитывается заново при каждой полной загрузке страницы (нужен
    и там, и при HX-Trigger от regulator_firewall_commands_apply — не имеет
    смысла тащить в HTMX-партиал сортировки писем)."""
    context = _regulator_letters_context(request)
    if request.headers.get("HX-Request") == "true":
        return render(request, "ip_plan/_regulator_letters_body.html", context)
    context.update(_regulator_firewall_context())
    return render(request, "ip_plan/regulator_lookup.html", context)


@require_http_methods(["POST"])
def regulator_document_delete(request, document_id):
    """Удаление письма целиком вместе со всеми его записями. FK
    (RegulatorEntry.document) — on_delete=PROTECT, это намеренно оставлено
    как есть на уровне модели (страховка от случайного document.delete() из
    Django admin/shell) — здесь каскад явный и осознанный, только после
    подтверждения в UI (см. _regulator_letters_body.html, confirm с числом
    записей через ru_plural_zapis)."""
    document = get_object_or_404(RegulatorDocument, pk=document_id)
    document.entries.all().delete()
    document.delete()
    context = _regulator_letters_context(request)
    html = render_to_string(
        "ip_plan/_regulator_letters_body.html", {**context, "oob": True}, request=request
    )
    response = HttpResponse(html)
    response["HX-Trigger"] = json.dumps(
        {"show-toast": {"message": "Письмо удалено", "kind": "success"}}
    )
    return response


def _serialize_regulator_entry(entry):
    """Одна запись блокировки для React-таблицы вкладки «Записи» — вся
    фильтрация/поиск/пагинация там клиентские (см. design: "на той же
    основе, на которой сделан основной ip plan" — там тоже один fetch
    целиком и всё дальше на клиенте), так что отдаём плоский список сразу
    со всем нужным для отображения, без пагинации на уровне API."""
    return {
        "id": entry.id,
        "kind": entry.kind,
        "kind_display": entry.get_kind_display(),
        "value": entry.value,
        "status": entry.status,
        "status_display": entry.get_status_display(),
        "revoked_at": entry.revoked_at.isoformat() if entry.revoked_at else None,
        "revoked_reason": entry.revoked_reason,
        "created_at": entry.created_at.isoformat(),
        "document": {
            "id": entry.document_id,
            "reference_no": entry.document.reference_no,
            "document_date": entry.document.document_date.isoformat(),
            "url": reverse("ip_plan:regulator_document_detail", args=[entry.document_id]),
        },
    }


def regulator_entries_json(request):
    """?document=<id> — тот же список, но только записи ОДНОГО письма (см.
    static_src/regulator_table/main.jsx: data-document-id на
    #regulator-entries-root на детальной странице письма). Без параметра —
    как раньше, все письма разом (вкладка «Записи» общего списка)."""
    entries = RegulatorEntry.objects.select_related("document").all()
    document_id = request.GET.get("document")
    if document_id:
        entries = entries.filter(document_id=document_id)
    return JsonResponse({"entries": [_serialize_regulator_entry(e) for e in entries]})


@require_http_methods(["POST"])
def regulator_entry_revoke_api(request, entry_id):
    """JSON-версия прежней HTML-модалки снятия (regulator_entry_revoke +
    regulator_entry_revoke_modal.html, обе убраны) — для React-таблицы на
    детальной странице письма (static_src/regulator_table). Причина снятия
    по-прежнему необязательна, но собирается не формой-модалкой, а
    window.prompt() на клиенте (row-actions.jsx) — тут просто читаем её из
    JSON-тела запроса вместо request.POST (fetchWithCsrf шлёт
    application/json, не form-encoded)."""
    entry = get_object_or_404(RegulatorEntry, pk=entry_id)
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        payload = {}
    # [:255] — сознательно, а не полагаемся на валидацию формы (которой тут
    # больше нет): у RegulatorEntry.revoked_reason max_length=255. SQLite
    # длину VARCHAR не проверяет — без обрезки слишком длинный prompt() тихо
    # сохранился бы длиннее объявленного поля; PostgreSQL проверяет, и без
    # обрезки тот же ввод уронил бы запрос с DataError (500). Обрезка нужна
    # при обоих движках (см. IP_PLAN_DB_ENGINE в config/settings.py).
    entry.status = "revoked"
    entry.revoked_at = timezone.now()
    entry.revoked_reason = (payload.get("revoked_reason") or "").strip()[:255]
    entry.save(update_fields=["status", "match_value", "revoked_at", "revoked_reason"])
    return JsonResponse({"entry": _serialize_regulator_entry(entry)})


@require_http_methods(["POST"])
def regulator_entry_reactivate_api(request, entry_id):
    """JSON-версия regulator_entry_reactivate (убрана вместе с HTML-таблицей
    записей письма) — используется и вкладкой «Записи» общего списка, и
    React-таблицей на детальной странице письма (обе — static_src/
    regulator_table, один и тот же TableRoot)."""
    entry = get_object_or_404(RegulatorEntry, pk=entry_id)
    entry.status = "active"
    entry.revoked_at = None
    entry.revoked_reason = ""
    entry.save(update_fields=["status", "match_value", "revoked_at", "revoked_reason"])
    return JsonResponse({"entry": _serialize_regulator_entry(entry)})


@require_http_methods(["POST"])
def regulator_entry_delete_api(request, entry_id):
    """JSON-версия regulator_entry_delete (убрана вместе с HTML-таблицей
    записей письма) — используется и вкладкой «Записи», и React-таблицей на
    детальной странице письма."""
    entry = get_object_or_404(RegulatorEntry, pk=entry_id)
    entry.delete()
    return JsonResponse({"ok": True, "id": entry_id})


def regulator_document_detail(request, document_id):
    """Детальная страница одного письма — рабочее пространство для ввода
    записей (см. design doc, раздел "детальная страница письма"): строка
    приёма сверху (regulator_entry_quick_add) + таблица только записей
    ЭТОГО письма. Замена прежнему workflow "модалка + выбор письма из
    списка на каждую запись"."""
    document = get_object_or_404(RegulatorDocument, pk=document_id)
    return render(request, "ip_plan/reg_document.html", _reg_document_context(document))


def _reg_document_context(document):
    snap = regulator_sync.snapshot()
    index = regulator_sync._value_index()
    progress = next((x for x in regulator_sync.letters(snap, index) if x["id"] == document.id), None)
    entries = regulator_sync.annotate(document.entries.select_related("document").order_by("-created_at"), snap, index)
    order = {"pending": 0, "revoked_present": 1, "unknown": 2, "applied": 3, "removed": 4, "not_applicable": 5}
    entries.sort(key=lambda e: order.get(e["fw_state"], 9))
    return {"document": document, "progress": progress, "entries": entries,
            "snapshot": _reg_snapshot_public(snap)}


def regulator_document_entries(request, document_id):
    """HTMX-фрагмент записей письма — перерисовка по событию
    regulator-entries-changed (быстрый ввод, импорт, снятие)."""
    document = get_object_or_404(RegulatorDocument, pk=document_id)
    return render(request, "ip_plan/_reg_doc_entries.html", _reg_document_context(document))


def _next_regulator_reference_no():
    """Следующий порядковый номер письма — целое, на 1 больше самого
    большого УЖЕ ЧИСТО ЧИСЛОВОГО reference_no в реестре. Письма, заведённые
    раньше вручную (до автонумерации), могли содержать произвольный текст
    вроде "К-22_38525" — такие в подсчёт не идут, чтобы нечисловое
    старое значение не сломало последовательность для новых писем."""
    numeric_values = [
        int(no) for no in RegulatorDocument.objects.values_list("reference_no", flat=True)
        if no.isdigit()
    ]
    return str(max(numeric_values, default=0) + 1)


@require_http_methods(["POST"])
def regulator_document_add(request):
    """Создание письма — без формы и без модалки: номер (по порядку) и
    дата (сегодня) проставляются автоматически, оператору на этом шаге
    нечего заполнять (см. design doc про переработку создания письма).
    Реальные номер/дата, которые ФСТЭК печатает в самом письме,
    распознаются позже из шапки загруженных файлов (см.
    regulator_document_ioc_parse/_save) и ложатся в примечание — они не
    заменяют это поле, оно остаётся внутренним порядковым номером СИЦ.
    Ручная правка по-прежнему доступна (regulator_document_edit), если
    порядковый номер нужно поправить.

    Успех ведёт сразу на детальную страницу нового письма (HX-Redirect) —
    тот же workflow, что и раньше: добавил письмо → сразу внутри него
    вносишь записи, без промежуточного возврата к общему списку."""
    document = RegulatorDocument.objects.create(
        reference_no=_next_regulator_reference_no(),
        document_date=timezone.localdate(),
        created_by=request.user.get_username(),
    )
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse("ip_plan:regulator_document_detail", args=[document.id])
    return response


def regulator_document_edit(request, document_id):
    """GET/POST — модалка правки письма. Только примечание
    (RegulatorDocumentNoteForm) — номер и дата НЕ редактируются здесь
    вообще: номер выдаётся один раз при создании (по порядку, см.
    _next_regulator_reference_no), дата — момент создания записи в
    реестре, менять их задним числом не должно быть возможности ни у
    кого. Реальные номер/дата письма ФСТЭК распознаются OCR'ом штампа
    (regulator_document_ioc_save) и так и лежат в этом же примечании.

    Успех никуда не уводит: список писем обновляется на месте (OOB, тот
    же приём, что у regulator_document_delete), модалка просто
    закрывается."""
    document = get_object_or_404(RegulatorDocument, pk=document_id)
    if request.method == "POST":
        form = RegulatorDocumentNoteForm(request.POST, instance=document)
        if form.is_valid():
            form.save()
            context = _regulator_letters_context(request)
            html = render_to_string(
                "ip_plan/_regulator_letters_body.html", {**context, "oob": True}, request=request
            )
            response = HttpResponse(html)
            response["HX-Trigger"] = json.dumps(
                {"close-modal": None, "show-toast": {"message": "Письмо изменено", "kind": "success"}}
            )
            return response
        return render(
            request, "ip_plan/regulator_document_edit_modal.html",
            {"form": form, "document": document}, status=400,
        )
    form = RegulatorDocumentNoteForm(instance=document)
    return render(request, "ip_plan/regulator_document_edit_modal.html", {"form": form, "document": document})


def regulator_entry_quick_add(request, document_id):
    """POST — строка приёма на детальной странице письма. document уже
    известен из URL (не поле формы, в отличие от прежнего RegulatorEntryForm
    с общим выпадающим списком) — ключевое отличие от старого regulator_entry_add,
    которое и убирает повторный выбор письма на каждую запись.

    Тип (kind) больше не выбирается вручную (убран kind-toggle, см. design
    doc) — определяется автоматически той же classify_clean_line(), что и
    "чистая" ветка IOC-парсера для CSV/.txt (services/ioc_parser.py):
    оператор печатает голое значение, тип виден только в готовой строке
    таблицы после сохранения. classify_clean_line() лишь ПРЕДПОЛАГАЕТ тип
    (её собственные регулярки мягче) — итоговая строгая проверка по
    выбранному типу всё равно идёт через RegulatorEntryForm.clean(), как и
    раньше, так что заведомо некорректное значение (невалидный IP,
    доменное имя не по формату и т.п.) по-прежнему отклоняется с обычной
    ошибкой валидации.

    Успех — тело ответа пустое (таблица теперь React-island, см.
    static_src/regulator_table — вставлять готовый <tr> некуда), только
    OOB-очистка слота ошибки на случай, если предыдущая попытка её
    показывала, и "regulator-entries-changed" в HX-Trigger — тот же
    механизм, что show-toast/close-modal (см. base.html), React слушает его
    глобально и перезапрашивает список (TableRoot.jsx). Поле ввода/фокус
    сбрасывает сам клиент (hx-on::after-request в regulator_document_detail.html)
    — здесь сервер про это ничего не знает и не должен.

    Ошибка — НЕ трогает таблицу вообще (no main swap), только OOB-обновление
    #quick-add-error рядом со строкой приёма. Значение, которое пользователь
    уже напечатал, остаётся в поле — не наказываем повторным вводом при
    опечатке.

    Значение (тот же тип, без учёта регистра), уже существующее В ЭТОМ ЖЕ
    письме — активное или снятое, неважно — молча пропускается вместо
    создания дубля: тем же принципом, что batch-сохранение из
    regulator_document_ioc_save (см. её докстринг про то, почему не по
    другим письмам и не по match_value). UI-реакция та же, что на успех
    (поле очищается) — с точки зрения оператора нужный результат ("это
    значение есть в письме") уже достигнут, просто отдельным тоастом, не
    "Запись добавлена"."""
    document = get_object_or_404(RegulatorDocument, pk=document_id)
    raw_value = (request.POST.get("value") or "").strip()
    classified = ioc_parser.classify_clean_line(raw_value) if raw_value else None
    if classified is None:
        error_html = render_to_string(
            "ip_plan/_regulator_quick_add_error.html", {"error": "Введите значение", "oob": True}, request=request
        )
        return HttpResponse(error_html, status=400)
    parser_kind, classified_value, _needs_review, _note = classified
    data = request.POST.copy()
    data["document"] = str(document.id)
    data["kind"] = _IOC_KIND_TO_ENTRY_KIND[parser_kind]
    data["value"] = classified_value
    form = RegulatorEntryForm(data)
    if form.is_valid():
        dup_kind = form.cleaned_data["kind"]
        dup_value = form.cleaned_data["value"]
        if RegulatorEntry.objects.filter(document=document, kind=dup_kind, value__iexact=dup_value).exists():
            error_html = render_to_string(
                "ip_plan/_regulator_quick_add_error.html", {"error": None, "oob": True}, request=request
            )
            response = HttpResponse(error_html)
            response["HX-Trigger"] = json.dumps({
                "clear-quick-add": None,
                "show-toast": {"message": "Уже есть в этом письме — не задвоено", "kind": "info"},
            })
            return response
        form.save()
        error_html = render_to_string(
            "ip_plan/_regulator_quick_add_error.html", {"error": None, "oob": True}, request=request
        )
        response = HttpResponse(error_html)
        response["HX-Trigger"] = json.dumps({
            "clear-quick-add": None,
            "show-toast": {"message": "Запись добавлена", "kind": "success"},
            "regulator-entries-changed": True,
        })
        return response
    error = " ".join(form.errors.get("value") or form.errors.get("kind") or ["Проверьте значение"])
    error_html = render_to_string(
        "ip_plan/_regulator_quick_add_error.html", {"error": error, "oob": True}, request=request
    )
    return HttpResponse(error_html, status=400)


# Раньше здесь была отдельная загрузка CSV (одна колонка, один тип на
# весь файл, тот же radio, что у строки приёма) — regulator_entry_csv_upload
# и её error-хелпер _regulator_csv_error. Убрана: общая загрузка файлов
# в "Разборе документов" ниже (regulator_document_ioc_parse) уже
# поддерживает CSV/.txt с автоопределением типа построчно, дублировать
# незачем (см. docs/plans/2026-09-07-ioc-parser-fstec-corpus-audit.md).

# Порядок вывода типов в превью-таблице разбора документов — тот же, что и
# везде в UI (IP/FQDN/URL/IOC), не порядок обнаружения в тексте. sha256/
# sha1/md5 идут подряд — все три отображаются одинаково как "IOC" на
# уровне записи реестра (см. _IOC_KIND_TO_ENTRY_KIND), порядок между ними
# внутри превью не имеет значения.
_IOC_KIND_ORDER = ["ip", "domain", "url", "sha256", "sha1", "md5"]
# ioc_parser использует "domain"/"sha256"/"sha1"/"md5" (говорящие имена
# внутри самого парсера, не привязанные к модели), RegulatorEntry —
# "fqdn"/"hash" (уже устоявшиеся значения kind в остальном приложении, см.
# models.py) — все три алгоритма хэша ложатся в один и тот же "hash"
# ("IOC"), в реестре они не различаются. Только на этой границе (парсер ->
# реестр) и нужно сопоставление.
_IOC_KIND_TO_ENTRY_KIND = {
    "ip": "ip", "domain": "fqdn", "url": "url",
    "sha256": "hash", "sha1": "hash", "md5": "hash",
}


def regulator_document_ioc_parse(request, document_id):
    """POST — разбор загруженных PDF/DOCX/CSV на детальной странице письма (см.
    ip_plan/services/ioc_parser.py и design doc). Файлы НЕ сохраняются на
    диск — parse_file() читает каждый прямо из памяти (Django
    UploadedFile — тот же файлоподобный объект, что pdfplumber/python-docx
    принимают напрямую) и тут же забывает; на выходе только СТРУКТУРИРОВАННЫЙ
    список кандидатов, ничего не пишется в RegulatorEntry — это отдельный
    шаг (regulator_document_ioc_save), оператор сначала правит список.

    request.FILES.getlist('ioc_files') — одно и то же имя поля что для
    обычного multi-file input, что для input с webkitdirectory (выбор папки
    целиком) — с точки зрения Django оба варианта неотличимы, оба отдают
    плоский список файлов, поэтому один view обслуживает и "выбрать файлы",
    и "выбрать папку" из брифа.

    Заодно (только для .pdf) — OCR штампа "дата + № письма" из шапки
    бланка ФСТЭК (см. ioc_parser.extract_letter_header_stamp) —
    результат ("09.04.2026 № 240/92/2320" и т.п.) идёт не в indicators, а
    отдельным списком stamp_values в превью: оператор видит его ДО
    сохранения и может поправить/убрать (это OCR по стилизованному
    шрифту, не эталон), а при сохранении (regulator_document_ioc_save)
    значения ложатся в примечание письма через запятую. f.seek(0) перед
    вызовом обязателен — parse_file() уже дочитал этот же файловый объект
    до конца, pdfplumber.open() сам не перематывает поток."""
    document = get_object_or_404(RegulatorDocument, pk=document_id)
    files = request.FILES.getlist("ioc_files")
    if not files:
        return render(
            request, "ip_plan/_regulator_ioc_preview.html",
            {"document": document, "error": "Файлы не выбраны."},
        )

    indicators = {}
    warnings = []
    stamp_values = []
    for f in files:
        warning = ioc_parser.parse_file(f.name, f, indicators)
        if warning:
            warnings.append(warning)
        if f.name.lower().endswith(".pdf"):
            f.seek(0)
            stamp = ioc_parser.extract_letter_header_stamp(f)
            if stamp and stamp not in stamp_values:
                stamp_values.append(stamp)

    rows = []
    for kind in _IOC_KIND_ORDER:
        for ind in sorted(indicators.get(kind, {}).values(), key=lambda i: i.value):
            rows.append({
                "entry_kind": _IOC_KIND_TO_ENTRY_KIND[kind],
                "value": ind.value,
                "value_defanged": ind.value_defanged,
                "occurrences": ind.occurrences,
                "source_files": sorted(ind.source_files),
                "needs_review": ind.needs_review,
                "note": ind.note,
            })

    # Для экрана «Проверьте, что распознано»: дубль в этом письме (снят по
    # умолчанию) и совпадения с другими письмами (подсвечены).
    by_mv = {}
    for e in RegulatorEntry.objects.select_related("document").all():
        by_mv.setdefault((e.kind if e.kind != "url" else "fqdn", e.match_value), []).append(e)
    for row in rows:
        k = row["entry_kind"]
        mv = RegulatorEntry.compute_match_value(k, row["value"])
        found = by_mv.get((k if k != "url" else "fqdn", mv), [])
        row["dup_here"] = any(e.document_id == document.id for e in found)
        row["other_letters"] = sorted({(e.document_id, e.document.reference_no) for e in found
                                       if e.document_id != document.id})
        row["selected"] = not row["dup_here"]
    return render(request, "ip_plan/_regulator_ioc_preview.html", {
        "document": document,
        "rows": rows,
        "summary": {"found": len(rows), "dups": sum(r["dup_here"] for r in rows),
                    "elsewhere": sum(bool(r["other_letters"]) for r in rows),
                    "selected": sum(r["selected"] for r in rows), "files": [f.name for f in files]},
        "warnings": warnings,
        "kind_choices": RegulatorEntry.KIND_CHOICES,
        "stamp_values": stamp_values,
    })


def regulator_document_ioc_save(request, document_id):
    """POST — сохранение отредактированного оператором превью (см.
    regulator_document_ioc_parse) в реестр. ioc_kind[]/ioc_value[] —
    ПАРАЛЛЕЛЬНЫЕ массивы (одноимённые поля формы, i-е значение kind
    соответствует i-му value — это гарантирует порядок полей в самой
    форме/DOM, см. _regulator_ioc_preview.html: в каждой строке сначала
    select "kind", потом input "value", браузер сериализует поля формы в
    DOM-порядке, так что getlist() возвращает оба списка в одном и том же
    порядке независимо от того, сколько строк оператор удалил кнopкой
    «Убрать» до отправки).

    Как и CSV-загрузка — хотя бы одна невалидная строка ПОСЛЕ правок
    оператором отклоняет ВЕСЬ список целиком (ничего не сохраняется),
    ошибки показываются построчно в той же превью-форме, ничего не
    трогая — можно поправить и отправить снова.

    Строки, совпадающие (тип + значение, без учёта регистра) с уже
    существующей в ЭТОМ ЖЕ письме записью — активной или снятой, неважно —
    молча пропускаются, а не отклоняются как ошибка: реальный сценарий,
    из-за которого это появилось — та же папка/файлы загружены в письмо
    повторно (по невнимательности или чтобы дозаписать пропущенное),
    раньше это просто задваивало (а при третьей попытке — утраивало) все
    записи. Значение, уже встречавшееся в ДРУГОМ письме, не считается
    дублем — регулятор может законно упомянуть один и тот же IOC в
    нескольких письмах, это не ошибка, а обычная переписка. Сравниваем по
    ПОЛНОМУ value, не по match_value — у kind=url match_value обрезан до
    хоста (см. RegulatorEntry.compute_match_value), а разные payload-URL на
    одном хосте (типичный случай в письмах регулятора: .../client32.ini и
    .../client32.exe на одном сервере) — это разные IOC, не дубли.

    stamp_value[] — штампы "дата + № письма", распознанные OCR на шаге
    разбора (см. regulator_document_ioc_parse) и показанные оператору
    РЕДАКТИРУЕМЫМИ полями в той же превью-форме: сюда приходит то, что
    оператор реально оставил/поправил, а не сырой результат OCR. Можно
    сохранять сами по себе, без единой строки IOC (например, письмо
    оказалось административным, без индикаторов, но штамп всё равно стоит
    занести) — поэтому "нечего сохранять" проверяется по ОБОИМ спискам
    сразу, не только по kinds/values."""
    document = get_object_or_404(RegulatorDocument, pk=document_id)
    kinds = request.POST.getlist("ioc_kind")
    values = request.POST.getlist("ioc_value")
    stamp_values = [v.strip() for v in request.POST.getlist("stamp_value") if v.strip()]

    if not kinds and not stamp_values:
        return render(
            request, "ip_plan/_regulator_ioc_preview.html",
            {"document": document, "error": "Список пуст — нечего сохранять.", "oob": True},
            status=400,
        )

    if len(kinds) != len(values):
        return render(
            request, "ip_plan/_regulator_ioc_preview.html",
            {"document": document, "error": "Список пуст или повреждён — попробуйте разобрать документы заново.", "oob": True},
            status=400,
        )

    errors = []
    validated = []
    for i, (kind, value) in enumerate(zip(kinds, values), start=1):
        form = RegulatorEntryForm(data={"document": document.id, "kind": kind, "value": value})
        if form.is_valid():
            validated.append((form.cleaned_data["kind"], form.cleaned_data["value"]))
        else:
            msg = " ".join(form.errors.get("value") or form.errors.get("kind") or ["некорректное значение"])
            errors.append(f"Строка {i}: «{value}» — {msg}")

    if errors:
        return render(
            request, "ip_plan/_regulator_ioc_preview.html",
            {
                "document": document, "errors": errors,
                # Возвращаем то же самое (уже отредактированное оператором)
                # содержимое обратно в форму — без этого пришлось бы
                # заново разбирать документы после каждой опечатки. Штампы
                # (если были) — туда же, чтобы повторная отправка после
                # исправления опечатки в IOC не потеряла их попутно.
                "rows": [
                    {"entry_kind": k, "value": v, "value_defanged": None,
                     "occurrences": None, "source_files": [], "needs_review": False, "note": ""}
                    for k, v in zip(kinds, values)
                ],
                "kind_choices": RegulatorEntry.KIND_CHOICES,
                "stamp_values": stamp_values,
                "oob": True,
            },
            status=400,
        )

    # (kind, value.lower()) уже в этом письме — и то, что реально лежит в
    # БД, и то, что мы вот-вот создадим в этом же цикле (превью — форма, её
    # можно отредактировать так, что две строки совпадут между собой, не
    # только с уже сохранённым).
    existing_keys = {
        (kind, value.lower())
        for kind, value in RegulatorEntry.objects.filter(document=document).values_list("kind", "value")
    }
    created = []
    skipped_existing = 0
    for kind, value in validated:
        key = (kind, value.lower())
        if key in existing_keys:
            skipped_existing += 1
            continue
        existing_keys.add(key)
        entry = RegulatorEntry(document=document, kind=kind, value=value)
        entry.save()
        created.append(entry)

    # Штампы "дата + № письма" — в примечание письма через запятую, не
    # дублируя уже присутствующие там значения (повторная загрузка того же
    # файла/письма не должна плодить одну и ту же строку в note бесконечно).
    note_updated = False
    if stamp_values:
        existing_note = document.note or ""
        new_stamp_values = [v for v in stamp_values if v not in existing_note]
        if new_stamp_values:
            document.note = (existing_note + ", " if existing_note else "") + ", ".join(new_stamp_values)
            document.save(update_fields=["note"])
            note_updated = True

    # Таблица записей — React-island (см. её докстринг выше про
    # "regulator-entries-changed"): готовые <tr> вставлять больше некуда,
    # только OOB-очистка превью, а созданные записи React подхватит сам
    # перезапросом по тому же триггеру.
    clear_preview_html = render_to_string(
        "ip_plan/_regulator_ioc_preview.html", {"document": document, "rows": [], "oob": True}, request=request
    )
    # hx-swap="none" на самой форме (см. _regulator_ioc_preview.html) — весь
    # "основной" ответ игнорируется, htmx обрабатывает только OOB-фрагменты,
    # поэтому можно просто склеить второй такой фрагмент следом, не заводя
    # отдельный запрос за свежим текстом примечания.
    note_oob_html = (
        render_to_string("ip_plan/_regulator_document_note.html", {"document": document, "oob": True}, request=request)
        if note_updated else ""
    )
    message_parts = []
    if created or skipped_existing:
        if skipped_existing:
            message_parts.append(f"Сохранено новых: {len(created)}; уже было в этом письме: {skipped_existing}")
        else:
            message_parts.append(f"Сохранено записей: {len(created)}")
    if note_updated:
        message_parts.append("данные из шапки добавлены в примечание")
    message = "; ".join(message_parts) if message_parts else "Сохранено"
    response = HttpResponse(clear_preview_html + note_oob_html)
    response["HX-Trigger"] = json.dumps(
        {"show-toast": {"message": message, "kind": "success"}, "regulator-entries-changed": True}
    )
    return response


# regulator_entry_revoke/_reactivate/_delete (HTML, с модалкой и OOB-
# рендером всей таблицы сервером) убраны — таблица записей письма теперь
# React (см. комментарий над _regulator_letters_context выше), снятие/
# восстановление/удаление идут через JSON-эндпоинты (_api, см. выше:
# regulator_entry_revoke_api/_reactivate_api/_delete_api).


def _regulator_firewall_context():
    """Контекст вкладки «Команды для xFW» — две готовые CLI-команды
    (firewall ip-object add): одна создаёт новый список IP, другая — новый
    список FQDN (с доменом из URL-записей — firewall фильтрует только по
    DNS/IP, не по path, см. RegulatorEntry.match_value).

    Значения — ОБЪЕДИНЕНИЕ двух источников, не только реестр: (1) все
    активные записи RegulatorEntry и (2) то, что реально уже применено на
    firewall прямо сейчас (снимок netbox-sync, см. firewall_blocklist.py).
    Без пункта 2 значения, попавшие на firewall в обход реестра (руками,
    до того как реестр начали вести, или кем-то ещё) молча выпали бы при
    следующей замене списка — а мы создаём НОВЫЙ список целиком, не патчим
    старый построчно, так что пропущенное в новом списке значение
    равносильно случайной разблокировке.

    Почему НОВЫЙ список, а не правка старого: на xFW 5000 под обычным SSH-
    логином нет команды редактирования уже созданного ip-object'а — только
    firewall object show (проверено на живом устройстве, firewall ip-object ?
    отдаёт только "show"). Реальный workflow: скопировать команду в
    консоль устройства (нужен admin-режим/enable, свой пароль), затем
    применить новый список вместо старого через веб-интерфейс ViPNet —
    та часть по SSH недоступна вообще, только руками.

    Вынесено в отдельную функцию (раньше — тело regulator_firewall_commands,
    модалки): теперь эти же данные нужны в двух местах — при обычном
    рендере вкладки «Команды для xFW» (regulator_list) и при OOB-обновлении
    после «Отметить применённым» (regulator_firewall_commands_apply)."""
    ip_obj, _ = FirewallObjectName.objects.get_or_create(kind="ip", defaults={"current_name": "BLOCK_BY_IP_1"})
    fqdn_obj, _ = FirewallObjectName.objects.get_or_create(kind="fqdn", defaults={"current_name": "BBDNS_0"})

    registry_ip = set(
        RegulatorEntry.objects.filter(kind="ip", status="active").values_list("match_value", flat=True)
    )
    registry_fqdn = set(
        RegulatorEntry.objects.filter(kind__in=["fqdn", "url"], status="active").values_list("match_value", flat=True)
    )

    # IP регистронезависимости не касается, но .strip() на всякий случай —
    # снимок пришёл из парсинга сырого текстового вывода устройства
    # (netbox_firewall_blocklist_sync.py), лишний пробел не исключён.
    # Домены — приводим к тому же регистру, что и match_value в реестре
    # (RegulatorEntry.compute_match_value уже делает .lower()), иначе
    # "Example.com" с firewall и "example.com" из письма задвоились бы в
    # объединении вместо дедупликации.
    current_ip = firewall_blocklist.get_current_values(ip_obj.current_name)
    current_fqdn = firewall_blocklist.get_current_values(fqdn_obj.current_name)
    current_ip_set = {v.strip() for v in current_ip} if current_ip is not None else set()
    current_fqdn_set = {v.strip().lower() for v in current_fqdn} if current_fqdn is not None else set()

    ip_values = sorted(registry_ip | current_ip_set)
    fqdn_values = sorted(registry_fqdn | current_fqdn_set)

    ip_next_name = ip_obj.next_name()
    fqdn_next_name = fqdn_obj.next_name()

    context = {
        "ip_obj": ip_obj,
        "fqdn_obj": fqdn_obj,
        "ip_next_name": ip_next_name,
        "fqdn_next_name": fqdn_next_name,
        "ip_count": len(ip_values),
        "fqdn_count": len(fqdn_values),
        "ip_current_available": current_ip is not None,
        "fqdn_current_available": current_fqdn is not None,
        "snapshot_mtime": firewall_blocklist.snapshot_mtime(),
    }
    if ip_values:
        context["ip_command"] = f"firewall ip-object add name @{ip_next_name} {','.join(ip_values)}"
    if fqdn_values:
        context["fqdn_command"] = f"firewall ip-object add name @{fqdn_next_name} {','.join(fqdn_values)}"
    return context


@require_http_methods(["POST"])
def regulator_firewall_commands_apply(request):
    """POST — "Отметить применённым": администратор реально создал и
    применил оба новых списка через веб-интерфейс ViPNet — продвигает
    current_name на то имя, что было показано на вкладке (ip_next_name/
    fqdn_next_name из скрытых полей формы, НЕ пересчитываем заново здесь:
    если между открытием вкладки и нажатием этой кнопки кто-то добавил
    новую активную запись, свежий next_name() уже успел бы уйти на шаг
    дальше — отметка "применено" продвинула бы current_name на имя, которое
    администратор на самом деле не создавал).

    Ответ — OOB-обновление всей вкладки (свежие ip_next_name/fqdn_next_name
    после продвижения current_name), тот же паттерн, что у
    _regulator_saved_response — но без close-modal: это больше не модалка,
    отдельная вкладка «Команды для xFW»."""
    ip_next_name = request.POST.get("ip_next_name")
    fqdn_next_name = request.POST.get("fqdn_next_name")
    if ip_next_name:
        FirewallObjectName.objects.update_or_create(kind="ip", defaults={"current_name": ip_next_name})
    if fqdn_next_name:
        FirewallObjectName.objects.update_or_create(kind="fqdn", defaults={"current_name": fqdn_next_name})
    response = HttpResponse("")
    response["HX-Redirect"] = reverse("ip_plan:regulator_commands") + "?applied=1"
    return response


@login_not_required
@ensure_csrf_cookie
def csrf_refresh(request):
    """Лёгкий пинг без обращения к NetBox — продлевает CSRF-cookie на открытой
    надолго странице. login_not_required нужен, чтобы LoginRequiredMiddleware
    не делал redirect на /login/ и cookie действительно обновлялась."""
    return JsonResponse({"ok": True})


@ensure_csrf_cookie
def ip_list(request):
    """Главная страница — только оболочка (hero/тулбар-root/модалки).
    Сам список IP-адресов больше не запрашивается тут: React дёргает
    /ip/list.json (ip_list_json) сам при монтировании, поэтому запрос к
    NetBox здесь был бы лишним двойным походом на каждую загрузку
    страницы. Ошибки недоступности NetBox теперь показываются внутри
    #ip-table-root (см. TableRoot.jsx), не на уровне этой view."""
    return render(
        request,
        "ip_plan/ip_table.html",
        {
            "os_catalog_json": os_catalog.catalog_json(os_catalog.grouped_entries()),
            "column_info_json": COLUMN_INFO_JSON,
        },
    )


@ensure_csrf_cookie
def ip_list_json(request):
    """JSON-источник данных для React-таблицы (docs/plans/2026-09-03-ip-
    table-react-design.md). Дублирует выборку ip_list() — сознательно
    отдельная view, а не JSON-режим той же: ip_list остаётся HTML-
    первичной загрузкой страницы (hero/тулбар/root-div), эта — тем, что
    React дёргает fetch'ем при монтировании и может дёргать повторно
    (напр. после закрытия модалки добавления/редактирования) без
    перезагрузки всей страницы."""
    try:
        client = NetBoxClient()
        ip_addresses = client.list_ip_addresses()
        prefixes = _sorted_prefixes(client.list_prefixes())
        _attach_roles_bulk(client, ip_addresses)
        _attach_sites_from_prefixes(ip_addresses, prefixes)
    except NetBoxAPIError as exc:
        logger.error("Не удалось получить список IP-адресов/подсетей (JSON): %s", exc)
        return JsonResponse({"error": str(exc)}, status=502)

    return JsonResponse({
        "results": [serialize_ip(ip) for ip in ip_addresses],
        # Подсети — объектами (01.10.2026): меню «Фильтр» показывает VLAN и
        # описание рядом с CIDR, модалка добавления — подзаголовок «в подсети …».
        "prefixes": [{
            "prefix": p["prefix"],
            "vlan": ({"vid": p["vlan"].get("vid"), "name": p["vlan"].get("name") or ""}
                     if p.get("vlan") else None),
            "description": p.get("description") or "",
            "site": ({"slug": p["scope"].get("slug"), "name": p["scope"].get("name")}
                     if p.get("scope_type") == "dcim.site" and p.get("scope") else None),
        } for p in prefixes],
        "status_choices": STATUS_CHOICES,
        "role_choices": _distinct_roles(ip_addresses),
        "site_choices": _site_choices(client),
    })


def help_modal(request):
    """Справка по работе с таблицей (статический контент, без обращений к NetBox)."""
    return render(request, "ip_plan/help_modal.html")


def sync_log_summary(request):
    """Сводка по последнему прогону netbox-sync/full_sync.sh — для блока
    в dropdown "Настройки" (см. sync_log.py, оттуда же формат ответа)."""
    return JsonResponse(sync_log.get_summary())


def sync_log_modal(request):
    """Полный текст последнего лога netbox-sync — модалка по кнопке
    "Развернуть лог" в том же dropdown."""
    log_file, text, truncated = sync_log.get_raw_text()
    return render(request, "ip_plan/sync_log_modal.html", {
        "log_file": log_file,
        "log_text": text,
        "truncated": truncated,
    })


def _os_catalog_label(category, type_):
    return f"{os_catalog.CATEGORY_LABELS[category]} — {os_catalog.TYPE_LABELS[type_]}"


def os_catalog_manage(request):
    """Полная страница управления справочником: слева фиксированная
    навигация (категория x тип), справа — карточки записей выбранного узла."""
    tree = os_catalog.load_tree()
    nav_items = [
        {
            "category": cat,
            "type": t,
            "label": _os_catalog_label(cat, t),
            "count": len(tree[cat][t]),
        }
        for cat in os_catalog.CATEGORIES
        for t in os_catalog.TYPES
    ]
    return render(request, "ip_plan/os_catalog_manage.html", {"nav_items": nav_items})


def os_catalog_entries(request, category, type_):
    """GET — карточки записей одного узла (категория x тип) для правой панели."""
    entries = os_catalog.load_tree().get(category, {}).get(type_, [])
    return render(
        request,
        "ip_plan/_os_catalog_entries.html",
        {"category": category, "type": type_, "label": _os_catalog_label(category, type_), "entries": entries},
    )


def os_catalog_entry_form(request, category, type_, entry_id=None):
    """GET — форма добавления (entry_id=None) или редактирования записи."""
    entry = (entry_id and os_catalog.get_entry(category, type_, entry_id)) or {
        "id": None, "name": "", "versions": [], "certification": "",
    }
    return render(
        request,
        "ip_plan/_os_catalog_entry_form.html",
        {"category": category, "type": type_, "entry": entry},
    )


@require_http_methods(["POST"])
def os_catalog_entry_save(request, category, type_, entry_id=None):
    """POST — создать (entry_id=None) или обновить запись, вернуть обновлённые карточки."""
    name = request.POST.get("name", "").strip()
    versions = [v.strip() for v in request.POST.get("versions", "").split(",") if v.strip()]
    certification = request.POST.get("certification", "").strip()

    if not name:
        entry = {"id": entry_id, "name": name, "versions": versions, "certification": certification}
        return render(
            request,
            "ip_plan/_os_catalog_entry_form.html",
            {"category": category, "type": type_, "entry": entry, "error": "Название обязательно"},
            status=400,
        )

    os_catalog.upsert_entry(category, type_, entry_id, name, versions, certification)
    entries = os_catalog.load_tree()[category][type_]
    return render(
        request,
        "ip_plan/_os_catalog_entries.html",
        {"category": category, "type": type_, "label": _os_catalog_label(category, type_), "entries": entries},
    )


@require_http_methods(["POST"])
def os_catalog_entry_delete(request, category, type_, entry_id):
    """POST — удалить запись, вернуть обновлённые карточки."""
    os_catalog.delete_entry(category, type_, entry_id)
    entries = os_catalog.load_tree()[category][type_]
    return render(
        request,
        "ip_plan/_os_catalog_entries.html",
        {"category": category, "type": type_, "label": _os_catalog_label(category, type_), "entries": entries},
    )


def ip_add(request):
    """GET — форма добавления (для модального окна), POST — создание записи в NetBox."""
    if request.method == "POST":
        form = IPAddressForm(request.POST)
        client = NetBoxClient()
        if form.is_valid():
            try:
                tag_ids = client.resolve_tags(form.cleaned_data["tags"])
                payload = _build_payload(form.cleaned_data, tag_ids)
                ip = client.create_ip_address(payload)
            except NetBoxAPIError as exc:
                logger.error("Не удалось создать IP-адрес: %s", exc)
                form.add_error(None, str(exc))
            else:
                if request.POST.get("add_another"):
                    # Галочка «Добавить ещё один»: модалка остаётся, форма
                    # очищается (статус и подсеть сохраняются), таблица
                    # перечитывается событием ip-addresses-changed.
                    fresh = IPAddressForm(initial={"status": form.cleaned_data["status"]})
                    response = render(request, "ip_plan/ip_add_modal.html",
                                      _add_context(client, fresh, request.POST.get("prefix", ""),
                                                   request.POST.get("free", ""), add_another=True))
                    response["HX-Trigger"] = json.dumps({
                        "ip-addresses-changed": True,
                        "show-toast": {"message": "IP-адрес добавлен", "kind": "success"}})
                    return response
                return _saved_response(
                    request, client, ip, "beforeend:#ip-table-body", "IP-адрес добавлен"
                )
        return render(request, "ip_plan/ip_add_modal.html",
                      _add_context(client, form, request.POST.get("prefix", ""),
                                   request.POST.get("free", "")), status=400)

    # ?address= — предзаполнение из «Занять первый» у строки свободного
    # диапазона; ?prefix= и ?free= (до 8 свободных адресов через запятую) —
    # подзаголовок «в подсети …» и чипы «Свободные» в модалке.
    initial = {"status": "active"}
    prefill_address = request.GET.get("address")
    if prefill_address:
        initial["address"] = prefill_address
    form = IPAddressForm(initial=initial)
    return render(request, "ip_plan/ip_add_modal.html",
                  _add_context(NetBoxClient(), form, request.GET.get("prefix", ""),
                               request.GET.get("free", "")))


def _add_context(client, form, prefix, free, add_another=False):
    free_list = [a.strip() for a in (free or "").split(",") if a.strip()][:8]
    prefix_ctx = None
    if prefix:
        try:
            prefix_ctx = _prefix_context(client, prefix.split("/")[0] + "/32",
                                         client.list_prefixes())
        except NetBoxAPIError:
            prefix_ctx = {"prefix": prefix, "vlan_vid": None, "vlan_name": "", "description": ""}
    return {"form": form, "prefix_ctx": prefix_ctx, "prefix": prefix,
            "free_list": json.dumps(free_list), "free_csv": ",".join(free_list),
            "status_choices": STATUS_CHOICES, "add_another": add_another}


def ip_edit_modal(request, ip_id):
    """GET — форма редактирования текущего IP-адреса (для модального окна)."""
    try:
        ip = NetBoxClient().get_ip_address(ip_id)
    except NetBoxAPIError as exc:
        logger.error("Не удалось загрузить IP-адрес %s: %s", ip_id, exc)
        return render(request, "ip_plan/_modal_error.html", {"error": str(exc)})

    form = IPAddressForm(initial=_ip_to_initial(ip))
    return render(request, "ip_plan/ip_edit_panel.html", _edit_context(form, ip_id, ip))


def _edit_context(form, ip_id, ip):
    """Контекст правой панели карточки адреса (ip_edit_panel.html)."""
    client = NetBoxClient()
    try:
        _attach_role(client, ip)
    except NetBoxAPIError:
        pass
    try:
        prefix_ctx = _prefix_context(client, ip.get("address", ""))
    except NetBoxAPIError:
        prefix_ctx = None
    assigned = ip.get("assigned_object") or {}
    device = (assigned.get("device") or assigned.get("virtual_machine") or {}).get("name")
    custom_fields = ip.get("custom_fields") or {}
    address = ip.get("address", "")
    return {
        "form": form,
        "ip_id": ip_id,
        "address": address,
        "address_ip": address.split("/")[0],
        "address_mask": address.split("/")[1] if "/" in address else "",
        "status_value": (ip.get("status") or {}).get("value", "active"),
        "status_choices": STATUS_CHOICES,
        "status_labels_json": json.dumps(STATUS_LABELS, ensure_ascii=False),
        "dns_name": ip.get("dns_name") or "",
        "site": ip.get("_site"),
        "device_name": device or "",
        "interface_name": assigned.get("name") or "",
        "prefix_ctx": prefix_ctx,
        "scan": {
            "open_ports": [p.strip() for p in (custom_fields.get("open_ports") or "").split(",") if p.strip()],
            "os_guess": custom_fields.get("os_guess") or "",
            "cpe": custom_fields.get("cpe") or "",
            "dns_records": custom_fields.get("dns_records") or "",
        },
    }


def ip_save(request, ip_id):
    """POST — полное сохранение формы редактирования (модалка) с проксированием в NetBox."""
    client = NetBoxClient()
    form = IPAddressForm(request.POST)
    if form.is_valid():
        try:
            tag_ids = client.resolve_tags(form.cleaned_data["tags"])
            payload = _build_payload(form.cleaned_data, tag_ids)
            ip = client.update_ip_address(ip_id, payload)
        except NetBoxAPIError as exc:
            logger.error("Не удалось обновить IP-адрес %s: %s", ip_id, exc)
            form.add_error(None, str(exc))
        else:
            return _saved_response(request, client, ip, "true", "IP-адрес сохранён",
                                   close="close-drawer")

    try:
        current = client.get_ip_address(ip_id)
    except NetBoxAPIError:
        current = {"address": form.data.get("address", ""), "status": {"value": form.data.get("status", "active")}}
    return render(request, "ip_plan/ip_edit_panel.html", _edit_context(form, ip_id, current), status=400)


@require_http_methods(["PATCH"])
def ip_patch(request, ip_id):
    """PATCH — точечное inline-сохранение одного поля ячейки таблицы (JSON in/out)."""
    try:
        data = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Некорректный JSON в теле запроса"}, status=400)

    patch_data = {k: v for k, v in data.items() if k in PATCHABLE_FIELDS}
    if not patch_data:
        return JsonResponse({"error": "Нет разрешённых для inline-правки полей"}, status=400)

    payload = {}
    custom_fields = {}
    for key, value in patch_data.items():
        if key in INLINE_CUSTOM_FIELDS:
            custom_fields[key] = value or None
        else:
            payload[key] = value
    if custom_fields:
        payload["custom_fields"] = custom_fields

    try:
        ip = NetBoxClient().update_ip_address(ip_id, payload)
    except NetBoxAPIError as exc:
        logger.error("Inline-патч IP %s не выполнен: %s", ip_id, exc)
        return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)

    return JsonResponse({"ok": True, "ip": ip})


@require_http_methods(["DELETE"])
def ip_delete(request, ip_id):
    """DELETE — удаление IP-адреса (пункт «Удалить» в меню действий строки)."""
    try:
        NetBoxClient().delete_ip_address(ip_id)
    except NetBoxAPIError as exc:
        logger.error("Не удалось удалить IP-адрес %s: %s", ip_id, exc)
        return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)

    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def ip_console(request, ip_id):
    """POST — консоль виртуальной машины, которой назначен адрес: файл .vv
    (RedVirt/oVirt, remote-viewer) или ссылка vmrc:// (vCenter). Логика и
    причины отказов — services/vm_console.py."""
    try:
        client = NetBoxClient()
        ip = client.get_ip_address(ip_id)
        vm_ref = (ip.get("assigned_object") or {}).get("virtual_machine")
        if not vm_ref:
            return JsonResponse({"error": "Адрес не назначен виртуальной машине — консоль гипервизора "
                                          "есть только у ВМ; для остального используйте SSH/VNC/RDP"}, status=400)
        vm = client.get_virtual_machine(vm_ref["id"])
        cluster = client.get_cluster(vm["cluster"]["id"]) if vm.get("cluster") else None
    except NetBoxAPIError as exc:
        logger.error("Консоль ВМ для IP %s: NetBox: %s", ip_id, exc)
        return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)
    try:
        result = vm_console.console_for_vm(vm, cluster)
    except vm_console.ConsoleError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    if not result:
        return JsonResponse({"error": f"ВМ «{vm.get('name')}» не найдена ни в одном настроенном гипервизоре"}, status=404)
    return JsonResponse(result)


@require_http_methods(["POST"])
def ip_connect(request, ip_id):
    """POST {proto: ssh|ssh-key|rdp|vnc|console, username?} — сессия в браузере
    через шлюз Guacamole; в ответе {url} для новой вкладки. Адрес хоста
    берётся из NetBox по id записи, не из запроса. Логин и пароль спрашивает
    сам шлюз; у консоли ВМ (RedVirt) пароль VNC одноразовый, от гипервизора;
    ssh-key — вход служебной учётной записью и ключом из «Настроек»
    (services/ssh_access.py). См. services/remote_gateway.py."""
    if not remote_gateway.enabled():
        return JsonResponse({"error": "Шлюз удалённого доступа не настроен"}, status=503)
    if not remote_gateway.allowed_for(request.user):
        return JsonResponse({"error": "Подключение через шлюз доступно только администраторам"}, status=403)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    proto = str(body.get("proto") or "").lower()
    username = str(body.get("username") or "")[:128]
    if proto not in (*remote_gateway.PROTOCOLS, "console", "ssh-key"):
        return JsonResponse({"error": f"Неизвестный протокол «{proto}»"}, status=400)
    try:
        client = NetBoxClient()
        ip = client.get_ip_address(ip_id)
        host = ip["address"].split("/")[0]
        label = ip.get("dns_name") or host
        if proto == "console":
            vm_ref = (ip.get("assigned_object") or {}).get("virtual_machine")
            if not vm_ref:
                return JsonResponse({"error": "Адрес не назначен виртуальной машине — консоль гипервизора "
                                              "есть только у ВМ"}, status=400)
            vm = client.get_virtual_machine(vm_ref["id"])
            cluster = client.get_cluster(vm["cluster"]["id"]) if vm.get("cluster") else None
    except NetBoxAPIError as exc:
        logger.error("Шлюз, IP %s: NetBox: %s", ip_id, exc)
        return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)
    try:
        if proto == "console":
            info = vm_console.console_for_vm(vm, cluster, web=True)
            if not info:
                return JsonResponse({"error": f"ВМ «{vm.get('name')}» не найдена ни в одном источнике RedVirt"},
                                    status=404)
            protocol, label, target = "vnc", vm.get("name") or label, f"{info['host']}:{info['port']}"
            params = remote_gateway.connection_parameters("vnc", info["host"], info["port"],
                                                          password=info["password"])
        elif proto == "ssh-key":
            try:
                key_cfg = ssh_access.load()[0]
            except sync_config.SyncConfigError as exc:
                return JsonResponse({"error": str(exc)}, status=500)
            if not ssh_access.configured(key_cfg):
                return JsonResponse({"error": "Ключ не задан — «Настройки» → «Доступ к хостам»"}, status=409)
            protocol, target = "ssh", f"{key_cfg['username']}@{host}"
            params = remote_gateway.connection_parameters(
                "ssh", host, port=key_cfg.get("port"), username=key_cfg["username"],
                private_key=key_cfg["private_key"], passphrase=key_cfg.get("passphrase") or "")
        else:
            protocol, target = proto, host
            params = remote_gateway.connection_parameters(proto, host, username=username)
        url = remote_gateway.open_session(request.user.get_username(), protocol, label, params)
    except vm_console.ConsoleError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    except remote_gateway.GatewayError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    # Журнал подключений: кто, чем и куда. Пока строкой в лог контейнера.
    logger.info("Шлюз: %s открыл %s к %s (%s, IP id %s)", request.user.get_username(), proto, label, target, ip_id)
    what = {"console": "Консоль ВМ", "ssh-key": "SSH по ключу"}.get(proto, proto.upper())
    return JsonResponse({"url": url, "message": f"{what} → {label}: сессия открыта в новой вкладке"})


@require_http_methods(["POST"])
def ip_check_availability(request, ip_id):
    """POST — пинг (fping) адреса.
    - Отвечает и сейчас НЕ active → сразу переводим в active (как раньше).
    - НЕ отвечает и сейчас active → сами статус НЕ трогаем, просим фронт
      спросить подтверждение у пользователя (см. suggest_status) — он сам
      вызовет обычный inline-patch, как и при конфликте PTR в resolve_dns."""
    try:
        client = NetBoxClient()
        ip = client.get_ip_address(ip_id)
    except NetBoxAPIError as exc:
        logger.error("Не удалось загрузить IP-адрес %s: %s", ip_id, exc)
        return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)

    address_only = ip["address"].split("/")[0]
    try:
        alive, latency_ms = ping_check(address_only)
    except NetworkToolError as exc:
        logger.error("ping_check %s: %s", address_only, exc)
        return JsonResponse({"error": str(exc)}, status=502)

    current_status = (ip.get("status") or {}).get("value")
    status_changed = False
    suggest_status = None
    if alive and current_status != "active":
        try:
            ip = client.update_ip_address(ip_id, {"status": "active"})
            status_changed = True
        except NetBoxAPIError as exc:
            logger.error("Не удалось обновить статус IP %s: %s", ip_id, exc)
            return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)
    elif not alive and current_status == "active":
        suggest_status = "reserved"

    return JsonResponse({
        "ok": True,
        "alive": alive,
        "latency_ms": latency_ms,
        "status_changed": status_changed,
        "suggest_status": suggest_status,
        "ip": ip,
    })


@require_http_methods(["POST"])
def ip_scan_ports(request, ip_id):
    """POST — сканирование первых 1000 портов (nmap). Результат всегда
    перезаписывает custom_fields.open_ports (в т.ч. если он был не пуст)."""
    try:
        client = NetBoxClient()
        ip = client.get_ip_address(ip_id)
    except NetBoxAPIError as exc:
        logger.error("Не удалось загрузить IP-адрес %s: %s", ip_id, exc)
        return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)

    address_only = ip["address"].split("/")[0]
    try:
        ports = scan_ports(address_only)
    except NetworkToolError as exc:
        logger.error("scan_ports %s: %s", address_only, exc)
        return JsonResponse({"error": str(exc)}, status=502)

    ports_str = ",".join(str(p) for p in ports)
    try:
        ip = client.update_ip_address(ip_id, {"custom_fields": {"open_ports": ports_str or None}})
    except NetBoxAPIError as exc:
        logger.error("Не удалось записать open_ports для IP %s: %s", ip_id, exc)
        return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)

    return JsonResponse({"ok": True, "ports": ports_str, "ip": ip})


@require_http_methods(["POST"])
def ip_resolve_dns(request, ip_id):
    """POST — PTR-резолв DNS-имени.
    - Текущего имени нет → сразу сохраняем найденное.
    - Текущее имя совпадает с PTR → ничего не делаем.
    - Текущее имя отличается → не сохраняем, просим фронт спросить
      подтверждение у пользователя (он сам вызовет обычный inline-patch)."""
    try:
        client = NetBoxClient()
        ip = client.get_ip_address(ip_id)
    except NetBoxAPIError as exc:
        logger.error("Не удалось загрузить IP-адрес %s: %s", ip_id, exc)
        return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)

    address_only = ip["address"].split("/")[0]
    resolved = resolve_ptr(address_only)
    current = ip.get("dns_name") or ""

    if not resolved:
        return JsonResponse({"ok": True, "resolved": None, "action": "none"})

    if not current:
        try:
            ip = client.update_ip_address(ip_id, {"dns_name": resolved})
        except NetBoxAPIError as exc:
            logger.error("Не удалось записать dns_name для IP %s: %s", ip_id, exc)
            return JsonResponse({"error": str(exc)}, status=exc.status_code or 502)
        return JsonResponse({"ok": True, "resolved": resolved, "action": "saved", "ip": ip})

    if current == resolved:
        return JsonResponse({"ok": True, "resolved": resolved, "action": "none"})

    return JsonResponse({"ok": True, "resolved": resolved, "current": current, "action": "confirm"})


# ── Смена собственного пароля ──────────────────────────────────────────────
# Обычная страница, а не модалка: сюда принудительно приводит
# ForcePasswordChangeMiddleware локального админа с дефолтным паролем, и в
# этот момент никакого остального интерфейса вокруг ещё быть не должно.

def password_change(request):
    """Смена пароля текущего пользователя.

    Только для локальных учёток: у пользователя из FreeIPA пароля в этой
    системе нет вообще (django-auth-ldap ставит зеркалу unusable password),
    менять тут нечего — показываем объяснение вместо формы.
    """
    account = getattr(request.user, "local_account", None)
    if account is None:
        return render(request, "ip_plan/password_change.html", {"ldap_user": True})

    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            # Сессия иначе инвалидируется сменой пароля и пользователя
            # выкидывает на логин сразу после успешной смены.
            update_session_auth_hash(request, user)
            if account.must_change_password:
                account.must_change_password = False
                account.save(update_fields=["must_change_password"])
            logger.info("Пользователь %s сменил свой пароль", user.username)
            return redirect(settings.PASSWORD_CHANGE_REDIRECT_URL)
    else:
        form = PasswordChangeForm(request.user)

    return render(request, "ip_plan/password_change.html", {
        "form": form,
        "ldap_user": False,
        "forced": account.must_change_password,
    })
