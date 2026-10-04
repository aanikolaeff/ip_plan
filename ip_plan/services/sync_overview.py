"""Данные экрана «Настройки → Запуск и расписание».

Всё, что интерфейс знает о прогонах netbox-sync, он берёт из двух мест:
лог full_sync.sh (sync_log.parse) и lock-файл (sync_config.nmap_pid_file_present).
Здесь из них собирается то, что рисует экран: строки шагов с русскими
подписями и временем, карточки последнего прогона, прогресс идущего прогона
с оценкой «осталось» по прошлым логам и раскрашенный хвост журнала.

Время в логе — местное время хоста синка без зоны (settings.SYNC_LOG_TZ).

Запуск синка из интерфейса пока не сделан (решение 01.10.2026): кнопки на
экране — заглушки, см. views_settings.sync_run.
"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from . import sync_log
from . import sync_config as sc

STATUS_LABELS = {
    "ok": "OK", "fail": "Ошибка", "timeout": "Таймаут", "run": "выполняется",
    "wait": "в очереди", "none": "не выполнялся",
}
_LOG_STATUS = {"OK": "ok", "FAILED": "fail", "TIMEOUT": "timeout"}


def tz():
    return ZoneInfo(settings.SYNC_LOG_TZ)


def now():
    return datetime.now(tz())


def parse_log_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz())
    except ValueError:
        return None


def human_when(dt):
    """«сегодня, 03:00» / «вчера, 17:40» / «завтра, 03:00» / «28.09, 11:30» /
    «03.02.2025, 11:30». Принимает aware datetime (любой зоны)."""
    if dt is None:
        return ""
    local = dt.astimezone(tz())
    today = now().date()
    delta = (local.date() - today).days
    hm = local.strftime("%H:%M")
    if delta == 0:
        return f"сегодня, {hm}"
    if delta == -1:
        return f"вчера, {hm}"
    if delta == 1:
        return f"завтра, {hm}"
    if local.year == today.year:
        return f"{local:%d.%m}, {hm}"
    return f"{local:%d.%m.%Y}, {hm}"


def duration(seconds):
    """«48 с» / «3 мин 02 с» / «1 ч 05 мин» — один формат на весь раздел."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60:02d} с"
    return f"{seconds // 3600} ч {seconds % 3600 // 60:02d} мин"


_CRON_DAILY_RE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$")


def next_run(schedule=None):
    """Следующий запуск по расписанию вида «M H * * *» или None."""
    m = _CRON_DAILY_RE.match(schedule if schedule is not None else settings.SYNC_SCHEDULE)
    if not m:
        return None
    minute, hour = int(m.group(1)), int(m.group(2))
    current = now()
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def _averages(runs):
    """Средняя длительность каждого шага по прошлым прогонам (сек)."""
    sums, counts = {}, {}
    for run in runs:
        for step in run["steps"]:
            if step["elapsed"] is not None:
                sums[step["name"]] = sums.get(step["name"], 0) + step["elapsed"]
                counts[step["name"]] = counts.get(step["name"], 0) + 1
    return {name: sums[name] / counts[name] for name in sums}


def _line_kind(line):
    stripped = line.strip()
    if re.match(r"^\[[^\]]+\]\s+OK\s+\(", stripped):
        return "ok"
    if re.match(r"^\[[^\]]+\]\s+(FAILED|TIMEOUT)\s+\(", stripped):
        return "fail"
    if line.startswith("  [") and not re.match(r"^\[[^\]]+\]\s+(OK|FAILED|TIMEOUT)", stripped):
        return "start"
    if (not stripped or stripped.startswith("====") or stripped.startswith("----")
            or stripped.startswith("Full sync") or stripped.startswith("Scan:")
            or stripped.startswith("Log:") or stripped == "SUMMARY"):
        return "meta"
    if "ERROR" in line or "Traceback" in line:
        return "fail"
    return "out"


def journal(lines=80):
    """Хвост текущего лога, строки с видом для раскраски (ok/fail/start/meta/out).
    Пустые и разделительные строки подряд схлопываются — в журнале прогона
    они только занимают высоту."""
    result = []
    for line in sync_log.get_tail(lines):
        kind = _line_kind(line)
        if kind == "meta" and (not line.strip() or line.strip()[:4] in ("====", "----")):
            continue
        result.append({"text": line.rstrip(), "kind": kind})
    return result


