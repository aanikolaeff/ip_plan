"""Кэш распарсенных списков доступа из netbox-sync/config_backups/<device>/.

Ничего не пишет в NetBox и не имеет собственных Django-моделей — только
читает файлы снимков (см. settings.ACL_CONFIG_BACKUPS_DIR) и держит разбор
в памяти процесса, инвалидируя по mtime файлов: их обновляет
netbox_config_backup.py в фоне, без рестарта этого контейнера.

Раскладка каталога устройства (пишет netbox_config_backup.py):
  meta.json                 — устройство, модель, parser, время, по файлам
                              команда/время изменения/версия политики
  latest.cfg                — полная конфигурация («config»)
  latest.<имя>.txt          — прочие снимки (policy_commands: firewall_rules…)
  <dev>_<ts>.cfg / .<имя>.txt — история изменений
Парсер выбирается по meta.json["parser"] через acl_parsers.PARSERS; каталог
без meta.json (снимок ASR до появления файла) разбирается как Cisco.
"""

import difflib
import ipaddress
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from django.conf import settings

from . import acl_parsers
from .acl_model import ParsedDevice

logger = logging.getLogger(__name__)

# device_name -> {"key": tuple(mtime...), "data": ParsedDevice, "meta": dict}
_CACHE: dict = {}


def _config_root() -> Path:
    return Path(settings.ACL_CONFIG_BACKUPS_DIR)


def _read_meta(device_dir: Path) -> dict:
    path = device_dir / "meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("meta.json устройства %s не читается: %s", device_dir.name, exc)
        return {}


def _latest_name(snapshot: str) -> str:
    return "latest.cfg" if snapshot == "config" else f"latest.{snapshot}.txt"


def _snapshot_paths(device_dir: Path, meta: dict) -> dict:
    """{имя снимка: Path} для файлов, нужных парсеру устройства и реально
    лежащих на диске."""
    parser = meta.get("parser") or acl_parsers.DEFAULT_PARSER
    paths = {}
    for name in acl_parsers.needed_files(parser):
        path = device_dir / _latest_name(name)
        if path.is_file():
            paths[name] = path
    return paths


def _iter_device_dirs():
    root = _config_root()
    if not root.is_dir():
        return
    for device_dir in sorted(root.iterdir()):
        if device_dir.is_dir():
            yield device_dir


def get_all_devices() -> list:
    """Все устройства с распарсенными списками, читая заново только те, у
    которых изменился хоть один файл снимка (по mtime). Возвращает [] и
    логирует warning, если каталог с бэкапами не смонтирован/пуст — не
    исключение, чтобы страница не падала, если full_sync.sh ещё ни разу не
    прогонялся на этом стенде."""
    root = _config_root()
    if not root.is_dir():
        logger.warning("ACL_CONFIG_BACKUPS_DIR не найден: %s", root)
        return []

    result = []
    seen = set()
    for device_dir in _iter_device_dirs():
        meta = _read_meta(device_dir)
        paths = _snapshot_paths(device_dir, meta)
        if not paths:
            continue   # каталог без нужных парсеру файлов — ещё не снимали
        device_name = device_dir.name
        seen.add(device_name)
        key = tuple(sorted((name, p.stat().st_mtime) for name, p in paths.items()))
        cached = _CACHE.get(device_name)
        if cached and cached["key"] == key:
            result.append(cached["data"])
            continue
        files = {}
        try:
            for name, path in paths.items():
                files[name] = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.error("Не удалось прочитать снимок %s: %s", device_dir, exc)
            continue
        parser = meta.get("parser") or acl_parsers.DEFAULT_PARSER
        try:
            parsed = acl_parsers.parse(parser, files, device_name)
        except Exception as exc:   # noqa: BLE001 — один сломанный снимок не должен валить страницу
            logger.exception("Разбор снимка %s (%s) упал", device_name, parser)
            parsed = ParsedDevice(device_name=device_name, parser=parser,
                                  warnings=[f"Разбор снимка упал: {exc}"])
        _CACHE[device_name] = {"key": key, "data": parsed, "meta": meta}
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


