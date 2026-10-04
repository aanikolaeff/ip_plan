"""Расписания и задания сборщика (U8 плана
docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md, KTD3, KTD4,
KTD14, KTD15).

Таблицы `sync_schedule` и `sync_job` — контракт между репозиториями: сборщик
netbox-sync ходит в них SQL-запросами ролью `sync_worker`, без моделей Django.
Поэтому имена таблиц и колонок закреплены здесь, а не только в модели:
переименование поля в модели должно ронять этот тест, а не сборщик на проде.

Права `sync_worker` на PostgreSQL проверяются функциями has_*_privilege от
имени ip_plan: войти под sync_worker тест не может (пароля у web нет, и
CREATEROLE у ip_plan нет). Если роли в кластере нет — эти тесты пропускаются;
порядок «миграция до роли / роль до миграции» проверяется на одноразовом
кластере вне Django (bootstrap.sh + тот же блок прав).
"""
import importlib
import re
import unittest
from datetime import time, timedelta
from pathlib import Path

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from ip_plan.models import SyncJob, SyncSchedule
from ip_plan.services import db_transfer

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = importlib.import_module("ip_plan.migrations.0012_sync_jobs")

# Контракт с netbox-sync (сборщик, U9). Меняется только вместе с ним.
SCHEDULE_COLUMNS = {
    "id", "kind", "title", "steps", "trigger", "interval_minutes", "daily_at",
    "fallback_at", "enabled", "next_run", "updated_at", "updated_by",
}
JOB_COLUMNS = {
    "id", "kind", "steps", "schedule_id", "scheduled_for", "source",
    "requested_by", "status", "wait_reason", "worker_id", "lease_until",
    "created_at", "started_at", "finished_at", "step_results",
    "output_head", "output_tail", "log_path", "conf_hash", "code_version",
    "summary",
}
JOB_STATUSES = {"queued", "running", "done", "done_with_errors", "failed",
                "timeout", "interrupted"}


def _columns(table):
    with connection.cursor() as cursor:
        return {c.name for c in connection.introspection.get_table_description(cursor, table)}


class ContractTests(TestCase):
    def test_table_names_are_explicit(self):
        self.assertEqual(SyncSchedule._meta.db_table, "sync_schedule")
        self.assertEqual(SyncJob._meta.db_table, "sync_job")
        tables = set(connection.introspection.table_names())
        self.assertIn("sync_schedule", tables)
        self.assertIn("sync_job", tables)

    def test_schedule_columns(self):
        self.assertEqual(_columns("sync_schedule"), SCHEDULE_COLUMNS)

    def test_job_columns(self):
        self.assertEqual(_columns("sync_job"), JOB_COLUMNS)

    def test_job_status_values(self):
        self.assertEqual({v for v, _ in SyncJob.STATUS_CHOICES}, JOB_STATUSES)
        self.assertEqual(SyncJob.ACTIVE_STATUSES, ("queued", "running"))

    def test_schedule_trigger_values(self):
        self.assertEqual({v for v, _ in SyncSchedule.TRIGGER_CHOICES},
                         {"interval", "daily", "after_nmap"})

    def test_worker_can_insert_job_with_minimal_columns(self):
        # Сборщик пишет SQL сам: всё, кроме вида и шагов, имеет умолчание в
        # самой базе, а не только в Python.
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO sync_job (kind, steps) VALUES (%s, %s)",
                           ["netbox_devices_sync", '["netbox_devices_sync"]'])
        job = SyncJob.objects.get(kind="netbox_devices_sync")
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.steps, ["netbox_devices_sync"])
        self.assertEqual(job.step_results, [])
        self.assertEqual(job.wait_reason, "")
        self.assertEqual(job.source, "user")
        self.assertIsNotNone(job.created_at)
        self.assertIsNone(job.lease_until)


