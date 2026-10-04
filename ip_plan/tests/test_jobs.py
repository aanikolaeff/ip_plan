"""Раздел «Задания», баннер сборщика и блокировка правок conf (U11 плана
docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md, R14, R15,
KTD14, KTD17, KTD18).

Сборщик здесь не запускается: /health подменяется (или указывает на закрытый
порт), занятость общей блокировки netobs — подменой netobs_lock_held, а сам
запрос к pg_locks проверяется на PostgreSQL через алиас default.
"""
import ast
import json
import socket
import time
from datetime import time as dtime, timedelta, datetime, timezone as dt_timezone
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from ip_plan.models import LocalAccount, SettingsAudit, SyncJob, SyncSchedule
from ip_plan.services import sync_jobs, worker_health
from ip_plan.services import sync_config as sc
from ip_plan.tests.test_settings import _ConfMixin, _device_post

NETBOX_SYNC = Path(__file__).resolve().parents[3] / "netbox-sync"
HEALTH_OK = {"worker_id": "w1", "state": "running", "db": "up", "netobs": "up",
             "migrations": "ok", "nmap_missed": [], "package_warnings": [], "jobs": [],
             "version": "0.5", "env": "local"}


def _module_constants(path):
    """Литералы верхнего уровня модуля без его импорта: ip_plan не исполняет
    код netbox-sync, даже в тестах."""
    return ast.parse(path.read_text(encoding="utf-8"))


class WorkerStepsContractTests(SimpleTestCase):
    """Белый список шагов и ключ общей блокировки — копии из netbox-sync
    (см. докстринг sync_jobs). Рядом с соседним репозиторием сверяются с
    оригиналом, в контейнере без него — пропуск."""

    def _need(self, name):
        path = NETBOX_SYNC / name
        if not path.exists():
            self.skipTest(f"netbox-sync рядом не лежит ({path})")
        return path

    def test_steps_match_worker_steps(self):
        tree = _module_constants(self._need("worker_steps.py"))
        consts = {"_MIN": 60}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                    isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("GROUP_") \
                    and isinstance(node.value, ast.Constant):
                consts[node.targets[0].id] = node.value.value
        steps_node = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and getattr(n.targets[0], "id", "") == "STEPS_LIST")

        def value(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return consts[node.id]
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                return value(node.left) * value(node.right)
            raise AssertionError(f"неожиданное выражение в STEPS_LIST: {ast.dump(node)}")

        theirs = [tuple(value(a) for a in call.args) for call in steps_node.value.elts]
        ours = [(s.name, s.script, s.group, s.timeout, s.title) for s in sync_jobs.STEPS_LIST]
        self.assertEqual(ours, theirs)

    def test_sync_lock_key_matches_netobs(self):
        tree = _module_constants(self._need("netobs.py"))
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "SYNC_LOCK_KEY")
        self.assertEqual(node.value.value, sync_jobs.SYNC_LOCK_KEY)


class _Enabled(TestCase):
    """Сборщик «включён»: PostgreSQL (подменой — тесты идут и на SQLite) и
    адрес /health."""
    url = "http://worker.test:8090/health"

    def setUp(self):
        cache.clear()
        for p in (override_settings(SYNC_WORKER_HEALTH_URL=self.url, SYNC_WORKER_HEALTH_TIMEOUT=1.5,
                                    SYNC_WORKER_HEALTH_CACHE_SECONDS=20, SYNC_LOG_TZ="Europe/Moscow"),
                  mock.patch.object(worker_health, "_is_postgres", return_value=True),
                  mock.patch.object(sync_jobs, "netobs_lock_held", return_value=False),
                  mock.patch("ip_plan.context_processors.site_options_cached", return_value=[])):
            p.start() if hasattr(p, "start") else p.enable()
            self.addCleanup(p.stop if hasattr(p, "stop") else p.disable)
        self.staff = User.objects.create_user("adm", password="x", is_staff=True)
        self.user = User.objects.create_user("viewer", password="x")

    def health(self, data):
        p = mock.patch.object(worker_health, "fetch", return_value=data)
        p.start()
        self.addCleanup(p.stop)


class EnabledSwitchTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client.force_login(User.objects.create_user("adm", password="x", is_staff=True))

    def test_no_section_and_no_banner_when_not_enabled(self):
        cases = [("", True), ("http://worker:8090/health", False)]
        for url, postgres in cases:
            with self.subTest(url=url, postgres=postgres), override_settings(SYNC_WORKER_HEALTH_URL=url), \
                    mock.patch.object(worker_health, "_is_postgres", return_value=postgres):
                self.assertFalse(worker_health.enabled())
                self.assertEqual(self.client.get("/jobs/").status_code, 404)
                self.assertFalse(self.client.get("/worker/health.json").json()["enabled"])
                page = self.client.get("/settings/tab/audit/").content.decode()
                self.assertNotIn('href="/jobs/"', page)
                self.assertNotIn("shell-banner", page)

    def test_health_url_default_follows_compose_profiles(self):
        from config import settings as conf

        def fake(env):
            return lambda key, default=None, **kw: env.get(key, default)
        cases = [
            ({"COMPOSE_PROFILES": "gateway, worker", "SYNC_WORKER_HEALTH_PORT": "8091"},
             "http://worker:8091/health"),
            ({"COMPOSE_PROFILES": "worker"}, "http://worker:8090/health"),
            ({"COMPOSE_PROFILES": "gateway"}, ""),
            ({"COMPOSE_PROFILES": "worker", "SYNC_WORKER_HEALTH_URL": ""}, ""),
            ({"SYNC_WORKER_HEALTH_URL": "http://w:1/health"}, "http://w:1/health"),
        ]
        for env, expected in cases:
            with self.subTest(env=env), mock.patch.object(conf, "config", side_effect=fake(env)):
                self.assertEqual(conf._worker_health_url(), expected)


class JobsPageTests(_Enabled):
    def test_non_staff_sees_page_but_cannot_run_or_edit(self):
        self.health(HEALTH_OK)
        schedule = SyncSchedule.objects.create(kind="night", title="Ночь", steps=["netbox_devices_sync"],
                                               trigger="daily", daily_at=dtime(2, 30))
        self.client.force_login(self.user)
        page = self.client.get("/jobs/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Нужна группа netbox-admins")
        self.assertNotContains(page, 'hx-post="/jobs/run/"')
        self.assertEqual(self.client.post("/jobs/run/", {"step": "netbox_devices_sync"}).status_code, 403)
        self.assertEqual(self.client.get("/jobs/schedules/add/").status_code, 403)
        self.assertEqual(self.client.post(f"/jobs/schedules/{schedule.pk}/save/",
                                          {"trigger": "daily", "daily_at": "05:00"}).status_code, 403)
        self.assertEqual(self.client.post(f"/jobs/schedules/{schedule.pk}/delete/").status_code, 403)
        self.assertFalse(SyncJob.objects.exists())
        schedule.refresh_from_db()
        self.assertEqual(schedule.daily_at, dtime(2, 30))

    def test_empty_states(self):
        self.health(HEALTH_OK)
        self.client.force_login(self.user)
        page = self.client.get("/jobs/")
        self.assertContains(page, "Расписаний нет")
        self.assertContains(page, "Заданий ещё не было")
        page = self.client.get("/jobs/?status=failed")
        self.assertContains(page, "Нет заданий с этим состоянием")

    def test_staff_run_now_creates_job_and_repeat_returns_same(self):
        self.client.force_login(self.staff)
        r = self.client.post("/jobs/run/", {"step": "netbox_config_backup"})
        self.assertEqual(r.status_code, 204)
        job = SyncJob.objects.get()
        self.assertEqual((job.kind, job.steps, job.status, job.requested_by, job.source),
                         ("netbox_config_backup", ["netbox_config_backup"], "queued", "adm", "user"))
        self.assertEqual(r["HX-Redirect"], f"/jobs/{job.pk}/")
        again = self.client.post("/jobs/run/", {"step": "netbox_config_backup"})
        self.assertEqual(again["HX-Redirect"], f"/jobs/{job.pk}/")
        self.assertIn("уже выполняется", json.loads(again["HX-Trigger"])["show-toast"]["message"])
        self.assertEqual(SyncJob.objects.count(), 1)
        # Идущее (а не только стоящее в очереди) — тоже то же самое.
        SyncJob.objects.filter(pk=job.pk).update(status="running")
        self.client.post("/jobs/run/", {"step": "netbox_config_backup"})
        self.assertEqual(SyncJob.objects.count(), 1)
        audit = SettingsAudit.objects.get()
        self.assertEqual((audit.scope, audit.username, audit.target), ("sync", "adm", f"задание #{job.pk}"))
        # Завершённое не мешает новому запуску.
        SyncJob.objects.filter(pk=job.pk).update(status="done")
        self.client.post("/jobs/run/", {"step": "netbox_config_backup"})
        self.assertEqual(SyncJob.objects.count(), 2)

    def test_run_schedule_now_uses_its_steps_and_kind(self):
        s = SyncSchedule.objects.create(kind="night_after_nmap", title="После nmap",
                                        steps=["netbox_devices_sync", "netobs_retention"],
                                        trigger="after_nmap", fallback_at=dtime(6, 0))
        self.client.force_login(self.staff)
        self.client.post("/jobs/run/", {"schedule": s.pk})
        job = SyncJob.objects.get()
        self.assertEqual((job.kind, job.steps, job.schedule_id, job.scheduled_for),
                         ("night_after_nmap", ["netbox_devices_sync", "netobs_retention"], s.pk, None))

    def test_unknown_step_is_refused(self):
        self.client.force_login(self.staff)
        for step in ("scan_all", "netobs_migrate", "../../bin/sh"):
            r = self.client.post("/jobs/run/", {"step": step})
            self.assertEqual(r.status_code, 400)
        self.assertFalse(SyncJob.objects.exists())
        with self.assertRaises(ValueError):
            sync_jobs.enqueue("x", ["scan_all"])

    def test_enqueue_survives_a_race_on_the_unique_index(self):
        """Между проверкой и вставкой задание того же вида поставил другой
        запрос: IntegrityError частичного индекса → возвращается то, что есть."""
        existing = SyncJob.objects.create(kind="netbox_dns_enrich", steps=["netbox_dns_enrich"])
        with mock.patch.object(sync_jobs, "_active_job", side_effect=[None, existing]):
            job, created = sync_jobs.enqueue("netbox_dns_enrich", ["netbox_dns_enrich"])
        self.assertEqual((job.pk, created), (existing.pk, False))
        self.assertEqual(SyncJob.objects.count(), 1)


