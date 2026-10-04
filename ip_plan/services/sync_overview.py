"""Данные экрана «Настройки → Запуск и расписание» (сводка «Синк», KTD19).

С этапа 2 (04.10.2026) ночные шаги — задания сборщика netbox-sync, а
full_sync.sh на хосте выполняет только nmap. Поэтому сводка собирается из
двух мест:

* шаги белого списка сборщика (sync_jobs.STEPS_LIST) — по истории заданий
  sync_job: у каждого шага — итог последнего задания, в котором он
  выполнялся (step_results), идущее задание — текущий шаг и очередь;
* nmap — по логу full_sync.sh (sync_log) и его PID-файлу
  (sync_config.nmap_pid_file_present).

Цепочки ночи («До nmap», «После nmap» — расписания миграции 0013) показаны
карточками с последним заданием. История пуста — «ещё не запускалось».

Время в логе — местное время хоста синка без зоны (settings.SYNC_LOG_TZ).
"""
import re
from datetime import datetime, timedelta

from django.conf import settings
from django.utils.dateparse import parse_datetime

from ..models import SyncJob, SyncSchedule
from . import sync_config as sc
from . import sync_jobs, sync_log, worker_health

# Цепочки ночи по умолчанию (миграция 0013) — в порядке ночи.
NIGHT_CHAINS = ("night_before_nmap", "night_after_nmap")
# После какого шага сборщика в ночи идёт nmap: строки экрана — в порядке ночи.
_NMAP_AFTER_STEP = "netbox_network_setup"
# Сколько последних заданий смотреть, собирая итог по шагам: ночь — два
# задания, SNMP этапа 2 — до 144 в сутки; шаг, не выполнявшийся за это
# окно, показывается «ещё не запускалось».
_HISTORY_JOBS = 300

NEVER = "ещё не запускалось"
STATUS_LABELS = {
    "ok": "OK", "warn": "С ошибками", "fail": "Ошибка", "timeout": "Таймаут", "run": "выполняется",
    "wait": "в очереди", "none": NEVER,
}
_LOG_STATUS = {"OK": "ok", "FAILED": "fail", "TIMEOUT": "timeout"}
# Итог шага/задания сборщика → класс строки — sync_jobs.STATUS_CLASS.
_JOB_LABELS = dict(SyncJob.STATUS_CHOICES)
_BAD = ("fail", "timeout", "warn")


def now():
    return datetime.now(sync_jobs.tz())


def parse_log_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=sync_jobs.tz())
    except ValueError:
        return None


def human_when(dt):
    """«сегодня, 03:00» / «вчера, 17:40» / «завтра, 03:00» / «28.09, 11:30» /
    «03.02.2025, 11:30». Принимает aware datetime (любой зоны)."""
    if dt is None:
        return ""
    local = dt.astimezone(sync_jobs.tz())
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


def job_duration(job):
    """Длительность задания: завершённого — до конца, идущего — до сейчас."""
    end = job.finished_at or (now() if job.status == SyncJob.STATUS_RUNNING else None)
    if not job.started_at or not end:
        return ""
    return duration((end - job.started_at).total_seconds())


_CRON_DAILY_RE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$")


def next_run(schedule=None):
    """Следующий запуск по расписанию вида «M H * * *» или None (cron nmap)."""
    m = _CRON_DAILY_RE.match(schedule if schedule is not None else settings.SYNC_SCHEDULE)
    if not m:
        return None
    minute, hour = int(m.group(1)), int(m.group(2))
    current = now()
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


# ── журнал: хвост лога nmap или вывода задания ─────────────────────────────

_JOB_STEP_HEADER = re.compile(r"^=== \[[^\]]+\] ")


def _line_kind(line):
    stripped = line.strip()
    if _JOB_STEP_HEADER.match(stripped):
        return "start"
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


def _journal_lines(lines):
    """Строки с видом для раскраски (ok/fail/start/meta/out). Пустые и
    разделительные строки схлопываются — в журнале они только занимают
    высоту."""
    result = []
    for line in lines:
        kind = _line_kind(line)
        if kind == "meta" and (not line.strip() or line.strip()[:4] in ("====", "----")):
            continue
        result.append({"text": line.rstrip(), "kind": kind})
    return result


def journal(lines=80):
    """Хвост текущего лога nmap."""
    return _journal_lines(sync_log.get_tail(lines))


def _job_journal(job, lines=80):
    return _journal_lines((job.output_tail or job.output_head or "").splitlines()[-lines:])


# ── итог шагов по заданиям ─────────────────────────────────────────────────

def _result_time(result):
    value = result.get("started_at") if isinstance(result, dict) else None
    return parse_datetime(value) if isinstance(value, str) else None


def _results(job):
    return [r for r in (job.step_results or []) if isinstance(r, dict) and r.get("step")]