class ActiveJobUniquenessTests(TestCase):
    def test_two_queued_of_same_kind_rejected(self):
        SyncJob.objects.create(kind="topology", steps=["topology"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncJob.objects.create(kind="topology", steps=["topology"])

    def test_queued_and_running_of_same_kind_rejected(self):
        SyncJob.objects.create(kind="topology", steps=["topology"], status="running")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncJob.objects.create(kind="topology", steps=["topology"])

    def test_finished_jobs_do_not_block_new_one(self):
        for status in JOB_STATUSES - {"queued", "running"}:
            SyncJob.objects.create(kind="topology", steps=["topology"], status=status)
        SyncJob.objects.create(kind="topology", steps=["topology"])
        self.assertEqual(SyncJob.objects.filter(kind="topology").count(), len(JOB_STATUSES) - 1)

    def test_different_kinds_run_side_by_side(self):
        SyncJob.objects.create(kind="topology", steps=["topology"], status="running")
        SyncJob.objects.create(kind="dns", steps=["dns"], status="queued")
        self.assertEqual(SyncJob.objects.count(), 2)

    def test_unknown_status_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncJob.objects.create(kind="topology", steps=["topology"], status="paused")


class ScheduleTickTests(TestCase):
    def setUp(self):
        # Расписания по умолчанию (миграция 0013) здесь не нужны — тот же вид.
        SyncSchedule.objects.all().delete()
        self.schedule = SyncSchedule.objects.create(
            kind="night_after_nmap", steps=["netbox_devices_sync", "topology"],
            trigger="after_nmap", fallback_at=time(6, 0))

    def test_one_job_per_schedule_tick(self):
        tick = timezone.now().replace(microsecond=0)
        SyncJob.objects.create(kind="night_after_nmap", steps=["topology"], status="done",
                               source="schedule", schedule=self.schedule, scheduled_for=tick)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncJob.objects.create(kind="night_after_nmap_dup", steps=["topology"],
                                   source="schedule", schedule=self.schedule, scheduled_for=tick)

    def test_next_tick_and_manual_runs_allowed(self):
        tick = timezone.now().replace(microsecond=0)
        SyncJob.objects.create(kind="a", steps=[], status="done", source="schedule",
                               schedule=self.schedule, scheduled_for=tick)
        SyncJob.objects.create(kind="b", steps=[], status="done", source="schedule",
                               schedule=self.schedule, scheduled_for=tick + timedelta(days=1))
        # Ручные запуски без такта: NULL в уникальном ключе не совпадает.
        SyncJob.objects.create(kind="c", steps=[], status="done", schedule=self.schedule)
        SyncJob.objects.create(kind="d", steps=[], status="done", schedule=self.schedule)
        self.assertEqual(self.schedule.jobs.count(), 4)

    def test_deleting_schedule_keeps_job_history(self):
        job = SyncJob.objects.create(kind="a", steps=[], status="done", source="schedule",
                                     schedule=self.schedule, scheduled_for=timezone.now())
        self.schedule.delete()
        job.refresh_from_db()
        self.assertIsNone(job.schedule_id)

    def test_schedule_kind_is_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncSchedule.objects.create(kind="night_after_nmap", steps=[], trigger="daily",
                                        daily_at=time(2, 30))


class ScheduleTriggerConsistencyTests(TestCase):
    """Запуск по интервалу, по времени суток или «после nmap» с запасным
    временем (KTD15): у каждого вида — своё обязательное поле. Сборщик читает
    таблицу сам, и строка без нужного поля остановила бы его цикл."""

    def setUp(self):
        SyncSchedule.objects.all().delete()   # без засеянных миграцией 0013

    def _bad(self, **kwargs):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncSchedule.objects.create(kind="x", steps=["topology"], **kwargs)

    def test_valid_rows(self):
        SyncSchedule.objects.create(kind="snmp", steps=["snmp"], trigger="interval",
                                    interval_minutes=10)
        SyncSchedule.objects.create(kind="before", steps=["vlans"], trigger="daily",
                                    daily_at=time(2, 30))
        SyncSchedule.objects.create(kind="after", steps=["topology"], trigger="after_nmap",
                                    fallback_at=time(6, 0))
        self.assertEqual(SyncSchedule.objects.count(), 3)

    def test_interval_needs_positive_minutes(self):
        self._bad(trigger="interval")
        self._bad(trigger="interval", interval_minutes=0)

    def test_daily_needs_time(self):
        self._bad(trigger="daily")

    def test_after_nmap_needs_fallback_time(self):
        self._bad(trigger="after_nmap")

    def test_unknown_trigger(self):
        self._bad(trigger="cron", interval_minutes=5)


class TransferExclusionTests(TestCase):
    def test_jobs_are_not_transferred(self):
        # Задания — журнал работы сборщика, который есть только на PostgreSQL;
        # в SQLite (и обратно) их не переносим. Расписания — настройка, они
        # переносятся.
        self.assertIn("ip_plan.syncjob", db_transfer.EXCLUDED_MODELS)
        self.assertIn("ip_plan.syncjob", db_transfer.dump_excludes())
        labels = {m._meta.label_lower for m in db_transfer.transferred_models()}
        self.assertNotIn("ip_plan.syncjob", labels)
        self.assertIn("ip_plan.syncschedule", labels)


class GrantsTextTests(unittest.TestCase):
    """Права выдают два места — миграция и bootstrap.sh — одним и тем же
    блоком SQL, чтобы итог не зависел от порядка их запуска."""

    def test_bootstrap_contains_same_grant_block(self):
        sh = (ROOT / "db/bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn(MIGRATION.SYNC_WORKER_GRANTS_SQL.strip(), sh)

    def test_grant_block_matches_plan(self):
        sql = MIGRATION.SYNC_WORKER_GRANTS_SQL
        self.assertIn("to_regrole('sync_worker') IS NOT NULL", sql)
        self.assertIn("REVOKE ALL ON TABLE sync_job, sync_schedule FROM sync_worker", sql)
        self.assertIn("GRANT SELECT, INSERT, UPDATE ON TABLE sync_job TO sync_worker", sql)
        self.assertIn("GRANT SELECT ON TABLE sync_schedule TO sync_worker", sql)
        self.assertIn("GRANT UPDATE (next_run) ON TABLE sync_schedule TO sync_worker", sql)
        self.assertIn("GRANT USAGE ON SEQUENCE", sql)
        self.assertNotRegex(sql, r"(?i)\bDELETE\b|\bTRUNCATE\b|\bALL PRIVILEGES\b")

    def test_grants_skipped_on_sqlite(self):
        calls = []

        class _Editor:
            class connection:
                vendor = "sqlite"

            def execute(self, sql, params=()):
                calls.append(sql)

        MIGRATION.grant_sync_worker(None, _Editor())
        self.assertEqual(calls, [])


@unittest.skipUnless(connection.vendor == "postgresql", "права — только на PostgreSQL")
class SyncWorkerPrivilegeTests(TestCase):
    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regrole('sync_worker') IS NOT NULL")
            if not cursor.fetchone()[0]:
                self.skipTest("роли sync_worker в кластере нет (bootstrap.sh не запускался)")

    def _q(self, sql, *params):
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()[0]

    def _table(self, table, priv):
        return self._q("SELECT has_table_privilege('sync_worker', %s, %s)", table, priv)

    def _column(self, table, column, priv):
        return self._q("SELECT has_column_privilege('sync_worker', %s, %s, %s)", table, column, priv)

    def test_job_table(self):
        for priv in ("SELECT", "INSERT", "UPDATE"):
            self.assertTrue(self._table("sync_job", priv), priv)
        for priv in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            self.assertFalse(self._table("sync_job", priv), priv)

    def test_schedule_only_next_run_writable(self):
        self.assertTrue(self._table("sync_schedule", "SELECT"))
        self.assertTrue(self._column("sync_schedule", "next_run", "UPDATE"))
        for column in SCHEDULE_COLUMNS - {"next_run"}:
            self.assertFalse(self._column("sync_schedule", column, "UPDATE"), column)
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            self.assertFalse(self._table("sync_schedule", priv), priv)

    def test_sequences(self):
        for table in ("sync_job", "sync_schedule"):
            seq = self._q("SELECT pg_get_serial_sequence(%s, 'id')", table)
            self.assertTrue(self._q("SELECT has_sequence_privilege('sync_worker', %s, 'USAGE')", seq))

    def test_no_access_to_other_tables(self):
        with connection.cursor() as cursor:
            tables = [t for t in connection.introspection.table_names(cursor)
                      if t not in ("sync_job", "sync_schedule")]
        self.assertTrue(tables)
        for table in tables:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                self.assertFalse(self._table(table, priv), f"{table}: {priv}")


class BootstrapTextTests(unittest.TestCase):
    def setUp(self):
        self.sh = (ROOT / "db/bootstrap.sh").read_text(encoding="utf-8")

    def test_requires_sync_worker_password(self):
        self.assertIn(': "${SYNC_WORKER_DB_PASSWORD:?', self.sh)
        self.assertIn(r"\getenv sync_worker_pw SYNC_WORKER_DB_PASSWORD", self.sh)

    def test_role_and_connect(self):
        self.assertRegex(self.sh, r"'sync_worker'\]\) AS r")
        self.assertRegex(self.sh, r"ALTER ROLE sync_worker\s+LOGIN NOCREATEDB NOSUPERUSER NOCREATEROLE")
        self.assertIn("GRANT CONNECT ON DATABASE ip_plan TO sync_worker", self.sh)
        # В netobs сборщик ходит ролью netbox_sync, sync_worker там не нужен.
        self.assertNotIn("ON DATABASE netobs TO sync_worker", self.sh)

    def test_grant_block_runs_in_ip_plan_database(self):
        tail = self.sh.split("run_psql ip_plan", 1)
        self.assertEqual(len(tail), 2, "блок прав должен выполняться в базе ip_plan")
        self.assertIn(MIGRATION.SYNC_WORKER_GRANTS_SQL.strip(), tail[1])
        self.assertTrue(re.search(r"to_regclass\('sync_job'\) IS NOT NULL", MIGRATION.SYNC_WORKER_GRANTS_SQL))