def get_meta(device_name: str) -> dict:
    """meta.json устройства (после get_all_devices — из кэша)."""
    get_all_devices()
    cached = _CACHE.get(device_name)
    return cached["meta"] if cached else {}


def _wanted(device, device_name, devices) -> bool:
    """Общий фильтр: конкретное устройство (device_name) и/или множество
    устройств площадки (devices, None — без ограничения)."""
    if device_name and device.device_name != device_name:
        return False
    if devices is not None and device.device_name not in devices:
        return False
    return True


def list_device_names(devices=None) -> list:
    """Имена устройств для дропбокса фильтра — в порядке, в котором
    get_all_devices() их отдаёт (сортировка по имени каталога); devices —
    ограничение площадкой из бокового меню."""
    return [d.device_name for d in get_all_devices() if _wanted(d, None, devices)]


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def snapshot_info(device_name: str | None = None, devices=None) -> list:
    """Сводка по снимкам для шапки страницы: устройство, парсер, когда
    снимали, когда последний раз менялось, версия политики (ViPNet Rules Set
    Id), предупреждения парсера. device_name — фильтр, None — все."""
    rows = []
    for device in get_all_devices():
        if not _wanted(device, device_name, devices):
            continue
        meta = get_meta(device.device_name)
        files = meta.get("files") or {}
        changed = [f.get("changed") for f in files.values() if f.get("changed")]
        versions = sorted({f["rules_set_id"] for f in files.values() if f.get("rules_set_id")})
        rows.append({
            "device": device.device_name,
            "parser": device.parser or meta.get("parser"),
            "model": meta.get("model", ""),
            "site": meta.get("site") or "",
            "updated": _parse_iso(meta.get("updated")),
            "changed": _parse_iso(max(changed)) if changed else None,
            "rules_set_id": ", ".join(versions) if versions else None,
            "files": sorted(files),
            "acl_count": len(device.acls),
            "rule_count": sum(len(a.rules) for a in device.acls),
            "warnings": list(device.warnings),
        })
    return rows


def overview(device_name: str | None = None, devices=None):
    """Список для обзорной таблицы: по каждому ACL каждого устройства —
    имя, тип, число правил, применён ли и где. device_name — фильтр по
    конкретному устройству, None/'' — показать все."""
    rows = []
    for device in get_all_devices():
        if not _wanted(device, device_name, devices):
            continue
        for acl in device.acls:
            apps = [a for a in device.applications if a.acl_name == acl.name]
            rows.append({
                "device": device.device_name,
                "acl_name": acl.name,
                "acl_type": acl.acl_type,
                "rule_count": len(acl.rules),
                "inactive_count": sum(1 for r in acl.rules if r.inactive),
                "applied": bool(apps),
                "applications": apps,
            })
    return rows


def _endpoint_objects(endpoint) -> list:
    """Все объекты, на которые ссылается операнд: Endpoint.object_groups, а
    для парсеров, которые его не заполняют, — object_group."""
    if endpoint is None:
        return []
    if endpoint.object_groups:
        return list(endpoint.object_groups)
    return [endpoint.object_group] if endpoint.object_group else []


def _object_group_users(device, name: str) -> list:
    """Имена ACL устройства, у которых хотя бы одно правило ссылается на
    объект name как source или destination — в том числе внутри смешанного
    операнда (объект + адреса, kind «mixed»)."""
    used = set()
    for acl in device.acls:
        for rule in acl.rules:
            for ep in (rule.source, rule.destination):
                if name in _endpoint_objects(ep):
                    used.add(acl.name)
    return sorted(used)


def _object_group_parents(device, name: str) -> list:
    """Объекты устройства, среди включений которых есть name."""
    return sorted(parent for parent, children in device.object_group_includes.items()
                  if name in children)