def _current_index(job):
    """Номер текущего шага идущего задания (0-based) — первый без итога."""
    return len(_results(job))


def _current_started(job):
    """Когда начался текущий шаг идущего задания: конец предыдущего или
    начало задания."""
    results = _results(job)
    if results:
        last = results[-1]
        begun = _result_time(last)
        if begun is not None:
            return begun + timedelta(seconds=float(last.get("duration") or 0))
    return job.started_at


def _step_state(result, job):
    status = result.get("status") or ""
    cls = sync_jobs.STATUS_CLASS.get(status, "none")
    label = _JOB_LABELS.get(status) or STATUS_LABELS.get(cls, status)
    if cls == "ok":
        label = STATUS_LABELS["ok"]
    seconds = result.get("duration")
    begun = _result_time(result)
    return {"status": cls, "status_label": label,
            "elapsed": int(seconds) if seconds is not None else None,
            "duration": duration(seconds) if seconds is not None else "",
            "when": human_when(begun), "started": begun, "job_id": job.pk}


def _active_jobs():
    return list(SyncJob.objects.filter(status__in=SyncJob.ACTIVE_STATUSES)
                .order_by("started_at", "created_at", "id"))


def _step_states(active):
    """{шаг: состояние} — идущие задания поверх истории: у идущего —
    выполненные шаги, текущий («run» или «wait», если задание ждёт
    блокировку) и очередь; у остальных шагов — итог последнего задания, где
    шаг действительно выполнялся (skipped не считается)."""
    states = {}
    for job in active:
        results = _results(job)
        for r in results:
            if r["step"] not in states and r.get("status") != "skipped":
                states[r["step"]] = _step_state(r, job)
        current = len(results)
        for i, name in enumerate(job.steps or []):
            if i < current or name in states:
                continue
            if job.status == SyncJob.STATUS_RUNNING and i == current and not job.wait_reason:
                states[name] = {"status": "run", "status_label": STATUS_LABELS["run"],
                                "elapsed": None, "duration": "", "when": "",
                                "started": _current_started(job), "job_id": job.pk}
            else:
                states[name] = {"status": "wait", "status_label": STATUS_LABELS["wait"],
                                "elapsed": None, "duration": "", "when": "", "started": None,
                                "job_id": job.pk}
    history = (SyncJob.objects.exclude(status__in=SyncJob.ACTIVE_STATUSES)
               .order_by("-created_at", "-id").only("id", "step_results")[:_HISTORY_JOBS])
    best = {}
    for job in history:
        for r in _results(job):
            if r.get("status") == "skipped" or r["step"] in states:
                continue
            begun = _result_time(r)
            prev = best.get(r["step"])
            # Последний по времени начала шага, а не по порядку заданий:
            # ручной запуск одного шага может закончиться раньше ночного.
            if prev is None or (begun and (prev[0] is None or begun > prev[0])):
                best[r["step"]] = (begun, r, job)
    for name, (_, r, job) in best.items():
        states[name] = _step_state(r, job)
    return states


def _never(extra=None):
    row = {"status": "none", "status_label": NEVER, "elapsed": None, "duration": "", "when": "",
           "started": None, "job_id": None}
    row.update(extra or {})
    return row


# ── nmap по логу ───────────────────────────────────────────────────────────

def _nmap(summary, lock):
    """Состояние nmap: строка шага и карточка."""
    base = {"schedule": settings.SYNC_SCHEDULE, "next_run": human_when(next_run()),
            "running": False, "aborted": False, "env": None, "sync_version": None, "log_file": None}
    if not summary.get("available"):
        state = _never()
        if lock:
            state.update(status="run", status_label=STATUS_LABELS["run"])
            base["running"] = True
        return {**base, **state}
    step = next((s for s in summary["steps"] if s["name"] == sync_log.NMAP_STEP["name"]), {})
    started = parse_log_time(summary["started_at"])
    finished = parse_log_time(summary.get("finished_at"))
    status = _LOG_STATUS.get(step.get("status"))
    unfinished = summary["overall"] in ("RUNNING", "UNKNOWN")
    running = lock and unfinished
    if running:
        state = {"status": "run", "status_label": STATUS_LABELS["run"],
                 "elapsed": int((now() - started).total_seconds()) if started else None}
    elif status:
        state = {"status": status, "status_label": STATUS_LABELS[status], "elapsed": step.get("elapsed")}
    elif unfinished:
        state = {"status": "fail", "status_label": "прерван", "elapsed": None}
    else:
        state = {"status": "none", "status_label": NEVER, "elapsed": None}
    state["duration"] = duration(state["elapsed"])
    if not state["duration"] and finished and started and not running:
        state["duration"] = duration((finished - started).total_seconds())
    return {**base, **state, "when": human_when(started), "started": started, "job_id": None,
            "running": running, "aborted": unfinished and not lock,
            "env": summary["env"], "sync_version": summary["sync_version"],
            "log_file": summary.get("log_file")}


