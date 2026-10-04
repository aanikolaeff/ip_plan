"""Задания и расписания сборщика netbox-sync со стороны ip_plan (U11 плана
docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md, KTD14,
KTD15, KTD18).

ip_plan задания только ставит и показывает — выполняет их сборщик
netbox-sync (worker.py) в своём контейнере. Здесь:

* белый список шагов сборщика (STEPS) — копия netbox-sync/worker_steps.py;
* постановка «запустить сейчас» (enqueue) — один queued/running на вид;
* следующий такт расписания (next_run_after) — та же формула, что у сборщика;
* блокировка правок conf (edit_lock, KTD18).

Почему белый список продублирован, а не прочитан из netbox-sync: web не
монтирует корень netbox-sync (KTD2, только registry/ и каталоги вывода), и
ip_plan не исполняет код соседнего репозитория. Монтировать ещё один каталог
ради одного списка — лишняя связь деплоев, а расхождение ловит контрактный
тест test_jobs.WorkerStepsContractTests: он разбирает worker_steps.py через
ast (без импорта) и сравнивает имя, скрипт, группу, таймаут и подпись каждого
шага по порядку. Добавили шаг в сборщик — добавьте и сюда, иначе тест упадёт
там, где рядом лежит netbox-sync (на хосте разработчика; в контейнере без
соседнего каталога он пропускается).
"""
from collections import namedtuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, connections, transaction
from django.utils import timezone

from ..models import SyncJob, SyncSchedule

GROUP_NETBOX = "netbox"
GROUP_NETOBS = "netobs"

_MIN = 60

# name, script, group, timeout, title — ровно как в worker_steps.STEPS_LIST
# (порядок тоже: по нему строится упорядоченный выбор шагов цепочки и строки
# сводки «Синк»); label и desc — подписи для людей в ip_plan. Других копий
# списка шагов в ip_plan нет: сводка (sync_overview) берёт его отсюда, из
# лога full_sync.sh читается только nmap (sync_log.NMAP_STEP).
Step = namedtuple("Step", "name script group timeout title label desc")

STEPS_LIST = (
    Step("netbox_ensure_custom_fields", "netbox_ensure_custom_fields.py", GROUP_NETBOX,
         15 * _MIN, "Custom fields", "Служебные поля",
         "Проверяет и создаёт пользовательские поля NetBox"),
    Step("netbox_network_setup", "netbox_network_setup.py", GROUP_NETBOX,
         60 * _MIN, "VLAN+prefixes", "VLAN и подсети", "Подсети и VLAN с ядра сети"),
    Step("netbox_devices_sync", "netbox_devices_sync.py", GROUP_NETBOX,
         120 * _MIN, "Devices", "Устройства",
         "Интерфейсы, адреса, MAC- и ARP-таблицы, серийные номера"),
    Step("netbox_config_backup", "netbox_config_backup.py", GROUP_NETBOX,
         90 * _MIN, "Config backup", "Снимки конфигураций",
         "Конфигурации и политика доступа — из них строится раздел ACL"),
    Step("netbox_firewall_blocklist_sync", "netbox_firewall_blocklist_sync.py", GROUP_NETBOX,
         60 * _MIN, "Firewall blocklist", "Списки блокировок xFW",
         "Текущие списки на firewall — для сверки в «Блокировках»"),
    Step("netbox_redvirt_sync", "netbox_redvirt_sync.py", GROUP_NETBOX,
         90 * _MIN, "RedVirt", "Виртуальные машины RedVirt",
         "Кластеры, хосты, ВМ, их адреса и гостевые ОС"),
    Step("netbox_vcenter_sync", "netbox_vcenter_sync.py", GROUP_NETBOX,
         90 * _MIN, "vCenter", "Виртуальные машины vCenter",
         "Кластеры, хосты ESXi, ВМ, их адреса и гостевые ОС"),
    Step("netbox_lldp_topology", "netbox_lldp_topology.py", GROUP_NETBOX,
         120 * _MIN, "LLDP topology", "Связи между устройствами",
         "Кабели по LLDP, LACP, гипервизорам, MAC- и ARP-таблицам"),
    Step("netbox_reachability", "netbox_reachability.py", GROUP_NETBOX,
         60 * _MIN, "Reachability", "Доступность",
         "Какие адреса отвечают — для статусов в таблице адресов"),
    Step("netbox_dns_enrich", "netbox_dns_enrich.py", GROUP_NETBOX,
         90 * _MIN, "DNS enrich", "DNS-имена", "Прямые и обратные DNS-записи для адресов"),
    Step("netobs_retention", "netobs_retention.py", GROUP_NETBOX,
         60 * _MIN, "Netobs retention", "Ротация наблюдений",
         "Удаляет старую историю опросов и закрытые интервалы netobs"),
)
STEPS = {s.name: s for s in STEPS_LIST}
# Подпись шага сводки full_sync.sh («Devices») и имя скрипта → имя шага
# сборщика: кнопки раздела «Запуск и расписание» шлют имена из sync_log.
_BY_ALIAS = {**{s.title: s.name for s in STEPS_LIST},
             **{s.script: s.name for s in STEPS_LIST}}