class JobDetailTests(_Enabled):
    def test_running_job_shows_current_step_and_polls(self):
        job = SyncJob.objects.create(kind="manual_selected",
                                     steps=["netbox_devices_sync", "netbox_lldp_topology"],
                                     status="running", started_at=timezone.now(),
                                     lease_until=timezone.now() + timedelta(minutes=2),
                                     step_results=[{"step": "netbox_devices_sync", "status": "done",
                                                    "duration": 12.5, "exit_code": 0, "errors": 0}],
                                     output_head="строка 1\n")
        self.client.force_login(self.user)
        page = self.client.get(f"/jobs/{job.pk}/").content.decode()
        self.assertIn("Выполняется шаг 2 из 2 — Связи между устройствами", page)
        self.assertIn(f'hx-get="/jobs/{job.pk}/body/"', page)
        self.assertIn('data-keep-scroll', page)
        # Ждёт общую блокировку — причина вместо текущего шага.
        SyncJob.objects.filter(pk=job.pk).update(wait_reason="ждёт общую блокировку синка")
        self.assertContains(self.client.get(f"/jobs/{job.pk}/body/"), "Ждёт: ждёт общую блокировку синка")

    def test_finished_job_stops_polling_and_marks_truncated_output(self):
        job = SyncJob.objects.create(kind="netbox_vcenter_sync", steps=["netbox_vcenter_sync"],
                                     status="done_with_errors", summary="vCenter: 2 ERROR",
                                     output_head="начало\n… вывод обрезан: показаны начало и конец …\n",
                                     output_tail="конец\n", log_path="sync_logs/worker/7.log")
        self.client.force_login(self.user)
        body = self.client.get(f"/jobs/{job.pk}/body/").content.decode()
        self.assertNotIn("hx-trigger", body)
        self.assertIn("вывод обрезан, полный лог: sync_logs/worker/7.log", body)
        self.assertIn("конец", body)
        self.assertIn("Нужна группа netbox-admins", body)  # «Запустить ещё раз» отключена

    def test_empty_output(self):
        job = SyncJob.objects.create(kind="netbox_dns_enrich", steps=["netbox_dns_enrich"], status="failed")
        self.client.force_login(self.user)
        self.assertContains(self.client.get(f"/jobs/{job.pk}/"), "Задание ничего не вывело")


