"""Чтение и запись редактируемых конфигов netbox-sync (раздел "Настройки").

Единственное место в приложении, которое пишет в примонтированную
директорию netbox-sync/conf (см. docker-compose.yml и settings.
NETBOX_SYNC_CONF_DIR). Всё остальное — вью и шаблоны — работает только
через функции отсюда.

Почему так осторожно: эти файлы читает не ip_plan, а скрипты синка,
которые ходят по SSH на боевой firewall и коммутаторы и раз в сутки
перезаписывают IPAM. Битый devices.json роняет шаги 2-5 full_sync.sh
целиком, и узнают об этом только утром по логу. Отсюда четыре свойства,
которые здесь обязательны:

1. Атомарная запись (tmp + os.replace) — крон в 03:00 читает те же файлы,
   он должен увидеть либо старую версию целиком, либо новую целиком.
2. История версий (conf/.history/) — откат без git, потому что эти файлы
   из git как раз вынесены.
3. Пароли наружу не отдаются никогда: в UI уходит маска, а на сохранении
   пустое/масочное значение означает "оставить прежнее".
4. Проверка хеша при сохранении — два администратора не затирают правки
   друг друга молча.
"""
import hashlib
import ipaddress
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# Логический ключ -> имя файла в conf/. Ключи используются в URL, поэтому
# менять их нельзя без правки ссылок.
FILES = {
    "devices": "devices.json",
    "config": "config.json",
    "sources_redvirt": "sources_redvirt.json",
    "sources_vcenter": "sources_vcenter.json",
}

# Сколько версий держим в conf/.history/ на файл.
HISTORY_KEEP = 20

# Маска, которая уходит в браузер вместо пароля. Она же принимается
# обратно и означает "не менять".
MASK = "•" * 8

# Имена полей, значения которых считаются секретами. Проверка по вхождению
# подстроки: password, enable_password, bind_password, token.
_SECRET_PARTS = ("password", "token", "secret")

# Парсеры интерфейсов, реально реализованные в netbox_devices_sync.py
# (жёсткий if/elif в sync_device). Любое другое значение = шаг "Interfaces"
# молча пропускается, поэтому в UI это строго выпадающий список.
INTERFACE_PARSERS = ["xfw", "eltex_status", "eltex_mes24", "cisco_ios", "maipu", "huawei_vrp", "continent_snmp"]
# Парсеры, которые ходят на устройство не по SSH, а по SNMPv3: у АПКШ
# «Континент» CLI — меню локального администратора, разбирать его нечем, и
# netbox_continent_snmp.py снимает интерфейсы, адреса, ARP и состояние
# кластера с агента. У таких устройств вместо логина/пароля — имя
# безопасности и пароли аутентификации/шифрования, а проверка подключения
# делает SNMP GET вместо TCP+SSH.
SNMP_PARSERS = {"continent_snmp"}
SNMP_AUTH_PROTOS = ["md5", "sha1"]
SNMP_PRIV_PROTOS = ["des", "aes"]
# Раздел «SNMP» есть у каждого устройства, независимо от транспорта: пустая
# версия — SNMP не настроен; v2c — community (список: у кластеров ViPNet первая
# адресует активную ноду, вторая пассивную); v3 — пользователь и пароли.
# У парсеров из SNMP_PARSERS раздел обязателен — по нему идёт весь сбор; у
# остальных он нужен проверке подключения (шаг SNMP) и будущим SNMP-парсерам.
SNMP_VERSIONS = ["", "v2c", "v3"]
SNMP_KEYS = ("snmp_version", "snmp_port", "snmp_communities", "snmp_user",
             "snmp_auth_proto", "snmp_auth_password", "snmp_priv_proto", "snmp_priv_password")


def snmp_version(cfg):
    """Версия SNMP устройства с учётом записей, сделанных до появления поля."""
    version = (cfg.get("snmp_version") or "").strip()
    if version:
        return version
    if cfg.get("snmp_user"):
        return "v3"
    if cfg.get("snmp_communities"):
        return "v2c"
    return ""


def transport(parser):
    """«snmp» или «ssh» — чем синк ходит на устройства с этим парсером."""
    return "snmp" if parser in SNMP_PARSERS else "ssh"
