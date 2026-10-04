"""Сводка «Синк» по заданиям и логу nmap, расписания ночи по умолчанию (U12
плана docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md,
R11, KTD15, KTD19).

С этапа 2 ночные шаги идут заданиями сборщика, full_sync.sh выполняет только
nmap. Экран «Настройки → Запуск и расписание» берёт итог каждого шага из
истории заданий, а строку nmap — из лога full_sync.sh.
"""
import importlib
import io
import os
import tempfile
from datetime import time as dtime, timedelta
from pathlib import Path

from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from ip_plan.models import SyncJob, SyncSchedule
from ip_plan.services import db_transfer, sync_jobs, sync_log, sync_overview

seed_migration = importlib.import_module("ip_plan.migrations.0013_default_night_chain")


def _nmap_log(status="OK", sec=600, started="2026-10-04 03:00:01", finished=True, running=False):
    lines = ["=" * 64, f"  Full sync started: {started}  [env=local]  [version=0.6]",
             "  Scan: mode=full  resolve-dns=off  timeout=7200s", "=" * 64,
             "", "-" * 64, "  [Nmap scan] python3 scan_all.py --env local --mode full", "-" * 64,
             "[Phase 1] Getting subnet list..."]
    if not running:
        lines.append(f"  [Nmap scan] {status}  ({sec}s)")
    if finished:
        lines += ["", "=" * 64, "  SUMMARY", "=" * 64,
                  f"  Nmap scan       {status}       {sec}s", "=" * 64,
                  "  Full sync finished: 2026-10-04 03:10:01", "=" * 64]
    return "\n".join(lines) + "\n"


def _result(step, status="done", minutes_ago=60, duration=12.5):
    return {"step": step, "status": status, "duration": duration, "exit_code": 0, "errors": 0,
            "started_at": (timezone.now() - timedelta(minutes=minutes_ago)).isoformat()}


class DefaultScheduleTests(TestCase):
    """Миграция 0013: две цепочки ночи, по умолчанию включены."""

    def test_defaults_after_migrate(self):
        before = SyncSchedule.objects.get(kind="night_before_nmap")
        after = SyncSchedule.objects.get(kind="night_after_nmap")
        self.assertEqual((before.title, before.trigger, before.daily_at, before.enabled),
                         ("До nmap", "daily", dtime(2, 30), True))
        self.assertEqual(before.steps, ["netbox_ensure_custom_fields", "netbox_network_setup"])
        self.assertEqual((after.title, after.trigger, after.fallback_at, after.enabled),
                         ("После nmap", "after_nmap", dtime(6, 0), True))
        self.assertEqual(after.steps[-1], "netobs_retention")
        # Следующий такт ставит сборщик (KTD15) — миграция его не угадывает.
        self.assertIsNone(before.next_run)
        self.assertIsNone(after.next_run)

    def test_chains_cover_whitelist_in_its_order(self):
        # До и после nmap вместе — весь белый список сборщика, каждый шаг
        # один раз и в прежнем порядке full_sync.sh, ротация последней.
        before = SyncSchedule.objects.get(kind="night_before_nmap").steps
        after = SyncSchedule.objects.get(kind="night_after_nmap").steps
        self.assertEqual(before + after, [s.name for s in sync_jobs.STEPS_LIST])

    def test_seed_is_idempotent_and_keeps_manual_schedule(self):
        SyncSchedule.objects.filter(kind="night_before_nmap").update(
            daily_at=dtime(1, 15), updated_by="ivanov")
        seed_migration.seed(django_apps, connection.schema_editor())
        seed_migration.seed(django_apps, connection.schema_editor())
        self.assertEqual(SyncSchedule.objects.filter(kind__startswith="night_").count(), 2)
        before = SyncSchedule.objects.get(kind="night_before_nmap")
        self.assertEqual((before.daily_at, before.updated_by), (dtime(1, 15), "ivanov"))

    def test_seed_recreates_only_missing(self):
        SyncSchedule.objects.filter(kind="night_after_nmap").delete()
        seed_migration.seed(django_apps, connection.schema_editor())
        self.assertEqual(SyncSchedule.objects.filter(kind__startswith="night_").count(), 2)

    def test_trigger_text_and_tz(self):
        after = SyncSchedule.objects.get(kind="night_after_nmap")
        self.assertEqual(sync_jobs.describe_trigger(after), "после ночного nmap, не позже 06:00")


