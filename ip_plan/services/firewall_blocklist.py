"""Чтение снимка объектов блокировки firewall
(netbox-sync/netbox_firewall_blocklist_sync.py, см. settings.FIREWALL_BLOCKLISTS_DIR
— по умолчанию путь внутри контейнера, куда docker-compose.yml монтирует
${NETBOX_SYNC_DIR}/firewall_blocklists, read-only, тот же принцип, что
sync_log.py/acl_store.py).

Используется генератором CLI-команд (views.regulator_firewall_commands):
"новый" список для firewall должен включать не только записи из реестра
регулятора, но и то, что реально уже применено сейчас — иначе значения,
попавшие на firewall в обход реестра (руками, до того как реестр начали
вести), молча выпали бы при следующей замене списка."""

import datetime
import json
from pathlib import Path

from django.conf import settings

# Единственное устройство с блокировками сейчас (netbox-sync/devices.json,
# sync_blocklist: true) — см. docs/plans/2026-09-04-regulator-blocklist-design.md.
# Если появится второе — вынести в настройку, как blocklist_objects в
# devices.json у netbox-sync.
DEVICE = "xfw5000-mdc"


def _snapshot_path():
    return Path(settings.FIREWALL_BLOCKLISTS_DIR) / DEVICE / "latest.json"


def get_current_values(object_name):
    """Значения именованного объекта (BLOCK_BY_IP_2, BBDNS_1 и т.п.) из
    последнего снимка. None — снимок недоступен (netbox-sync ни разу не
    запускался/не смонтирован) или конкретно этот объект в нём не найден
    (например, имя уже сменилось на firewall, а здесь ещё старое) —
    отличается от []: пустой список — снимок прочитан, но объект реально
    пуст, тоже валидный случай."""
    path = _snapshot_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get(object_name)


def snapshot_mtime():
    """Время последнего обновления снимка (для "актуально на..." в UI) —
    None, если файла нет."""
    path = _snapshot_path()
    if not path.is_file():
        return None
    return datetime.datetime.fromtimestamp(path.stat().st_mtime)