# Снятие MAC-таблицы поддержано для этих парсеров. У Eltex, Maipu и Huawei
# это таблица коммутации (кто за каким портом), у ViPNet — ARP-таблица:
# команда там называется «inet show mac-address-table», но отдаёт вывод
# arp -n, поэтому в NetBox такие записи помечаются отдельным маркером
# (см. _ARP_DESCRIPTION в netbox_devices_sync.py). ViPNet снимается не
# отдельным проходом, а вместе с интерфейсами кластера, одним логином.
# У Континента, как и у ViPNet, это ARP (ipNetToMediaTable по SNMP), снимается
# тем же опросом, что и интерфейсы.
MAC_CAPABLE_PARSERS = {"eltex_status", "eltex_mes24", "maipu", "huawei_vrp", "xfw", "continent_snmp"}
# LLDP умеют Cisco, Eltex, Maipu и Huawei — у xFW 5000 протокола нет вовсе,
# так что включённый на нём флаг означал бы шаг, который нечем выполнить.
LLDP_CAPABLE_PARSERS = {"eltex_status", "eltex_mes24", "cisco_ios", "maipu", "huawei_vrp"}

# Справочник парсеров для карточки модели: транспорт и что каждый умеет
# снять. Возможности выводятся из MAC/LLDP-множеств выше, а не пишутся
# второй раз — иначе карточки разъедутся с валидацией.
def parser_catalog():
    return [{"key": p, "transport": transport(p),
             "caps": ["интерфейсы"]
                     + (["MAC"] if p in MAC_CAPABLE_PARSERS else [])
                     + (["LLDP"] if p in LLDP_CAPABLE_PARSERS else [])}
            for p in INTERFACE_PARSERS]


# Режимы кластера модели. Синк знает один — vipnet_failover
# (netbox_devices_sync.py: опрос обеих нод ViPNet xFW/HW по очереди).
CLUSTER_MODES = ["", "vipnet_failover"]

# Флаги «что снимать с устройства», в порядке шагов синка. Живут и на
# устройстве, и на его модели; смысл один и тот же, см. effective() ниже.
SYNC_FLAGS = ("sync_interfaces", "import_vlans", "import_prefixes",
              "sync_macs", "sync_lldp", "backup_config", "sync_blocklist")


def effective(device_cfg, device_types):
    """Настройки устройства поверх настроек его модели.

    Ровно то же, что делает _resolve в netbox-sync (одинаковая копия в
    netbox_devices_sync.py, netbox_lldp_topology.py, netbox_config_backup.py и
    netbox_firewall_blocklist_sync.py): база — конфиг модели, поверх — конфиг
    устройства. Слияние плоское, на один уровень.

    Существенно, что update() переносит и значение False: «ключа нет» значит
    «наследовать от модели», а «ключ = false» — «выключено, несмотря на
    модель». Интерфейс обязан уметь записать именно false, иначе снятая
    галочка на устройстве не отключает шаг, включённый у модели.
    """
    eff = dict((device_types or {}).get(device_cfg.get("model")) or {})
    eff.update(device_cfg)
    return eff


DEVICE_STATUSES = ["active", "planned", "staged", "failed", "inventory", "offline"]

# Площадка по умолчанию — та же, что в скриптах синка (DEFAULT_SITE_SLUG).
# Ключ site у устройства/источника отсутствовал в старых конфигах, и синк
# кладёт такие объекты на MDC; форма показывает это явно, а не пустым полем.
DEFAULT_SITE_SLUG = "mdc"


def site_options():
    """Площадки из NetBox для <select> в формах устройств и источников.

    Справочник живёт в NetBox (у площадки регион, адрес, часовой пояс), и
    синк площадок не создаёт — опечатка в slug сделала бы объект
    невидимым для синка. Если NetBox недоступен, возвращаем пустой список:
    форма тогда показывает текстовое поле, а validate_site пропускает любой
    непустой slug — редактировать конфиг при лежащем NetBox всё равно нужно."""
    from .netbox_client import NetBoxAPIError, NetBoxClient
    try:
        return NetBoxClient().list_sites()
    except NetBoxAPIError as exc:
        logger.warning("Не удалось получить площадки из NetBox: %s", exc)
        return []


def netbox_names(kind):
    """Производители или роли NetBox (kind = manufacturers | roles) для
    подсказок в карточке модели. NetBox недоступен — пустой список, поле
    остаётся свободным вводом."""
    from .netbox_client import NetBoxAPIError, NetBoxClient
    try:
        client = NetBoxClient()
        return client.list_manufacturers() if kind == "manufacturers" else client.list_device_roles()
    except NetBoxAPIError as exc:
        logger.warning("Не удалось получить справочник %s из NetBox: %s", kind, exc)
        return []


