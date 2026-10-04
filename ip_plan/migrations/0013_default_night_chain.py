"""Расписания ночного прогона по умолчанию (U12 плана
docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md, R11, KTD15).

С этапа 2 ночные шаги идут заданиями сборщика, а full_sync.sh на хосте
выполняет только nmap (cron, 03:00). Ночь делится nmap на две цепочки:

* «До nmap» — ежедневно в 02:30: кастомные поля и VLAN с префиксами — nmap
  сканирует по префиксам, и они должны быть свежими;
* «После nmap» — по событию «ночной nmap завершился», запасное время 06:00
  (nmap не завершился — цепочка стартует всё равно, баннер пишет «ночной
  nmap не прошёл»): остальные шаги в прежнем порядке full_sync.sh и ротация
  netobs последней (R9).

Время суток — в поясе SYNC_LOG_TZ (так его читает сборщик). next_run не
задаётся: сборщик сам ставит ближайший такт новому расписанию.

Идемпотентно: расписание с тем же видом не создаётся второй раз и не
перезаписывается — если вид уже заведён руками, остаётся как есть. Засеянные
строки помечены updated_by = «migration 0013 …»: по этой пометке перенос
SQLite ↔ PostgreSQL (services/db_transfer.py) отличает их от настроенных
людьми — правка расписания в интерфейсе пишет в updated_by имя, а сборщик
меняет только next_run.

Данные — здесь, а не импортом из services/sync_jobs: миграция фиксирует
состояние на момент выкатки, а белый список шагов может меняться дальше
(тест сверяет, что все шаги отсюда в нём есть).
"""
from datetime import time

from django.db import migrations

SEED_MARK = "migration 0013"

BEFORE_NMAP = ["netbox_ensure_custom_fields", "netbox_network_setup"]
AFTER_NMAP = [
    "netbox_devices_sync", "netbox_config_backup", "netbox_firewall_blocklist_sync",
    "netbox_redvirt_sync", "netbox_vcenter_sync", "netbox_lldp_topology",
    "netbox_reachability", "netbox_dns_enrich", "netobs_retention",
]

DEFAULT_SCHEDULES = [
    {"kind": "night_before_nmap", "title": "До nmap", "steps": BEFORE_NMAP,
     "trigger": "daily", "daily_at": time(2, 30)},
    {"kind": "night_after_nmap", "title": "После nmap", "steps": AFTER_NMAP,
     "trigger": "after_nmap", "fallback_at": time(6, 0)},
]


def seed(apps, schema_editor):
    SyncSchedule = apps.get_model("ip_plan", "SyncSchedule")
    db = schema_editor.connection.alias
    for spec in DEFAULT_SCHEDULES:
        if SyncSchedule.objects.using(db).filter(kind=spec["kind"]).exists():
            continue
        SyncSchedule.objects.using(db).create(
            enabled=True, updated_by=f"{SEED_MARK} (расписание по умолчанию)", **spec)


def unseed(apps, schema_editor):
    SyncSchedule = apps.get_model("ip_plan", "SyncSchedule")
    db = schema_editor.connection.alias
    SyncSchedule.objects.using(db).filter(
        kind__in=[s["kind"] for s in DEFAULT_SCHEDULES], updated_by__startswith=SEED_MARK,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ip_plan", "0012_sync_jobs"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
