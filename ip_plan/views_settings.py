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

from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import LdapConfig, LocalAccount, SettingsAudit
from .services import connection_test, ldap_check, settings_bundle
from .services import sync_config as sc

logger = logging.getLogger(__name__)

# Вкладки раздела. Ключ — часть URL, "perm" — какой флаг нужен.
# Разделы. "file" — что этот раздел реально правит: рельс слева показывает
# его под названием, чтобы человек видел, какой файл на диске (или какая
# таблица в базе) стоит за экраном. Это не украшение: те же имена он потом
# ищет на прод-хосте, когда разбирается с прогоном синка.
TABS = [
    {"key": "devices",     "title": "Оборудование", "file": "conf/devices.json",       "perm": "staff"},
    {"key": "models",      "title": "Модели",       "file": "conf/devices.json",       "perm": "staff"},
    {"key": "hypervisors", "title": "Гипервизоры",  "file": "conf/sources_*.json",     "perm": "staff"},
    {"key": "scan",        "title": "Скан и DNS",   "file": "conf/config.json",        "perm": "staff"},
    {"key": "ldap",        "title": "Вход и LDAP",  "file": "база ip_plan",            "perm": "superuser"},
    {"key": "users",       "title": "Пользователи", "file": "база ip_plan",            "perm": "superuser"},
    {"key": "audit",       "title": "Журнал",       "file": "база ip_plan",            "perm": "staff"},
]

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

@staff_required
def settings_page(request, tab="devices"):
    # Вкладки LDAP и "Пользователи" — только суперпользователю: там
    # управление тем, кто вообще может войти. Невидимые вкладки не просто
    # не рисуются, но и недоступны по прямой ссылке.
    visible = [t for t in TABS if t["perm"] != "superuser" or request.user.is_superuser]
    if tab not in {t["key"] for t in visible}:
        if tab in {t["key"] for t in TABS}:
            raise PermissionDenied("Требуется доступ суперпользователя")
        tab = "devices"

    ctx = {
        "tabs": visible,
        "active_tab": tab,
        "conf_available": sc.available(),
        "conf_dir": sc.conf_dir(),
        "sync_running": sc.sync_running(),
    }
    if not ctx["conf_available"]:
        return render(request, "ip_plan/settings.html", ctx)

    try:
        ctx.update(_tab_context(request, tab))
    except sc.SyncConfigError as e:
        ctx["load_error"] = str(e)
    return render(request, "ip_plan/settings.html", ctx)


