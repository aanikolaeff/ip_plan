"""Состояние сборщика netbox-sync для баннера и раздела «Задания» (U11 плана
docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md, KTD17).

Сборщик отдаёт /health по HTTP только внутри compose (http://worker:8090/health
по умолчанию): JSON с кэшем состояния его цикла — db up/down, netobs,
миграции, текущие задания, предупреждения о пакетах, nmap_missed. ip_plan
ходит к нему со стороны сервера (браузер этот адрес не видит), с коротким
таймаутом и кэшем на воркер gunicorn (CACHES не настроен → LocMemCache,
расхождение между воркерами — не дольше CACHE_SECONDS).

Три состояния, которые нельзя путать:
* «не включён» — SQLite или адрес /health не задан (без профиля worker):
  ни баннера, ни раздела «Задания»;
* «не отвечает» — включён, но /health не ответил: баннер-ошибка;
* ответил — баннер по таблице состояний плана (норма — без баннера).
"""
import json
import urllib.request

from django.conf import settings
from django.core.cache import cache

from ..models import SyncJob, SyncSchedule
from . import sync_jobs

_CACHE_KEY = "worker_health_raw"


def enabled():
    """Включён ли сборщик на этом стенде: база ip_plan в PostgreSQL (KTD1) и
    задан адрес /health (по умолчанию — когда в COMPOSE_PROFILES есть worker,
    см. config/settings.py)."""
    return _is_postgres() and bool(settings.SYNC_WORKER_HEALTH_URL)


def _is_postgres():
    return settings.DATABASES["default"]["ENGINE"].endswith("postgresql")


def fetch(use_cache=True):
    """JSON /health или {"_down": "причина"}; None — сборщик не включён."""
    if not enabled():
        return None
    if use_cache:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached
    try:
        with urllib.request.urlopen(settings.SYNC_WORKER_HEALTH_URL,
                                    timeout=settings.SYNC_WORKER_HEALTH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ответ /health — не объект JSON")
    except (OSError, ValueError) as e:  # URLError и HTTPError — подклассы OSError
        data = {"_down": str(getattr(e, "reason", e)) or e.__class__.__name__}
    cache.set(_CACHE_KEY, data, settings.SYNC_WORKER_HEALTH_CACHE_SECONDS)
    return data


def _failed_schedule_job():
    """Последнее завершённое плановое задание каждого включённого расписания;
    первое из них со статусом failed/timeout/interrupted — или None."""
    bad = (SyncJob.STATUS_FAILED, SyncJob.STATUS_TIMEOUT, SyncJob.STATUS_INTERRUPTED)
    finished = (SyncJob.objects.filter(source=SyncJob.SOURCE_SCHEDULE)
                .exclude(status__in=SyncJob.ACTIVE_STATUSES))
    schedules, last_jobs = sync_jobs.with_last_job(SyncSchedule.objects.filter(enabled=True), finished)
    for schedule in schedules:
        last = last_jobs.get(schedule.pk)
        if last is not None and last.status in bad:
            return schedule, last
    return None


def _running(health):
    """Идущие задания для индикатора в шапке: из базы (у них есть подписи)."""
    jobs = []
    current = {j.get("id"): j for j in (health or {}).get("jobs") or [] if isinstance(j, dict)}
    for job in SyncJob.objects.filter(status=SyncJob.STATUS_RUNNING).order_by("started_at", "id")[:5]:
        step = (current.get(job.pk) or {}).get("step") or ""
        jobs.append({"id": job.pk, "kind": job.kind, "label": sync_jobs.kind_label(job.kind),
                     "step": sync_jobs.step_label(step) if step else ""})
    return jobs


def banner(health=None, jobs_url=""):
    """Что показать в шапке: {"enabled", "level", "text", "link", "link_text",
    "running"}.

    level: error | warn | "" (норма — баннера нет, только индикатор идущих
    заданий). Порядок — от самого важного: сборщик молчит, без базы, затем
    итог расписаний, nmap, миграции, пакеты."""
    if not enabled():
        return {"enabled": False, "level": "", "text": "", "link": "", "link_text": "", "running": []}
    health = fetch() if health is None else health
    out = {"enabled": True, "level": "", "text": "", "link": "", "link_text": "Задания", "running": []}
    if "_down" in health:
        out.update(level="error", text="Сборщик не отвечает", link=jobs_url)
        return out
    out["running"] = _running(health)
    if health.get("db") == "down":
        out.update(level="error", text="Сборщик не достаёт базу", link=jobs_url)
        return out
    failed = _failed_schedule_job()
    if failed is not None:
        schedule, job = failed
        out.update(level="warn",
                   text=f"{schedule.title or schedule.kind}: {job.get_status_display().lower()}",
                   link=f"{jobs_url}{job.pk}/" if jobs_url else "", link_text="Открыть задание")
        return out
    if health.get("nmap_missed"):
        out.update(level="warn", text="Ночной nmap не прошёл", link=jobs_url)
        return out
    if health.get("migrations") == "error":
        out.update(level="warn", text="Сборщик не применил миграции netobs", link=jobs_url)
        return out
    if health.get("package_warnings"):
        out.update(level="warn", text="Сборщику нужна пересборка образа", link=jobs_url)
        return out
    return out
