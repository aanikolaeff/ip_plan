"""Экспорт и импорт настроек подключения одним архивом.

Задача узкая и намеренно узкая: перенести на чистую установку то, к чему
приложение подключается — устройства, их модели, источники гипервизоров и
учётные данные доступа к ним, — чтобы сразу запускать синхронизацию, а не
заводить подключения заново.

Рабочих данных здесь нет ни в каком виде: письма регулятора, журнал
изменений, локальные учётки, каталог ОС в архив не попадают. Для них нужен
бэкап тома sqlite_data, и кнопка в настройках его не заменяет.

Что в архиве:

    conf/devices.json          модели и устройства, вместе с паролями
    conf/sources_redvirt.json  источники RedVirt, вместе с паролями
    conf/sources_vcenter.json  источники vCenter, вместе с паролями
    manifest.json              состав, дата, версия формата

    conf/config.json           опционально: NetBox (URL, токен, окружения), DNS
    app/ldap.json              опционально: параметры FreeIPA и bind-пароль

Последние два привязаны к конкретной инсталляции, а не к оборудованию: при
переносе с теста на прод адрес NetBox и токен заведомо другие, а LDAP — это
доступ людей в приложение, а не доступ к железу. Поэтому в архив они кладутся,
но при импорте применяются только по явному выбору (см. views_settings).

Архив не шифруется и паролем не защищается — это осознанное решение: он
содержит пароли в открытом виде, и предупреждение об этом показывается перед
скачиванием.
"""
import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from . import sync_config as sc
from ..models import LdapConfig

logger = logging.getLogger(__name__)

# Версия формата архива. Менять при несовместимом изменении состава: импорт
# сверяется с ней и отказывается читать то, чего не понимает.
FORMAT_VERSION = 1

# Ядро переноса — эти файлы в архиве всегда.
CORE_FILES = ("devices", "sources_redvirt", "sources_vcenter")

# Привязаны к инсталляции: кладутся, но применяются только по выбору.
OPTIONAL_FILES = ("config",)

LDAP_MEMBER = "app/ldap.json"


class BundleError(Exception):
    """Текст пригоден для показа пользователю."""


def _member_name(key):
    return f"conf/{sc.FILES[key]}"


# ── Экспорт ──────────────────────────────────────────────────────────────────

def ldap_payload():
    """Настройки LDAP в переносимом виде.

    bind-пароль отдаётся расшифрованным намеренно: в базе он зашифрован
    ключом, выведенным из SECRET_KEY этой инсталляции (см. secretbox), и на
    другом стенде расшифроваться не может в принципе. Перенести шифротекст
    значит перенести неработающую настройку.
    """
    cfg = LdapConfig.load()
    return {
        "enabled": cfg.enabled,
        "servers": cfg.servers,
        "bind_dn": cfg.bind_dn,
        "bind_password": cfg.bind_password,
        "user_search_base": cfg.user_search_base,
        "user_search_filter": cfg.user_search_filter,
        "group_search_base": cfg.group_search_base,
        "require_group": cfg.require_group,
        "staff_group": cfg.staff_group,
        "superuser_group": cfg.superuser_group,
        "cache_timeout": cfg.cache_timeout,
    }


