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
INTERFACE_PARSERS = ["xfw", "eltex_status", "cisco_ios", "maipu", "huawei_vrp"]
# Снятие MAC-таблицы поддержано для этих парсеров. У Eltex, Maipu и Huawei
# это таблица коммутации (кто за каким портом), у ViPNet — ARP-таблица:
# команда там называется «inet show mac-address-table», но отдаёт вывод
# arp -n, поэтому в NetBox такие записи помечаются отдельным маркером
# (см. _ARP_DESCRIPTION в netbox_devices_sync.py). ViPNet снимается не
# отдельным проходом, а вместе с интерфейсами кластера, одним логином.
MAC_CAPABLE_PARSERS = {"eltex_status", "maipu", "huawei_vrp", "xfw"}
# LLDP умеют Cisco, Eltex, Maipu и Huawei — у xFW 5000 протокола нет вовсе,
# так что включённый на нём флаг означал бы шаг, который нечем выполнить.
LLDP_CAPABLE_PARSERS = {"eltex_status", "cisco_ios", "maipu", "huawei_vrp"}

DEVICE_STATUSES = ["active", "planned", "staged", "failed", "inventory", "offline"]

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
        try:
            when = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
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


def validate_name(value, existing, original=None, field="Имя"):
    """Непустое, без пробелов, уникальное. original — имя до правки, оно
    из проверки уникальности исключается."""
    value = (value or "").strip()
    if not value:
        raise SyncConfigError(f"{field}: обязательное поле")
    if re.search(r"\s", value):
        raise SyncConfigError(f"{field}: пробелы недопустимы — «{value}»")
    if value != original and value in existing:
        raise SyncConfigError(f"{field}: «{value}» уже есть в этом файле")
    return value
