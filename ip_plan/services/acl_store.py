"""Кэш распарсенных ACL из netbox-sync/config_backups/<device>/latest.cfg.

Ничего не пишет в NetBox и не имеет собственных Django-моделей — только
читает .cfg-файлы (см. settings.ACL_CONFIG_BACKUPS_DIR) и держит разбор в
памяти процесса, инвалидируя по mtime файла (та же идея, что и column_info.json
у прочих справочников проекта, но с проверкой свежести вместо разового чтения
при старте — файлы обновляются netbox_config_backup.py в фоне, без рестарта
этого контейнера).
"""

import ipaddress
import logging
from pathlib import Path

from django.conf import settings

from . import cisco_acl

logger = logging.getLogger(__name__)

# device_name -> {"mtime": float, "data": ParsedDevice}
_CACHE: dict = {}


def _config_root() -> Path:
    return Path(settings.ACL_CONFIG_BACKUPS_DIR)


def _iter_latest_cfg_files():
    root = _config_root()
    if not root.is_dir():
        return
    for device_dir in sorted(root.iterdir()):
        if not device_dir.is_dir():
            continue
        cfg_file = device_dir / "latest.cfg"
        if cfg_file.is_file():
            yield device_dir.name, cfg_file


def get_all_devices() -> list:
    """Все устройства с распарсенными ACL, читая заново только изменившиеся
    (по mtime) файлы. Возвращает [] и логирует warning, если каталог с
    бэкапами не смонтирован/пуст — не исключение, чтобы страница не падала,
    если full_sync.sh ещё ни разу не прогонялся на этом стенде."""
    root = _config_root()
    if not root.is_dir():
        logger.warning("ACL_CONFIG_BACKUPS_DIR не найден: %s", root)
        return []

    result = []
    seen = set()
    for device_name, cfg_file in _iter_latest_cfg_files():
        seen.add(device_name)
        mtime = cfg_file.stat().st_mtime
        cached = _CACHE.get(device_name)
        if cached and cached["mtime"] == mtime:
            result.append(cached["data"])
            continue
        try:
            text = cfg_file.read_text(encoding="utf-8", errors="replace")
            parsed = cisco_acl.parse_config(text, device_name=device_name)
        except OSError as exc:
            logger.error("Не удалось прочитать %s: %s", cfg_file, exc)
            continue
        _CACHE[device_name] = {"mtime": mtime, "data": parsed}
        result.append(parsed)

    # Устройство пропало из config_backups (переименовали/удалили) — не
    # держим его в кэше вечно.
    for stale in set(_CACHE) - seen:
        del _CACHE[stale]

    return result


def get_device(device_name: str):
    """ParsedDevice по точному имени (как в config_backups/<device>/) или
    None — для страницы деталей одного ACL (acl_detail)."""
    for device in get_all_devices():
        if device.device_name == device_name:
            return device
    return None


def list_device_names() -> list:
    """Имена устройств для дропбокса фильтра — в порядке, в котором
    get_all_devices() их отдаёт (сортировка по имени каталога, см. _iter_latest_cfg_files)."""
    return [d.device_name for d in get_all_devices()]


def overview(device_name: str | None = None):
    """Список для обзорной таблицы: по каждому ACL каждого устройства —
    имя, тип, число правил, применён ли и где. device_name — фильтр по
    конкретному устройству, None/'' — показать все."""
    rows = []
    for device in get_all_devices():
        if device_name and device.device_name != device_name:
            continue
        for acl in device.acls:
            apps = [a for a in device.applications if a.acl_name == acl.name]
            rows.append({
                "device": device.device_name,
                "acl_name": acl.name,
                "acl_type": acl.acl_type,
                "rule_count": len(acl.rules),
                "applied": bool(apps),
                "applications": apps,
            })
    return rows


def _object_group_users(device, name: str) -> list:
    """Имена ACL устройства, у которых хотя бы одно правило ссылается на
    object-group name как source или destination (см. Endpoint.kind ==
    'object-group' в cisco_acl.py)."""
    used = set()
    for acl in device.acls:
        for rule in acl.rules:
            for ep in (rule.source, rule.destination):
                if ep is not None and ep.kind == "object-group" and ep.object_group == name:
                    used.add(acl.name)
    return sorted(used)


def list_object_groups(device_name: str | None = None):
    """Обзорная таблица object-group network: устройство, имя, число
    адресов/подсетей в группе, какими ACL она используется. device_name —
    фильтр, None/'' — все устройства (тот же смысл, что и в overview())."""
    rows = []
    for device in get_all_devices():
        if device_name and device.device_name != device_name:
            continue
        for name, networks in device.object_groups.items():
            rows.append({
                "device": device.device_name,
                "name": name,
                "member_count": len(networks),
                "used_by": _object_group_users(device, name),
            })
    return rows


def get_object_group(device_name: str, name: str):
    """dict с полным содержимым одной object-group network (для страницы
    деталей) или None, если устройство/группа не найдены."""
    device = get_device(device_name)
    if device is None or name not in device.object_groups:
        return None
    return {
        "device": device.device_name,
        "name": name,
        "networks": device.object_groups[name],
        "used_by": _object_group_users(device, name),
    }


def _endpoint_matches(ip: ipaddress.IPv4Address, endpoint) -> bool:
    if endpoint is None or not endpoint.resolved:
        return False
    return any(ip in net for net in endpoint.networks)


def _is_catch_all(rule) -> bool:
    """'any' по source И (нет destination, или тоже 'any') — правило матчит
    вообще всё. Технически корректный результат для любого IP, но если не
    отсортировать его вниз, такие строки (обычно 'deny/permit ip any any'
    в хвосте ACL) забивают верх результатов при каждом запросе."""
    src_any = rule.source is not None and rule.source.kind == "any"
    dst_any = rule.destination is None or rule.destination.kind == "any"
    return src_any and dst_any


def search_ip(ip_str: str, device_name: str | None = None):
    """Все правила (по всем устройствам/ACL, либо только по device_name,
    если задан), где ip_str встречается как source и/или destination.
    Бросает ValueError, если ip_str — не валидный IPv4-адрес (пусть
    вызывающая view сама решает, как это показать).

    Сортировка: применённые ACL выше неприменённых, конкретные совпадения
    выше catch-all 'any'/'any' — иначе 'deny ip any any' в хвосте ACL
    (матчит буквально любой IP) забивал бы верх результатов всегда."""
    ip = ipaddress.ip_address(ip_str)

    matches = []
    for device in get_all_devices():
        if device_name and device.device_name != device_name:
            continue
        for acl in device.acls:
            applied = device.is_applied(acl.name)
            for rule in acl.rules:
                if rule.action == "remark":
                    continue
                src_hit = _endpoint_matches(ip, rule.source)
                dst_hit = _endpoint_matches(ip, rule.destination)
                if not (src_hit or dst_hit):
                    continue
                matches.append({
                    "device": device.device_name,
                    "acl_name": acl.name,
                    "acl_type": acl.acl_type,
                    "applied": applied,
                    "rule": rule,
                    "matched_as": (
                        "source+destination" if src_hit and dst_hit
                        else "source" if src_hit else "destination"
                    ),
                    "catch_all": _is_catch_all(rule),
                })

    matches.sort(key=lambda m: (not m["applied"], m["catch_all"]))
    return matches