class ScheduleFormTests(_Enabled):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def post(self, url="/jobs/schedules/add/save/", **data):
        return self.client.post(url, data)

    def test_forms_open(self):
        self.assertContains(self.client.get("/jobs/schedules/add/"), "Новое расписание")
        s = SyncSchedule.objects.create(kind="night", title="Ночь", steps=["netbox_devices_sync"],
                                        trigger="after_nmap", fallback_at=dtime(6, 0))
        page = self.client.get(f"/jobs/schedules/{s.pk}/edit/")
        self.assertContains(page, "после создания не меняется")
        self.assertContains(page, 'value="06:00"')

    def test_validation_errors_next_to_fields(self):
        r = self.post(mode="chain", kind="night", trigger="interval", interval_minutes="0",
                      steps=["netbox_devices_sync"])
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "не меньше 1", status_code=400)
        r = self.post(mode="chain", kind="night", trigger="daily", daily_at="02:30")
        self.assertContains(r, "Цепочка не может быть пустой", status_code=400)
        r = self.post(mode="chain", kind="night", trigger="daily", daily_at="02:30",
                      steps=["netbox_devices_sync", "scan_all"])
        self.assertContains(r, "вне белого списка", status_code=400)
        r = self.post(mode="step", step="scan_all", trigger="daily", daily_at="02:30")
        self.assertContains(r, "Выберите шаг", status_code=400)
        r = self.post(mode="chain", kind="Ночь!", trigger="daily", daily_at="25:00",
                      steps=["netbox_devices_sync"])
        self.assertContains(r, "Латиница", status_code=400)
        self.assertContains(r, "ЧЧ:ММ", status_code=400)
        self.assertFalse(SyncSchedule.objects.exists())

    def test_create_chain_computes_next_run_and_audits(self):
        before = timezone.now()
        r = self.post(mode="chain", kind="night_before_nmap", title="До nmap", trigger="daily",
                      daily_at="02:30", enabled="1",
                      steps=["netbox_network_setup", "netbox_ensure_custom_fields"])
        self.assertEqual(r.status_code, 204)
        s = SyncSchedule.objects.get()
        # Порядок — как выбран, не как в белом списке.
        self.assertEqual(s.steps, ["netbox_network_setup", "netbox_ensure_custom_fields"])
        local = s.next_run.astimezone(sync_jobs.tz())
        self.assertEqual((local.hour, local.minute), (2, 30))
        self.assertTrue(before < s.next_run <= before + timedelta(days=1))
        self.assertIn("Следующий запуск", json.loads(r["HX-Trigger"])["show-toast"]["message"])
        audit = SettingsAudit.objects.get()
        self.assertEqual((audit.scope, audit.action, audit.target), ("sync", "create", "расписание night_before_nmap"))
        # Тот же вид второй раз — ошибка у поля.
        r = self.post(mode="chain", kind="night_before_nmap", trigger="daily", daily_at="03:00",
                      steps=["netbox_dns_enrich"])
        self.assertContains(r, "уже есть", status_code=400)

    def test_edit_keeps_kind_and_disabling_clears_next_run(self):
        s = SyncSchedule.objects.create(kind="netbox_config_backup", steps=["netbox_config_backup"],
                                        trigger="daily", daily_at=dtime(4, 0), next_run=timezone.now())
        r = self.post(f"/jobs/schedules/{s.pk}/save/", mode="chain", kind="hijack", step="netbox_dns_enrich",
                      trigger="interval", interval_minutes="15")
        self.assertEqual(r.status_code, 204)
        s.refresh_from_db()
        self.assertEqual((s.kind, s.steps, s.trigger, s.interval_minutes, s.enabled, s.next_run),
                         ("netbox_config_backup", ["netbox_config_backup"], "interval", 15, False, None))
        self.assertEqual(SettingsAudit.objects.get().action, "update")

    def test_after_nmap_and_delete(self):
        r = self.post(mode="chain", kind="after", trigger="after_nmap", fallback_at="06:00", enabled="1",
                      steps=["netbox_devices_sync"])
        self.assertEqual(r.status_code, 204)
        s = SyncSchedule.objects.get()
        self.assertEqual(s.fallback_at, dtime(6, 0))
        job = SyncJob.objects.create(kind="after", steps=["netbox_devices_sync"], schedule=s, status="done")
        self.assertEqual(self.client.post(f"/jobs/schedules/{s.pk}/delete/").status_code, 204)
        self.assertFalse(SyncSchedule.objects.exists())
        job.refresh_from_db()
        self.assertIsNone(job.schedule_id)  # история остаётся

    def test_next_run_after(self):
        now = datetime(2026, 10, 4, 0, 0, tzinfo=dt_timezone.utc)  # 03:00 по Москве
        s = SyncSchedule(trigger="interval", interval_minutes=10)
        self.assertEqual(sync_jobs.next_run_after(s, now), now + timedelta(minutes=10))
        s = SyncSchedule(trigger="daily", daily_at=dtime(2, 30))
        self.assertEqual(sync_jobs.next_run_after(s, now), datetime(2026, 10, 4, 23, 30, tzinfo=dt_timezone.utc))
        s = SyncSchedule(trigger="after_nmap", fallback_at=dtime(6, 0))
        self.assertEqual(sync_jobs.next_run_after(s, now), datetime(2026, 10, 4, 3, 0, tzinfo=dt_timezone.utc))


