"""Раздел «Задания»: расписания и запуски сборщика netbox-sync, история
заданий с выводом, баннер состояния сборщика (U11 плана
docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md, R14, KTD14,
KTD17).

Смотреть раздел может любой вошедший; запускать и править расписания —
только staff (группа netbox-admins, staff_required как в «Настройках»).
Раздела нет вовсе, пока сборщик не включён (SQLite или без профиля worker —
worker_health.enabled()): 404, пункта меню и баннера тоже нет.

ip_plan задания только ставит в таблицу sync_job; выполняет их сборщик.
Запуск и правка расписания пишутся в журнал настроек (SettingsAudit, scope
sync).
"""
import json
import re
from datetime import datetime

from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from .models import SyncJob, SyncSchedule
from .services import sync_jobs, sync_overview, worker_health
from .views_common import audit, error_modal, start_job, toast_redirect
from .views_settings import staff_required

HISTORY_PAGE = 50
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_STEP_STATUS_LABELS = dict(SyncJob.STATUS_CHOICES, skipped="Пропущен")
_STEP_STATUS_CLASS = dict(sync_jobs.STATUS_CLASS, skipped="skip")


def _require_enabled():
    if not worker_health.enabled():
        raise Http404("Сборщик заданий не включён")


def _jobs_url():
    return reverse("ip_plan:jobs")


# ── Представление для шаблонов ─────────────────────────────────────────────

def _when(value):
    return sync_jobs.local_time(value)


def _job_row(job, titles=None):
    """titles — {вид: название расписания} для подписи без запроса на строку
    (см. sync_jobs.kind_label)."""
    return {
        "id": job.pk, "kind": job.kind, "label": sync_jobs.kind_label(job.kind, titles),
        "status": job.status, "status_label": job.get_status_display(),
        "status_class": sync_jobs.STATUS_CLASS.get(job.status, "none"),
        "source_label": job.get_source_display(), "requested_by": job.requested_by,
        "created": _when(job.created_at), "started": _when(job.started_at),
        "finished": _when(job.finished_at), "duration": sync_overview.job_duration(job),
        "summary": job.summary, "wait_reason": job.wait_reason,
        "steps_count": len(job.steps or []),
    }


def _schedule_row(schedule, last_jobs, active, titles):
    last = last_jobs.get(schedule.pk)
    return {
        "id": schedule.pk, "kind": schedule.kind, "title": schedule.title or schedule.kind,
        "single": sync_jobs.is_single_step(schedule), "enabled": schedule.enabled,
        "trigger": sync_jobs.describe_trigger(schedule),
        "next_run": _when(schedule.next_run) if schedule.enabled else "",
        "steps": [sync_jobs.step_label(s) for s in schedule.steps or []],
        "last": _job_row(last, titles) if last else None,
        "active_job": active.get(schedule.kind),
    }


def _step_rows(job):
    """Шаги задания по порядку со статусами из step_results; у идущего —
    текущий шаг (первый без итога) и причина ожидания."""
    results = {}
    for r in job.step_results or []:
        if isinstance(r, dict) and r.get("step") not in results:
            results[r.get("step")] = r
    rows, current_set = [], False
    for i, name in enumerate(job.steps or [], 1):
        r = results.get(name)
        row = {"index": i, "name": name, "label": sync_jobs.step_label(name),
               "status": "", "status_label": "", "status_class": "none", "duration": "",
               "exit_code": None, "errors": 0}
        if r:
            st = r.get("status") or ""
            row.update(status=st, status_label=_STEP_STATUS_LABELS.get(st, st),
                       status_class=_STEP_STATUS_CLASS.get(st, "none"),
                       duration=f"{r.get('duration') or 0} с", exit_code=r.get("exit_code"),
                       errors=r.get("errors") or 0)
        elif job.status == SyncJob.STATUS_RUNNING and not current_set:
            current_set = True
            if job.wait_reason:
                row.update(status="wait", status_label="ждёт", status_class="wait")
            else:
                row.update(status="running", status_label="выполняется", status_class="run")
        elif job.status == SyncJob.STATUS_QUEUED:
            row.update(status_label="в очереди", status_class="wait")
        else:
            row.update(status_label="не выполнялся", status_class="none")
        rows.append(row)
    return rows


