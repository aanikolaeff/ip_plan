import ipaddress
import json
import logging
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.contrib.auth.decorators import login_not_required
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .forms import (
    CUSTOM_FIELD_KEYS, STATUS_CHOICES, IPAddressForm,
    RegulatorDocumentForm, RegulatorEntryForm, RegulatorEntryRevokeForm,
)
from .models import RegulatorDocument, RegulatorEntry
from .services import acl_store, os_catalog, sync_log
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
        return ip
    parent_id, is_device = ref
    parent = client.get_device(parent_id) if is_device else client.get_virtual_machine(parent_id)
    ip["_role"] = parent.get("role")
    ip["_parent_description"] = parent.get("description") or None
    ip["_guest_os"] = None if is_device else (parent.get("custom_fields") or {}).get("guest_os") or None
    return ip


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
            continue
        parent_id, is_device = ref
        parent = (devices if is_device else vms).get(parent_id) or {}
        ip["_role"] = parent.get("role")
        ip["_parent_description"] = parent.get("description") or None
        ip["_guest_os"] = None if is_device else (parent.get("custom_fields") or {}).get("guest_os") or None
    return ip_addresses


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


def _saved_response(request, client, ip, oob, message):
    """Ответ на успешное сохранение: OOB-обновление строки + закрытие модалки + тост."""
    _attach_role(client, ip)
    row_html = render_to_string(
        "ip_plan/ip_row.html", {"ip": ip, "oob": oob}, request=request
    )
    response = HttpResponse(row_html)
    response["HX-Trigger"] = json.dumps(
        {"close-modal": None, "show-toast": {"message": message, "kind": "success"}}
    )
    response["HX-Reswap"] = "none"
    return response


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
            "label": (ip.get("status") or {}).get("label", ""),
        },
        "dns_name": ip.get("dns_name") or "",
        "dns_records": custom_fields.get("dns_records") or "",
        "assignment": _assignment_display(ip),
        "role": role,  # {"slug": ..., "name": ...} либо null
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


def _acl_page_context(ip_query, device_name):
    """Общий контекст для acl_lookup() — и обычного GET (полная страница),
    и HTMX-запроса (partial, реагирует и на ввод IP, и на смену дропбокса
    устройства) — один и тот же URL для обоих, см. докстринг acl_lookup —
    чтобы обе точки входа вели себя одинаково, включая прямое открытие
    ссылки вида ?ip=...&device=....

    device_name фильтрует ОБА блока (результаты поиска и обзорную таблицу);
    '' / None — «все устройства»."""
    ip_query = (ip_query or "").strip()
    device_name = (device_name or "").strip()
    results, error = [], None
    if ip_query:
        try:
            results = acl_store.search_ip(ip_query, device_name=device_name or None)
        except ValueError:
            error = f"«{ip_query}» — не похоже на IPv4-адрес"
    return {
        "ip_query": ip_query,
        "selected_device": device_name,
        "results": results,
        "search_error": error,
        "overview_rows": acl_store.overview(device_name=device_name or None),
        "object_group_rows": acl_store.list_object_groups(device_name=device_name or None),
    }


def acl_lookup(request):
    """Страница «ACL» — поиск, какие ACL-правила на Cisco-устройствах
    трогают заданный IP, плюс обзор всех загруженных ACL, с фильтром по
    устройству (дропбокс, '' = все). Данные — только из
    netbox-sync/config_backups/*/latest.cfg (см. acl_store.py), в NetBox
    эта страница никогда не пишет и не читает.

    Один URL на обычный GET и на HTMX-запрос (форма поиска и дропбокс
    устройства бьют сюда же, не в отдельный /acl/search/) — иначе
    hx-push-url кладёт в адресную строку URL самого AJAX-запроса, и
    F5/переход по «Назад» на нём отдавал бы голый фрагмент без <head>/CSS/
    дропбокса (ровно так и было, пока это были разные эндпоинты — фрагмент
    ошибочно принимали за "стили и дропбокс пропали")."""
    context = _acl_page_context(request.GET.get("ip"), request.GET.get("device"))
    if request.headers.get("HX-Request") == "true":
        return render(request, "ip_plan/_acl_page_body.html", context)
    context["device_choices"] = acl_store.list_device_names()
    return render(request, "ip_plan/acl_lookup.html", context)


def acl_detail(request, device, acl_name):
    """Полное содержимое одного ACL (все правила, включая remark) в виде
    таблицы — переход сюда со строки обзорной таблицы на /acl/. Поиск —
    штатный built-in search у DataTables (см. acl_detail.html), не
    серверный: для ACL вроде '20' (32к+ правил) весь список рендерится
    один раз, дальше фильтрация/пагинация — на клиенте, без повторных
    round-trip на каждый символ."""
    dev = acl_store.get_device(device)
    if dev is None:
        raise Http404(f"Устройство «{device}» не найдено в config_backups")
    acl = dev.acl_by_name(acl_name)
    if acl is None:
        raise Http404(f"ACL «{acl_name}» не найден на {device}")

    applications = [a for a in dev.applications if a.acl_name == acl.name]
    return render(request, "ip_plan/acl_detail.html", {
        "device": device,
        "acl": acl,
        "applications": applications,
        "applied": bool(applications),
    })