# Общая блокировка синка KTD13 — копия netbox-sync/netobs.py SYNC_LOCK_KEY
# (сверяет тот же контрактный тест). Одноаргументная advisory-блокировка
# с bigint-ключом видна в pg_locks как classid = старшие 32 бита, objid =
# младшие, objsubid = 1.
SYNC_LOCK_KEY = 0x6E65746F6273796E  # "netobsyn"
NETOBS_ALIAS = "netobs"
# Недоступная netobs: не стучаться в неё на каждой странице (connect_timeout
# 3 с у алиаса) — результат «не знаем» помнится столько секунд.
_NETOBS_DOWN_TTL = 30
_NETOBS_DOWN_KEY = "sync_jobs_netobs_down"


def step_label(name):
    step = STEPS.get(name)
    return step.label if step else name


def resolve_step(alias):
    """Имя шага сборщика по имени шага, подписи сводки или скрипту; None —
    шага нет в белом списке (nmap, миграции netobs и прочее)."""
    if alias in STEPS:
        return alias
    return _BY_ALIAS.get(alias)


# Виды ручных заданий кнопок «Настройки → Запуск и расписание» (кроме одного
# шага — у него вид = имя шага).
KIND_ALL_STEPS = "manual_all"
KIND_SELECTED = "manual_selected"
_MANUAL_KINDS = {KIND_ALL_STEPS: "Все шаги (вручную)", KIND_SELECTED: "Выбранные шаги (вручную)"}


def kind_label(kind):
    """Подпись вида задания: название расписания, подпись шага или сам вид."""
    if kind in STEPS:
        return STEPS[kind].label
    if kind in _MANUAL_KINDS:
        return _MANUAL_KINDS[kind]
    title = SyncSchedule.objects.filter(kind=kind).values_list("title", flat=True).first()
    return title or kind


# ── «Запустить сейчас» ─────────────────────────────────────────────────────

def enqueue(kind, steps, user="", schedule=None):
    """Поставить задание; (задание, создано). Если задание того же вида уже
    в очереди или выполняется — возвращается оно, второе не создаётся
    (KTD14: частичный уникальный индекс sync_job_one_active_per_kind)."""
    bad = [s for s in steps if s not in STEPS]
    if not steps or bad:
        raise ValueError("шаги вне белого списка сборщика: " + ", ".join(map(str, bad or ["(пусто)"])))
    existing = _active_job(kind)
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            job = SyncJob.objects.create(kind=kind, steps=list(steps), schedule=schedule,
                                         source=SyncJob.SOURCE_USER, requested_by=user or "")
        return job, True
    except IntegrityError:
        # Гонка: между проверкой и вставкой задание этого вида поставил
        # другой запрос или сборщик по расписанию.
        existing = _active_job(kind)
        if existing is None:
            raise
        return existing, False


def _active_job(kind):
    return SyncJob.objects.filter(kind=kind, status__in=SyncJob.ACTIVE_STATUSES).first()


# ── Расписание ─────────────────────────────────────────────────────────────

def tz():
    return ZoneInfo(settings.SYNC_LOG_TZ)


def _next_time_of_day(at, now, zone):
    local = now.astimezone(zone)
    cand = datetime.combine(local.date(), at, tzinfo=zone)
    while cand <= now:
        cand = datetime.combine(cand.date() + timedelta(days=1), at, tzinfo=zone)
    return cand