def overview():
    """Всё для экрана синхронизации и точки в рейке.

    state: running — есть lock-файл; failed — последний прогон с ошибкой;
    aborted — лог не дописан, а lock-файла нет (прогон оборвали);
    ok; none — логов нет вовсе.
    """
    summary = sync_log.get_summary()
    lock = sc.nmap_pid_file_present()
    if not summary.get("available"):
        return {"available": False, "state": "running" if lock else "none",
                "steps": _rows(None, {}, False), "schedule": settings.SYNC_SCHEDULE,
                "next_run": human_when(next_run()), "running": lock}

    overall = summary["overall"]
    running = lock and overall in ("RUNNING", "UNKNOWN")
    if running:
        state = "running"
    elif overall in ("RUNNING", "UNKNOWN"):
        state = "aborted"
    elif overall == "FAILED":
        state = "failed"
    else:
        state = "ok"

    started = parse_log_time(summary["started_at"])
    finished = parse_log_time(summary.get("finished_at"))
    averages = _averages(sync_log.previous_runs())
    rows = _rows(summary, averages, running, started)

    done = [r for r in rows if r["status"] in ("ok", "fail", "timeout")]
    failed = [r for r in done if r["status"] != "ok"]
    total_elapsed = sum(r["elapsed"] or 0 for r in done)

    result = {
        "available": True,
        "state": state,
        "running": running,
        "log_file": summary["log_file"],
        "env": summary["env"],
        "sync_version": summary["sync_version"],
        "started": human_when(started),
        "started_dt": started,
        "finished": human_when(finished),
        "duration": duration((finished - started).total_seconds()) if started and finished
                    else duration(total_elapsed) if done else "",
        "done_count": len(done),
        "ok_count": len(done) - len(failed),
        "total_count": len(rows),
        "failed": [{"title": r["title"], "name": r["name"], "label": r["status_label"]} for r in failed],
        "steps": rows,
        "schedule": settings.SYNC_SCHEDULE,
        "next_run": human_when(next_run()),
    }
    if running:
        result["progress"] = _progress(rows, averages, started)
        result["journal"] = journal()
    return result


def _rows(summary, averages, running, started=None):
    by_name = {s["name"]: s for s in (summary or {}).get("steps", [])}
    running_step = (summary or {}).get("running_step") if running else None
    rows, offset, after_running = [], 0, False
    for index, meta in enumerate(sync_log.STEPS, start=1):
        step = by_name.get(meta["name"], {})
        status = _LOG_STATUS.get(step.get("status"))
        elapsed = step.get("elapsed")
        when = ""
        if status:
            offset += elapsed or 0
            if started:
                when = human_when(started + timedelta(seconds=offset))
        elif running and meta["name"] == running_step:
            status = "run"
            after_running = True
            if started:
                elapsed = max(0, int((now() - started).total_seconds()) - offset)
        elif running and after_running:
            status = "wait"
        else:
            status = "none"
        rows.append({
            "index": index, "name": meta["name"], "script": meta["script"],
            "title": meta["title"], "desc": meta["desc"],
            "schedule_only": bool(meta.get("schedule_only")),
            "status": status, "status_label": STATUS_LABELS[status],
            "elapsed": elapsed, "duration": duration(elapsed),
            "when": when,
            "avg": averages.get(meta["name"]),
        })
    return rows


def _progress(rows, averages, started):
    current = next((r for r in rows if r["status"] == "run"), None)
    position = current["index"] if current else sum(1 for r in rows if r["status"] != "wait")
    done_time = sum(r["elapsed"] or 0 for r in rows if r["status"] in ("ok", "fail", "timeout"))
    run_time = (current["elapsed"] or 0) if current else 0
    remaining = None
    if averages:
        remaining = sum(averages.get(r["name"], 0) for r in rows if r["status"] == "wait")
        if current:
            remaining += max(0, averages.get(current["name"], 0) - run_time)
    total = done_time + run_time + (remaining or 0)
    if remaining is not None and total:
        pct = round(100 * (done_time + run_time) / total)
    else:
        pct = round(100 * (position - 1) / len(rows))
    return {
        "position": position,
        "total": len(rows),
        "step_name": current["name"] if current else "",
        "step_title": current["title"] if current else "",
        "elapsed": duration(int((now() - started).total_seconds())) if started else "",
        "remaining": f"~{max(1, round(remaining / 60))} мин" if remaining else "",
        "pct": max(2, min(99, pct)),
    }


def step_info(name):
    """Итог одного шага последнего прогона — для «Гипервизоров»
    (последний сбор RedVirt / vCenter). Синк пишет итог на шаг целиком, не
    на источник, поэтому и показываем его на весь тип источников."""
    summary = sync_log.get_summary()
    if not summary.get("available"):
        return None
    started = parse_log_time(summary["started_at"])
    offset = 0
    for step in summary["steps"]:
        if step["elapsed"] is not None:
            offset += step["elapsed"]
        if step["name"] != name:
            continue
        status = _LOG_STATUS.get(step["status"])
        if not status:
            return {"status": "run" if summary["running_step"] == name else "none",
                    "status_label": "идёт" if summary["running_step"] == name else "не выполнялся",
                    "when": "", "duration": ""}
        return {"status": status, "status_label": STATUS_LABELS[status],
                "when": human_when(started + timedelta(seconds=offset)) if started else "",
                "duration": duration(step["elapsed"])}
    return None


def devices_step_started():
    """Когда в последний раз начинался шаг Devices — граница для баннера
    «Изменено N устройств»: правки позже неё попадут в NetBox только на
    следующем прогоне. None — логов нет."""
    candidates = []
    summary = sync_log.get_summary()
    if summary.get("available"):
        candidates.append(summary)
    candidates.extend(sync_log.previous_runs(limit=1))
    for run in candidates:
        started = parse_log_time(run["started_at"])
        if not started:
            continue
        offset = 0
        for step in run["steps"]:
            if step["name"] == "Devices":
                begun = step["status"] is not None or run["running_step"] == "Devices"
                if begun:
                    return started + timedelta(seconds=offset)
                break
            offset += step["elapsed"] or 0
    return None