def acl_object_group_detail(request, device, name):
    """Полное содержимое одной object-group network (все адреса/подсети,
    которые объединяет группа) + список ACL, где она используется. Та же
    DataTables-таблица + built-in поиск, что и на acl_detail — эти группы
    тоже бывают на тысячи записей (RusNets — ~9200 на каждом ASR)."""
    result = acl_store.get_object_group(device, name)
    if result is None:
        raise Http404(f"object-group «{name}» не найден на {device}")
    return render(request, "ip_plan/acl_object_group_detail.html", result)


# --- Реестр блокировок регулятора (models.py) ---
# В отличие от ACL Lookup выше — настоящие Django-модели (см. models.py:
# записи накапливаются во времени, нужна история/статусы, а не просто
# зеркало текущего файла на диске). Страница/фильтры — тот же htmx-паттерн,
# что у acl_lookup (один URL на обычный GET и на HX-запрос, hx-push-url),
# формы add/revoke — модалки через общий #modal-content, тот же паттерн,
# что ip_add/os_catalog_entry_save (см. _regulator_saved_response ниже —
# аналог _saved_response, но с OOB outerHTML всей страницы вместо одной
# строки: тут нет клиентской React-таблицы, которая сама умеет вставлять
# новую строку, весь список рендерится сервером).

def _regulator_page_context(request):
    kind = request.GET.get("kind", "")
    status = request.GET.get("status", "active")
    document_id = request.GET.get("document", "")
    q = request.GET.get("q", "").strip()

    entries = RegulatorEntry.objects.select_related("document").all()
    if kind:
        entries = entries.filter(kind=kind)
    if status in ("active", "revoked"):
        entries = entries.filter(status=status)
    if document_id:
        entries = entries.filter(document_id=document_id)
    if q:
        entries = entries.filter(Q(value__icontains=q) | Q(match_value__icontains=q))

    documents = RegulatorDocument.objects.annotate(
        entries_count=Count("entries"),
        active_count=Count("entries", filter=Q(entries__status="active")),
    )

    return {
        "entries": entries,
        "documents": documents,
        "kind": kind,
        "status": status,
        "document_id": str(document_id),
        "q": q,
    }


def regulator_list(request):
    """Страница «Блокировки регулятора» — тот же принцип, что у acl_lookup:
    один URL на обычный GET и на HTMX (форма поиска/фильтры бьют сюда же,
    hx-push-url кладёт query-string в адресную строку)."""
    context = _regulator_page_context(request)
    if request.headers.get("HX-Request") == "true":
        return render(request, "ip_plan/_regulator_page_body.html", context)
    return render(request, "ip_plan/regulator_lookup.html", context)


def _regulator_saved_response(request, message):
    """Ответ на успешное добавление/снятие/удаление: OOB outerHTML всей
    #regulator-page-body (фильтры сбрасываются к дефолтным — простая
    компромиссная цена за то, что модалка не обязана знать текущие фильтры
    фоновой страницы) + закрытие модалки + тост. Аналог _saved_response у
    IP-таблицы, но там OOB — одна строка внутри React-управляемой таблицы,
    здесь — весь список, страница рендерится сервером целиком."""
    # request.GET на POST-запросе (все action-view ниже — POST) — пустой
    # QueryDict, так что _regulator_page_context сама откатится к дефолтным
    # фильтрам (status=active, без kind/document/q) без специальной возни.
    context = _regulator_page_context(request)
    html = render_to_string(
        "ip_plan/_regulator_page_body.html", {**context, "oob": True}, request=request
    )
    response = HttpResponse(html)
    response["HX-Trigger"] = json.dumps(
        {"close-modal": None, "show-toast": {"message": message, "kind": "success"}}
    )
    return response


def regulator_document_add(request):
    """GET — форма добавления письма (для модалки), POST — сохранение."""
    if request.method == "POST":
        form = RegulatorDocumentForm(request.POST)
        if form.is_valid():
            form.save()
            return _regulator_saved_response(request, "Письмо добавлено")
        return render(request, "ip_plan/regulator_document_add_modal.html", {"form": form}, status=400)
    form = RegulatorDocumentForm(initial={"document_date": timezone.localdate()})
    return render(request, "ip_plan/regulator_document_add_modal.html", {"form": form})