def _job_ctx(job, user):
    head, tail = job.output_head or "", job.output_tail or ""
    truncated = "вывод обрезан" in head
    steps = _step_rows(job)
    current = next((r for r in steps if r["status"] in ("running", "wait")), None)
    return {
        "job": job, "row": _job_row(job), "steps": steps, "current": current,
        "active": job.status in SyncJob.ACTIVE_STATUSES,
        "output_head": head, "output_tail": tail, "truncated": truncated,
        "has_output": bool(head or tail), "log_path": job.log_path,
        "can_run": user.is_staff,
        # «Запустить ещё раз» — для задания расписания или одного шага; у
        # ручной цепочки из «Настроек» вида для повторного запуска нет.
        "can_rerun": bool(job.schedule_id) or job.kind in sync_jobs.STEPS,
    }


# ── Страницы ───────────────────────────────────────────────────────────────

def jobs(request):
    _require_enabled()
    status = request.GET.get("status") or ""
    valid = {k for k, _ in SyncJob.STATUS_CHOICES}
    qs = SyncJob.objects.all()
    if status in valid:
        qs = qs.filter(status=status)
    else:
        status = ""
    page = Paginator(qs.order_by("-created_at", "-id"), HISTORY_PAGE).get_page(request.GET.get("page"))

    schedules, last_jobs = sync_jobs.with_last_job(SyncSchedule.objects.all())
    # Все расписания уже загружены — подписи видов заданий берутся из них.
    titles = {s.kind: s.title for s in schedules}
    active = {j.kind: j.pk for j in SyncJob.objects.filter(status__in=SyncJob.ACTIVE_STATUSES)}

    health = worker_health.fetch()
    return render(request, "ip_plan/jobs.html", {
        "health": health, "health_down": "_down" in health, "health_reason": health.get("_down", ""),
        "banner": worker_health.banner(health, _jobs_url()),
        "schedules": [_schedule_row(s, last_jobs, active, titles) for s in schedules],
        "history": [_job_row(j, titles) for j in page.object_list], "page": page,
        "status": status, "statuses": SyncJob.STATUS_CHOICES,
        "steps": [{"name": s.name, "label": s.label, "desc": s.desc, "active_job": active.get(s.name)}
                  for s in sync_jobs.STEPS_LIST],
        "can_edit": request.user.is_staff, "tz": sync_jobs.tz().key,
        "edit_lock": sync_jobs.edit_lock(),
    })


def job_detail(request, job_id):
    _require_enabled()
    job = get_object_or_404(SyncJob, pk=job_id)
    return render(request, "ip_plan/job_detail.html", _job_ctx(job, request.user))


def job_body(request, job_id):
    """Тело страницы задания — его перерисовывает опрос htmx, пока задание
    в очереди или выполняется; в ответе по завершённому опроса уже нет."""
    _require_enabled()
    job = get_object_or_404(SyncJob, pk=job_id)
    return render(request, "ip_plan/_job_body.html", _job_ctx(job, request.user))


def health_json(request):
    """Баннер оболочки ($store.worker в app.js). Пропускается мимо
    принудительной смены пароля (middleware): ответ — JSON, а не редирект на
    HTML-страницу, которую fetch не разобрал бы."""
    return JsonResponse(worker_health.banner(jobs_url=_jobs_url() if worker_health.enabled() else ""))


# ── Запуск ─────────────────────────────────────────────────────────────────

@staff_required
def job_run(request):
    """POST step=<имя шага> — один шаг белого списка; schedule=<id> — шаги
    расписания (снимок на момент запуска)."""
    _require_enabled()
    if request.method != "POST":
        return HttpResponse(status=405)
    if request.POST.get("schedule"):
        schedule = get_object_or_404(SyncSchedule, pk=request.POST["schedule"])
        if not sync_jobs.steps_allowed(schedule.steps):
            return error_modal(request, "В расписании шаги вне белого списка сборщика — запуск невозможен.")
        return start_job(request, schedule.kind, list(schedule.steps), schedule=schedule)
    step = sync_jobs.resolve_step(request.POST.get("step") or "")
    if step is None:
        return error_modal(request, "Такого шага нет в белом списке сборщика.")
    return start_job(request, step, [step])