def next_run_after(schedule, now=None):
    """Следующий такт после now — та же формула, что next_run_after в
    netbox-sync/worker.py (KTD15): интервал — now + интервал; время суток и
    «после nmap» (по запасному времени) — ближайшее такое время в SYNC_LOG_TZ."""
    now = now or timezone.now()
    if schedule.trigger == SyncSchedule.TRIGGER_INTERVAL:
        return now + timedelta(minutes=int(schedule.interval_minutes))
    if schedule.trigger == SyncSchedule.TRIGGER_DAILY:
        return _next_time_of_day(schedule.daily_at, now, tz())
    if schedule.trigger == SyncSchedule.TRIGGER_AFTER_NMAP:
        return _next_time_of_day(schedule.fallback_at, now, tz())
    raise ValueError(f"неизвестный запуск расписания: {schedule.trigger!r}")


def describe_trigger(schedule):
    if schedule.trigger == SyncSchedule.TRIGGER_INTERVAL:
        return f"каждые {schedule.interval_minutes} мин"
    if schedule.trigger == SyncSchedule.TRIGGER_DAILY:
        return f"ежедневно в {schedule.daily_at:%H:%M}"
    if schedule.trigger == SyncSchedule.TRIGGER_AFTER_NMAP:
        return f"после ночного nmap, не позже {schedule.fallback_at:%H:%M}"
    return schedule.trigger


def local_time(value):
    """Время в поясе SYNC_LOG_TZ для показа («04.10 02:30»)."""
    if value is None:
        return ""
    return value.astimezone(tz()).strftime("%d.%m %H:%M")


# ── Блокировка правок conf (KTD18) ─────────────────────────────────────────

def running_job(now=None):
    """Задание running с живой арендой — оно сейчас читает conf. Задание с
    истёкшей арендой (сборщик упал) правки не держит: его переведёт в
    interrupted первый живой сборщик."""
    now = now or timezone.now()
    return (SyncJob.objects.filter(status=SyncJob.STATUS_RUNNING, lease_until__gt=now)
            .order_by("started_at", "id").first())


def netobs_lock_held():
    """Занята ли общая блокировка синка KTD13 (её держат nmap и задания
    «пишет в NetBox»). Запрос к pg_locks через алиас netobs (роль
    ip_plan_ro, pg_locks читается любой ролью). Нет алиаса или netobs
    недоступна — False: блокировку правок держит только то, что видно."""
    if NETOBS_ALIAS not in connections.databases or cache.get(_NETOBS_DOWN_KEY):
        return False
    classid, objid = SYNC_LOCK_KEY >> 32, SYNC_LOCK_KEY & 0xFFFFFFFF
    try:
        with connections[NETOBS_ALIAS].cursor() as cur:
            cur.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM pg_locks
                       WHERE locktype = 'advisory' AND granted
                         AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
                         AND classid = %s::oid AND objid = %s::oid AND objsubid = 1)""",
                [classid, objid])
            return bool(cur.fetchone()[0])
    except Exception:  # noqa: BLE001 — проверка не должна ронять страницу настроек
        cache.set(_NETOBS_DOWN_KEY, True, _NETOBS_DOWN_TTL)
        return False


def edit_lock():
    """Почему правки conf сейчас нельзя сохранить; None — можно.

    {"reason": "job"|"nmap", "text": ..., "job_id": ..., "kind": ...}. Сначала
    задание: при задании «пишет в NetBox» общую блокировку держит сам
    сборщик, и причина — это задание, а не nmap. PID-файл nmap сюда не
    входит (KTD18): после SIGKILL он остаётся и держал бы правки вечно."""
    job = running_job()
    if job is not None:
        label = kind_label(job.kind)
        return {"reason": "job", "job_id": job.pk, "kind": job.kind,
                "text": f"Выполняется задание «{label}» (#{job.pk}) — правки конфигурации "
                        f"синка заблокированы до его окончания."}
    if netobs_lock_held():
        return {"reason": "nmap", "job_id": None, "kind": "",
                "text": "Идёт сканирование сетей (nmap) — общая блокировка синка занята, правки "
                        "конфигурации синка заблокированы до его окончания."}
    return None