class BannerTests(_Enabled):
    def banner(self):
        return self.client.get("/worker/health.json").json()

    def test_worker_not_answering_is_fast_and_shows_error(self):
        self.client.force_login(self.user)
        with override_settings(SYNC_WORKER_HEALTH_URL="http://127.0.0.1:9/health"):
            t0 = time.monotonic()
            data = self.banner()
            page = self.client.get("/jobs/")
            elapsed = time.monotonic() - t0
        self.assertEqual((data["level"], data["text"], data["link"]), ("error", "Сборщик не отвечает", "/jobs/"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Сборщик не отвечает")
        self.assertLess(elapsed, 2.0)

    def test_timeout_counts_as_not_answering_and_is_cached(self):
        self.client.force_login(self.user)
        with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")) as urlopen:
            self.assertEqual(self.banner()["text"], "Сборщик не отвечает")
            self.banner()
        self.assertEqual(urlopen.call_count, 1)

    def test_states_in_priority_order(self):
        self.client.force_login(self.user)
        cases = [
            (dict(HEALTH_OK, db="down"), "error", "Сборщик не достаёт базу"),
            (dict(HEALTH_OK, nmap_missed=[{"schedule": "after"}]), "warn", "Ночной nmap не прошёл"),
            (dict(HEALTH_OK, package_warnings=["psycopg: установлен 3.1"]), "warn",
             "Сборщику нужна пересборка образа"),
            (HEALTH_OK, "", ""),
        ]
        for health, level, text in cases:
            with self.subTest(text=text), mock.patch.object(worker_health, "fetch", return_value=health):
                data = self.banner()
                self.assertEqual((data["level"], data["text"]), (level, text))

    def test_failed_scheduled_job_and_running_indicator(self):
        self.client.force_login(self.user)
        s = SyncSchedule.objects.create(kind="night", title="Ночная цепочка", steps=["netbox_devices_sync"],
                                        trigger="daily", daily_at=dtime(2, 30))
        failed = SyncJob.objects.create(kind="night", steps=s.steps, schedule=s, source="schedule",
                                        scheduled_for=timezone.now(), status="interrupted")
        running = SyncJob.objects.create(kind="netbox_dns_enrich", steps=["netbox_dns_enrich"], status="running")
        with mock.patch.object(worker_health, "fetch", return_value=dict(
                HEALTH_OK, jobs=[{"id": running.pk, "step": "netbox_dns_enrich"}])):
            data = self.banner()
        self.assertEqual((data["level"], data["text"], data["link"]),
                         ("warn", "Ночная цепочка: прервано", f"/jobs/{failed.pk}/"))
        self.assertEqual(data["running"], [{"id": running.pk, "kind": "netbox_dns_enrich",
                                            "label": "DNS-имена", "step": "DNS-имена"}])
        # Следующее плановое прошло — предупреждения нет.
        SyncJob.objects.create(kind="night", steps=s.steps, schedule=s, source="schedule",
                               scheduled_for=timezone.now() + timedelta(days=1), status="done")
        with mock.patch.object(worker_health, "fetch", return_value=HEALTH_OK):
            self.assertEqual(self.banner()["level"], "")

    def test_menu_item_and_banner_markup(self):
        self.health(HEALTH_OK)
        self.client.force_login(self.user)
        page = self.client.get("/jobs/").content.decode()
        self.assertIn('href="/jobs/"', page)
        self.assertIn('role="status"', page)
        self.assertIn("window.IP_PLAN_WORKER = true", page)

    def test_forced_password_change_gets_json_not_redirect(self):
        self.health(HEALTH_OK)
        LocalAccount.objects.create(user=self.user, must_change_password=True)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/jobs/").status_code, 302)  # остальное — по-прежнему нет
        r = self.client.get("/worker/health.json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertTrue(r.json()["enabled"])


class EditLockTests(_ConfMixin, TestCase):
    """KTD18: пока идёт задание с живой арендой или nmap — правки conf → 409."""

    def setUp(self):
        super().setUp()
        cache.clear()
        p = mock.patch.object(sync_jobs, "netobs_lock_held", return_value=False)
        self.nmap = p.start()
        self.addCleanup(p.stop)
        self.client.force_login(User.objects.create_superuser("root", password="x"))

    def running(self, lease):
        return SyncJob.objects.create(kind="netbox_devices_sync", steps=["netbox_devices_sync"],
                                      status="running", started_at=timezone.now(),
                                      lease_until=timezone.now() + lease)

    def conf_posts(self):
        return [
            ("/settings/devices/sw-1/save/", _device_post(file_hash=self.hash())),
            ("/settings/devices/sw-1/delete/", {"file_hash": self.hash()}),
            ("/settings/models/MES2300/save/", {"name": "MES2300", "file_hash": self.hash()}),
            ("/settings/models/C9300/delete/", {"file_hash": self.hash()}),
            ("/settings/sources/redvirt/add/save/", {"name": "rv"}),
            ("/settings/sources/vcenter/0/delete/", {}),
            ("/settings/scan/save/", {}),
            ("/settings/access/save/", {"username": "svc"}),
            ("/settings/access/delete/", {}),
            ("/settings/environment/prod/save/", {"netbox_url": "https://x"}),
            ("/settings/import/preview/", {}),
            ("/settings/import/apply/", {"apply": "device:x"}),
        ]

    def test_running_job_blocks_conf_writes_but_not_ldap(self):
        job = self.running(timedelta(minutes=2))
        before = (self.conf / "devices.json").read_text()
        for url, data in self.conf_posts():
            with self.subTest(url=url):
                r = self.client.post(url, data)
                self.assertEqual(r.status_code, 409)
                self.assertIn(f"#{job.pk}", r.content.decode())
        self.assertEqual((self.conf / "devices.json").read_text(), before)
        r = self.client.post("/settings/ldap/save/", {"enabled": "", "servers": ""})
        self.assertNotEqual(r.status_code, 409)
        # Просмотр доступен: плашка с причиной и ссылкой, формы помечены.
        page = self.client.get("/settings/tab/devices/").content.decode()
        self.assertIn("data-conf-locked", page)
        self.assertIn(f"Открыть задание #{job.pk}", page)
        self.assertNotIn("data-conf-locked", self.client.get("/settings/tab/ldap/").content.decode())

    def test_expired_lease_does_not_block(self):
        self.running(-timedelta(seconds=1))
        self.assertIsNone(sync_jobs.edit_lock())
        r = self.client.post("/settings/devices/sw-1/save/", _device_post(file_hash=self.hash(), host="10.0.0.11"))
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.devices()["devices"]["sw-1"]["host"], "10.0.0.11")

    def test_nmap_lock_blocks_until_released(self):
        self.nmap.return_value = True
        lock = sync_jobs.edit_lock()
        self.assertEqual(lock["reason"], "nmap")
        page = self.client.get("/settings/tab/devices/").content.decode()
        self.assertIn("data-conf-locked", page)
        self.assertIn("nmap", page)
        r = self.client.post("/settings/devices/sw-1/save/", _device_post(file_hash=self.hash()))
        self.assertEqual(r.status_code, 409)
        self.assertTrue(sc.sync_running())
        self.nmap.return_value = False
        self.assertNotIn("data-conf-locked", self.client.get("/settings/tab/devices/").content.decode())
        r = self.client.post("/settings/devices/sw-1/save/", _device_post(file_hash=self.hash()))
        self.assertEqual(r.status_code, 204)

    def test_stale_nmap_pid_file_does_not_block(self):
        Path(self.tmp.name, "logs", ".lock").write_text("12345")
        self.assertTrue(sc.nmap_pid_file_present())
        self.assertIsNone(sync_jobs.edit_lock())
        self.assertFalse(sc.sync_running())
        r = self.client.post("/settings/devices/sw-1/save/", _device_post(file_hash=self.hash()))
        self.assertEqual(r.status_code, 204)


class NetobsLockProbeTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_without_alias_is_free(self):
        with mock.patch.dict("django.db.connections.databases", {}, clear=False) as dbs:
            dbs.pop("netobs", None)
            self.assertFalse(sync_jobs.netobs_lock_held())

    def test_unreachable_netobs_is_free_and_not_retried(self):
        broken = mock.MagicMock()
        broken.cursor.side_effect = Exception("connection refused")
        with mock.patch.dict("django.db.connections.databases", {"netobs": {}}), \
                mock.patch.object(sync_jobs, "connections") as conns:
            conns.databases = {"netobs": {}}
            conns.__getitem__.return_value = broken
            self.assertFalse(sync_jobs.netobs_lock_held())
            self.assertFalse(sync_jobs.netobs_lock_held())
        self.assertEqual(broken.cursor.call_count, 1)

    def test_pg_locks_query_sees_the_advisory_lock(self):
        """Тот же запрос, что к netobs, — на базе default (PostgreSQL):
        pg_locks и advisory-блокировки устроены одинаково в любой базе."""
        if connection.vendor != "postgresql":
            self.skipTest("нужен PostgreSQL")
        with mock.patch.object(sync_jobs, "NETOBS_ALIAS", "default"):
            self.assertFalse(sync_jobs.netobs_lock_held())
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", [sync_jobs.SYNC_LOCK_KEY])
            try:
                self.assertTrue(sync_jobs.netobs_lock_held())
            finally:
                with connection.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", [sync_jobs.SYNC_LOCK_KEY])
            self.assertFalse(sync_jobs.netobs_lock_held())


class SettingsSyncButtonsTests(_ConfMixin, _Enabled):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def run_sync(self, **data):
        return self.client.post("/settings/sync/run/", dict(action="run", **data))

    def test_run_all_selected_and_single(self):
        r = self.run_sync()
        self.assertEqual(r.status_code, 204)
        job = SyncJob.objects.get(kind=sync_jobs.KIND_ALL_STEPS)
        self.assertEqual(job.steps, [s.name for s in sync_jobs.STEPS_LIST])
        self.assertEqual(r["HX-Redirect"], f"/jobs/{job.pk}/")
        self.run_sync(steps="LLDP topology,Devices")
        self.assertEqual(SyncJob.objects.get(kind=sync_jobs.KIND_SELECTED).steps,
                         ["netbox_devices_sync", "netbox_lldp_topology"])
        self.run_sync(steps="Config backup")
        self.assertTrue(SyncJob.objects.filter(kind="netbox_config_backup").exists())
        self.assertEqual(SettingsAudit.objects.filter(scope="sync").count(), 3)

    def test_nmap_and_migrations_are_not_jobs(self):
        r = self.run_sync(steps="Nmap scan")
        self.assertEqual(r.status_code, 204)
        self.assertIn("не запускает", json.loads(r["HX-Trigger"])["show-toast"]["message"])
        self.run_sync(steps="Netobs migrate")
        self.assertFalse(SyncJob.objects.exists())

    def test_stop_is_not_supported(self):
        r = self.client.post("/settings/sync/run/", {"action": "stop"})
        self.assertIn("нельзя", json.loads(r["HX-Trigger"])["show-toast"]["message"])
        self.assertFalse(SyncJob.objects.exists())
