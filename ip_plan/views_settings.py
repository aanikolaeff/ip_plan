"""Раздел "Настройки" — редактирование конфигов netbox-sync через UI.

Вынесено из views.py отдельным модулем: там уже 1400 строк про IP-таблицу,
ACL и блокировки, а это самостоятельная подсистема со своей моделью прав и
своим источником данных (файлы на диске, а не NetBox API).

Общий принцип всех POST-вью здесь: собрать новую версию ЦЕЛОГО файла,
отдать её sync_config.save() вместе с хешем, который форма получила при
открытии, и на успехе увести браузер на ту же вкладку через HX-Redirect.
Частичных обновлений нет намеренно — файл маленький, а целостность важнее
экономии на перерисовке.
"""
import json
import logging
import re

from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .context_processors import SITE_COOKIE, site_options_cached
from .models import LdapConfig, LocalAccount, SettingsAudit
from .services import connection_test, ldap_check, remote_gateway, settings_bundle, ssh_access
from .services import sync_overview
from .services import sync_config as sc

logger = logging.getLogger(__name__)

# Разделы. "file" — что этот раздел реально правит: рейка слева показывает
# его под названием, чтобы человек видел, какой файл на диске (или какая
# таблица в базе) стоит за экраном. Это не украшение: те же имена он потом
# ищет на прод-хосте, когда разбирается с прогоном синка. "group" — заголовок
# группы в рейке (макет test_des/Settings*.html).
TABS = [
    {"key": "sync",        "title": "Запуск и расписание", "file": "full_sync.sh",        "perm": "staff",     "group": "Синхронизация"},
    {"key": "devices",     "title": "Оборудование",    "file": "conf/devices.json",       "perm": "staff",     "group": "Источники данных"},
    {"key": "models",      "title": "Модели",          "file": "conf/devices.json",       "perm": "staff",     "group": "Источники данных"},
    {"key": "hypervisors", "title": "Гипервизоры",     "file": "conf/sources_*.json",     "perm": "staff",     "group": "Источники данных"},
    {"key": "scan",        "title": "Скан и DNS",      "file": "conf/config.json",        "perm": "staff",     "group": "Источники данных"},
    {"key": "ldap",        "title": "Вход и LDAP",     "file": "база ip_plan",            "perm": "superuser", "group": "Доступ"},
    {"key": "users",       "title": "Пользователи",    "file": "база ip_plan",            "perm": "superuser", "group": "Доступ"},
    {"key": "access",      "title": "Доступ к хостам", "file": "conf/ssh_access.json",    "perm": "staff",     "group": "Доступ"},
    {"key": "audit",       "title": "Журнал изменений", "file": "база ip_plan",           "perm": "staff",     "group": "История"},
]
DEFAULT_TAB = "sync"
# Разделы, которым не нужен смонтированный conf/ netbox-sync.
_DB_TABS = {"ldap", "users", "audit"}