class SeededSchedulesTransferTests(TestCase):
    """Засеянные расписания не делают целевую базу «непустой» (db_transfer)."""

    def test_seed_only_target_is_accepted(self):
        self.assertTrue(SyncSchedule.objects.exists())
        self.assertEqual(db_transfer.target_problems(), [])

    def test_edited_seeded_schedule_is_not_seed(self):
        SyncSchedule.objects.filter(kind="night_after_nmap").update(updated_by="ivanov")
        problems = db_transfer.target_problems()
        self.assertTrue(any("SyncSchedule" in p for p in problems), problems)

    def test_extra_schedule_is_not_seed(self):
        SyncSchedule.objects.create(kind="snmp", steps=["netbox_devices_sync"], trigger="interval",
                                    interval_minutes=10)
        problems = db_transfer.target_problems()
        self.assertTrue(any("SyncSchedule" in p for p in problems), problems)

    def test_worker_next_run_keeps_seed(self):
        # Сборщик пишет только next_run — засеянной строку это не отменяет.
        SyncSchedule.objects.update(next_run=timezone.now())
        self.assertEqual(db_transfer.target_problems(), [])

    def test_load_replaces_seeded_schedules_with_source(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dump = os.path.join(tmp.name, "dump.json")
        SyncSchedule.objects.filter(kind="night_before_nmap").update(
            daily_at=dtime(1, 45), enabled=False, updated_by="ivanov")
        call_command("db_transfer", "dump", "--output", dump, stdout=io.StringIO())
        # Цель — «пустая»: те же засеянные строки, что после migrate.
        SyncSchedule.objects.all().delete()
        seed_migration.seed(django_apps, connection.schema_editor())
        call_command("db_transfer", "load", dump, stdout=io.StringIO())
        before = SyncSchedule.objects.get(kind="night_before_nmap")
        self.assertEqual((before.daily_at, before.enabled, before.updated_by),
                         (dtime(1, 45), False, "ivanov"))
        self.assertEqual(SyncSchedule.objects.count(), 2)

    def test_load_refuses_edited_target_schedule(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dump = os.path.join(tmp.name, "dump.json")
        call_command("db_transfer", "dump", "--output", dump, stdout=io.StringIO())
        SyncSchedule.objects.filter(kind="night_after_nmap").update(enabled=False, updated_by="x")
        with self.assertRaises(CommandError):
            call_command("db_transfer", "load", dump, stdout=io.StringIO())


class _Logs(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.lock = self.dir / ".full_sync.lock"
        patcher = override_settings(SYNC_LOGS_DIR=str(self.dir), NETBOX_SYNC_LOCK_FILE=str(self.lock),
                                    SYNC_LOG_TZ="Europe/Moscow", SYNC_SCHEDULE="0 3 * * *")
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.before = SyncSchedule.objects.get(kind="night_before_nmap")
        self.after = SyncSchedule.objects.get(kind="night_after_nmap")

    def write_log(self, text, name="full_sync_2026-10-04_0300.log"):
        (self.dir / name).write_text(text, encoding="utf-8")

    def row(self, overview, name):
        return next(r for r in overview["steps"] if r["name"] == name)


class SyncLogNmapTests(_Logs):
    def test_log_steps_are_only_nmap(self):
        # Ручной копии шагов full_sync.sh больше нет: в логе — только nmap,
        # остальные шаги — задания (их список — sync_jobs).
        self.assertEqual(sync_log.STEP_NAMES, ["Nmap scan"])
        self.assertTrue(sync_log.STEPS[0]["schedule_only"])

    def test_nmap_log_parsed(self):
        parsed = sync_log.parse(_nmap_log())
        self.assertEqual(parsed["overall"], "OK")
        self.assertEqual(parsed["steps"], [{"name": "Nmap scan", "status": "OK", "elapsed": 600}])
        parsed = sync_log.parse(_nmap_log(running=True, finished=False))
        self.assertEqual((parsed["overall"], parsed["running_step"]), ("RUNNING", "Nmap scan"))

    def test_full_sync_has_only_nmap(self):
        script = Path(__file__).resolve().parents[3] / "netbox-sync" / "full_sync.sh"
        if not script.exists():
            self.skipTest("netbox-sync рядом не лежит (контейнер без монтирования)")
        text = script.read_text(encoding="utf-8")
        self.assertIn('STEP_NAMES=("Nmap scan")', text)
        self.assertIn('STEP_SCRIPTS=("scan_all.py")', text)


class OverviewTests(_Logs):
    def test_nothing_ran_yet(self):
        ov = sync_overview.overview()
        self.assertEqual(ov["state"], "none")
        self.assertFalse(ov["available"])
        self.assertEqual([r["name"] for r in ov["steps"]],
                         ["netbox_ensure_custom_fields", "netbox_network_setup", "Nmap scan",
                          *[s.name for s in sync_jobs.STEPS_LIST[2:]]])
        self.assertEqual({r["status_label"] for r in ov["steps"]}, {"ещё не запускалось"})
        self.assertEqual([c["kind"] for c in ov["chains"]], ["night_before_nmap", "night_after_nmap"])
        self.assertTrue(all(c["last"] is None for c in ov["chains"]))
        self.assertEqual(ov["nmap"]["status_label"], "ещё не запускалось")

    def test_rows_from_jobs_and_nmap_from_log(self):
        SyncJob.objects.create(kind="night_before_nmap", steps=self.before.steps, schedule=self.before,
                               source="schedule", status="done", started_at=timezone.now(),
                               finished_at=timezone.now(),
                               step_results=[_result("netbox_ensure_custom_fields"),
                                             _result("netbox_network_setup", duration=95)])
        after = SyncJob.objects.create(
            kind="night_after_nmap", steps=self.after.steps, schedule=self.after, source="schedule",
            status="done_with_errors", started_at=timezone.now(), finished_at=timezone.now(),
            step_results=[_result("netbox_devices_sync", "done_with_errors", minutes_ago=30, duration=200),
                          _result("netbox_config_backup", "failed", minutes_ago=20)])
        self.write_log(_nmap_log(sec=610))
        ov = sync_overview.overview()
        self.assertTrue(ov["available"])
        self.assertEqual(ov["state"], "failed")
        net = self.row(ov, "netbox_network_setup")
        self.assertEqual((net["status"], net["title"], net["duration"]), ("ok", "VLAN и подсети", "1 мин 35 с"))
        dev = self.row(ov, "netbox_devices_sync")
        self.assertEqual((dev["status"], dev["status_label"], dev["job_id"]),
                         ("warn", "Выполнено с ошибками", after.pk))
        self.assertEqual(self.row(ov, "netbox_config_backup")["status"], "fail")
        self.assertEqual(self.row(ov, "netbox_dns_enrich")["status_label"], "ещё не запускалось")
        nmap = self.row(ov, "Nmap scan")
        self.assertEqual((nmap["status"], nmap["duration"], nmap["schedule_only"]), ("ok", "10 мин 10 с", True))
        chains = {c["kind"]: c for c in ov["chains"]}
        self.assertEqual(chains["night_after_nmap"]["last"]["id"], after.pk)
        self.assertEqual(chains["night_after_nmap"]["last"]["status_label"], "Выполнено с ошибками")
        self.assertEqual(ov["nmap"]["sync_version"], "0.6")
        self.assertEqual({f["name"] for f in ov["failed"]}, {"netbox_devices_sync", "netbox_config_backup"})

    def test_latest_result_wins_and_skipped_is_ignored(self):
        SyncJob.objects.create(kind="netbox_dns_enrich", steps=["netbox_dns_enrich"], status="failed",
                               step_results=[_result("netbox_dns_enrich", "failed", minutes_ago=300)])
        SyncJob.objects.create(kind="netbox_dns_enrich", steps=["netbox_dns_enrich"], status="done",
                               step_results=[_result("netbox_dns_enrich", minutes_ago=10)])
        SyncJob.objects.create(kind="manual_all", steps=["netbox_reachability", "netbox_dns_enrich"],
                               status="interrupted",
                               step_results=[_result("netbox_reachability", "interrupted", minutes_ago=5),
                                             {"step": "netbox_dns_enrich", "status": "skipped",
                                              "started_at": None, "duration": 0.0}])
        ov = sync_overview.overview()
        self.assertEqual(self.row(ov, "netbox_dns_enrich")["status"], "ok")
        reach = self.row(ov, "netbox_reachability")
        self.assertEqual((reach["status"], reach["status_label"]), ("fail", "Прервано"))

    def test_running_job_progress_and_journal(self):
        started = timezone.now() - timedelta(minutes=3)
        job = SyncJob.objects.create(
            kind="night_after_nmap", steps=self.after.steps, schedule=self.after, source="schedule",
            status="running", started_at=started, lease_until=timezone.now() + timedelta(minutes=1),
            step_results=[_result("netbox_devices_sync", minutes_ago=2, duration=60)],
            output_tail="=== [netbox_devices_sync] python x ===\nok\n=== [netbox_config_backup] python y ===\n"
                        "  ERROR сбой\n")
        ov = sync_overview.overview()
        self.assertEqual(ov["state"], "running")
        self.assertTrue(ov["running"])
        self.assertEqual(self.row(ov, "netbox_config_backup")["status"], "run")
        self.assertEqual(self.row(ov, "netbox_firewall_blocklist_sync")["status"], "wait")
        self.assertEqual(self.row(ov, "netbox_devices_sync")["status"], "ok")
        self.assertEqual(ov["progress"]["job_id"], job.pk)
        self.assertEqual((ov["progress"]["position"], ov["progress"]["total"]), (2, len(self.after.steps)))
        self.assertEqual(ov["progress"]["step_title"], "Снимки конфигураций")
        kinds = {line["kind"] for line in ov["journal"]}
        self.assertIn("start", kinds)
        self.assertIn("fail", kinds)

    def test_waiting_job_shows_wait_reason(self):
        SyncJob.objects.create(kind="night_before_nmap", steps=self.before.steps, schedule=self.before,
                               status="running", started_at=timezone.now(),
                               lease_until=timezone.now() + timedelta(minutes=1),
                               wait_reason="ждёт общую блокировку синка")
        ov = sync_overview.overview()
        self.assertEqual(self.row(ov, "netbox_ensure_custom_fields")["status"], "wait")
        self.assertIn("ждёт", ov["progress"]["wait_reason"])

    def test_running_nmap_from_lock_and_log(self):
        self.write_log(_nmap_log(running=True, finished=False))
        self.lock.write_text("123")
        ov = sync_overview.overview()
        self.assertEqual(ov["state"], "running")
        self.assertEqual(self.row(ov, "Nmap scan")["status"], "run")
        self.assertIsNone(ov["progress"].get("job_id"))
        self.assertTrue(ov["journal"])

    def test_unfinished_nmap_without_lock_is_aborted(self):
        self.write_log(_nmap_log(running=True, finished=False))
        self.assertEqual(sync_overview.overview()["state"], "aborted")

    def test_step_info_and_devices_step_started_from_jobs(self):
        self.assertIsNone(sync_overview.step_info("RedVirt"))
        self.assertIsNone(sync_overview.devices_step_started())
        SyncJob.objects.create(kind="night_after_nmap", steps=self.after.steps, status="done",
                               step_results=[_result("netbox_devices_sync", minutes_ago=90),
                                             _result("netbox_redvirt_sync", "timeout", minutes_ago=50)])
        info = sync_overview.step_info("RedVirt")
        self.assertEqual((info["status"], info["status_label"]), ("timeout", "Прервано по таймауту"))
        self.assertEqual(sync_overview.step_info("netbox_vcenter_sync")["status"], "none")
        since = sync_overview.devices_step_started()
        self.assertAlmostEqual((timezone.now() - since).total_seconds(), 90 * 60, delta=120)

    def test_devices_step_started_while_running(self):
        started = timezone.now() - timedelta(minutes=4)
        SyncJob.objects.create(kind="night_after_nmap", steps=self.after.steps, status="running",
                               started_at=started, lease_until=timezone.now() + timedelta(minutes=1))
        since = sync_overview.devices_step_started()
        self.assertAlmostEqual((since - started).total_seconds(), 0, delta=1)


class FormattingTests(_Logs):
    def test_formatting(self):
        self.assertEqual(sync_overview.duration(48), "48 с")
        self.assertEqual(sync_overview.duration(190), "3 мин 10 с")
        self.assertEqual(sync_overview.duration(3900), "1 ч 05 мин")
        self.assertIsNone(sync_overview.next_run("*/5 * * * *"))
        nxt = sync_overview.next_run("30 4 * * *")
        self.assertEqual((nxt.hour, nxt.minute), (4, 30))
        self.assertTrue(sync_overview.human_when(sync_overview.now()).startswith("сегодня"))

    def test_cards_in_night_order(self):
        cards = sync_overview.overview()["cards"]
        self.assertEqual([c["title"] for c in cards], ["До nmap", "Сканирование сетей (nmap)", "После nmap"])

    def test_screen_renders_without_stale_hints(self):
        from django.template.loader import render_to_string
        html = render_to_string("ip_plan/_settings_sync_body.html", {"sync": sync_overview.overview()})
        self.assertNotIn("Скоро", html)
        self.assertNotIn("пока не подключ", html)
        self.assertIn("ещё не запускалось", html)
