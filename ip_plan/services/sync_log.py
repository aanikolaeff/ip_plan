"""Чтение и разбор последнего лога netbox-sync/full_sync.sh
(см. settings.SYNC_LOGS_DIR — по умолчанию путь внутри контейнера, куда
docker-compose.yml монтирует ${NETBOX_SYNC_DIR}/sync_logs, read-only).

Ничего не пишет и не имеет своих Django-моделей — только читает файлы вида
full_sync_YYYY-MM-DD_HHMM.log, которые full_sync.sh кладёт в эту папку при
каждом прогоне (хранит 30 дней, чистит сам). Формат — обычный текстовый лог
(см. netbox-sync/full_sync.sh, функция log()): заголовок с временем
старта/env/параметрами скана, затем по каждому шагу (см. STEPS) — сырой
stdout/stderr скрипта, заканчивающийся строкой "  [Имя шага] СТАТУС (Nс)",
и в конце (только если прогон полностью завершился) блок "SUMMARY" с той же
таблицей статусов ещё раз.

Никакого IPC/lock-файла не читаем: "выполняется ли прогон прямо сейчас"
выводим из самого текста лога — если в нём нет строки SUMMARY, но встретился
хотя бы один шаг без строки статуса, значит скрипт до него ещё не дошёл (шаги
всегда идут строго по порядку, см. STEP_NAMES ниже — тот же порядок, что и в
full_sync.sh)."""

import re
from pathlib import Path

from django.conf import settings

# Шаги full_sync.sh: имя (дословно STEP_NAMES оттуда — по нему строки лога
# связываются с шагом), скрипт (STEP_SCRIPTS) и подписи для «Настроек».
# Это ручная копия: 02.10.2026 обнаружилось, что здесь не хватает
# «Reachability», и пока он шёл, сводка показывала текущим шагом
# «DNS enrich». Меняя full_sync.sh, правьте и этот список — тест
# test_sync_log сверяет его с самим скриптом, если тот рядом.
STEPS = [
    {"name": "Custom fields", "script": "netbox_ensure_custom_fields.py",
     "title": "Служебные поля", "desc": "Проверяет и создаёт пользовательские поля NetBox"},
    {"name": "VLAN+prefixes", "script": "netbox_network_setup.py",
     "title": "VLAN и подсети", "desc": "Подсети и VLAN с ядра сети"},
    {"name": "Devices", "script": "netbox_devices_sync.py",
     "title": "Устройства", "desc": "Интерфейсы, адреса, MAC- и ARP-таблицы, серийные номера"},
    {"name": "Config backup", "script": "netbox_config_backup.py",
     "title": "Снимки конфигураций", "desc": "Конфигурации и политика доступа — из них строится раздел ACL"},
    {"name": "Firewall blocklist", "script": "netbox_firewall_blocklist_sync.py",
     "title": "Списки блокировок xFW", "desc": "Текущие списки на firewall — для сверки в «Блокировках»"},
    {"name": "Nmap scan", "script": "scan_all.py",
     "title": "Сканирование сетей", "desc": "Открытые порты и ОС по всем подсетям",
     "schedule_only": True},
    {"name": "RedVirt", "script": "netbox_redvirt_sync.py",
     "title": "Виртуальные машины RedVirt", "desc": "Кластеры, хосты, ВМ, их адреса и гостевые ОС"},
    {"name": "vCenter", "script": "netbox_vcenter_sync.py",
     "title": "Виртуальные машины vCenter", "desc": "Кластеры, хосты ESXi, ВМ, их адреса и гостевые ОС"},
    {"name": "LLDP topology", "script": "netbox_lldp_topology.py",
     "title": "Связи между устройствами", "desc": "Кабели по LLDP, LACP, гипервизорам, MAC- и ARP-таблицам"},
    {"name": "Reachability", "script": "netbox_reachability.py",
     "title": "Доступность", "desc": "Какие адреса отвечают — для статусов в таблице адресов"},
    {"name": "DNS enrich", "script": "netbox_dns_enrich.py",
     "title": "DNS-имена", "desc": "Прямые и обратные DNS-записи для адресов"},
]
STEP_NAMES = [s["name"] for s in STEPS]

_HEADER_RE = re.compile(
    r"Full sync started:\s*(?P<started>\S+ \S+)\s+\[env=(?P<env>\w+)\]"
    r"(?:\s+\[version=(?P<version>[^\]]+)\])?"
)
_SCAN_RE = re.compile(r"Scan:\s*mode=(?P<mode>\S+)\s+resolve-dns=(?P<dns>on|off)\s+timeout=(?P<timeout>\d+)s")
# "  [Имя шага] СТАТУС  (Nс)" — пишется строкой log() сразу по завершении
# шага (см. full_sync.sh), задолго до итогового блока SUMMARY в конце.
_STEP_STATUS_RE = re.compile(
    r"^  \[(?P<name>[^\]]+)\]\s+(?P<status>OK|FAILED|TIMEOUT)\s+\((?P<elapsed>\d+)s\)\s*$",
    re.MULTILINE,
)