# ── Расписания ─────────────────────────────────────────────────────────────

def _parse_time(value):
    try:
        return datetime.strptime((value or "").strip(), "%H:%M").time()
    except ValueError:
        return None


def _form_ctx(schedule, values=None, errors=None):
    if values is None:
        single = bool(schedule and sync_jobs.is_single_step(schedule))
        values = {
            "title": schedule.title if schedule else "",
            "kind": schedule.kind if schedule else "",
            "mode": "step" if single else "chain",
            "step": schedule.kind if single else "",
            "steps": list(schedule.steps) if schedule and not single else [],
            "trigger": schedule.trigger if schedule else SyncSchedule.TRIGGER_DAILY,
            "interval_minutes": schedule.interval_minutes if schedule and schedule.interval_minutes else "",
            "daily_at": f"{schedule.daily_at:%H:%M}" if schedule and schedule.daily_at else "",
            "fallback_at": f"{schedule.fallback_at:%H:%M}" if schedule and schedule.fallback_at else "",
            "enabled": schedule.enabled if schedule else True,
        }
    return {
        "schedule": schedule, "values": values, "errors": errors or {},
        "steps_json": json.dumps([{"name": s.name, "label": s.label} for s in sync_jobs.STEPS_LIST],
                                 ensure_ascii=False),
        "picked_json": json.dumps(values.get("steps") or []),
        "all_steps": sync_jobs.STEPS_LIST, "tz": sync_jobs.tz().key,
        "triggers": SyncSchedule.TRIGGER_CHOICES,
    }


def _validate(post, schedule):
    """(values, errors, cleaned). Ошибки — по полям, рядом с ними в форме."""
    values = {
        "title": (post.get("title") or "").strip(),
        "kind": schedule.kind if schedule else (post.get("kind") or "").strip(),
        "mode": post.get("mode") or "chain",
        "step": (post.get("step") or "").strip(),
        "steps": [s for s in post.getlist("steps") if s],
        "trigger": post.get("trigger") or "",
        "interval_minutes": (post.get("interval_minutes") or "").strip(),
        "daily_at": (post.get("daily_at") or "").strip(),
        "fallback_at": (post.get("fallback_at") or "").strip(),
        "enabled": bool(post.get("enabled")),
    }
    errors, cleaned = {}, {}
    if schedule is not None:
        # Вид и режим не меняются: по виду ищутся задания расписания.
        single = sync_jobs.is_single_step(schedule)
        values["mode"] = "step" if single else "chain"
        if single:
            values["step"] = schedule.kind
    if values["mode"] == "step":
        if values["step"] not in sync_jobs.STEPS:
            errors["step"] = "Выберите шаг из списка."
        else:
            cleaned["kind"], cleaned["steps"] = values["step"], [values["step"]]
    else:
        kind, steps = values["kind"], values["steps"]
        if schedule is None:
            if not _KIND_RE.match(kind):
                errors["kind"] = "Латиница в нижнем регистре, цифры и «_», от 2 символов, начиная с буквы."
            elif kind in sync_jobs.STEPS:
                errors["kind"] = "Это имя шага — для расписания одного шага выберите режим «Один шаг»."
        if not steps:
            errors["steps"] = "Цепочка не может быть пустой."
        elif not sync_jobs.steps_allowed(steps):
            errors["steps"] = "В цепочке шаг вне белого списка сборщика."
        elif len(set(steps)) != len(steps):
            errors["steps"] = "Шаг повторяется в цепочке."
        cleaned["kind"], cleaned["steps"] = kind, steps
    if schedule is None and "kind" not in errors and cleaned.get("kind") and \
            SyncSchedule.objects.filter(kind=cleaned["kind"]).exists():
        errors["step" if values["mode"] == "step" else "kind"] = "Расписание с таким видом уже есть."

    trigger = values["trigger"]
    cleaned.update(trigger=trigger, interval_minutes=None, daily_at=None, fallback_at=None)
    if trigger == SyncSchedule.TRIGGER_INTERVAL:
        try:
            minutes = int(values["interval_minutes"])
        except ValueError:
            minutes = 0
        if minutes < 1:
            errors["interval_minutes"] = "Интервал — целое число минут, не меньше 1."
        cleaned["interval_minutes"] = minutes
    elif trigger == SyncSchedule.TRIGGER_DAILY:
        cleaned["daily_at"] = _parse_time(values["daily_at"])
        if cleaned["daily_at"] is None:
            errors["daily_at"] = "Время в формате ЧЧ:ММ."
    elif trigger == SyncSchedule.TRIGGER_AFTER_NMAP:
        cleaned["fallback_at"] = _parse_time(values["fallback_at"])
        if cleaned["fallback_at"] is None:
            errors["fallback_at"] = "Запасное время в формате ЧЧ:ММ."
    else:
        errors["trigger"] = "Выберите, когда запускать."
    cleaned["title"] = values["title"] or cleaned.get("kind", "")
    cleaned["enabled"] = values["enabled"]
    return values, errors, cleaned


