"""Чтение и разбор последнего лога netbox-sync/full_sync.sh
(см. settings.SYNC_LOGS_DIR — по умолчанию путь внутри контейнера, куда
docker-compose.yml монтирует ${NETBOX_SYNC_DIR}/sync_logs, read-only).

Ничего не пишет и не имеет своих Django-моделей — только читает файлы вида
full_sync_YYYY-MM-DD_HHMM.log, которые full_sync.sh кладёт в эту папку при
каждом прогоне (хранит 30 дней, чистит сам). Формат — обычный текстовый лог
(см. netbox-sync/full_sync.sh, функция log()): заголовок с временем
старта/env/параметрами скана, затем по каждому из 8 шагов — сырой
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

# Порядок и названия шагов — дословно STEP_NAMES из netbox-sync/full_sync.sh.
# Если этот список там когда-нибудь изменится, поправить и здесь.
STEP_NAMES = [
    "Custom fields", "VLAN+prefixes", "Devices", "Config backup",
    "Nmap scan", "RedVirt", "vCenter", "DNS enrich",
]

_HEADER_RE = re.compile(r"Full sync started:\s*(?P<started>\S+ \S+)\s+\[env=(?P<env>\w+)\]")
_SCAN_RE = re.compile(r"Scan:\s*mode=(?P<mode>\S+)\s+resolve-dns=(?P<dns>on|off)\s+timeout=(?P<timeout>\d+)s")
# "  [Имя шага] СТАТУС  (Nс)" — пишется строкой log() сразу по завершении
# шага (см. full_sync.sh), задолго до итогового блока SUMMARY в конце.
_STEP_STATUS_RE = re.compile(
    r"^  \[(?P<name>[^\]]+)\]\s+(?P<status>OK|FAILED|TIMEOUT)\s+\((?P<elapsed>\d+)s\)\s*$",
    re.MULTILINE,
)

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


def get_summary():
    """Сводка по последнему прогону для dropdown в шапке. available=False —
    либо папка логов не примонтирована/пуста (netbox-sync ещё ни разу не
    запускался на этом хосте, либо NETBOX_SYNC_DIR неверно настроен)."""
    path = _latest_log_path()
    if path is None:
        return {"available": False}

    text = path.read_text(encoding="utf-8", errors="replace")
    header = _HEADER_RE.search(text)
    scan = _SCAN_RE.search(text)
    statuses = {
        m.group("name"): (m.group("status"), int(m.group("elapsed")))
        for m in _STEP_STATUS_RE.finditer(text)
    }
    has_summary = "\n  SUMMARY\n" in text

    steps = []
    running_step = None
    for name in STEP_NAMES:
        found = statuses.get(name)
        if found:
            status, elapsed = found
        else:
            status, elapsed = None, None
            if running_step is None and not has_summary:
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
        "log_file": path.name,
        "started_at": header.group("started") if header else None,
        "env": header.group("env") if header else None,
        "scan_mode": scan.group("mode") if scan else None,
        "resolve_dns": scan.group("dns") if scan else None,
        "overall": overall,
        "running_step": running_step,
        "steps": steps,
    }


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