# ── экран ──────────────────────────────────────────────────────────────────

def _rows(states, nmap):
    rows = []
    for step in sync_jobs.STEPS_LIST:
        rows.append({"name": step.name, "script": step.script, "title": step.label,
                     "desc": step.desc, "schedule_only": False,
                     **states.get(step.name, _never())})
        if step.name == _NMAP_AFTER_STEP:
            meta = sync_log.NMAP_STEP
            rows.append({"name": meta["name"], "script": meta["script"], "title": meta["title"],
                         "desc": meta["desc"], "schedule_only": True,
                         **{k: nmap[k] for k in ("status", "status_label", "elapsed", "duration",
                                                 "when", "started", "job_id")}})
    for i, row in enumerate(rows, start=1):
        row["index"] = i
    return rows


def _job_card(job):
    if job is None:
        return None
    cls = sync_jobs.STATUS_CLASS.get(job.status, "none")
    return {"id": job.pk, "status": cls, "status_label": job.get_status_display(),
            "when": human_when(job.started_at or job.created_at),
            "duration": job_duration(job),
            "summary": job.summary, "code_version": job.code_version}


def _chains():
    found, last_jobs = sync_jobs.with_last_job(SyncSchedule.objects.filter(kind__in=NIGHT_CHAINS))
    schedules = {s.kind: s for s in found}
    result = []
    for kind in NIGHT_CHAINS:
        s = schedules.get(kind)
        if s is None:
            continue
        last = last_jobs.get(s.pk)
        result.append({"kind": kind, "title": s.title or kind, "enabled": s.enabled,
                       "trigger": sync_jobs.describe_trigger(s),
                       "next_run": human_when(s.next_run) if s.enabled and s.next_run else "",
                       "last": _job_card(last)})
    return result


def _cards(chains, nmap):
    """Карточки над шагами в порядке ночи: «До nmap», nmap, «После nmap»
    (нет расписания — нет и карточки). Одна форма у всех трёх."""
    cards = []
    for chain in chains:
        last = chain["last"]
        title = chain["title"] + ("" if chain["enabled"] else " · выключено")
        if last:
            cards.append({"title": title, "status": last["status"], "label": last["status_label"],
                          "job_id": last["id"], "hint": last["summary"] or chain["trigger"],
                          "note": " · ".join(x for x in (last["when"], last["duration"]) if x)})
        else:
            cards.append({"title": title, "status": "none", "label": NEVER, "job_id": None,
                          "hint": chain["trigger"],
                          "note": " · ".join(x for x in (chain["trigger"], chain["next_run"]) if x)})
    nmap_card = {"title": "Сканирование сетей (nmap)", "status": nmap["status"],
                 "label": nmap["status_label"], "job_id": None,
                 "hint": f"cron на хосте · {nmap['schedule']}" if nmap["schedule"] else "cron на хосте",
                 "note": " · ".join(x for x in (nmap["when"], nmap["duration"]) if x)
                         or (f"следующий — {nmap['next_run']}" if nmap["next_run"] else "cron на хосте")}
    pos = 1 if chains and chains[0]["kind"] == NIGHT_CHAINS[0] else 0
    cards.insert(pos, nmap_card)
    return cards


def _progress_job(job, rows, states):
    steps = job.steps or []
    position = min(_current_index(job) + 1, len(steps)) if steps else 0
    current = steps[position - 1] if steps else ""
    started = _current_started(job)
    # Оценка «осталось» — по последним длительностям тех же шагов.
    remaining = 0
    for name in steps[position:]:
        remaining += (states.get(name) or {}).get("elapsed") or 0
    return {
        "job_id": job.pk, "kind": job.kind, "label": sync_jobs.kind_label(job.kind),
        "position": position, "total": len(steps),
        "step_name": current, "step_title": sync_jobs.step_label(current) if current else "",
        "wait_reason": job.wait_reason,
        "elapsed": duration(int((now() - job.started_at).total_seconds())) if job.started_at else "",
        "step_elapsed": duration(int((now() - started).total_seconds())) if started else "",
        "remaining": f"~{max(1, round(remaining / 60))} мин" if remaining else "",
        "pct": max(2, min(99, round(100 * (position - 1) / len(steps)))) if steps else 2,
    }


def _progress_nmap(nmap):
    return {"job_id": None, "kind": "nmap", "label": "Сканирование сетей (nmap)",
            "position": 1, "total": 1, "step_name": sync_log.NMAP_STEP["name"],
            "step_title": sync_log.NMAP_STEP["title"], "wait_reason": "",
            "elapsed": nmap["duration"], "step_elapsed": nmap["duration"], "remaining": "", "pct": 50}