def build(include_config=True, include_ldap=True, author=""):
    """Собирает архив в памяти. Возвращает (bytes, имя файла, список членов).

    Список членов возвращается отдельно, чтобы вью записал в аудит состав, не
    заглядывая внутрь архива и не рискуя вытащить в журнал секреты.
    """
    if not sc.available():
        raise BundleError(
            f"Каталог конфигурации недоступен: {sc.conf_dir()}. "
            f"Проверьте монтирование conf/ из netbox-sync."
        )

    now = datetime.now(timezone.utc)
    members = []
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        keys = list(CORE_FILES) + (list(OPTIONAL_FILES) if include_config else [])
        for key in keys:
            try:
                data, _ = sc.load(key)
            except sc.SyncConfigError as e:
                # Отсутствующий sources_vcenter.json (бывает на стенде без
                # vCenter) не должен ронять весь экспорт.
                if key in CORE_FILES and key != "sources_vcenter":
                    raise BundleError(str(e))
                logger.warning("Экспорт: пропущен %s — %s", key, e)
                continue
            name = _member_name(key)
            zf.writestr(name, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            members.append(name)

        if include_ldap:
            zf.writestr(LDAP_MEMBER,
                        json.dumps(ldap_payload(), ensure_ascii=False, indent=2) + "\n")
            members.append(LDAP_MEMBER)

        manifest = {
            "format": FORMAT_VERSION,
            "created_at": now.isoformat(timespec="seconds"),
            "created_by": author,
            "members": members,
            "note": "Настройки подключения. Рабочих данных не содержит. "
                    "Пароли и токены внутри — в открытом виде.",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    filename = f"netbox-sync-settings-{now:%Y-%m-%d}.zip"
    # В лог — только состав и размер: содержимое файлов сюда попадать не должно.
    logger.info("Экспорт настроек: %s, файлов %d, %d байт",
                filename, len(members), buffer.tell())
    return buffer.getvalue(), filename, members


# ── Импорт ───────────────────────────────────────────────────────────────────
#
# Импорт дополняет конфигурацию, а не заменяет её. Отсюда три правила, общих
# для всех типов записей:
#
#   * новая запись добавляется;
#   * совпадающая пропускается;
#   * расходящаяся показывается как конфликт и применяется только по явному
#     выбору человека.
#
# Секреты из загружаемого файла по умолчанию не затирают существующие: пароль
# в конфиге — рабочий доступ к боевому железу, и молча заменить его значит
# в худшем случае оставить синк без доступа, а узнать об этом утром по логу.

# Размерные ограничения на загрузку: защита от случайного (или намеренного)
# огромного файла. Настройки измеряются десятками килобайт.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ZIP_MEMBERS = 50

ACTION_ADD = "add"
ACTION_SAME = "same"
ACTION_CONFLICT = "conflict"


def _source_key(entry):
    """Идентификатор источника гипервизоров.

    Записи лежат списком без ключей, а индекс в списке идентификатором быть
    не может: он меняется при удалении соседней записи. Опознаём по паре
    «имя + адрес» — у источников она уникальна.
    """
    return ((entry.get("name") or "").strip().lower(),
            (entry.get("url") or "").strip().lower())


def _diff_fields(current, incoming):
    """Поля, которыми записи различаются. Секреты сравниваются по факту
    наличия: показывать значения нельзя, а знать о расхождении нужно."""
    changed = []
    for field in sorted(set(current) | set(incoming)):
        old, new = current.get(field), incoming.get(field)
        if old == new:
            continue
        if sc.is_secret(field):
            changed.append({"field": field, "secret": True,
                            "old": sc.masked(old), "new": sc.masked(new)})
        else:
            changed.append({"field": field, "secret": False, "old": old, "new": new})
    return changed


def _plan_mapping(current, incoming, kind, label_of=lambda k: k):
    """План слияния для словаря записей (устройства, модели)."""
    items = []
    for key, value in (incoming or {}).items():
        if key not in (current or {}):
            items.append({"kind": kind, "key": key, "label": label_of(key),
                          "action": ACTION_ADD, "changes": [], "payload": value})
            continue
        changes = _diff_fields(current[key], value)
        items.append({
            "kind": kind, "key": key, "label": label_of(key),
            "action": ACTION_SAME if not changes else ACTION_CONFLICT,
            "changes": changes, "payload": value,
        })
    return items


def _plan_sources(current, incoming, kind):
    """План слияния для списка источников гипервизоров."""
    index = {_source_key(e): e for e in (current or [])}
    items = []
    for entry in (incoming or []):
        key = _source_key(entry)
        label = entry.get("name") or entry.get("url") or "—"
        if not any(key):
            items.append({"kind": kind, "key": "", "label": label,
                          "action": ACTION_CONFLICT, "payload": entry,
                          "changes": [{"field": "name/url", "secret": False,
                                       "old": "—", "new": "запись без имени и адреса"}]})
            continue
        if key not in index:
            items.append({"kind": kind, "key": "|".join(key), "label": label,
                          "action": ACTION_ADD, "changes": [], "payload": entry})
            continue
        changes = _diff_fields(index[key], entry)
        items.append({
            "kind": kind, "key": "|".join(key), "label": label,
            "action": ACTION_SAME if not changes else ACTION_CONFLICT,
            "changes": changes, "payload": entry,
        })
    return items


def read_upload(name, raw):
    """Разбирает один загруженный файл в {логический ключ: данные}.

    Понимает и ZIP, собранный экспортом, и отдельные JSON. Имя файла для
    JSON значения не имеет: тип определяется по содержимому, потому что
    выгруженный файл легко переименовать.
    """
    if len(raw) > MAX_UPLOAD_BYTES:
        raise BundleError(f"{name}: файл больше {MAX_UPLOAD_BYTES // 1024 // 1024} МБ")

    if raw[:2] == b"PK":
        return _read_zip(name, raw)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BundleError(f"{name}: не разбирается как JSON ({e})")
    key = _detect_kind(data)
    if key is None:
        raise BundleError(
            f"{name}: не похоже ни на один из знакомых файлов настроек "
            f"(devices.json, sources_*.json, config.json, ldap.json)"
        )
    return {key: data}


def _read_zip(name, raw):
    out = {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise BundleError(f"{name}: повреждённый ZIP-архив")

    names = zf.namelist()
    if len(names) > MAX_ZIP_MEMBERS:
        raise BundleError(f"{name}: в архиве слишком много файлов ({len(names)})")

    manifest_raw = None
    for member in names:
        # Zip Slip: путь наружу архива читать нельзя даже на предпросмотре.
        if member.startswith("/") or ".." in member.split("/"):
            raise BundleError(f"{name}: недопустимый путь внутри архива — {member}")
        if member.endswith("/"):
            continue
        if zf.getinfo(member).file_size > MAX_UPLOAD_BYTES:
            raise BundleError(f"{name}: файл {member} внутри архива слишком велик")

        body = zf.read(member)
        if member == "manifest.json":
            manifest_raw = body
            continue
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise BundleError(f"{name}: {member} не разбирается как JSON ({e})")

        key = _key_for_member(member) or _detect_kind(data)
        if key:
            out[key] = data

    if manifest_raw is not None:
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BundleError(f"{name}: manifest.json не разбирается")
        version = manifest.get("format")
        if version != FORMAT_VERSION:
            raise BundleError(
                f"{name}: архив версии {version}, приложение понимает {FORMAT_VERSION}"
            )
    if not out:
        raise BundleError(f"{name}: в архиве нет знакомых файлов настроек")
    return out


def _key_for_member(member):
    if member == LDAP_MEMBER:
        return "ldap"
    for key, filename in sc.FILES.items():
        if member == f"conf/{filename}" or member == filename:
            return key
    return None


def _detect_kind(data):
    """Тип файла по его содержимому."""
    if isinstance(data, dict):
        if "devices" in data or "device_types" in data:
            return "devices"
        if "netbox_url" in data or "environments" in data:
            return "config"
        if "user_search_base" in data or "require_group" in data:
            return "ldap"
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if {"name", "url"} <= set(data[0]):
            # RedVirt и vCenter описываются одинаково; различаем по адресу API.
            return "sources_redvirt" if "ovirt-engine" in (data[0].get("url") or "") \
                else "sources_vcenter"
    return None


def plan(uploaded):
    """Строит план импорта: что добавится, что совпадает, где конфликт.

    Ничего не пишет. uploaded — результат read_upload, объединённый по всем
    загруженным файлам.
    """
    items, notes = [], []

    if "devices" in uploaded:
        current, _ = sc.load("devices")
        incoming = uploaded["devices"]
        items += _plan_mapping(current.get("device_types") or {},
                               incoming.get("device_types") or {}, "model")
        items += _plan_mapping(current.get("devices") or {},
                               incoming.get("devices") or {}, "device")

        # Устройство без своей модели синк пропустит целиком, поэтому такие
        # записи отсекаем на предпросмотре, а не после записи.
        known = set(current.get("device_types") or {}) | set(incoming.get("device_types") or {})
        for item in items:
            if item["kind"] != "device":
                continue
            model = (item["payload"] or {}).get("model")
            if model and model not in known:
                item["action"] = ACTION_CONFLICT
                item["blocked"] = f"модели «{model}» нет ни в текущем конфиге, ни в загруженном"

    for key, kind in (("sources_redvirt", "redvirt"), ("sources_vcenter", "vcenter")):
        if key not in uploaded:
            continue
        try:
            current, _ = sc.load(key)
        except sc.SyncConfigError:
            current = []
        items += _plan_sources(current, uploaded[key], kind)

    if "config" in uploaded:
        notes.append("config")
    if "ldap" in uploaded:
        notes.append("ldap")

    summary = {
        "add": sum(1 for i in items if i["action"] == ACTION_ADD),
        "same": sum(1 for i in items if i["action"] == ACTION_SAME),
        "conflict": sum(1 for i in items if i["action"] == ACTION_CONFLICT),
        "blocked": sum(1 for i in items if i.get("blocked")),
    }
    return {"items": items, "summary": summary, "optional": notes}


def apply(uploaded, selected, replace_secrets=False, apply_config=False, apply_ldap=False):
    """Применяет отобранные записи. Возвращает список строк для журнала.

    Всё собирается в памяти и пишется в конце: если валидация упадёт на любом
    шаге, на диске не останется полуприменённой конфигурации.
    """
    applied = []
    writes = {}

    chosen = set(selected or [])
    plan_items = plan(uploaded)["items"]

    dev_data = None
    for item in plan_items:
        token = f"{item['kind']}:{item['key']}"
        if item.get("blocked") or token not in chosen:
            continue

        if item["kind"] in ("model", "device"):
            if dev_data is None:
                dev_data, _ = sc.load("devices")
            bucket = "device_types" if item["kind"] == "model" else "devices"
            dev_data.setdefault(bucket, {})
            existing = dev_data[bucket].get(item["key"])
            payload = dict(item["payload"])
            if existing and not replace_secrets:
                # Пароль остаётся прежним: заменяем только по явному выбору.
                for field in list(payload):
                    if sc.is_secret(field) and existing.get(field):
                        payload[field] = existing[field]
            dev_data[bucket][item["key"]] = payload
            writes["devices"] = dev_data
            applied.append(f"{item['kind']} {item['key']}")

        elif item["kind"] in ("redvirt", "vcenter"):
            key = f"sources_{item['kind']}"
            if key not in writes:
                try:
                    writes[key] = sc.load(key)[0]
                except sc.SyncConfigError:
                    writes[key] = []
            target = writes[key]
            payload = dict(item["payload"])
            for pos, entry in enumerate(target):
                if _source_key(entry) == _source_key(payload):
                    if not replace_secrets:
                        for field in list(payload):
                            if sc.is_secret(field) and entry.get(field):
                                payload[field] = entry[field]
                    target[pos] = payload
                    break
            else:
                target.append(payload)
            applied.append(f"{item['kind']} {item['label']}")

    if apply_config and "config" in uploaded:
        writes["config"] = uploaded["config"]
        applied.append("config.json целиком")

    for key, data in writes.items():
        # expected_hash=None: форма предпросмотра не держит хеш, а запись всё
        # равно атомарна и версионируется.
        sc.save(key, data, None)

    if apply_ldap and "ldap" in uploaded:
        _apply_ldap(uploaded["ldap"])
        applied.append("настройки LDAP")

    return applied


def _apply_ldap(payload):
    """Переносит настройки LDAP, шифруя bind-пароль ключом этой системы."""
    cfg = LdapConfig.load()
    for field in ("enabled", "servers", "bind_dn", "user_search_base",
                  "user_search_filter", "group_search_base", "require_group",
                  "staff_group", "superuser_group", "cache_timeout"):
        if field in payload:
            setattr(cfg, field, payload[field])
    if payload.get("bind_password"):
        # bind_password — property только на чтение (расшифровка); запись идёт
        # через метод, который шифрует ключом ЭТОЙ инсталляции.
        cfg.set_bind_password(payload["bind_password"])
    cfg.save()