def _tab_context(request, tab):
    if tab == "devices":
        data, digest = sc.load("devices")
        types = data.get("device_types") or {}
        devices = []
        for name, cfg in (data.get("devices") or {}).items():
            # Синк читает настройки устройства поверх настроек его модели
            # (см. _resolve в netbox_devices_sync.py), поэтому шаги показываем
            # по этой же, эффективной, картине: сбор MAC-таблицы, например,
            # задан у моделей, и по собственным полям устройства его не видно
            # — в списке получалось «ничего не собираем» там, где собирается.
            eff = sc.effective(cfg, types)
            model_cfg = types.get(cfg.get("model")) or {}
            devices.append({"name": name, "cfg": cfg, "eff": eff,
                            "from_model": sorted(k for k in sc.SYNC_FLAGS
                                                 if eff.get(k) and not cfg.get(k)),
                            # Шаг, выключенный на устройстве вопреки модели.
                            # Без этой подписи строка молчала бы о том, что
                            # человек шаг снял: чипа нет — и непонятно, потому
                            # ли, что нигде не включено, или потому что снято.
                            "off_here": sorted(k for k in sc.SYNC_FLAGS
                                               if model_cfg.get(k) and cfg.get(k) is False),
                            "has_password": bool(cfg.get("password"))})
        tests = connection_test.state_for("device", [d["name"] for d in devices])
        for item in devices:
            item["test"] = tests.get(item["name"])
        return {"devices": devices, "device_types": sorted((data.get("device_types") or {})),
                "file_hash": digest}

    if tab == "models":
        data, digest = sc.load("devices")
        used = {}
        for name, cfg in (data.get("devices") or {}).items():
            used.setdefault(cfg.get("model"), []).append(name)
        types = [{"name": n, "cfg": c, "used_by": used.get(n, [])}
                 for n, c in (data.get("device_types") or {}).items()]
        return {"device_type_rows": types, "file_hash": digest}

    if tab == "hypervisors":
        redvirt, rv_hash = sc.load("sources_redvirt")
        vcenter, vc_hash = sc.load("sources_vcenter")
        rv_tests = connection_test.state_for("redvirt", [s.get("name") or s.get("url") for s in redvirt])
        vc_tests = connection_test.state_for("vcenter", [s.get("name") or s.get("url") for s in vcenter])
        return {
            "redvirt": [(i, s, rv_tests.get(s.get("name") or s.get("url")))
                        for i, s in enumerate(redvirt)],
            "redvirt_hash": rv_hash,
            "vcenter": [(i, s, vc_tests.get(s.get("name") or s.get("url")))
                        for i, s in enumerate(vcenter)],
            "vcenter_hash": vc_hash,
        }

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
            rows.append({"user": u, "is_local": hasattr(u, "local_account"),
                         "account": getattr(u, "local_account", None)})
        return {"user_rows": rows}

    if tab == "audit":
        return {"audit_rows": SettingsAudit.objects.all()[:200]}

    return {}


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
    """(ключ, подпись, пояснение, отмечен, унаследован) для блока флагов."""
    eff = sc.effective(cfg, device_types)
    return [(key, label, hint, bool(eff.get(key)), key not in cfg and bool(eff.get(key)))
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


@staff_required
def device_form(request, name=None):
    data, digest = sc.load("devices")
    devices = data.get("devices") or {}
    if name is not None and name not in devices:
        return _error_modal(request, f"Устройство «{name}» не найдено", status=404)

    cfg = devices.get(name, {}) if name else {}
    # Галочки — по эффективной картине (модель + устройство), как в списке и
    # как в самом синке. Раньше форма показывала только собственные поля
    # устройства, и шаг, включённый у модели, выглядел выключенным: человек
    # видел снятую галочку «MAC-таблицу» там, где MAC исправно собирался.
    flag_fields = _device_flag_fields(cfg, data.get("device_types") or {})
    return render(request, "ip_plan/_settings_device_form.html", {
        "name": name,
        "cfg": cfg,
        "flag_fields": flag_fields,
        "password_mask": sc.masked(cfg.get("password")),
        "enable_mask": sc.masked(cfg.get("enable_password")),
        "device_types": sorted(data.get("device_types") or {}),
        "statuses": sc.DEVICE_STATUSES,
        "blocklist_objects": json.dumps(cfg.get("blocklist_objects") or {},
                                        ensure_ascii=False, indent=2),
        "file_hash": digest,
        "sync_running": sc.sync_running(),
    })


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
        cfg["host"] = sc.validate_host(p.get("host"))
        # Адрес, по которому устройство учитывается в NetBox, когда он не
        # совпадает с адресом подключения: к криптошлюзам ходят через
        # ViPNet-туннель (7.0.0.x), а в учёте они должны стоять реальным
        # адресом. Пусто — берётся host, как было всегда.
        mgmt_ip = (p.get("mgmt_ip") or "").strip()
        if mgmt_ip:
            cfg["mgmt_ip"] = sc.validate_host(mgmt_ip, field="Адрес управления")
        else:
            cfg.pop("mgmt_ip", None)
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
        cfg["site"] = (p.get("site") or "").strip()
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
        type_cfg = data["device_types"][model] or {}
        for flag in sc.SYNC_FLAGS:
            if p.get(flag):
                cfg[flag] = True
            elif type_cfg.get(flag):
                cfg[flag] = False
            else:
                cfg.pop(flag, None)

        # Дальше проверяем эффективную картину (модель + устройство): именно её
        # исполняет синк. По одному лишь блоку устройства проверка пропускала
        # всё, что включено на модели.
        eff = sc.effective(cfg, data.get("device_types") or {})

        # sync_macs имеет смысл только для парсеров, у которых он вообще
        # реализован (_mac_dispatch в netbox_devices_sync.py) — иначе шаг
        # молча пропустится, а человек будет думать, что включил.
        parser = type_cfg.get("interfaces_parser", "")
        if eff.get("sync_macs") and parser not in sc.MAC_CAPABLE_PARSERS:
            raise sc.SyncConfigError(
                f"Сбор MAC-таблицы не поддержан для парсера «{parser}» "
                f"(есть только для {', '.join(sorted(sc.MAC_CAPABLE_PARSERS))})."
            )

        # MAC снимается хвостом шага интерфейсов (netbox_devices_sync.py:
        # ранний return при выключенном sync_interfaces стоит ДО блока MAC),
        # так что одно без другого — молча несобираемая таблица.
        if eff.get("sync_macs") and not eff.get("sync_interfaces"):
            raise sc.SyncConfigError(
                "Сбор MAC-таблицы включён, но выключены интерфейсы: MAC снимается "
                "внутри шага интерфейсов и без него собран не будет."
            )

        # У xfw MAC/ARP снимается не общим диспетчером, а внутри обхода
        # кластера ViPNet, и только при cluster=vipnet_failover. Без кластера
        # синк напечатает «нет поддержки для парсера xfw» и ничего не соберёт.
        if eff.get("sync_macs") and parser == "xfw" and eff.get("cluster") != "vipnet_failover":
            raise sc.SyncConfigError(
                "Для парсера «xfw» ARP-таблица снимается только в режиме кластера: "
                "у модели должно быть задано cluster=vipnet_failover."
            )

        # Та же логика, что у sync_macs: LLDP есть не у всех вендоров, и без
        # команды на модели собирать соседей нечем.
        if eff.get("sync_lldp"):
            if parser not in sc.LLDP_CAPABLE_PARSERS:
                raise sc.SyncConfigError(
                    f"Сбор LLDP-соседей не поддержан для парсера «{parser}» "
                    f"(есть только для {', '.join(sorted(sc.LLDP_CAPABLE_PARSERS))})."
                )
            if not type_cfg.get("lldp_command"):
                raise sc.SyncConfigError(
                    f"Сбор LLDP-соседей включён, но у модели «{model}» не задана "
                    f"команда LLDP — задайте её на вкладке «Модели»."
                )

        if eff.get("sync_blocklist"):
            raw = (p.get("blocklist_objects") or "").strip()
            try:
                objects = json.loads(raw) if raw else {}
            except json.JSONDecodeError as e:
                raise sc.SyncConfigError(f"Объекты блокировки: не разбирается как JSON ({e})")
            if not isinstance(objects, dict) or not objects:
                raise sc.SyncConfigError(
                    "Объекты блокировки: при включённом sync_blocklist нужен непустой "
                    'объект вида {"ip": "BLOCK_BY_IP_2", "fqdn": "BBDNS_1"}'
                )
            cfg["blocklist_objects"] = objects
        else:
            cfg.pop("blocklist_objects", None)

        if eff.get("backup_config"):
            if not type_cfg.get("config_command"):
                raise sc.SyncConfigError(
                    f"Резервное копирование включено, но у модели «{model}» не задан "
                    f"config_command — задайте его на вкладке «Модели»."
                )

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
    ("no_pager", "Команда отключения постраничного вывода"),
    ("interfaces_command", "Команда списка интерфейсов"),
    ("description_command", "Команда описаний интерфейсов"),
    ("vlan_command", "Команда VLAN-ов"),
    ("mac_command", "Команда MAC-таблицы"),
    ("lag_command", "Команда LAG"),
    ("ip_command", "Команда IP-адресов"),
    ("lldp_command", "Команда LLDP-соседей"),
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


@staff_required
def device_type_form(request, name=None):
    data, digest = sc.load("devices")
    types = data.get("device_types") or {}
    if name is not None and name not in types:
        return _error_modal(request, f"Модель «{name}» не найдена", status=404)
    cfg = types.get(name, {}) if name else {}
    return render(request, "ip_plan/_settings_device_type_form.html", {
        "name": name,
        "cfg": cfg,
        "text_fields": [(k, label, cfg.get(k, "")) for k, label in _TYPE_TEXT_FIELDS],
        "flag_fields": [(k, label, bool(cfg.get(k))) for k, label in _TYPE_FLAG_FIELDS],
        "list_fields": [(k, label, ", ".join(cfg.get(k) or [])) for k, label in _TYPE_LIST_FIELDS],
        "parsers": sc.INTERFACE_PARSERS,
        "mac_capable": sorted(sc.MAC_CAPABLE_PARSERS),
        "lldp_capable": sorted(sc.LLDP_CAPABLE_PARSERS),
        "file_hash": digest,
    })


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
        if parser not in sc.INTERFACE_PARSERS:
            raise sc.SyncConfigError(
                f"Парсер интерфейсов «{parser}» не реализован в netbox_devices_sync.py. "
                f"Допустимо: {', '.join(sc.INTERFACE_PARSERS)}"
            )

        # База — прежний конфиг модели, а не пустой словарь. Собранный с нуля,
        # он терял всё, чего в форме нет: cluster=vipnet_failover у xFW 5000 и
        # HW5000 (без него не опрашивается кластер криптошлюзов) и любые
        # будущие ключи. Форма правит ровно свои поля, остальное не трогает.
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

        if cfg.get("sync_macs") and parser not in sc.MAC_CAPABLE_PARSERS:
            raise sc.SyncConfigError(
                f"Сбор MAC-таблицы не реализован для парсера «{parser}» "
                f"(есть только для {', '.join(sorted(sc.MAC_CAPABLE_PARSERS))})."
            )
        if cfg.get("sync_lldp") and parser not in sc.LLDP_CAPABLE_PARSERS:
            raise sc.SyncConfigError(
                f"Сбор LLDP-соседей не реализован для парсера «{parser}» "
                f"(есть только для {', '.join(sorted(sc.LLDP_CAPABLE_PARSERS))})."
            )
        if cfg.get("sync_lldp") and not cfg.get("lldp_command"):
            raise sc.SyncConfigError("Сбор LLDP-соседей включён, но не задана команда LLDP-соседей")
        if cfg.get("backup_config") and not cfg.get("config_command"):
            raise sc.SyncConfigError("Резервное копирование включено, но не задана команда выгрузки конфигурации")
        if not cfg.get("manufacturer") or not cfg.get("role"):
            raise sc.SyncConfigError("Производитель и роль обязательны — по ним синк заводит объекты в NetBox")

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

    details = _describe_changes(previous, cfg) if name else "создана"
    if applied:
        details += f"; флаги применены к устройствам: {', '.join(applied)}"
    if renamed:
        details += f"; переименование применено к устройствам: {', '.join(renamed)}"
    _audit(request, "device_types", new_name, "update" if name else "create", details)
    return _redirect_to("models", f"Модель «{new_name}» сохранена")


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
# Оба файла — плоские JSON-массивы объектов {name, url, user, password}.
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

        item = {"name": name, "url": url, "user": user, "password": password}
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

    try:
        lines = ldap_check.test_bind(values["servers"], values["bind_dn"], values["bind_password"])
        if username:
            if not password:
                raise ldap_check.LdapCheckError("Для проверки входа нужен пароль пользователя")
            lines = lines + ["—"] + ldap_check.test_user(values, username, password)
        ok, message = True, "\n".join(lines)
    except ldap_check.LdapCheckError as e:
        ok, message = False, str(e)

    return render(request, "ip_plan/_settings_ldap_result.html", {"ok": ok, "message": message})


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