def _collect():
    """Идущие задания, итог по шагам, nmap и строки экрана — общее для
    overview() и state()."""
    active = _active_jobs()
    states = _step_states(active)
    nmap = _nmap(sync_log.get_summary(), sc.nmap_pid_file_present())
    return active, states, nmap, _rows(states, nmap)


def _state(active, rows, nmap):
    """(state, идущее задание, строки с ошибкой, available) — см. overview()."""
    running_job = next((j for j in active if j.status == SyncJob.STATUS_RUNNING), None)
    running = running_job is not None or nmap["running"]
    # Оборванный nmap — отдельное состояние «aborted», не «ошибка шага».
    failed = [r for r in rows if r["status"] in _BAD
              and not (r["schedule_only"] and nmap["aborted"])]
    available = any(r["status"] != "none" for r in rows) or nmap.get("log_file") is not None
    if running:
        state = "running"
    elif not available:
        state = "none"
    elif failed:
        state = "failed"
    elif nmap["aborted"]:
        state = "aborted"
    else:
        state = "ok"
    return state, running_job, failed, available


def state():
    """Только state из overview() — для точки в рейке «Настроек» на каждой
    странице раздела: без цепочек ночи, карточек, прогресса и хвоста лога."""
    active, _, nmap, rows = _collect()
    return _state(active, rows, nmap)[0]


def overview():
    """Всё для экрана синхронизации и точки в рейке.

    state: running — идёт задание или nmap; failed — у какого-то шага
    последний итог с ошибкой; aborted — лог nmap не дописан, а PID-файла нет
    (скан оборвали); ok; none — ни заданий, ни логов nmap.
    """
    active, states, nmap, rows = _collect()
    chains = _chains()
    state, running_job, failed, available = _state(active, rows, nmap)
    running = state == "running"

    last_versions = [c["last"]["code_version"] for c in chains if c["last"] and c["last"]["code_version"]]
    result = {
        "available": available,
        "state": state,
        # Ссылки на задания и «Задания» — только когда сборщик включён
        # (иначе страница заданий отвечает 404).
        "worker_enabled": worker_health.enabled(),
        "running": running,
        "queued": [j.pk for j in active if j.status == SyncJob.STATUS_QUEUED],
        "steps": rows,
        "chains": chains,
        "cards": _cards(chains, nmap),
        "nmap": nmap,
        "failed": [{"title": r["title"], "name": r["name"], "label": r["status_label"]} for r in failed],
        "done_count": sum(1 for r in rows if r["status"] in ("ok", *_BAD)),
        "ok_count": sum(1 for r in rows if r["status"] == "ok"),
        "total_count": len(rows),
        "sync_version": last_versions[-1] if last_versions else nmap["sync_version"],
        "env": nmap["env"],
        # Совместимость с прежним контекстом: cron nmap.
        "schedule": settings.SYNC_SCHEDULE,
        "next_run": nmap["next_run"],
    }
    if running_job is not None:
        result["progress"] = _progress_job(running_job, rows, states)
        result["journal"] = _job_journal(running_job)
    elif nmap["running"]:
        result["progress"] = _progress_nmap(nmap)
        result["journal"] = journal()
    return result


def step_info(name):
    """Итог одного шага по заданиям — для «Гипервизоров» (последний сбор
    RedVirt / vCenter). name — имя шага сборщика, подпись сводки («RedVirt»)
    или скрипт. None — такого шага у сборщика нет или заданий не было ни
    одного."""
    return steps_info([name])[name]


def steps_info(names):
    """{name: step_info(name)} — итог по шагам считается один раз на все."""
    resolved = {name: sync_jobs.resolve_step(name) for name in names}
    if all(step is None for step in resolved.values()) or not SyncJob.objects.exists():
        return dict.fromkeys(resolved)
    states = _step_states(_active_jobs())
    return {name: None if step is None else _info(states.get(step)) for name, step in resolved.items()}


def _info(state):
    if state is None:
        return {"status": "none", "status_label": NEVER, "when": "", "duration": ""}
    if state["status"] == "run":
        return {"status": "run", "status_label": "идёт", "when": "", "duration": ""}
    return {k: state[k] for k in ("status", "status_label", "when", "duration")}


def devices_step_started():
    """Когда в последний раз начинался шаг устройств (netbox_devices_sync) —
    граница для баннера «Изменено N устройств»: правки позже неё попадут в
    NetBox только на следующем запуске. None — шаг ещё не запускался."""
    step = "netbox_devices_sync"
    for job in _active_jobs():
        if job.status != SyncJob.STATUS_RUNNING or step not in (job.steps or []):
            continue
        for r in _results(job):
            if r["step"] == step:
                return _result_time(r)
        if (job.steps or [])[_current_index(job):_current_index(job) + 1] == [step]:
            return _current_started(job)
    state = _step_states([]).get(step)
    return state["started"] if state else None