def list_object_groups(device_name: str | None = None, devices=None):
    """Обзорная таблица объектов (Cisco object-group network, ViPNet Ip
    Objects): устройство, имя, число адресов/подсетей, какими ACL
    используется. device_name — фильтр, None/'' — все устройства."""
    rows = []
    for device in get_all_devices():
        if not _wanted(device, device_name, devices):
            continue
        for name, networks in device.object_groups.items():
            rows.append({
                "device": device.device_name,
                "name": name,
                "member_count": len(networks) or len(device.object_group_members.get(name) or ()),
                "exclusion_count": len(device.object_group_exclusions.get(name) or ()),
                "used_by": _object_group_users(device, name),
                "included_in": _object_group_parents(device, name),
            })
    return rows


def get_object_group(device_name: str, name: str):
    """dict с полным содержимым одного объекта (для страницы деталей) или
    None, если устройство/объект не найдены."""
    device = get_device(device_name)
    if device is None or name not in device.object_groups:
        return None
    return {
        "device": device.device_name,
        "name": name,
        "networks": device.object_groups[name],
        "exclusions": device.object_group_exclusions.get(name) or [],
        "used_by": _object_group_users(device, name),
        "included_in": _object_group_parents(device, name),
        "includes": device.object_group_includes.get(name) or [],
        # Состав показываем только у объектов из узлов ViPNet («0x…»): у
        # адресных объектов он дублировал бы networks той же таблицей.
        "members": [m for m in (device.object_group_members.get(name) or ())
                    if m.startswith("0x")],
        # Доменные имена и прочие не-адресные члены (объекты вроде BBDNS_1
        # состоят из одних FQDN — без этого карточка выглядела бы пустой).
        "fqdns": [m for m in (device.object_group_members.get(name) or ())
                  if m and not m.startswith("0x") and not m.startswith("@")
                  and _not_address(m)],
    }


def _not_address(value: str) -> bool:
    from .vipnet_firewall import _value_networks
    return _value_networks(value) is None


def _endpoint_matches(ip: ipaddress.IPv4Address, endpoint, device) -> bool:
    """Попадание IP в операнд. У объекта с исключениями (ViPNet Exclusion)
    адрес из исключения не считается совпадением."""
    if endpoint is None or not endpoint.resolved:
        return False
    if not any(ip in net for net in endpoint.networks):
        return False
    objects = _endpoint_objects(endpoint)
    if endpoint.kind == "object-group" and len(objects) == 1:
        # Единственный объект: его исключение снимает совпадение. У операнда
        # из нескольких объектов или с литералами исключение не применяем —
        # адрес мог попасть через другую часть, а по частям сети не хранятся.
        excl = device.object_group_exclusions.get(objects[0]) or ()
        if any(ip in net for net in excl):
            return False
    return True


def _is_catch_all(rule) -> bool:
    """'any' по source И (нет destination, или тоже 'any') — правило матчит
    вообще всё. Технически корректный результат для любого IP, но если не
    отсортировать его вниз, такие строки (обычно 'deny/permit ip any any'
    в хвосте ACL) забивают верх результатов при каждом запросе."""
    src_any = rule.source is not None and rule.source.kind == "any"
    dst_any = rule.destination is None or rule.destination.kind == "any"
    return src_any and dst_any


def search_ip(ip_str: str, device_name: str | None = None, devices=None):
    """Все правила (по всем устройствам/ACL, либо только по device_name,
    если задан), где ip_str встречается как source и/или destination.
    Бросает ValueError, если ip_str — не валидный IPv4-адрес (пусть
    вызывающая view сама решает, как это показать).

    Сортировка: применённые ACL выше неприменённых, действующие правила
    выше выключенных (inactive), конкретные совпадения выше catch-all
    'any'/'any' — иначе 'deny ip any any' в хвосте ACL (матчит буквально
    любой IP) забивал бы верх результатов всегда."""
    ip = ipaddress.ip_address(ip_str)

    matches = []
    for device in get_all_devices():
        if not _wanted(device, device_name, devices):
            continue
        for acl in device.acls:
            applied = device.is_applied(acl.name)
            for rule in acl.rules:
                if rule.action == "remark":
                    continue
                src_hit = _endpoint_matches(ip, rule.source, device)
                dst_hit = _endpoint_matches(ip, rule.destination, device)
                if not (src_hit or dst_hit):
                    continue
                # Совпадение только через «any» (permit ip host X any — любой
                # IP попадает как destination) — формально верно, но для
                # поиска малоинформативно; такие строки уходят вниз вместе с
                # catch-all, а конкретные попадания остаются сверху.
                specific = ((src_hit and rule.source.kind != "any")
                            or (dst_hit and rule.destination.kind != "any"))
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
                    "catch_all": _is_catch_all(rule) or not specific,
                    "specific": specific,
                })

    matches.sort(key=lambda m: (not m["applied"], m["rule"].inactive, not m["specific"]))
    return matches