def _require(check, what):
    """Свой декоратор вместо user_passes_test: тот при непрохождении
    отправляет на страницу входа, а здесь пользователь УЖЕ вошёл — просто
    не с теми правами. Редирект на логин в этой ситуации выглядит как
    поломка ("я же залогинен"), поэтому отвечаем честным 403. Для запросов
    из HTMX-модалки отдаём тот же партиал ошибки, что и остальные отказы,
    иначе в модалку прилетела бы целая страница 403."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not check(request.user):
                if request.headers.get("HX-Request"):
                    return _error_modal(request, f"Недостаточно прав: требуется {what}.", status=403)
                raise PermissionDenied(f"Требуется {what}")
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


staff_required = _require(lambda u: u.is_active and u.is_staff, "доступ администратора")
superuser_required = _require(lambda u: u.is_active and u.is_superuser, "доступ суперпользователя")


def _audit(request, scope, target, action, details=""):
    SettingsAudit.objects.create(
        username=request.user.username, scope=scope,
        target=target, action=action, details=details,
    )


def _redirect_to(tab, message):
    """Успешное сохранение: закрыть модалку и перезагрузить вкладку.
    HX-Redirect, а не частичный swap — см. докстринг модуля."""
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse("ip_plan:settings_tab", args=[tab])
    response["HX-Trigger"] = json.dumps({"show-toast": {"message": message, "kind": "success"}})
    return response


def _error_modal(request, message, status=400):
    return render(request, "ip_plan/_modal_error.html", {"error": message}, status=status)


# ── Страница ───────────────────────────────────────────────────────────────

def _visible_tabs(user):
    return [t for t in TABS if t["perm"] != "superuser" or user.is_superuser]


def _rail(user, active):
    """Группы рейки: [{title, items: [tab + dot]}]. Точка у «Запуск и
    расписание» — состояние последнего прогона (warn — ошибка или оборван,
    accent — идёт)."""
    state = sync_overview.overview()["state"] if active != "sync" else None
    groups = []
    for tab in _visible_tabs(user):
        item = dict(tab, dot="")
        if tab["key"] == "sync" and state is not None:
            item["dot"] = {"failed": "warn", "aborted": "warn", "running": "accent"}.get(state, "")
        if not groups or groups[-1]["title"] != tab["group"]:
            groups.append({"title": tab["group"], "items": []})
        groups[-1]["items"].append(item)
    return groups


def _page_ctx(request, active):
    """Общее для всех страниц раздела: рейка и состояние каталога conf."""
    return {
        "tabs": _visible_tabs(request.user),
        "rail": _rail(request.user, active),
        "active_tab": active,
        "conf_available": sc.available(),
        "conf_dir": sc.conf_dir(),
        "sync_running": sc.sync_running(),
    }


@staff_required
def settings_page(request, tab=DEFAULT_TAB):
    # Вкладки LDAP и "Пользователи" — только суперпользователю: там
    # управление тем, кто вообще может войти. Невидимые вкладки не просто
    # не рисуются, но и недоступны по прямой ссылке.
    visible = _visible_tabs(request.user)
    if tab not in {t["key"] for t in visible}:
        if tab in {t["key"] for t in TABS}:
            raise PermissionDenied("Требуется доступ суперпользователя")
        tab = DEFAULT_TAB

    ctx = _page_ctx(request, tab)
    if not ctx["conf_available"] and tab not in _DB_TABS and tab != "sync":
        return render(request, "ip_plan/settings.html", ctx)

    try:
        ctx.update(_tab_context(request, tab))
    except sc.SyncConfigError as e:
        ctx["load_error"] = str(e)
    return render(request, "ip_plan/settings.html", ctx)


# Короткие ярлыки шагов для чипов «Что собираем» (как в макете — латиницей,
# моноширинным), в порядке шагов синка.
_CHIP_LABELS = [
    ("sync_interfaces", "interfaces"),
    ("import_vlans", "vlans"),
    ("import_prefixes", "prefixes"),
    ("sync_macs", "macs"),
    ("sync_lldp", "lldp"),
    ("backup_config", "config"),
    ("sync_blocklist", "blocklist"),
]
assert {k for k, _ in _CHIP_LABELS} == set(sc.SYNC_FLAGS)
_FLAG_TITLES = {
    "sync_interfaces": "интерфейсы и адрес управления",
    "import_vlans": "VLAN",
    "import_prefixes": "префиксы сетей",
    "sync_macs": "MAC-таблица",
    "sync_lldp": "LLDP-соседи",
    "backup_config": "снимок конфигурации",
    "sync_blocklist": "списки блокировок firewall",
}

# Откуда у шага взялось его состояние — цветом чипа (легенда под таблицей).
# both — задано и у модели, и на устройстве; model — унаследовано от модели;
# dev — включено только на устройстве; off — у модели включено, на
# устройстве явно выключено (явный false, см. device_save).
_CHIP_TITLES = {
    "both": "включено и у модели, и на этом устройстве",
    "model": "включено у модели, устройство наследует",
    "dev": "включено только на этом устройстве; у модели выключено",
    "off": "у модели включено, на этом устройстве выключено",
}


def _chips(cfg, model_cfg, eff):
    """Чипы шагов с пометкой происхождения, включая «выключено здесь»."""
    chips = []
    for flag, label in _CHIP_LABELS:
        if eff.get(flag):
            if flag not in cfg:
                kind = "model"
            elif not model_cfg.get(flag):
                kind = "dev"
            else:
                kind = "both"
        elif model_cfg.get(flag) and cfg.get(flag) is False:
            kind = "off"
        else:
            continue
        chips.append({"label": label, "kind": kind,
                      "title": f"{_FLAG_TITLES[flag]}: {_CHIP_TITLES[kind]}"})
    if model_cfg.get("cluster"):
        chips.append({"label": "cluster", "kind": "model",
                      "title": f"кластер {model_cfg['cluster']} — задан у модели"})
    return chips


def _plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _device_subtitle(cfg, model_cfg, site_label):
    """Подпись под именем устройства в списке."""
    if cfg.get("cluster_peer_of"):
        return f"резерв для {cfg['cluster_peer_of']}"
    parts = []
    status = cfg.get("status") or "active"
    if status != "active":
        parts.append(status)
    if model_cfg.get("cluster") == "vipnet_failover":
        parts.append("кластер ViPNet")
    if cfg.get("cluster_reserve"):
        parts.append(f"резерв — {cfg['cluster_reserve']}")
    if cfg.get("network_core"):
        parts.append("ядро сети")
    parts.append(site_label)
    return " · ".join(parts)


def _device_connection(cfg, transport):
    if cfg.get("cluster_peer_of") or not cfg.get("host"):
        return "без подключения"
    if transport == "snmp":
        version = sc.snmp_version(cfg)
        user = cfg.get("snmp_user")
        return f"snmp {version}{' ' + user if user else ''}@{cfg['host']}"
    port = cfg.get("port")
    suffix = f":{port}" if port and int(port) != 22 else ""
    return f"{cfg.get('username', '')}@{cfg['host']}{suffix}"


def _changed_devices(existing):
    """Устройства, правленные после начала последнего шага Devices: эти
    правки ещё не в NetBox. Пусто — если логов нет (сравнивать не с чем)."""
    since = sync_overview.devices_step_started()
    if since is None:
        return []
    names = (SettingsAudit.objects.filter(scope="devices", created_at__gt=since)
             .exclude(action="delete").values_list("target", flat=True))
    return sorted({n for n in names if n in existing})


# Подписи разделов журнала и категории вкладок (Все / Запуски синхронизации /
# Источники данных / Доступ).
_AUDIT_SCOPES = {
    "devices": ("Оборудование", "conf"),
    "device_types": ("Модели", "conf"),
    "sources_redvirt": ("Гипервизоры", "conf"),
    "sources_vcenter": ("Гипервизоры", "conf"),
    "config": ("Скан и DNS", "conf"),
    "import": ("Импорт", "conf"),
    "export": ("Экспорт", "conf"),
    "sync": ("Синхронизация", "sync"),
    "ssh_access": ("Доступ к хостам", "access"),
    "ldap": ("Вход и LDAP", "access"),
    "users": ("Пользователи", "access"),
}
AUDIT_CATEGORIES = [
    ("", "Все"),
    ("sync", "Запуски синхронизации"),
    ("conf", "Источники данных"),
    ("access", "Доступ"),
]
_AUDIT_ACTIONS = {
    "create": ("add", "добавлено"),
    "update": ("change", "изменено"),
    "delete": ("delete", "удалено"),
}


def _audit_row(entry):
    label, category = _AUDIT_SCOPES.get(entry.scope, (entry.scope, "conf"))
    if entry.scope == "config" and entry.target.startswith("environments."):
        label = "Подключение к NetBox"
    if entry.scope == "import":
        kind, action = "import", "импорт"
    elif entry.scope == "export":
        kind, action = "export", "экспорт"
    elif entry.scope == "sync":
        kind, action = "run", "запуск"
    else:
        kind, action = _AUDIT_ACTIONS.get(entry.action, ("change", entry.action))
    return {
        "when": sync_overview.human_when(entry.created_at),
        "when_full": entry.created_at,
        "username": entry.username,
        "scope_label": label,
        "category": category,
        "target": entry.target,
        "action_kind": kind,
        "action_label": action,
        "details": entry.details,
    }


def _initials(user):
    full = " ".join(x for x in (user.first_name, user.last_name) if x).strip()
    if full:
        return "".join(w[0] for w in full.split()[:2]).upper()
    letters = re.sub(r"[^\w]", "", user.username)
    return (letters[:2] or user.username[:2]).upper()


def _site_filter(request):
    """Площадка из переключателя оболочки (cookie ip_plan_site): slug или
    None — «все площадки» / «без площадки» в Настройках означают всё."""
    value = request.COOKIES.get(SITE_COOKIE) or "all"
    return None if value in ("all", "__none__") else value


def _tab_context(request, tab):
    if tab == "sync":
        return {"sync": sync_overview.overview()}

    if tab == "devices":
        data, digest = sc.load("devices")
        types = data.get("device_types") or {}
        site_names = {s["slug"]: s["name"] for s in site_options_cached()}
        site = _site_filter(request)
        all_devices = data.get("devices") or {}
        devices = []
        for name, cfg in all_devices.items():
            site_slug = cfg.get("site") or sc.DEFAULT_SITE_SLUG
            if site and site_slug != site:
                continue
            # Синк читает настройки устройства поверх настроек его модели
            # (см. _resolve в netbox_devices_sync.py), поэтому шаги показываем
            # по этой же, эффективной, картине: сбор MAC-таблицы, например,
            # задан у моделей, и по собственным полям устройства его не видно.
            eff = sc.effective(cfg, types)
            model_cfg = types.get(cfg.get("model")) or {}
            transport = sc.transport(model_cfg.get("interfaces_parser", ""))
            site_label = site_names.get(site_slug, site_slug)
            devices.append({"name": name, "cfg": cfg, "eff": eff,
                            "model": cfg.get("model", ""),
                            "site_slug": site_slug, "site_label": site_label,
                            "subtitle": _device_subtitle(cfg, model_cfg, site_label),
                            "connection": _device_connection(cfg, transport),
                            "transport": transport,
                            "snmp_version": sc.snmp_version(cfg),
                            "chips": _chips(cfg, model_cfg, eff),
                            "testable": bool(cfg.get("host")) and not cfg.get("cluster_peer_of"),
                            # Для клиентского поиска по имени, адресу и модели.
                            "search": " ".join(str(x) for x in (
                                name, cfg.get("host", ""), cfg.get("mgmt_ip", ""),
                                cfg.get("model", ""))).lower()})
        tests = connection_test.state_for("device", [d["name"] for d in devices])
        for item in devices:
            item["test"] = tests.get(item["name"])
        changed = _changed_devices(set(all_devices))
        count = len(devices)
        return {
            "devices": devices,
            "device_count": count,
            "device_count_label": _plural(count, "устройство", "устройства", "устройств"),
            "site_filter": site,
            "site_filter_label": site_names.get(site, site) if site else "",
            "changed_devices": changed,
            "changed_label": (f"Изменено {len(changed)} "
                              f"{_plural(len(changed), 'устройство', 'устройства', 'устройств')}"),
            "chip_legend": _CHIP_TITLES,
            "file_hash": digest,
        }

    if tab == "models":
        data, digest = sc.load("devices")
        used = {}
        for name, cfg in (data.get("devices") or {}).items():
            used.setdefault(cfg.get("model"), []).append(name)
        rows, vendors = [], {}
        for name, cfg in (data.get("device_types") or {}).items():
            vendor = cfg.get("manufacturer") or "—"
            vendors[vendor] = vendors.get(vendor, 0) + 1
            parser = cfg.get("interfaces_parser", "")
            rows.append({
                "name": name, "cfg": cfg,
                "manufacturer": vendor,
                "initials": vendor[:2].upper(),
                "role": cfg.get("role", ""),
                "parser": parser,
                "transport": sc.transport(parser),
                "cluster": cfg.get("cluster", ""),
                "defaults": [label for flag, label in _CHIP_LABELS if cfg.get(flag)],
                "used_by": sorted(used.get(name, [])),
                "search": f"{name} {parser}".lower(),
            })
        rows.sort(key=lambda r: (r["manufacturer"].lower(), r["name"].lower()))
        return {
            "device_type_rows": rows,
            "vendors": sorted(vendors.items(), key=lambda kv: kv[0].lower()),
            "mac_capable": [p for p in sc.INTERFACE_PARSERS if p in sc.MAC_CAPABLE_PARSERS],
            "lldp_capable": [p for p in sc.INTERFACE_PARSERS if p in sc.LLDP_CAPABLE_PARSERS],
            "file_hash": digest,
        }

    if tab == "hypervisors":
        redvirt, rv_hash = sc.load("sources_redvirt")
        vcenter, vc_hash = sc.load("sources_vcenter")
        site_names = {s["slug"]: s["name"] for s in site_options_cached()}
        for s in (*redvirt, *vcenter):
            slug = s.get("site") or sc.DEFAULT_SITE_SLUG
            s["site_label"] = site_names.get(slug, slug)
        rv_tests = connection_test.state_for("redvirt", [s.get("name") or s.get("url") for s in redvirt])
        vc_tests = connection_test.state_for("vcenter", [s.get("name") or s.get("url") for s in vcenter])
        # Индекс в файле передаётся как есть: источники адресуются им в URL
        # формы и удаления (source_save пересобирает объект по индексу).
        return {
            "redvirt": [(i, s, rv_tests.get(s.get("name") or s.get("url")))
                        for i, s in enumerate(redvirt)],
            "redvirt_hash": rv_hash,
            "redvirt_last": sync_overview.step_info("RedVirt"),
            "vcenter": [(i, s, vc_tests.get(s.get("name") or s.get("url")))
                        for i, s in enumerate(vcenter)],
            "vcenter_hash": vc_hash,
            "vcenter_last": sync_overview.step_info("vCenter"),
        }

    if tab == "access":
        cfg, digest = ssh_access.load()
        return {"ssh": ssh_access.summary(cfg), "file_hash": digest,
                "gateway_enabled": remote_gateway.enabled()}

    if tab == "scan":
        data, digest = sc.load("config")
        envs = []
        for env_name, env_cfg in (data.get("environments") or {}).items():
            envs.append({"name": env_name, "url": env_cfg.get("netbox_url", ""),
                         "has_token": bool(env_cfg.get("token"))})
        return {
            "dns_servers": data.get("dns_servers") or [],
            "dns_skip_pattern": data.get("dns_skip_pattern", ""),
            "scan_exclude_prefixes": data.get("scan_exclude_prefixes") or [],
            "environments": envs,
            "file_hash": digest,
        }

    if tab == "ldap":
        cfg = LdapConfig.load()
        return {
            "ldap": cfg,
            "ldap_password_mask": sc.masked(cfg.bind_password_encrypted),
            # Пароль сохранён, но не расшифровывается — значит сменили
            # SECRET_KEY. Молчать нельзя: вход через каталог уже не работает.
            "ldap_password_broken": bool(cfg.bind_password_encrypted) and not cfg.bind_password,
            "ldap_ca_cert": settings.LDAP_TLS_CA_CERT,
            "local_superusers": _local_superusers().count(),
        }

    if tab == "users":
        rows = []
        for u in User.objects.select_related("local_account").order_by("username"):
            account = getattr(u, "local_account", None)
            if not u.is_active:
                state, state_label = "off", "отключена"
            elif account is not None and account.must_change_password:
                state, state_label = "warn", "нужна смена пароля"
            else:
                state, state_label = "ok", "активна"
            rows.append({"user": u, "is_local": account is not None, "account": account,
                         "initials": _initials(u),
                         "full_name": " ".join(x for x in (u.first_name, u.last_name) if x),
                         "state": state, "state_label": state_label,
                         "last_login": sync_overview.human_when(u.last_login) if u.last_login else ""})
        return {"user_rows": rows}

    if tab == "audit":
        category = request.GET.get("cat", "")
        if category not in {key for key, _ in AUDIT_CATEGORIES}:
            category = ""
        entries = SettingsAudit.objects.all()
        if category:
            scopes = [scope for scope, (_l, cat) in _AUDIT_SCOPES.items() if cat == category]
            entries = entries.filter(scope__in=scopes)
        return {"audit_rows": [_audit_row(e) for e in entries[:200]],
                "audit_category": category,
                "audit_categories": AUDIT_CATEGORIES}

    return {}


# ── Запуск и расписание ────────────────────────────────────────────────────
# Запуск синка из интерфейса — заглушка (решение 01.10.2026): ip_plan живёт в
# контейнере, full_sync.sh — на хосте, канала между ними нет. Экран честно
# показывает последний и идущий прогон по логу и lock-файлу, а кнопки
# запуска/остановки отвечают, что запуск пока делается на хосте.

@staff_required
def sync_state(request):
    """Тело экрана синхронизации — его перерисовывает опрос, пока идёт прогон."""
    return render(request, "ip_plan/_settings_sync_body.html",
                  {"sync": sync_overview.overview()})


@staff_required
def sync_run(request):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps({"show-toast": {
        "message": "Запуск из интерфейса пока не подключён: синхронизация запускается на хосте "
                   "(full_sync.sh) по расписанию или вручную.",
        "kind": "info"}})
    return response


# ── Оборудование ───────────────────────────────────────────────────────────

# ── Экспорт настроек ───────────────────────────────────────────────────────
# Переносит на чистую установку подключения: устройства, модели, источники
# гипервизоров и учётные данные к ним. Рабочих данных не отдаёт — см.
# services/settings_bundle.py.

@staff_required
def export_confirm(request):
    """Предупреждение перед скачиванием: в архиве пароли открытым текстом."""
    return render(request, "ip_plan/_settings_export_confirm.html", {
        "conf_available": sc.available(),
        "ldap_configured": bool(LdapConfig.load().servers),
    })


@staff_required
def export_download(request):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)

    include_config = bool(request.POST.get("include_config"))
    include_ldap = bool(request.POST.get("include_ldap"))
    try:
        payload, filename, members = settings_bundle.build(
            include_config=include_config,
            include_ldap=include_ldap,
            author=request.user.username,
        )
    except settings_bundle.BundleError as e:
        return _error_modal(request, str(e))

    # В журнал — состав, но не содержимое: имена файлов безопасны, значения
    # полей нет.
    _audit(request, "export", filename, "create", ", ".join(members))

    response = HttpResponse(payload, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Content-Length"] = str(len(payload))
    return response


# ── Импорт настроек ────────────────────────────────────────────────────────
# Два шага: предпросмотр (ничего не пишет) и применение отмеченных записей.
# Импорт дополняет конфигурацию, существующие записи и пароли не затирает без
# явного выбора.

def _import_blocked():
    """Импорт меняет несколько файлов сразу, а шаги синка читают конфиги
    каждый в момент своего старта: правка посреди прогона дала бы прогон на
    смеси старых и новых настроек."""
    return sc.sync_running()


@staff_required
def import_form(request):
    return render(request, "ip_plan/_settings_import_form.html", {
        "sync_running": _import_blocked(),
    })


@staff_required
def import_preview(request):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    if _import_blocked():
        return _error_modal(
            request,
            "Идёт синхронизация. Импорт изменил бы конфигурацию посреди прогона — "
            "дождитесь окончания и повторите.",
        )

    files = request.FILES.getlist("files")
    if not files:
        return _error_modal(request, "Не выбран ни один файл")

    uploaded, sources = {}, []
    try:
        for upload in files:
            parsed = settings_bundle.read_upload(upload.name, upload.read())
            for key, data in parsed.items():
                uploaded[key] = data
            sources.append(f"{upload.name} → {', '.join(sorted(parsed))}")
    except settings_bundle.BundleError as e:
        return _error_modal(request, str(e))

    try:
        result = settings_bundle.plan(uploaded)
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e))

    request.session["import_payload"] = uploaded
    return render(request, "ip_plan/_settings_import_preview.html", {
        "items": result["items"],
        "summary": result["summary"],
        "optional": result["optional"],
        "sources": sources,
        # config.json (токены NetBox) и LDAP напрямую правит только
        # суперпользователь — импорт не должен быть обходным путём.
        "can_apply_global": request.user.is_superuser,
    })


@staff_required
def import_apply(request):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    if _import_blocked():
        return _error_modal(request, "Идёт синхронизация — импорт отменён.")

    uploaded = request.session.get("import_payload")
    if not uploaded:
        return _error_modal(
            request, "Данные для импорта потерялись (истекла сессия). Загрузите файлы заново."
        )

    selected = request.POST.getlist("apply")
    replace_secrets = bool(request.POST.get("replace_secrets"))
    apply_config = bool(request.POST.get("apply_config"))
    apply_ldap = bool(request.POST.get("apply_ldap"))

    if (apply_config or apply_ldap) and not request.user.is_superuser:
        return _error_modal(
            request,
            "Подключение к NetBox и настройки LDAP импортирует только суперпользователь — "
            "снимите эти пункты или попросите его.", status=403)
    if not selected and not apply_config and not apply_ldap:
        return _error_modal(request, "Не отмечено ни одной записи")

    try:
        applied = settings_bundle.apply(
            uploaded, selected,
            replace_secrets=replace_secrets,
            apply_config=apply_config,
            apply_ldap=apply_ldap,
        )
    except (settings_bundle.BundleError, sc.SyncConfigError) as e:
        return _error_modal(request, str(e))

    request.session.pop("import_payload", None)
    # В журнал — что применили; значения полей сюда не попадают.
    _audit(request, "import", f"{len(applied)} записей", "update", "; ".join(applied))
    return _redirect_to("devices", f"Импортировано записей: {len(applied)}")


# ── Проверка подключений ───────────────────────────────────────────────────
# Кнопка «Тест» у каждого устройства и гипервизора. Сама проверка идёт в
# фоне (services/connection_test.py), сюда приходят только запуск и опрос
# состояния.

def _test_button(request, kind, key, test=None, busy_message=""):
    """Партиал кнопки. Он же ответ на запуск и на каждый опрос состояния."""
    if test is None:
        test = connection_test.state_for(kind, [key]).get(key)
    return render(request, "ip_plan/_settings_test_button.html", {
        "kind": kind, "key": key, "test": test, "busy_message": busy_message,
    })


@staff_required
def test_start(request, kind, key):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    if kind not in {"device", "redvirt", "vcenter"}:
        return _error_modal(request, f"Неизвестный тип объекта: {kind}")

    busy = ""
    try:
        test = connection_test.start(kind, key, username=request.user.username)
    except connection_test.TestBusy as e:
        # Не ошибка, а нормальный отказ: элемент остаётся как был.
        test, busy = None, str(e)

    # Запуск бывает из двух мест, и ответ должен подходить тому, кто спросил:
    # из списка — кнопка, из окна проверки — его тело.
    if request.POST.get("from") == "terminal":
        return test_terminal_body(request, kind, key)
    return _test_button(request, kind, key, test=test, busy_message=busy)


@staff_required
def test_status(request, kind, key):
    """Опрос состояния. Пока проверка идёт, шаблон сам продолжает опрашивать."""
    return _test_button(request, kind, key)


@staff_required
def test_terminal(request, kind, key):
    """Окно с ходом проверки конкретного объекта."""
    test = connection_test.state_for(kind, [key]).get(key)
    return render(request, "ip_plan/_settings_test_terminal.html", {
        "kind": kind, "key": key, "test": test, "modal": True,
    })


@staff_required
def test_terminal_body(request, kind, key):
    """Только содержимое окна — его и обновляет опрос, пока тест идёт."""
    test = connection_test.state_for(kind, [key]).get(key)
    return render(request, "ip_plan/_settings_test_terminal.html", {
        "kind": kind, "key": key, "test": test, "modal": False,
    })


# Подписи флагов устройства. Одним списком — чтобы форма, её обработчик и
# порядок шагов синка (sc.SYNC_FLAGS) не разъезжались.
_DEVICE_FLAG_FIELDS = [
    ("sync_interfaces", "Интерфейсы и адрес управления", ""),
    ("import_vlans", "VLAN-ы", ""),
    ("import_prefixes", "Префиксы сетей", ""),
    ("sync_macs", "MAC-таблицу",
     "Eltex, Maipu, Huawei; у ViPNet это ARP-таблица. Снимается внутри шага "
     "интерфейсов — без него не соберётся"),
    ("sync_lldp", "LLDP-соседей",
     "не умеет только xFW; команда задаётся на вкладке «Модели»"),
    ("backup_config", "Резервную копию конфигурации", ""),
    ("sync_blocklist", "Списки блокировок firewall", ""),
]
assert {k for k, _, _ in _DEVICE_FLAG_FIELDS} == set(sc.SYNC_FLAGS)


def _device_flag_fields(cfg, device_types):
    """(ключ, подпись, пояснение, отмечен, унаследован, на модели) для блока
    флагов. «На модели» — включён ли шаг у модели: по нему карточка
    подписывает, что снятая галочка запишется как «выключено здесь»."""
    eff = sc.effective(cfg, device_types)
    model_cfg = (device_types or {}).get(cfg.get("model")) or {}
    return [(key, label, hint, bool(eff.get(key)), key not in cfg and bool(eff.get(key)),
             bool(model_cfg.get(key)))
            for key, label, hint in _DEVICE_FLAG_FIELDS]


@staff_required
def device_flags(request):
    """Блок «Что синхронизировать» под выбранную в форме модель.

    Флаги модели — умолчания её устройств, поэтому при смене модели в
    выпадающем списке галочки должны встать сразу: иначе новое устройство
    заводится со всем снятым, а человек видит модель, у которой шаги
    включены. Собственные значения устройства, если оно уже существует,
    остаются главнее — как и в синке.
    """
    data, _ = sc.load("devices")
    name = request.GET.get("name") or None
    cfg = dict(((data.get("devices") or {}).get(name)) or {}) if name else {}
    cfg["model"] = (request.GET.get("model") or "").strip()
    return render(request, "ip_plan/_settings_device_flags.html",
                  {"flag_fields": _device_flag_fields(cfg, data.get("device_types") or {})})


def _load_error_page(request, active, error):
    """Карточка, которую нечем построить (нет devices.json, реестра платформ
    и т. п.): та же плашка, что у вкладок, вместо 500."""
    ctx = _page_ctx(request, active)
    ctx["load_error"] = str(error)
    return render(request, "ip_plan/settings.html", ctx)


@staff_required
def device_form(request, name=None):
    try:
        return _device_form(request, name)
    except sc.SyncConfigError as e:
        return _load_error_page(request, "devices", e)


def _device_form(request, name):
    data, digest = sc.load("devices")
    devices = data.get("devices") or {}
    if name is not None and name not in devices:
        raise Http404(f"Устройство «{name}» не найдено")

    cfg = devices.get(name, {}) if name else {}
    # Галочки — по эффективной картине (модель + устройство), как в списке и
    # как в самом синке. Раньше форма показывала только собственные поля
    # устройства, и шаг, включённый у модели, выглядел выключенным: человек
    # видел снятую галочку «MAC-таблицу» там, где MAC исправно собирался.
    flag_fields = _device_flag_fields(cfg, data.get("device_types") or {})
    types = data.get("device_types") or {}
    # Чем ходят на устройства каждой модели — по нему форма показывает либо
    # логин/пароль SSH, либо реквизиты SNMPv3, и переключается при смене
    # модели прямо в браузере (Alpine), без похода на сервер.
    model_transports = {m: sc.transport((t or {}).get("interfaces_parser", "")) for m, t in types.items()}
    ctx = _page_ctx(request, "devices")
    model_cfg = types.get(cfg.get("model")) or {}
    ctx.update({
        "name": name,
        "chips": _chips(cfg, model_cfg, sc.effective(cfg, types)) if name else [],
        "test": connection_test.state_for("device", [name]).get(name) if name else None,
        "testable": bool(name) and bool(cfg.get("host")) and not cfg.get("cluster_peer_of"),
        "peer_candidates": sorted(n for n in devices if n != name),
        "cfg": cfg,
        "flag_fields": flag_fields,
        "password_mask": sc.masked(cfg.get("password")),
        "enable_mask": sc.masked(cfg.get("enable_password")),
        "snmp_auth_mask": sc.masked(cfg.get("snmp_auth_password")),
        "snmp_priv_mask": sc.masked(cfg.get("snmp_priv_password")),
        "snmp_auth_protos": sc.SNMP_AUTH_PROTOS,
        "snmp_priv_protos": sc.SNMP_PRIV_PROTOS,
        "model_transports": json.dumps(model_transports),
        "device_types": sorted(data.get("device_types") or {}),
        "statuses": sc.DEVICE_STATUSES,
        "sites": sc.site_options(),
        "site": cfg.get("site") or sc.DEFAULT_SITE_SLUG,
        "blocklist_objects": json.dumps(cfg.get("blocklist_objects") or {},
                                        ensure_ascii=False, indent=2),
        "snmp_communities": ", ".join(cfg.get("snmp_communities") or []),
        "snmp_version": sc.snmp_version(cfg),
        "snmp_versions": sc.SNMP_VERSIONS,
        "file_hash": digest,
    })
    return render(request, "ip_plan/settings_device.html", ctx)


@staff_required
def device_save(request, name=None):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)

    try:
        data, _ = sc.load("devices")
        devices = data.get("devices") or {}
        previous = devices.get(name, {}) if name else {}

        p = request.POST
        new_name = sc.validate_name(p.get("name"), set(devices), original=name, field="Имя устройства")

        model = (p.get("model") or "").strip()
        if model not in (data.get("device_types") or {}):
            raise sc.SyncConfigError(
                f"Модель «{model}» отсутствует в device_types. Заведите её на вкладке «Модели»."
            )

        cfg = dict(previous)
        type_cfg = data["device_types"][model] or {}
        parser = type_cfg.get("interfaces_parser", "")
        transport = sc.transport(parser)

        # Адрес, по которому устройство учитывается в NetBox, когда он не
        # совпадает с адресом подключения: к криптошлюзам ходят через
        # ViPNet-туннель (7.0.0.x), а в учёте они должны стоять реальным
        # адресом. Пусто — берётся host, как было всегда.
        mgmt_ip = (p.get("mgmt_ip") or "").strip()
        if mgmt_ip:
            cfg["mgmt_ip"] = sc.validate_host(mgmt_ip, field="Адрес управления")
        else:
            cfg.pop("mgmt_ip", None)

        _SSH_KEYS = ("port", "username", "password", "enable_password")
        _CLUSTER_KEYS = ("cluster_reserve", "cluster_peer_of", "reserve_interface")

        # ── Раздел «SNMP» — у всех устройств, независимо от транспорта ─────
        # Пустая версия чистит все snmp_*-ключи: раздел выключен целиком, а не
        # оставлен с мёртвыми реквизитами.
        version = (p.get("snmp_version") or "").strip()
        if version not in sc.SNMP_VERSIONS:
            raise sc.SyncConfigError(f"Версия SNMP «{version}» недопустима")
        if not version:
            for key in sc.SNMP_KEYS:
                cfg.pop(key, None)
        else:
            cfg["snmp_version"] = version
            cfg["snmp_port"] = sc.validate_port(p.get("snmp_port") or 161)
            if version == "v2c":
                communities = [c.strip() for c in (p.get("snmp_communities") or "").split(",") if c.strip()]
                if not communities:
                    raise sc.SyncConfigError("SNMP v2c: укажите community (одну или несколько через запятую)")
                cfg["snmp_communities"] = communities
                for key in ("snmp_user", "snmp_auth_proto", "snmp_auth_password",
                            "snmp_priv_proto", "snmp_priv_password"):
                    cfg.pop(key, None)
            else:
                cfg.pop("snmp_communities", None)
                cfg["snmp_user"] = (p.get("snmp_user") or "").strip()
                if not cfg["snmp_user"]:
                    raise sc.SyncConfigError("SNMP v3: имя безопасности — обязательное поле")
                auth_proto = (p.get("snmp_auth_proto") or "md5").strip().lower()
                if auth_proto not in sc.SNMP_AUTH_PROTOS:
                    raise sc.SyncConfigError(f"SNMP v3: аутентификация «{auth_proto}» недопустима")
                cfg["snmp_auth_proto"] = auth_proto
                cfg["snmp_auth_password"] = sc.keep_or_new(p.get("snmp_auth_password"),
                                                           previous.get("snmp_auth_password", ""))
                if not cfg["snmp_auth_password"]:
                    raise sc.SyncConfigError("SNMP v3: пароль аутентификации — обязательное поле")
                priv_proto = (p.get("snmp_priv_proto") or "des").strip().lower()
                if priv_proto not in sc.SNMP_PRIV_PROTOS:
                    raise sc.SyncConfigError(f"SNMP v3: шифрование «{priv_proto}» недопустимо")
                priv = sc.keep_or_new(p.get("snmp_priv_password"), previous.get("snmp_priv_password", ""))
                if priv:
                    cfg["snmp_priv_proto"] = priv_proto
                    cfg["snmp_priv_password"] = priv
                else:
                    cfg.pop("snmp_priv_proto", None)
                    cfg.pop("snmp_priv_password", None)

        if transport == "snmp":
            # Континент: весь сбор идёт по SNMP (netbox_continent_snmp.py), SSH-
            # реквизиты не нужны. Кластер учитывается одним устройством с
            # реквизитами плюс инвентарной записью резервной платформы без
            # подключения («Резерв для»): у пассивной ноды те же IP и MAC, и
            # снаружи она не отвечает.
            for key in _SSH_KEYS:
                cfg.pop(key, None)
            peer_of = (p.get("cluster_peer_of") or "").strip()
            host = (p.get("host") or "").strip()
            if host:
                cfg["host"] = sc.validate_host(host)
            elif peer_of:
                cfg.pop("host", None)
            else:
                raise sc.SyncConfigError(
                    "Адрес: обязательное поле. Пустым он бывает только у инвентарной записи "
                    "резервной платформы — тогда заполните «Резерв для»."
                )
            if peer_of:
                if peer_of not in devices or peer_of == new_name:
                    raise sc.SyncConfigError(f"«Резерв для»: устройства «{peer_of}» нет в списке")
                cfg["cluster_peer_of"] = peer_of
                for key in sc.SNMP_KEYS + ("cluster_reserve", "reserve_interface"):
                    cfg.pop(key, None)
            else:
                cfg.pop("cluster_peer_of", None)
                if not cfg.get("snmp_version"):
                    raise sc.SyncConfigError(
                        f"Модель «{model}» опрашивается по SNMP — заполните раздел «SNMP» "
                        f"(для Континента это v3)."
                    )
                for key, field in (("cluster_reserve", "Резервная платформа"),
                                   ("reserve_interface", "Интерфейс резервирования")):
                    value = (p.get(key) or "").strip()
                    if value:
                        cfg[key] = sc.validate_name(value, set(), field=field) if key == "cluster_reserve" else value
                    else:
                        cfg.pop(key, None)
        else:
            for key in _CLUSTER_KEYS:
                cfg.pop(key, None)
            cfg["host"] = sc.validate_host(p.get("host"))
            cfg["port"] = sc.validate_port(p.get("port") or 22)
            cfg["username"] = (p.get("username") or "").strip()
            if not cfg["username"]:
                raise sc.SyncConfigError("Логин: обязательное поле")
            cfg["password"] = sc.keep_or_new(p.get("password"), previous.get("password", ""))
            if not cfg["password"]:
                raise sc.SyncConfigError("Пароль: обязательное поле")

            enable = sc.keep_or_new(p.get("enable_password"), previous.get("enable_password", ""))
            if enable:
                cfg["enable_password"] = enable
            else:
                cfg.pop("enable_password", None)

        cfg["model"] = model
        cfg["site"] = sc.validate_site(p.get("site"), sc.site_options())
        status = (p.get("status") or "active").strip()
        if status not in sc.DEVICE_STATUSES:
            raise sc.SyncConfigError(f"Статус «{status}» недопустим")
        cfg["status"] = status

        # Три состояния, а не два. Галочка стоит — true. Галочка снята, но шаг
        # включён у модели — пишем явный false: ключ, удалённый из блока
        # устройства, означает «наследовать», и шаг остался бы включённым.
        # Именно из-за этого снятая галочка «MAC-таблицу» ничего не выключала:
        # sync_macs задан у шести моделей из семи. Снята и у модели ничего —
        # ключ убираем, чтобы не засорять конфиг мусорными false.
        # И ещё одно: галочка, совпадающая с моделью, у устройства без личного
        # значения — это наследование, ключ не пишем. Иначе простое
        # «Сохранить» без правок превращало унаследованные шаги в личные
        # (чип из «унаследовано» становился «задано у модели и устройства»).
        for flag in sc.SYNC_FLAGS:
            checked, model_on = bool(p.get(flag)), bool(type_cfg.get(flag))
            if flag not in previous and checked == model_on:
                cfg.pop(flag, None)
            elif checked:
                cfg[flag] = True
            elif model_on:
                cfg[flag] = False
            else:
                cfg.pop(flag, None)
        # Инвентарная запись резервной платформы сама ничего не собирает: её
        # единственный интерфейс заводит сборщик основного узла. Явный false
        # нужен, потому что sync_macs включён на модели.
        if cfg.get("cluster_peer_of"):
            for flag in sc.SYNC_FLAGS:
                if type_cfg.get(flag):
                    cfg[flag] = False
                else:
                    cfg.pop(flag, None)

        # Ядро сети — единственный источник префиксов площадки (маршрутизирует
        # только xFW; адреса на L3-коммутаторах служебные).
        if p.get("network_core"):
            cfg["network_core"] = True
        else:
            cfg.pop("network_core", None)

        # Объекты блокировки разбираем здесь (это поле формы), а проверку
        # эффективной картины делает общий sc.validate_device — им же
        # проверяется импорт.
        eff = sc.effective(cfg, data.get("device_types") or {})
        if eff.get("sync_blocklist"):
            raw = (p.get("blocklist_objects") or "").strip()
            try:
                cfg["blocklist_objects"] = json.loads(raw) if raw else {}
            except json.JSONDecodeError as e:
                raise sc.SyncConfigError(f"Объекты блокировки: не разбирается как JSON ({e})")
        else:
            cfg.pop("blocklist_objects", None)
        sc.validate_device(cfg, data.get("device_types") or {})

        if name and new_name != name:
            devices.pop(name, None)
        devices[new_name] = cfg
        data["devices"] = devices

        sc.save("devices", data, p.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    changed = _describe_changes(previous, cfg) if name else "создано"
    _audit(request, "devices", new_name, "update" if name else "create", changed)
    return _redirect_to("devices", f"Устройство «{new_name}» сохранено")


@staff_required
def device_delete(request, name):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        data, _ = sc.load("devices")
        devices = data.get("devices") or {}
        if name not in devices:
            raise sc.SyncConfigError(f"Устройство «{name}» не найдено")
        devices.pop(name)
        data["devices"] = devices
        sc.save("devices", data, request.POST.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    _audit(request, "devices", name, "delete")
    return _redirect_to("devices", f"Устройство «{name}» удалено")


def _describe_changes(before, after):
    """Человекочитаемый diff для журнала. Секреты не раскрываем — только
    факт изменения."""
    parts = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if sc.is_secret(key):
            parts.append(f"{key}: изменён")
        else:
            parts.append(f"{key}: {old!r} → {new!r}")
    return "; ".join(parts) or "без изменений"


# ── Модели оборудования (device_types) ─────────────────────────────────────
# Поля здесь напрямую управляют тем, какие CLI-команды синк выполнит на
# боевом железе, поэтому форма ограничена известными ключами, а парсер —
# строго списком реализованных в коде.

_TYPE_TEXT_FIELDS = [
    ("manufacturer", "Производитель"),
    ("role", "Роль в NetBox"),
    # Режим кластера (sc.CLUSTER_MODES). До 02.10.2026 поля не было вовсе:
    # cluster=vipnet_failover жил только в JSON и переживал сохранение лишь
    # благодаря dict(previous).
    ("cluster", "Кластер"),
    ("no_pager", "Команда отключения постраничного вывода"),
    ("encoding", "Кодировка вывода CLI (пусто = utf-8; ViPNet — koi8-r)"),
    ("interfaces_command", "Команда списка интерфейсов"),
    ("description_command", "Команда описаний интерфейсов"),
    ("vlan_command", "Команда VLAN-ов"),
    ("mac_command", "Команда MAC-таблицы"),
    ("lag_command", "Команда LAG"),
    ("ip_command", "Команда IP-адресов"),
    ("lldp_command", "Команда LLDP-соседей"),
    ("arp_command", "Команда ARP-таблицы (L3-коммутатор: IP ↔ MAC для связей)"),
    ("serial_command", "Команда серийного номера (если по SNMP номера нет)"),
    ("config_command", "Команда выгрузки конфигурации"),
    ("vrrp_command", "Команда VRRP"),
]
_TYPE_FLAG_FIELDS = [
    ("legacy_crypto", "Устаревшие алгоритмы SSH"),
    ("import_prefixes", "Импортировать префиксы"),
    ("import_vlans", "Импортировать VLAN-ы"),
    ("sync_macs", "Собирать MAC-таблицу"),
    ("sync_lldp", "Собирать LLDP-соседей"),
    ("backup_config", "Резервировать конфигурацию"),
]
# Списки алгоритмов SSH — вводятся через запятую, хранятся списком.
_TYPE_LIST_FIELDS = [
    ("kex_algs", "Алгоритмы обмена ключами"),
    ("encryption_algs", "Алгоритмы шифрования"),
    ("mac_algs", "Алгоритмы MAC"),
    ("server_host_key_algs", "Алгоритмы ключа хоста"),
]


# Шаги, которые модель задаёт своим устройствам по умолчанию, — строки
# переключателей карточки модели. sync_interfaces и sync_blocklist — уровня
# устройства (интерфейсы снимаются всегда, блокировки — только у xFW).
_MODEL_FLAG_ROWS = [
    ("import_prefixes", "Подсети", "Импортировать префиксы — только с ядра сети"),
    ("import_vlans", "VLAN", "Импортировать VLAN-ы"),
    ("sync_macs", "MAC-таблица", "Собирается вместе с интерфейсами"),
    ("sync_lldp", "LLDP-соседи", "Связи между устройствами"),
    ("backup_config", "Снимок конфигурации", "Нужен для раздела ACL"),
]
# Команды устройства в порядке карточки модели.
_MODEL_COMMANDS = [
    ("interfaces_command", "Список интерфейсов"),
    ("description_command", "Описания интерфейсов"),
    ("vlan_command", "VLAN"),
    ("mac_command", "MAC-таблица"),
    ("lag_command", "LAG"),
    ("ip_command", "IP-адреса"),
    ("lldp_command", "LLDP-соседи"),
    ("arp_command", "ARP-таблица (L3)"),
    ("vrrp_command", "VRRP"),
    ("serial_command", "Серийный номер"),
    ("config_command", "Выгрузка конфигурации"),
]
assert {k for k, _ in _MODEL_COMMANDS} | {"manufacturer", "role", "cluster", "no_pager", "encoding"} \
    == {k for k, _ in _TYPE_TEXT_FIELDS}


def _with_counts(names, used):
    """[{name, count}] — справочник NetBox плюс то, что уже есть в devices.json
    (на случай лежащего NetBox или имени, ещё не заведённого синком)."""
    return [{"name": n, "count": used.get(n, 0)}
            for n in sorted(set(names) | set(used), key=str.lower)]


@staff_required
def device_type_form(request, name=None):
    try:
        return _device_type_form(request, name)
    except sc.SyncConfigError as e:
        return _load_error_page(request, "models", e)


def _device_type_form(request, name):
    data, digest = sc.load("devices")
    types = data.get("device_types") or {}
    if name is not None and name not in types:
        raise Http404(f"Модель «{name}» не найдена")
    cfg = types.get(name, {}) if name else {}

    site_names = {s["slug"]: s["name"] for s in site_options_cached()}
    members = []
    for dev_name, dev_cfg in sorted((data.get("devices") or {}).items()):
        if name and dev_cfg.get("model") == name:
            slug = dev_cfg.get("site") or sc.DEFAULT_SITE_SLUG
            members.append({
                "name": dev_name,
                "site_label": site_names.get(slug, slug),
                # Личные значения флагов: при смене флага у модели они
                # снимаются (массовый выключатель, см. device_type_save) —
                # карточка заранее перечисляет, у кого именно.
                "explicit": {k: dev_cfg[k] for k in sc.SYNC_FLAGS if k in dev_cfg},
                "testable": bool(dev_cfg.get("host")) and not dev_cfg.get("cluster_peer_of"),
            })
    vendors_used, roles_used = {}, {}
    for t in types.values():
        if t.get("manufacturer"):
            vendors_used[t["manufacturer"]] = vendors_used.get(t["manufacturer"], 0) + 1
        if t.get("role"):
            roles_used[t["role"]] = roles_used.get(t["role"], 0) + 1

    ctx = _page_ctx(request, "models")
    ctx.update({
        "name": name,
        "cfg": cfg,
        "manufacturers": _with_counts(sc.netbox_names("manufacturers"), vendors_used),
        "roles": _with_counts(sc.netbox_names("roles"), roles_used),
        "parsers": sc.parser_catalog(),
        "cluster_modes": sc.CLUSTER_MODES,
        "flag_rows": [{"key": k, "title": t, "note": n, "on": bool(cfg.get(k))}
                      for k, t, n in _MODEL_FLAG_ROWS],
        "commands": [{"key": k, "label": label, "value": cfg.get(k, "")} for k, label in _MODEL_COMMANDS],
        "legacy_crypto": bool(cfg.get("legacy_crypto")),
        "policy_commands": json.dumps(cfg.get("policy_commands") or {},
                                      ensure_ascii=False, indent=2) if cfg.get("policy_commands") else "",
        "list_fields": [(k, label, ", ".join(cfg.get(k) or [])) for k, label in _TYPE_LIST_FIELDS],
        "members": members,
        "test_target": next((m["name"] for m in members if m["testable"]), ""),
        "mac_capable": [p for p in sc.INTERFACE_PARSERS if p in sc.MAC_CAPABLE_PARSERS],
        "lldp_capable": [p for p in sc.INTERFACE_PARSERS if p in sc.LLDP_CAPABLE_PARSERS],
        "file_hash": digest,
    })
    return render(request, "ip_plan/settings_model.html", ctx)


@staff_required
def device_type_save(request, name=None):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        data, _ = sc.load("devices")
        types = data.get("device_types") or {}
        previous = types.get(name, {}) if name else {}
        p = request.POST

        applied = []
        new_name = sc.validate_name(p.get("name"), set(types), original=name,
                                    field="Название модели", allow_spaces=True)

        parser = (p.get("interfaces_parser") or "").strip()

        cfg = dict(previous)
        cfg["interfaces_parser"] = parser
        for key, _label in _TYPE_TEXT_FIELDS:
            value = (p.get(key) or "").strip()
            if value:
                cfg[key] = value
            else:
                cfg.pop(key, None)
        for key, _label in _TYPE_FLAG_FIELDS:
            if p.get(key):
                cfg[key] = True
            else:
                cfg.pop(key, None)
        for key, _label in _TYPE_LIST_FIELDS:
            raw = (p.get(key) or "").strip()
            if raw:
                cfg[key] = [item.strip() for item in raw.split(",") if item.strip()]
            else:
                cfg.pop(key, None)

        raw = (p.get("policy_commands") or "").strip()
        if raw:
            try:
                policy = json.loads(raw)
            except json.JSONDecodeError as e:
                raise sc.SyncConfigError(f"Команды политики доступа: не разбирается как JSON ({e})")
            cfg["policy_commands"] = ({k: v.strip() if isinstance(v, str) else v for k, v in policy.items()}
                                      if isinstance(policy, dict) else policy)
        else:
            cfg.pop("policy_commands", None)
        sc.validate_device_type(cfg)

        # Флаги модели — умолчания её устройств и заодно массовый выключатель:
        # изменённый здесь шаг применяется ко всем устройствам этой модели.
        # Иначе «выключил у модели» не меняло бы ничего там, где флаг
        # проставлен устройству персонально, а таких большинство. Технически
        # достаточно убрать персональное значение: устройство начинает
        # наследовать новое значение модели — включённое или выключенное.
        for key, _label in _TYPE_FLAG_FIELDS:
            if key not in sc.SYNC_FLAGS:      # legacy_crypto — параметр SSH, не шаг
                continue
            if bool(previous.get(key)) == bool(cfg.get(key)):
                continue
            for dev_name, dev_cfg in (data.get("devices") or {}).items():
                if dev_cfg.get("model") == name and key in dev_cfg:
                    dev_cfg.pop(key)
                    applied.append(f"{dev_name} ({key})")

        # Переименование модели: перевесить ссылающиеся устройства, иначе
        # они останутся с несуществующей моделью и синк их пропустит.
        renamed = []
        if name and new_name != name:
            types.pop(name, None)
            for dev_name, dev_cfg in (data.get("devices") or {}).items():
                if dev_cfg.get("model") == name:
                    dev_cfg["model"] = new_name
                    renamed.append(dev_name)

        types[new_name] = cfg
        data["device_types"] = types
        sc.save("devices", data, p.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    note = ""
    then_test = (p.get("then_test") or "").strip()
    if then_test:
        if (data.get("devices") or {}).get(then_test, {}).get("model") == new_name:
            try:
                connection_test.start("device", then_test, username=request.user.username)
                note = f"; проверка подключения {then_test} запущена"
            except connection_test.TestBusy as e:
                note = f"; проверка не запущена: {e}"

    details = _describe_changes(previous, cfg) if name else "создана"
    if applied:
        details += f"; флаги применены к устройствам: {', '.join(applied)}"
    if renamed:
        details += f"; переименование применено к устройствам: {', '.join(renamed)}"
    _audit(request, "device_types", new_name, "update" if name else "create", details)
    if then_test and note.startswith("; проверка подключения"):
        return _redirect_to("devices", f"Модель «{new_name}» сохранена{note}")
    return _redirect_to("models", f"Модель «{new_name}» сохранена{note}")


@staff_required
def device_type_delete(request, name):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        data, _ = sc.load("devices")
        types = data.get("device_types") or {}
        if name not in types:
            raise sc.SyncConfigError(f"Модель «{name}» не найдена")
        used = [d for d, cfg in (data.get("devices") or {}).items() if cfg.get("model") == name]
        if used:
            raise sc.SyncConfigError(
                f"Модель «{name}» используется устройствами: {', '.join(used)}. "
                f"Сначала переведите их на другую модель."
            )
        types.pop(name)
        data["device_types"] = types
        sc.save("devices", data, request.POST.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    _audit(request, "device_types", name, "delete")
    return _redirect_to("models", f"Модель «{name}» удалена")


# ── Гипервизоры (sources_redvirt.json / sources_vcenter.json) ──────────────
# Оба файла — плоские JSON-массивы объектов {name, url, user, password, site}.
# source_save собирает объект заново из этих пяти полей: ключ, дописанный в
# JSON руками, при сохранении из формы пропадёт.
# Порядок элементов сохраняем: скрипты синка обходят источники по порядку.

_SOURCE_KEYS = {"redvirt": "sources_redvirt", "vcenter": "sources_vcenter"}
_SOURCE_TITLES = {"redvirt": "RedVirt/oVirt", "vcenter": "vCenter"}


def _source_key(kind):
    try:
        return _SOURCE_KEYS[kind]
    except KeyError:
        raise sc.SyncConfigError(f"Неизвестный тип источника: {kind}")


@staff_required
def source_form(request, kind, index=None):
    try:
        key = _source_key(kind)
        sources, digest = sc.load(key)
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e))

    item = {}
    if index is not None:
        if not 0 <= index < len(sources):
            return _error_modal(request, "Источник не найден", status=404)
        item = sources[index]

    return render(request, "ip_plan/_settings_source_form.html", {
        "kind": kind,
        "kind_title": _SOURCE_TITLES[kind],
        "index": index,
        "item": item,
        "sites": sc.site_options(),
        "site": item.get("site") or sc.DEFAULT_SITE_SLUG,
        "password_mask": sc.masked(item.get("password")),
        "file_hash": digest,
        # У oVirt адрес — это API-эндпоинт, а не веб-морда: подсказываем,
        # но не запрещаем (вдруг нестандартная установка).
        "url_hint": "https://vmm.example.local/ovirt-engine/api" if kind == "redvirt"
                    else "https://vcenter.example.local",
    })


@staff_required
def source_save(request, kind, index=None):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        key = _source_key(kind)
        sources, _ = sc.load(key)
        previous = sources[index] if index is not None and 0 <= index < len(sources) else {}
        p = request.POST

        others = {s.get("name") for i, s in enumerate(sources) if i != index}
        name = sc.validate_name(p.get("name"), others, field="Имя источника")
        url = sc.validate_url(p.get("url"), field="URL")
        user = (p.get("user") or "").strip()
        if not user:
            raise sc.SyncConfigError("Пользователь: обязательное поле")
        password = sc.keep_or_new(p.get("password"), previous.get("password", ""))
        if not password:
            raise sc.SyncConfigError("Пароль: обязательное поле")

        # Площадка источника — площадка всех его кластеров, хостов и ВМ в
        # NetBox: один менеджер виртуализации обслуживает одну площадку.
        site = sc.validate_site(p.get("site"), sc.site_options())
        item = {"name": name, "url": url, "user": user, "password": password, "site": site}
        if index is None:
            sources.append(item)
        else:
            sources[index] = item

        sc.save(key, sources, p.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    _audit(request, key, name, "update" if index is not None else "create",
           _describe_changes(previous, item) if index is not None else "создан")
    return _redirect_to("hypervisors", f"Источник «{name}» сохранён")


@staff_required
def source_delete(request, kind, index):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        key = _source_key(kind)
        sources, _ = sc.load(key)
        if not 0 <= index < len(sources):
            raise sc.SyncConfigError("Источник не найден")
        removed = sources.pop(index)
        sc.save(key, sources, request.POST.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    _audit(request, key, removed.get("name", ""), "delete")
    return _redirect_to("hypervisors", f"Источник «{removed.get('name', '')}» удалён")


# ── Скан и DNS (config.json) ───────────────────────────────────────────────

@staff_required
def scan_save(request):
    """Списки приходят как многострочные textarea — по значению в строке.
    Так проще всего и вводить, и валидировать построчно с понятной ошибкой."""
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        data, _ = sc.load("config")
        before = {
            "dns_servers": data.get("dns_servers"),
            "dns_skip_pattern": data.get("dns_skip_pattern"),
            "scan_exclude_prefixes": data.get("scan_exclude_prefixes"),
        }
        p = request.POST

        servers = []
        for line in (p.get("dns_servers") or "").splitlines():
            line = line.strip()
            if line:
                servers.append(sc.validate_host(line, field="DNS-сервер"))

        prefixes = []
        for line in (p.get("scan_exclude_prefixes") or "").splitlines():
            line = line.strip()
            if line:
                prefixes.append(sc.validate_prefix(line))

        pattern = (p.get("dns_skip_pattern") or "").strip()
        if pattern:
            import re as _re
            try:
                _re.compile(pattern)
            except _re.error as e:
                raise sc.SyncConfigError(f"Шаблон пропуска DNS: некорректное регулярное выражение ({e})")

        data["dns_servers"] = servers
        data["dns_skip_pattern"] = pattern
        data["scan_exclude_prefixes"] = prefixes
        sc.save("config", data, p.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    after = {"dns_servers": servers, "dns_skip_pattern": pattern, "scan_exclude_prefixes": prefixes}
    _audit(request, "config", "scan_dns", "update", _describe_changes(before, after))
    return _redirect_to("scan", "Настройки скана и DNS сохранены")


# ── Доступ к хостам (ssh_access.json) ──────────────────────────────────────
# Служебная учётная запись и закрытый ключ для входа на серверы и ВМ.
# Ключ в браузер не возвращается: пустое поле ключа = «оставить прежний».

@staff_required
def ssh_access_save(request):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    p = request.POST
    try:
        cfg, _ = ssh_access.load()
        username = ssh_access.validate_username(p.get("username"))
        port = sc.validate_port(p.get("port") or ssh_access.DEFAULT_PORT)
        new_key = ssh_access.normalize_key(p.get("private_key"))
        key = new_key or cfg.get("private_key") or ""
        if not key:
            raise ssh_access.SshAccessError("Вставьте закрытый ключ")
        passphrase = sc.keep_or_new(p.get("passphrase"), cfg.get("passphrase") or "")
        # Ключ без парольной фразы читается и так — тогда фразу не храним
        # вовсе, даже если она осталась от прежнего ключа.
        if ssh_access.is_encrypted(key):
            pkey = ssh_access.parse_key(key, passphrase)
        else:
            pkey = ssh_access.parse_key(key)
            passphrase = ""
        ssh_access.save({"username": username, "port": port, "private_key": key,
                         "passphrase": passphrase}, p.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    info = ssh_access.describe_key(pkey)
    details = f"пользователь {username}, порт {port}; ключ {info['kind']} {info['fingerprint']}"
    if new_key:
        details += " (ключ заменён)" if cfg.get("private_key") else " (ключ задан)"
    _audit(request, "ssh_access", username, "update" if cfg else "create", details)
    return _redirect_to("access", "Доступ к хостам сохранён")


@staff_required
def ssh_access_delete(request):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        cfg, _ = ssh_access.load()
    except sc.SyncConfigError:
        cfg = {}
    ssh_access.delete()
    _audit(request, "ssh_access", cfg.get("username", ""), "delete", "ключ и пользователь удалены")
    return _redirect_to("access", "Ключ удалён")


@staff_required
def ssh_access_test(request):
    """Вход на указанный хост СОХРАНЁННЫМ ключом и чтение сведений об ОС.
    На хосте выполняется одна команда чтения."""
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        host = sc.validate_host(request.POST.get("test_host"), field="Хост")
        ok, lines, _ = ssh_access.probe(host)
    except sc.SyncConfigError as e:
        ok, lines = False, [str(e)]
        host = request.POST.get("test_host") or ""
    logger.info("Проверка SSH-ключа: %s → %s: %s", request.user.username, host, "успех" if ok else "отказ")
    return render(request, "ip_plan/_settings_ldap_result.html", {"ok": ok, "message": "\n".join(lines)})


@superuser_required
def environment_save(request, env):
    """URL и токен NetBox. Отдельно от остального config.json и только для
    суперпользователя: ошибка здесь останавливает синк целиком."""
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        data, _ = sc.load("config")
        envs = data.get("environments") or {}
        if env not in envs:
            raise sc.SyncConfigError(f"Окружение «{env}» не найдено в config.json")
        previous = dict(envs[env])
        p = request.POST

        url = sc.validate_url(p.get("netbox_url"), field="URL NetBox", require_https=False)
        token = sc.keep_or_new(p.get("token"), previous.get("token", ""))
        if not token:
            raise sc.SyncConfigError("Токен: обязательное поле")

        envs[env] = {**previous, "netbox_url": url, "token": token}
        data["environments"] = envs
        sc.save("config", data, p.get("file_hash"))
    except sc.SyncConfigError as e:
        return _error_modal(request, str(e), status=409 if isinstance(e, sc.ConfigConflict) else 400)

    _audit(request, "config", f"environments.{env}", "update",
           _describe_changes(previous, envs[env]))
    return _redirect_to("scan", f"Окружение «{env}» сохранено")


@superuser_required
def environment_form(request, env):
    data, digest = sc.load("config")
    envs = data.get("environments") or {}
    if env not in envs:
        return _error_modal(request, f"Окружение «{env}» не найдено", status=404)
    return render(request, "ip_plan/_settings_environment_form.html", {
        "env": env,
        "netbox_url": envs[env].get("netbox_url", ""),
        "token_mask": sc.masked(envs[env].get("token")),
        "file_hash": digest,
    })


# ── LDAP ───────────────────────────────────────────────────────────────────
# Настройки живут в БД (модель LdapConfig) и применяются со следующего
# входа без перезапуска — см. ip_plan/auth_backends.py.

def _ldap_form_values(post, cfg):
    """Значения из формы, с подстановкой сохранённого пароля, если поле
    оставили пустым. Используется и проверками, и сохранением — важно,
    чтобы проверяли ровно то, что потом запишется."""
    return {
        "servers": [s.strip() for s in (post.get("servers") or "").splitlines() if s.strip()],
        "bind_dn": (post.get("bind_dn") or "").strip(),
        "bind_password": sc.keep_or_new(post.get("bind_password"), cfg.bind_password),
        "user_search_base": (post.get("user_search_base") or "").strip(),
        "user_search_filter": (post.get("user_search_filter") or "").strip(),
        "group_search_base": (post.get("group_search_base") or "").strip(),
        "require_group": (post.get("require_group") or "").strip(),
        "staff_group": (post.get("staff_group") or "").strip(),
        "superuser_group": (post.get("superuser_group") or "").strip(),
    }


def _local_superusers():
    return User.objects.filter(is_superuser=True, is_active=True, local_account__isnull=False)


@superuser_required
def ldap_save(request):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)

    cfg = LdapConfig.load()
    values = _ldap_form_values(request.POST, cfg)
    enabled = bool(request.POST.get("enabled"))

    if not values["servers"] and enabled:
        return _error_modal(request, "Задайте хотя бы один сервер или снимите галочку включения LDAP")
    if enabled and not values["user_search_base"]:
        return _error_modal(request, "База поиска пользователей обязательна")

    # Выключение LDAP без единой локальной учётки с правами суперпользователя
    # закрыло бы вход вообще всем — и восстанавливать пришлось бы через
    # manage.py внутри контейнера.
    if not enabled and not _local_superusers().exists():
        return _error_modal(
            request,
            "Нельзя выключить вход через LDAP: в системе нет ни одной активной локальной "
            "учётной записи с правами суперпользователя. Сначала создайте её на вкладке "
            "«Пользователи»."
        )

    # Проверка bind перед сохранением. Пропускается только явной галочкой —
    # она нужна, например, когда каталог временно недоступен, а поля
    # поправить надо.
    if enabled and not request.POST.get("skip_check"):
        try:
            ldap_check.test_bind(values["servers"], values["bind_dn"], values["bind_password"])
        except ldap_check.LdapCheckError as e:
            return _error_modal(
                request,
                f"Проверка подключения не прошла, настройки НЕ сохранены:\n{e}\n\n"
                f"Если каталог сейчас недоступен, а сохранить нужно — отметьте "
                f"«сохранить без проверки»."
            )

    before = {
        "enabled": cfg.enabled, "servers": cfg.servers, "bind_dn": cfg.bind_dn,
        "user_search_base": cfg.user_search_base, "user_search_filter": cfg.user_search_filter,
        "group_search_base": cfg.group_search_base, "require_group": cfg.require_group,
        "staff_group": cfg.staff_group, "superuser_group": cfg.superuser_group,
        "bind_password": cfg.bind_password,
    }

    cfg.enabled = enabled
    cfg.servers = "\n".join(values["servers"])
    cfg.bind_dn = values["bind_dn"]
    cfg.set_bind_password(values["bind_password"])
    cfg.user_search_base = values["user_search_base"]
    cfg.user_search_filter = values["user_search_filter"] or "(uid=%(user)s)"
    cfg.group_search_base = values["group_search_base"]
    cfg.require_group = values["require_group"]
    cfg.staff_group = values["staff_group"]
    cfg.superuser_group = values["superuser_group"]
    try:
        cfg.cache_timeout = int(request.POST.get("cache_timeout") or 3600)
    except ValueError:
        return _error_modal(request, "Кеш групп: должно быть числом секунд")
    cfg.updated_by = request.user.username
    cfg.save()

    after = {
        "enabled": cfg.enabled, "servers": cfg.servers, "bind_dn": cfg.bind_dn,
        "user_search_base": cfg.user_search_base, "user_search_filter": cfg.user_search_filter,
        "group_search_base": cfg.group_search_base, "require_group": cfg.require_group,
        "staff_group": cfg.staff_group, "superuser_group": cfg.superuser_group,
        "bind_password": values["bind_password"],
    }
    _audit(request, "ldap", "подключение", "update", _describe_changes(before, after))
    return _redirect_to("ldap", "Настройки LDAP сохранены — применятся со следующего входа")


@superuser_required
def ldap_test(request):
    """Проверка соединения и (если указан пользователь) полной цепочки
    входа. Ничего не сохраняет и не создаёт пользователей в базе."""
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)

    cfg = LdapConfig.load()
    values = _ldap_form_values(request.POST, cfg)
    username = (request.POST.get("test_username") or "").strip()
    password = request.POST.get("test_password") or ""

    info = {}
    try:
        lines = ldap_check.test_bind(values["servers"], values["bind_dn"], values["bind_password"])
        if username:
            if not password:
                raise ldap_check.LdapCheckError("Для проверки входа нужен пароль пользователя")
            lines = lines + ["—"] + ldap_check.test_user(values, username, password, info=info)
        ok, message = True, "\n".join(lines)
    except ldap_check.LdapCheckError as e:
        ok, message = False, str(e)

    # Разобранный итог для карточки «Проверить вход»: войдёт ли, в каких
    # группах из настроек состоит, какие права получит. Есть только у
    # успешной проверки пользователя; у проверки одного подключения — None.
    verdict = None
    if ok and username:
        rights = ("суперпользователь" if info.get("superuser")
                  else "настройки" if info.get("staff") else "только просмотр")
        verdict = {"username": username, "dn": info.get("dn", ""),
                   "groups": info.get("groups", []), "rights": rights}
    return render(request, "ip_plan/_settings_ldap_result.html",
                  {"ok": ok, "message": message, "verdict": verdict})


@superuser_required
def ldap_status(request):
    """Плашка «Каталог доступен…» над формой LDAP. Грузится отдельным
    запросом после страницы: опрос каталога может занять до 10 с на сервер,
    и вкладка не должна ждать его."""
    cfg = LdapConfig.load()
    if not cfg.enabled or not cfg.servers:
        return render(request, "ip_plan/_settings_ldap_status.html", {"status": None})
    values = {
        "servers": [x.strip() for x in cfg.servers.splitlines() if x.strip()],
        "bind_dn": cfg.bind_dn, "bind_password": cfg.bind_password,
        "require_group": cfg.require_group,
    }
    status = ldap_check.directory_status(values)
    status["checked"] = sync_overview.human_when(sync_overview.now())
    status["group"] = values.get("require_group", "")
    if status["members"] is not None:
        status["members_label"] = _plural(status["members"], "пользователь", "пользователя", "пользователей")
    return render(request, "ip_plan/_settings_ldap_status.html", {"status": status})


# ── Пользователи ───────────────────────────────────────────────────────────
# Только локальные учётки. Пользователи из FreeIPA в списке показываются
# (иначе непонятно, кто вообще заходил), но правятся исключительно в
# каталоге — их пароля здесь нет, а флаги перезапишет ближайший вход.

def _ldap_username_taken(username):
    """Есть ли такой uid в каталоге. Локальная учётка с тем же именем
    перекрыла бы человеку вход через FreeIPA: ModelBackend стоит первым.

    Недоступный каталог не должен блокировать создание локальной учётки —
    именно в такой ситуации она чаще всего и нужна. Поэтому вместо ошибки
    возвращаем None ("проверить не удалось") и предупреждаем."""
    cfg = LdapConfig.load()
    if not cfg.enabled or not cfg.server_list() or not cfg.user_search_base:
        return None
    try:
        return bool(ldap_check.find_user(
            cfg.server_list(), cfg.bind_dn, cfg.bind_password,
            cfg.user_search_base, cfg.user_search_filter, username,
        ))
    except ldap_check.LdapCheckError:
        return None


@superuser_required
def user_form(request, user_id=None):
    target = None
    if user_id is not None:
        try:
            target = User.objects.select_related("local_account").get(pk=user_id)
        except User.DoesNotExist:
            return _error_modal(request, "Пользователь не найден", status=404)
        if not hasattr(target, "local_account"):
            return _error_modal(
                request,
                f"«{target.username}» — учётная запись из FreeIPA. Её имя, пароль и "
                f"членство в группах меняются в каталоге, а не здесь."
            )
    return render(request, "ip_plan/_settings_user_form.html", {"target": target})


@superuser_required
def user_save(request, user_id=None):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)

    p = request.POST
    username = (p.get("username") or "").strip()
    password = p.get("password") or ""
    is_staff = bool(p.get("is_staff"))
    is_superuser = bool(p.get("is_superuser"))

    if not username:
        return _error_modal(request, "Имя пользователя: обязательное поле")

    if user_id is None:
        if User.objects.filter(username=username).exists():
            return _error_modal(request, f"Пользователь «{username}» уже существует")
        if not password:
            return _error_modal(request, "Пароль: обязателен для новой учётной записи")
        taken = _ldap_username_taken(username)
        if taken:
            return _error_modal(
                request,
                f"В FreeIPA уже есть пользователь «{username}». Локальная учётка с тем же "
                f"именем перекрыла бы ему вход через каталог — выберите другое имя."
            )
        try:
            validate_password(password)
        except ValidationError as e:
            return _error_modal(request, "Пароль: " + "; ".join(e.messages))

        user = User.objects.create_user(
            username=username, password=password,
            is_staff=is_staff, is_superuser=is_superuser,
        )
        LocalAccount.objects.create(
            user=user, must_change_password=bool(p.get("must_change_password")),
            created_by=request.user.username,
        )
        _audit(request, "users", username, "create",
               f"is_staff={is_staff}, is_superuser={is_superuser}")
        note = "" if taken is not None else " (проверить имя в FreeIPA не удалось — каталог недоступен)"
        return _redirect_to("users", f"Пользователь «{username}» создан{note}")

    try:
        user = User.objects.select_related("local_account").get(pk=user_id)
    except User.DoesNotExist:
        return _error_modal(request, "Пользователь не найден", status=404)
    if not hasattr(user, "local_account"):
        return _error_modal(request, "Учётные записи из FreeIPA правятся в каталоге")

    before = {"username": user.username, "is_staff": user.is_staff,
              "is_superuser": user.is_superuser, "is_active": user.is_active}

    # Снятие последних прав суперпользователя с самого себя — верный способ
    # остаться без доступа к этой же вкладке.
    losing_superuser = user.is_superuser and not is_superuser
    if losing_superuser and _local_superusers().exclude(pk=user.pk).count() == 0:
        return _error_modal(
            request,
            "Это последняя локальная учётная запись с правами суперпользователя — "
            "снять их нельзя. Сначала выдайте права другой учётке."
        )

    if username != user.username and User.objects.filter(username=username).exclude(pk=user.pk).exists():
        return _error_modal(request, f"Имя «{username}» уже занято")

    user.username = username
    user.is_staff = is_staff
    user.is_superuser = is_superuser
    user.is_active = bool(p.get("is_active"))
    if password:
        try:
            validate_password(password, user=user)
        except ValidationError as e:
            return _error_modal(request, "Пароль: " + "; ".join(e.messages))
        user.set_password(password)
    user.save()

    account = user.local_account
    account.must_change_password = bool(p.get("must_change_password"))
    account.save(update_fields=["must_change_password"])

    after = {"username": user.username, "is_staff": user.is_staff,
             "is_superuser": user.is_superuser, "is_active": user.is_active}
    details = _describe_changes(before, after)
    if password:
        details += "; пароль сброшен"
    _audit(request, "users", user.username, "update", details)
    return _redirect_to("users", f"Пользователь «{user.username}» сохранён")


@superuser_required
def user_delete(request, user_id):
    if request.method != "POST":
        return _error_modal(request, "Ожидался POST", status=405)
    try:
        user = User.objects.select_related("local_account").get(pk=user_id)
    except User.DoesNotExist:
        return _error_modal(request, "Пользователь не найден", status=404)
    if not hasattr(user, "local_account"):
        return _error_modal(request, "Учётные записи из FreeIPA удаляются в каталоге")
    if user.pk == request.user.pk:
        return _error_modal(request, "Нельзя удалить учётную запись, под которой вы вошли")
    if user.is_superuser and _local_superusers().exclude(pk=user.pk).count() == 0:
        return _error_modal(
            request,
            "Это последняя локальная учётная запись с правами суперпользователя. "
            "Если её удалить, при недоступном LDAP в систему будет не попасть."
        )

    username = user.username
    user.delete()
    _audit(request, "users", username, "delete")
    return _redirect_to("users", f"Пользователь «{username}» удалён")