def _describe(schedule):
    return (f"{schedule.title or schedule.kind}: {sync_jobs.describe_trigger(schedule)}; "
            f"{'включено' if schedule.enabled else 'выключено'}; шаги: "
            + ", ".join(sync_jobs.step_label(s) for s in schedule.steps))


@staff_required
def schedule_form(request, schedule_id=None):
    _require_enabled()
    schedule = get_object_or_404(SyncSchedule, pk=schedule_id) if schedule_id else None
    return render(request, "ip_plan/_jobs_schedule_form.html", _form_ctx(schedule))


@staff_required
def schedule_save(request, schedule_id=None):
    _require_enabled()
    if request.method != "POST":
        return HttpResponse(status=405)
    schedule = get_object_or_404(SyncSchedule, pk=schedule_id) if schedule_id else None
    values, errors, cleaned = _validate(request.POST, schedule)
    if errors:
        return render(request, "ip_plan/_jobs_schedule_form.html",
                      _form_ctx(schedule, values, errors), status=400)
    before = _describe(schedule) if schedule else ""
    with transaction.atomic():
        obj = schedule or SyncSchedule(kind=cleaned["kind"])
        obj.title = cleaned["title"]
        obj.steps = cleaned["steps"]
        obj.trigger = cleaned["trigger"]
        obj.interval_minutes = cleaned["interval_minutes"]
        obj.daily_at = cleaned["daily_at"]
        obj.fallback_at = cleaned["fallback_at"]
        obj.enabled = cleaned["enabled"]
        # Следующий такт считает ip_plan той же формулой, что сборщик, — его
        # видно сразу после сохранения; выключенное расписание тактов не имеет.
        obj.next_run = sync_jobs.next_run_after(obj) if obj.enabled else None
        obj.updated_by = request.user.username
        obj.save()
    after = _describe(obj)
    audit(request, "sync", f"расписание {obj.kind}", "update" if schedule else "create",
           f"{before} → {after}" if before else after)
    when = _when(obj.next_run)
    message = (f"Расписание сохранено. Следующий запуск: {when} ({sync_jobs.tz().key})" if when
               else "Расписание сохранено и выключено.")
    return toast_redirect(_jobs_url(), message)


@staff_required
def schedule_delete(request, schedule_id):
    _require_enabled()
    if request.method != "POST":
        return HttpResponse(status=405)
    schedule = get_object_or_404(SyncSchedule, pk=schedule_id)
    details = _describe(schedule)
    kind = schedule.kind
    schedule.delete()
    audit(request, "sync", f"расписание {kind}", "delete", details)
    return toast_redirect(_jobs_url(), "Расписание удалено. История его заданий сохранена.")