# "  [Имя шага] <команда>" — строка начала шага между двумя линиями "----".
# По ней видно, какой шаг идёт сейчас: раньше текущим считался первый шаг без
# строки статуса, и при расхождении STEP_NAMES со скриптом это был не тот шаг.
_STEP_START_RE = re.compile(r"^  \[(?P<name>[^\]]+)\] (?!OK\b|FAILED\b|TIMEOUT\b)\S", re.MULTILINE)
_FINISHED_RE = re.compile(r"Full sync finished:\s*(?P<finished>\S+ \S+)")

# На случай очень большого/повреждённого лога — не читаем в память больше
# этого объёма сырого текста для модалки "развернуть лог".
_RAW_MAX_BYTES = 2_000_000


def _logs_dir() -> Path:
    return Path(settings.SYNC_LOGS_DIR)


def _latest_log_path():
    root = _logs_dir()
    if not root.is_dir():
        return None
    files = sorted(root.glob("full_sync_*.log"))
    return files[-1] if files else None


def parse(text):
    """Разбор текста одного лога full_sync.sh — общий для сводки в шапке и
    раздела «Настройки → Запуск и расписание». Только JSON-совместимые
    значения: результат уходит и в /sync-log/summary.json."""
    header = _HEADER_RE.search(text)
    scan = _SCAN_RE.search(text)
    finished = _FINISHED_RE.search(text)
    statuses = {
        m.group("name"): (m.group("status"), int(m.group("elapsed")))
        for m in _STEP_STATUS_RE.finditer(text)
    }
    started_steps = {m.group("name") for m in _STEP_START_RE.finditer(text)}
    has_summary = "\n  SUMMARY\n" in text

    steps = []
    running_step = None
    for name in STEP_NAMES:
        found = statuses.get(name)
        if found:
            status, elapsed = found
        else:
            status, elapsed = None, None
            # Идёт тот шаг, у которого есть строка начала, но нет итога. У
            # логов, снятых до появления строки начала в этом виде, остаётся
            # прежний признак — первый шаг без итога.
            if running_step is None and not has_summary and (
                    name in started_steps or not started_steps):
                running_step = name
        steps.append({"name": name, "status": status, "elapsed": elapsed})

    if has_summary:
        overall = "FAILED" if any(s["status"] in ("FAILED", "TIMEOUT") for s in steps) else "OK"
    elif running_step:
        overall = "RUNNING"
    else:
        # Лог начался, но не завершился ни один шаг и SUMMARY нет — прогон
        # оборвался совсем рано (например, не поставились зависимости venv).
        overall = "UNKNOWN"

    return {
        "available": True,
        "started_at": header.group("started") if header else None,
        "finished_at": finished.group("finished") if finished else None,
        "env": header.group("env") if header else None,
        # Версия netbox-sync, на которой шёл прогон. None у логов, снятых до
        # появления этой строки — тогда в интерфейсе её просто не показываем.
        "sync_version": header.group("version") if header else None,
        "scan_mode": scan.group("mode") if scan else None,
        "resolve_dns": scan.group("dns") if scan else None,
        "overall": overall,
        "running_step": running_step,
        "steps": steps,
    }


def get_summary():
    """Сводка по последнему прогону для индикатора в шапке. available=False —
    либо папка логов не примонтирована/пуста (netbox-sync ещё ни разу не
    запускался на этом хосте, либо NETBOX_SYNC_DIR неверно настроен)."""
    path = _latest_log_path()
    if path is None:
        return {"available": False}
    summary = parse(path.read_text(encoding="utf-8", errors="replace"))
    summary["log_file"] = path.name
    return summary


def previous_runs(limit=5):
    """Разборы завершённых прогонов перед последним (новые первыми) — для
    оценки «осталось ~N мин» по средним длительностям шагов. Логи
    full_sync.sh хранит 30 дней, здесь берём только несколько последних."""
    root = _logs_dir()
    if not root.is_dir():
        return []
    runs = []
    for path in sorted(root.glob("full_sync_*.log"))[-2::-1]:
        if len(runs) >= limit:
            break
        parsed = parse(path.read_text(encoding="utf-8", errors="replace"))
        if parsed["overall"] in ("OK", "FAILED"):
            runs.append(parsed)
    return runs


def get_tail(lines=60):
    """Последние строки текущего лога — «Журнал прогона» в «Настройках».
    Читаем только хвост файла: во время сканирования лог растёт до десятков
    мегабайт."""
    path = _latest_log_path()
    if path is None:
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 64_000))
        chunk = fh.read().decode("utf-8", errors="replace")
    result = chunk.splitlines()[-lines:]
    return result


def get_raw_text():
    """Полный текст последнего лога для модалки "Развернуть лог". Возвращает
    (имя_файла, текст, truncated) или (None, None, False), если лога нет.
    truncated=True — показан только хвост (см. _RAW_MAX_BYTES), самое
    интересное (текущий/последний шаг, SUMMARY) всегда в конце файла."""
    path = _latest_log_path()
    if path is None:
        return None, None, False

    data = path.read_bytes()
    truncated = len(data) > _RAW_MAX_BYTES
    if truncated:
        data = data[-_RAW_MAX_BYTES:]
    return path.name, data.decode("utf-8", errors="replace"), truncated