def validate_site(value, sites, field="Площадка"):
    slug = (value or "").strip().lower()
    if not slug:
        raise SyncConfigError(f"{field}: обязательное поле")
    if sites and slug not in {s["slug"] for s in sites}:
        raise SyncConfigError(f"{field} «{slug}» не найдена в NetBox")
    if not re.fullmatch(r"[a-z0-9_-]+", slug):
        raise SyncConfigError(f"{field}: slug может содержать только a-z, 0-9, «-» и «_»")
    return slug

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$")


class SyncConfigError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""


class ConfigConflict(SyncConfigError):
    """Файл изменился с момента, когда пользователь открыл форму."""


# ── Низкий уровень: файлы ──────────────────────────────────────────────────

def conf_dir():
    return Path(settings.NETBOX_SYNC_CONF_DIR)


def _path(key):
    try:
        return conf_dir() / FILES[key]
    except KeyError:
        raise SyncConfigError(f"Неизвестный конфиг: {key}")


def available():
    """Смонтирована ли директория conf/. False = mount не настроен, и вью
    показывают объяснение вместо форм, а не 500."""
    return conf_dir().is_dir()


def load(key):
    """(данные, хеш). Хеш считается по сырым байтам файла — именно он
    возвращается в форму и проверяется при сохранении."""
    path = _path(key)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise SyncConfigError(
            f"Файл {FILES[key]} не найден в {conf_dir()}. "
            f"Проверьте, что директория conf/ репозитория netbox-sync смонтирована."
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SyncConfigError(f"{FILES[key]}: не разбирается как JSON ({e})")
    return data, hashlib.sha256(raw).hexdigest()


def _backup(path):
    """Копия текущей версии в conf/.history/. Точка в имени директории —
    чтобы conf_loader.py в netbox-sync её не спутал с конфигом."""
    if not path.exists():
        return
    hist = conf_dir() / ".history"
    hist.mkdir(exist_ok=True)
    # Миллисекунды в имени — не педантизм: два сохранения в одну секунду
    # (правка и тут же исправление опечатки) с точностью до секунды писали
    # бы в один и тот же файл, и промежуточная версия терялась бы молча.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    shutil.copy2(path, hist / f"{path.name}.{stamp}")
    old = sorted(hist.glob(f"{path.name}.*"))
    for extra in old[:-HISTORY_KEEP]:
        extra.unlink()


def save(key, data, expected_hash):
    """Атомарная запись с проверкой на параллельную правку.

    expected_hash — тот, что был отдан форме в load(). None отключает
    проверку (для программных вызовов, не из формы)."""
    path = _path(key)
    if expected_hash is not None:
        current = load(key)[1]
        if current != expected_hash:
            raise ConfigConflict(
                f"{FILES[key]} изменился, пока была открыта форма "
                f"(правил кто-то ещё или файл поменяли на диске). "
                f"Откройте страницу заново, чтобы не затереть чужие изменения."
            )

    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # Контрольная проверка: то, что записываем, обязано читаться обратно.
    json.loads(payload)

    _backup(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    logger.info("Записан %s (%d байт)", path, len(payload))


def history(key):
    """Список сохранённых версий файла, новые сверху."""
    hist = conf_dir() / ".history"
    if not hist.is_dir():
        return []
    items = []
    for f in sorted(hist.glob(f"{FILES[key]}.*"), reverse=True):
        stamp = f.name.rsplit(".", 1)[-1]
        # С 2026 года в метке есть миллисекунды (см. _backup), у старых
        # копий — нет; без второго формата все новые версии пропадали.
        for fmt in ("%Y%m%d-%H%M%S-%f", "%Y%m%d-%H%M%S"):
            try:
                when = datetime.strptime(stamp, fmt)
                break
            except ValueError:
                continue
        else:
            continue
        items.append({"name": f.name, "when": when, "size": f.stat().st_size})
    return items


def sync_running():
    """Идёт ли прямо сейчас прогон full_sync.sh. Только для предупреждения
    в UI: сама запись атомарна и безопасна в любом случае, но результат
    текущего прогона получится смешанным (каждый шаг читает конфиг в момент
    своего старта, а не один раз в начале)."""
    try:
        return Path(settings.NETBOX_SYNC_LOCK_FILE).exists()
    except (OSError, ValueError):
        return False


# ── Секреты ────────────────────────────────────────────────────────────────

def is_secret(field):
    low = field.lower()
    return any(part in low for part in _SECRET_PARTS)


def masked(value):
    """Что показать вместо секрета. Пустое остаётся пустым — иначе
    невозможно отличить "пароль не задан" от "пароль скрыт"."""
    return MASK if value else ""


def keep_or_new(submitted, previous):
    """Значение секрета для записи: пустое поле или маска = оставить
    прежнее. Так пароль не уходит в браузер вообще никогда."""
    if submitted in ("", None, MASK):
        return previous
    return submitted


# ── Валидация ──────────────────────────────────────────────────────────────

def validate_host(value, field="Адрес"):
    value = (value or "").strip()
    if not value:
        raise SyncConfigError(f"{field}: обязательное поле")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(value) or ".." in value:
        raise SyncConfigError(f"{field}: не похоже ни на IP-адрес, ни на имя хоста — «{value}»")
    return value


def validate_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise SyncConfigError(f"Порт: должен быть числом, а не «{value}»")
    if not 1 <= port <= 65535:
        raise SyncConfigError(f"Порт: допустимо 1..65535, получено {port}")
    return port


def validate_prefix(value):
    value = (value or "").strip()
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError as e:
        raise SyncConfigError(f"Префикс «{value}»: {e}")


def validate_url(value, field="URL", require_https=True):
    value = (value or "").strip()
    if not value:
        raise SyncConfigError(f"{field}: обязательное поле")
    if require_https and not value.startswith("https://"):
        raise SyncConfigError(f"{field}: должен начинаться с https:// — «{value}»")
    if not require_https and not value.startswith(("http://", "https://")):
        raise SyncConfigError(f"{field}: должен начинаться с http:// или https:// — «{value}»")
    return value.rstrip("/")


def validate_name(value, existing, original=None, field="Имя", allow_spaces=False):
    """Непустое, уникальное; по умолчанию — без пробелов. original — имя до
    правки, оно из проверки уникальности исключается.

    allow_spaces нужен названиям моделей: они попадают в NetBox как
    device_type.model, где пробел законен и уже используется («xFW 5000»,
    «MES2300B-48»). Без этого послабления такую модель нельзя было
    сохранить вообще: форма отдавала её собственное имя, а валидатор его же
    и отвергал — правка любого поля упиралась в ошибку про пробелы.
    """
    value = (value or "").strip()
    if not value:
        raise SyncConfigError(f"{field}: обязательное поле")
    if not allow_spaces and re.search(r"\s", value):
        raise SyncConfigError(f"{field}: пробелы недопустимы — «{value}»")
    if allow_spaces and re.search(r"\s\s|^\s|\s$", value):
        raise SyncConfigError(f"{field}: лишние пробелы — «{value}»")
    if value != original and value in existing:
        raise SyncConfigError(f"{field}: «{value}» уже есть в этом файле")
    return value


# ── Проверки устройства и модели ───────────────────────────────────────────
# Общие для формы и для импорта: архив настроек с чужой установки должен
# проходить те же правила, что и ручная правка, иначе импорт кладёт в
# devices.json то, что форма сохранить не дала бы.

def validate_device(cfg, device_types):
    """Проверка эффективной картины устройства (модель + устройство) — именно
    её исполняет синк. По одному блоку устройства проверка пропускала бы всё,
    что включено на модели."""
    model = cfg.get("model")
    type_cfg = (device_types or {}).get(model)
    if type_cfg is None:
        raise SyncConfigError(f"Модель «{model}» отсутствует в device_types. Заведите её в разделе «Модели».")
    parser = type_cfg.get("interfaces_parser", "")
    eff = effective(cfg, device_types)

    # Ядро сети — единственный источник префиксов площадки (маршрутизирует
    # только xFW; адреса на L3-коммутаторах служебные).
    if eff.get("import_prefixes") and not cfg.get("network_core"):
        raise SyncConfigError(
            "Префиксы сетей импортируются только с ядра сети: отметьте «Ядро сети» "
            "или снимите шаг «Префиксы сетей» (адреса на L3-коммутаторах служебные).")
    # sync_macs имеет смысл только для парсеров, у которых он вообще
    # реализован (_mac_dispatch в netbox_devices_sync.py) — иначе шаг
    # молча пропустится, а человек будет думать, что включил.
    if eff.get("sync_macs") and parser not in MAC_CAPABLE_PARSERS:
        raise SyncConfigError(
            f"Сбор MAC-таблицы не поддержан для парсера «{parser}» "
            f"(есть только для {', '.join(sorted(MAC_CAPABLE_PARSERS))}).")
    # MAC снимается хвостом шага интерфейсов (ранний return при выключенном
    # sync_interfaces стоит ДО блока MAC), так что одно без другого — молча
    # несобираемая таблица.
    if eff.get("sync_macs") and not eff.get("sync_interfaces"):
        raise SyncConfigError(
            "Сбор MAC-таблицы включён, но выключены интерфейсы: MAC снимается "
            "внутри шага интерфейсов и без него собран не будет.")
    # У xfw MAC/ARP снимается не общим диспетчером, а внутри обхода кластера
    # ViPNet, и только при cluster=vipnet_failover.
    if eff.get("sync_macs") and parser == "xfw" and eff.get("cluster") != "vipnet_failover":
        raise SyncConfigError(
            "Для парсера «xfw» ARP-таблица снимается только в режиме кластера: "
            "у модели должно быть задано cluster=vipnet_failover.")
    if eff.get("sync_lldp"):
        if parser not in LLDP_CAPABLE_PARSERS:
            raise SyncConfigError(
                f"Сбор LLDP-соседей не поддержан для парсера «{parser}» "
                f"(есть только для {', '.join(sorted(LLDP_CAPABLE_PARSERS))}).")
        if not type_cfg.get("lldp_command"):
            raise SyncConfigError(
                f"Сбор LLDP-соседей включён, но у модели «{model}» не задана "
                f"команда LLDP — задайте её в разделе «Модели».")
    if eff.get("sync_blocklist"):
        objects = cfg.get("blocklist_objects")
        if not isinstance(objects, dict) or not objects:
            raise SyncConfigError(
                "Объекты блокировки: при включённом sync_blocklist нужен непустой "
                'объект вида {"ip": "BLOCK_BY_IP_2", "fqdn": "BBDNS_1"}')
    if eff.get("backup_config"):
        if not type_cfg.get("config_command") and not type_cfg.get("policy_commands"):
            raise SyncConfigError(
                f"Резервное копирование включено, но у модели «{model}» не заданы ни "
                f"команда выгрузки конфигурации, ни команды политики доступа — "
                f"задайте их в разделе «Модели».")


_POLICY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_device_type(cfg):
    """Проверка конфига модели — общая для карточки модели и импорта."""
    parser = cfg.get("interfaces_parser", "")
    if parser not in INTERFACE_PARSERS:
        raise SyncConfigError(
            f"Парсер интерфейсов «{parser}» не реализован в netbox_devices_sync.py. "
            f"Допустимо: {', '.join(INTERFACE_PARSERS)}")
    if cfg.get("sync_macs") and parser not in MAC_CAPABLE_PARSERS:
        raise SyncConfigError(
            f"Сбор MAC-таблицы не реализован для парсера «{parser}» "
            f"(есть только для {', '.join(sorted(MAC_CAPABLE_PARSERS))}).")
    if cfg.get("sync_lldp") and parser not in LLDP_CAPABLE_PARSERS:
        raise SyncConfigError(
            f"Сбор LLDP-соседей не реализован для парсера «{parser}» "
            f"(есть только для {', '.join(sorted(LLDP_CAPABLE_PARSERS))}).")
    if cfg.get("sync_lldp") and not cfg.get("lldp_command"):
        raise SyncConfigError("Сбор LLDP-соседей включён, но не задана команда LLDP-соседей")
    policy = cfg.get("policy_commands")
    if policy is not None and (
            not isinstance(policy, dict) or not policy
            or not all(isinstance(k, str) and _POLICY_NAME_RE.match(k)
                       and isinstance(v, str) and v.strip() for k, v in policy.items())):
        raise SyncConfigError(
            "Команды политики доступа: нужен объект вида "
            '{"firewall_rules": "firewall rules show"}; имя — латиница, цифры, «_» и «-» '
            "(из него получается имя файла снимка).")
    if cfg.get("backup_config") and not cfg.get("config_command") and not cfg.get("policy_commands"):
        raise SyncConfigError(
            "Резервное копирование включено, но не заданы ни команда выгрузки конфигурации, "
            "ни команды политики доступа")
    if (cfg.get("cluster") or "") not in CLUSTER_MODES:
        raise SyncConfigError(
            f"Кластер «{cfg.get('cluster')}» синку неизвестен. Допустимо: "
            f"{', '.join(m for m in CLUSTER_MODES if m)} или пусто.")
    if not cfg.get("manufacturer") or not cfg.get("role"):
        raise SyncConfigError("Производитель и роль обязательны — по ним синк заводит объекты в NetBox")