def regulator_entry_add(request, document_id=None):
    """GET — форма добавления записи (для модалки), POST — сохранение.
    document_id — предзаполнение письма (кнопка "+ запись" у строки письма
    в обзорной таблице); без него — пустой выпадающий список, письмо
    выбирается вручную (кнопка "+ запись" в общем тулбаре)."""
    if request.method == "POST":
        form = RegulatorEntryForm(request.POST)
        if form.is_valid():
            form.save()
            return _regulator_saved_response(request, "Запись добавлена")
        return render(request, "ip_plan/regulator_entry_add_modal.html", {"form": form}, status=400)
    initial = {"document": document_id} if document_id else {}
    form = RegulatorEntryForm(initial=initial)
    return render(request, "ip_plan/regulator_entry_add_modal.html", {"form": form})


def regulator_entry_revoke(request, entry_id):
    """GET — маленькая форма (причина снятия, необязательна), POST —
    статус → revoked. Отдельная от delete: запись остаётся в базе для
    истории/аудита (см. models.py, RegulatorEntry.status)."""
    entry = get_object_or_404(RegulatorEntry, pk=entry_id)
    if request.method == "POST":
        form = RegulatorEntryRevokeForm(request.POST)
        if form.is_valid():
            entry.status = "revoked"
            entry.revoked_at = timezone.now()
            entry.revoked_reason = form.cleaned_data["revoked_reason"]
            entry.save(update_fields=["status", "match_value", "revoked_at", "revoked_reason"])
            return _regulator_saved_response(request, "Запись снята")
        return render(request, "ip_plan/regulator_entry_revoke_modal.html", {"form": form, "entry": entry}, status=400)
    form = RegulatorEntryRevokeForm()
    return render(request, "ip_plan/regulator_entry_revoke_modal.html", {"form": form, "entry": entry})


@require_http_methods(["POST"])
def regulator_entry_reactivate(request, entry_id):
    """Снятие статуса «снята» без модалки — обратное действие, причина не нужна."""
    entry = get_object_or_404(RegulatorEntry, pk=entry_id)
    entry.status = "active"
    entry.revoked_at = None
    entry.revoked_reason = ""
    entry.save(update_fields=["status", "match_value", "revoked_at", "revoked_reason"])
    return _regulator_saved_response(request, "Запись восстановлена")


@require_http_methods(["POST"])
def regulator_entry_delete(request, entry_id):
    """Физическое удаление — только для исправления ошибки ввода (опечатка
    и т.п.), не для "регулятор снял блокировку" (для этого — revoke выше,
    сохраняет историю)."""
    entry = get_object_or_404(RegulatorEntry, pk=entry_id)
    entry.delete()
    return _regulator_saved_response(request, "Запись удалена")


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
    except NetBoxAPIError as exc:
        logger.error("Не удалось получить список IP-адресов/подсетей (JSON): %s", exc)
        return JsonResponse({"error": str(exc)}, status=502)

    return JsonResponse({
        "results": [serialize_ip(ip) for ip in ip_addresses],
        "prefixes": [p["prefix"] for p in prefixes],
        "status_choices": STATUS_CHOICES,
        "role_choices": _distinct_roles(ip_addresses),
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
        if form.is_valid():
            try:
                client = NetBoxClient()
                tag_ids = client.resolve_tags(form.cleaned_data["tags"])
                payload = _build_payload(form.cleaned_data, tag_ids)
                ip = client.create_ip_address(payload)
            except NetBoxAPIError as exc:
                logger.error("Не удалось создать IP-адрес: %s", exc)
                form.add_error(None, str(exc))
            else:
                return _saved_response(
                    request, client, ip, "beforeend:#ip-table-body", "IP-адрес добавлен"
                )
        return render(request, "ip_plan/ip_add_modal.html", {"form": form}, status=400)

    # ?address= — предзаполнение из кнопки "+" у строки свободного диапазона
    # (первый свободный адрес этого диапазона); остальные поля пустые.
    initial = {"status": "active"}
    prefill_address = request.GET.get("address")
    if prefill_address:
        initial["address"] = prefill_address
    form = IPAddressForm(initial=initial)
    return render(request, "ip_plan/ip_add_modal.html", {"form": form})


def ip_edit_modal(request, ip_id):
    """GET — форма редактирования текущего IP-адреса (для модального окна)."""
    try:
        ip = NetBoxClient().get_ip_address(ip_id)
    except NetBoxAPIError as exc:
        logger.error("Не удалось загрузить IP-адрес %s: %s", ip_id, exc)
        return render(request, "ip_plan/_modal_error.html", {"error": str(exc)})

    form = IPAddressForm(initial=_ip_to_initial(ip))
    interface_name = (ip.get("assigned_object") or {}).get("name") or "—"
    return render(
        request,
        "ip_plan/ip_edit_modal.html",
        {
            "form": form,
            "ip_id": ip_id,
            "address": ip.get("address", ""),
            "interface_name": interface_name,
        },
    )


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
            return _saved_response(request, client, ip, "true", "IP-адрес сохранён")

    return render(
        request,
        "ip_plan/ip_edit_modal.html",
        {"form": form, "ip_id": ip_id, "address": form.data.get("address", "")},
        status=400,
    )


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