# ── История снимков и diff ──────────────────────────────────────────────

_HISTORY_RE = re.compile(r"^(?P<dev>.+)_(?P<ts>\d{8}_\d{6})(?P<suffix>\.cfg|\.(?P<name>[A-Za-z0-9_-]+)\.txt)$")

# Строки конфигураций, которые нельзя показывать в diff: секреты Cisco/Huawei/
# Eltex/Maipu. Маскируем всю строку целиком — так проще, чем угадывать
# позицию значения в каждом вендорском синтаксисе.
_SECRET_LINE_RE = re.compile(
    r"(snmp-server community|snmp-agent community|community read|community write|"
    r"\bpassword\b|\bsecret\b|\bkey\b|authentication-key|pre-shared|encrypted)",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if _SECRET_LINE_RE.search(line):
            indent = len(line) - len(line.lstrip(" "))
            out.append(" " * indent + "<строка скрыта: содержит секрет>")
        else:
            out.append(line)
    return "\n".join(out)


def list_history(device_name: str) -> list:
    """Снимки устройства с диска, новые сверху: [{name, snapshot, ts, size}].
    snapshot — имя снимка («config», «firewall_rules»…)."""
    device_dir = _config_root() / device_name
    if not device_dir.is_dir():
        return []
    rows = []
    for path in device_dir.iterdir():
        m = _HISTORY_RE.match(path.name)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("ts"), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        rows.append({
            "name": path.name,
            "snapshot": m.group("name") or "config",
            "ts": ts,
            "size": path.stat().st_size,
        })
    rows.sort(key=lambda r: (r["snapshot"], r["ts"]), reverse=True)
    return rows


def diff_snapshot(device_name: str, file_name: str):
    """unified diff между снимком file_name и предыдущим снимком того же
    имени (или None, если предыдущего нет). Строки с секретами маскируются
    ДО сравнения, так что изменение пароля видно как изменение скрытой
    строки, но не его значение. Возвращает dict или None, если файла нет
    или имя не похоже на снимок (защита от ../ в URL)."""
    m = _HISTORY_RE.match(file_name)
    if not m or "/" in file_name:
        return None
    device_dir = _config_root() / device_name
    path = device_dir / file_name
    if not path.is_file():
        return None
    snapshot = m.group("name") or "config"
    history = [r for r in list_history(device_name) if r["snapshot"] == snapshot]
    history.sort(key=lambda r: r["ts"])
    names = [r["name"] for r in history]
    idx = names.index(file_name) if file_name in names else -1
    previous = names[idx - 1] if idx > 0 else None

    new_text = _scrub(path.read_text(encoding="utf-8", errors="replace"))
    old_text = _scrub((device_dir / previous).read_text(encoding="utf-8", errors="replace")) if previous else ""
    diff = list(difflib.unified_diff(
        old_text.split("\n") if previous else [], new_text.split("\n"),
        fromfile=previous or "(нет предыдущего снимка)", tofile=file_name, lineterm="", n=3,
    ))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return {
        "device": device_name,
        "file": file_name,
        "snapshot": snapshot,
        "previous": previous,
        "diff": diff,
        "added": added,
        "removed": removed,
        "lines": len(new_text.split("\n")),
    }
