"""Раздел «Настройки» (этап 6 редизайна, 02.10.2026): разбор лога синка и
экран «Запуск и расписание», происхождение флагов, сохранение устройства и
модели (явный false, массовый выключатель, cluster), импорт (права и те же
проверки, что у формы), журнал по категориям."""
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from ip_plan import views_settings as vs
from ip_plan.models import SettingsAudit
from ip_plan.services import settings_bundle, sync_log, sync_overview
from ip_plan.services import sync_config as sc

DEVICES = {
    "device_types": {
        "MES2300": {"manufacturer": "Eltex", "role": "switch", "interfaces_parser": "eltex_status",
                    "lldp_command": "show lldp neighbors", "sync_macs": True, "sync_lldp": True},
        "xFW 5000": {"manufacturer": "ViPNet", "role": "firewall", "interfaces_parser": "xfw",
                     "cluster": "vipnet_failover", "sync_macs": True},
        "C9300": {"manufacturer": "Cisco", "role": "switch", "interfaces_parser": "cisco_ios"},
    },
    "devices": {
        "sw-1": {"host": "10.0.0.1", "port": 22, "username": "admin", "password": "p",
                 "model": "MES2300", "site": "mdc", "status": "active",
                 "sync_interfaces": True, "sync_macs": True},
        "sw-2": {"host": "10.0.0.2", "port": 22, "username": "admin", "password": "p",
                 "model": "MES2300", "site": "mdc", "status": "active",
                 "sync_interfaces": True, "sync_lldp": False},
    },
}


def _log(steps, running=None, summary=True, started="2026-10-02 03:00:00"):
    lines = ["=" * 64, f"  Full sync started: {started}  [env=prod]  [version=0.4]",
             "  Scan: mode=ping  resolve-dns=off  timeout=7200s", "=" * 64]
    for name, status, sec in steps:
        lines += ["", "-" * 64, f"  [{name}] python3 x.py --env prod", "-" * 64, "вывод",
                  f"  [{name}] {status}  ({sec}s)"]
    if running:
        lines += ["", "-" * 64, f"  [{running}] python3 y.py --env prod", "-" * 64, "  идёт…"]
    if summary:
        lines += ["", "=" * 64, "  SUMMARY", "=" * 64, "=" * 64,
                  "  Full sync finished: 2026-10-02 03:40:00", "=" * 64]
    return "\n".join(lines) + "\n"


class SyncLogTests(SimpleTestCase):
    def test_reachability_is_a_step_and_matches_full_sync(self):
        self.assertIn("Reachability", sync_log.STEP_NAMES)
        self.assertEqual(sync_log.STEP_NAMES.index("Reachability") + 1,
                         sync_log.STEP_NAMES.index("DNS enrich"))
        script = Path(__file__).resolve().parents[3] / "netbox-sync" / "full_sync.sh"
        if not script.exists():
            self.skipTest("netbox-sync рядом не лежит (контейнер)")
        text = script.read_text()
        for step in sync_log.STEPS:
            self.assertIn(f'"{step["name"]}"', text)
            self.assertIn(f'"{step["script"]}"', text)

    def test_running_step_is_the_started_one(self):
        # LLDP завершён, Reachability начался: раньше текущим показывался
        # «DNS enrich» (Reachability не было в списке).
        # Срез — по имени шага, а не по числу: шаги добавляются в начало
        # (Netobs migrate, 03.10.2026), и позиционный срез молча съезжал.
        upto = sync_log.STEP_NAMES.index("LLDP topology") + 1
        done = [(n, "OK", 1) for n in sync_log.STEP_NAMES[:upto]]
        parsed = sync_log.parse(_log(done, running="Reachability", summary=False))
        self.assertEqual(parsed["overall"], "RUNNING")
        self.assertEqual(parsed["running_step"], "Reachability")

    def test_finished_and_failed(self):
        steps = [(n, "OK", 2) for n in sync_log.STEP_NAMES]
        steps[sync_log.STEP_NAMES.index("Config backup")] = ("Config backup", "FAILED", 1)
        parsed = sync_log.parse(_log(steps))
        self.assertEqual(parsed["overall"], "FAILED")
        self.assertEqual(parsed["finished_at"], "2026-10-02 03:40:00")


class SyncOverviewTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.lock = self.dir / ".full_sync.lock"
        patcher = override_settings(SYNC_LOGS_DIR=str(self.dir), NETBOX_SYNC_LOCK_FILE=str(self.lock),
                                    SYNC_LOG_TZ="Europe/Moscow", SYNC_SCHEDULE="0 3 * * *")
        patcher.enable()
        self.addCleanup(patcher.disable)

    def _write(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")

    def test_no_logs(self):
        overview = sync_overview.overview()
        self.assertFalse(overview["available"])
        self.assertEqual(overview["state"], "none")
        self.assertEqual(len(overview["steps"]), len(sync_log.STEPS))

    def test_failed_run_rows_and_cards(self):
        steps = [(n, "OK", 60) for n in sync_log.STEP_NAMES]
        idx = sync_log.STEP_NAMES.index
        steps[idx("vCenter")] = ("vCenter", "TIMEOUT", 120)
        self._write("full_sync_2026-10-02_0300.log", _log(steps))
        overview = sync_overview.overview()
        self.assertEqual(overview["state"], "failed")
        self.assertEqual((overview["ok_count"], overview["total_count"]),
                         (len(steps) - 1, len(steps)))
        self.assertEqual(overview["failed"][0]["label"], "Таймаут")
        row = overview["steps"][idx("Devices")]
        self.assertEqual((row["title"], row["duration"], row["status"]), ("Устройства", "1 мин 00 с", "ok"))
        self.assertTrue(overview["steps"][idx("Nmap scan")]["schedule_only"])

    def test_running_with_lock_has_progress_and_journal(self):
        prev = [(n, "OK", 100) for n in sync_log.STEP_NAMES]
        cb = sync_log.STEP_NAMES.index("Config backup")
        self._write("full_sync_2026-10-01_0300.log", _log(prev, started="2026-10-01 03:00:00"))
        started = sync_overview.now() - timedelta(minutes=5)
        self._write("full_sync_2026-10-02_0300.log",
                    _log([(n, "OK", 100) for n in sync_log.STEP_NAMES[:cb]], running="Config backup",
                         summary=False, started=started.strftime("%Y-%m-%d %H:%M:%S")))
        self.lock.write_text("123")
        overview = sync_overview.overview()
        self.assertEqual(overview["state"], "running")
        self.assertEqual(overview["progress"]["position"], cb + 1)
        self.assertEqual(overview["progress"]["step_name"], "Config backup")
        self.assertTrue(overview["progress"]["remaining"].startswith("~"))
        self.assertEqual([r["status"] for r in overview["steps"][cb:cb + 2]], ["run", "wait"])
        self.assertTrue(any(line["kind"] == "start" for line in overview["journal"]))

    def test_unfinished_log_without_lock_is_aborted(self):
        self._write("full_sync_2026-10-02_0300.log",
                    _log([("Custom fields", "OK", 1)], running="VLAN+prefixes", summary=False))
        self.assertEqual(sync_overview.overview()["state"], "aborted")

    def test_formatting(self):
        self.assertEqual(sync_overview.duration(48), "48 с")
        self.assertEqual(sync_overview.duration(190), "3 мин 10 с")
        self.assertEqual(sync_overview.duration(3900), "1 ч 05 мин")
        self.assertIsNone(sync_overview.next_run("*/5 * * * *"))
        nxt = sync_overview.next_run("30 4 * * *")
        self.assertEqual((nxt.hour, nxt.minute), (4, 30))
        self.assertTrue(sync_overview.human_when(sync_overview.now()).startswith("сегодня"))


class ChipsTests(SimpleTestCase):
    def test_origin_kinds_including_off_here(self):
        types = DEVICES["device_types"]
        cfg = {"model": "MES2300", "sync_interfaces": True, "sync_macs": True, "sync_lldp": False,
               "import_vlans": True}
        chips = {c["label"]: c["kind"]
                 for c in vs._chips(cfg, types["MES2300"], sc.effective(cfg, types))}
        self.assertEqual(chips, {"interfaces": "dev", "vlans": "dev", "macs": "both", "lldp": "off"})

    def test_inherited_and_cluster(self):
        types = DEVICES["device_types"]
        cfg = {"model": "xFW 5000", "sync_interfaces": True}
        chips = {c["label"]: c["kind"]
                 for c in vs._chips(cfg, types["xFW 5000"], sc.effective(cfg, types))}
        self.assertEqual(chips["macs"], "model")
        self.assertEqual(chips["cluster"], "model")


class _ConfMixin:
    """Временный conf/ с devices.json, NetBox недоступен (пустые справочники)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        conf = Path(self.tmp.name) / "conf"
        conf.mkdir()
        (conf / "devices.json").write_text(json.dumps(DEVICES), encoding="utf-8")
        (conf / "config.json").write_text(json.dumps({"environments": {"prod": {"netbox_url": "https://nb", "token": "t"}}}),
                                          encoding="utf-8")
        logs = Path(self.tmp.name) / "logs"
        logs.mkdir()
        patcher = override_settings(NETBOX_SYNC_CONF_DIR=str(conf), SYNC_LOGS_DIR=str(logs),
                                    NETBOX_SYNC_LOCK_FILE=str(logs / ".lock"))
        patcher.enable()
        self.addCleanup(patcher.disable)
        for target in ("ip_plan.services.sync_config.site_options",
                       "ip_plan.views_settings.site_options_cached",
                       "ip_plan.context_processors.site_options_cached"):
            p = mock.patch(target, return_value=[])
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("ip_plan.services.sync_config.netbox_names", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self.conf = conf

    def devices(self):
        return json.loads((self.conf / "devices.json").read_text())

    def hash(self):
        return sc.load("devices")[1]


def _device_post(**over):
    post = {"name": "sw-1", "model": "MES2300", "host": "10.0.0.1", "port": "22",
            "username": "admin", "password": "", "site": "mdc", "status": "active",
            "sync_interfaces": "1", "sync_lldp": "1"}
    post.update(over)
    return {k: v for k, v in post.items() if v is not None}


class DeviceAndModelSaveTests(_ConfMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(self.admin)

    def test_unchecked_model_flag_is_saved_as_explicit_false(self):
        r = self.client.post("/settings/devices/sw-1/save/", _device_post(file_hash=self.hash()))
        self.assertEqual(r.status_code, 204, r.content.decode())
        cfg = self.devices()["devices"]["sw-1"]
        self.assertIs(cfg["sync_macs"], False)
        self.assertEqual(cfg["password"], "p")       # пустое поле — пароль прежний
        self.assertTrue(SettingsAudit.objects.filter(scope="devices", target="sw-1").exists())

    def test_untouched_inherited_flag_stays_inherited(self):
        # sync_lldp у sw-1 унаследован от модели: «Сохранить» без правок не
        # должно делать его личным.
        r = self.client.post("/settings/devices/sw-1/save/",
                             _device_post(sync_macs="1", file_hash=self.hash()))
        self.assertEqual(r.status_code, 204, r.content.decode())
        cfg = self.devices()["devices"]["sw-1"]
        self.assertNotIn("sync_lldp", cfg)
        self.assertIs(cfg["sync_macs"], True)        # личное значение было — остаётся

    def test_prefixes_need_network_core(self):
        r = self.client.post("/settings/devices/sw-1/save/",
                             _device_post(import_prefixes="1", file_hash=self.hash()))
        self.assertEqual(r.status_code, 400)
        self.assertIn("ядра сети", r.content.decode())

    def test_model_save_keeps_unknown_keys_and_edits_cluster(self):
        post = {"name": "xFW 5000", "manufacturer": "ViPNet", "role": "firewall",
                "interfaces_parser": "xfw", "cluster": "vipnet_failover", "sync_macs": "1",
                "file_hash": self.hash()}
        r = self.client.post("/settings/models/xFW%205000/save/", post)
        self.assertEqual(r.status_code, 204, r.content.decode())
        self.assertEqual(self.devices()["device_types"]["xFW 5000"]["cluster"], "vipnet_failover")
        post.update(cluster="bogus", file_hash=self.hash())
        r = self.client.post("/settings/models/xFW%205000/save/", post)
        self.assertEqual(r.status_code, 400)

    def test_model_flag_change_drops_personal_values(self):
        post = {"name": "MES2300", "manufacturer": "Eltex", "role": "switch",
                "interfaces_parser": "eltex_status", "lldp_command": "show lldp neighbors",
                "sync_macs": "1", "file_hash": self.hash()}      # sync_lldp выключен у модели
        r = self.client.post("/settings/models/MES2300/save/", post)
        self.assertEqual(r.status_code, 204, r.content.decode())
        self.assertNotIn("sync_lldp", self.devices()["devices"]["sw-2"])

    def test_save_and_test_starts_connection_test(self):
        post = {"name": "MES2300", "manufacturer": "Eltex", "role": "switch",
                "interfaces_parser": "eltex_status", "lldp_command": "show lldp neighbors",
                "sync_macs": "1", "sync_lldp": "1", "then_test": "sw-1", "file_hash": self.hash()}
        with mock.patch("ip_plan.views_settings.connection_test.start") as start:
            r = self.client.post("/settings/models/MES2300/save/", post)
        self.assertEqual(r.status_code, 204)
        start.assert_called_once()
        self.assertIn("/settings/tab/devices/", r["HX-Redirect"])


class ImportTests(_ConfMixin, TestCase):
    def test_staff_cannot_import_config_or_ldap(self):
        staff = User.objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(staff)
        session = self.client.session
        session["import_payload"] = {"config": {"environments": {}}}
        session.save()
        r = self.client.post("/settings/import/apply/", {"apply_config": "1"})
        self.assertEqual(r.status_code, 403)

    def test_import_runs_the_same_device_checks(self):
        uploaded = {"devices": {"device_types": DEVICES["device_types"], "devices": {
            "c-1": {"host": "10.0.0.9", "username": "a", "password": "b", "model": "C9300",
                    "site": "mdc", "sync_interfaces": True, "sync_macs": True}}}}
        with self.assertRaises(settings_bundle.BundleError) as ctx:
            settings_bundle.apply(uploaded, ["device:c-1"])
        self.assertIn("c-1", str(ctx.exception))
        self.assertNotIn("c-1", self.devices()["devices"])


class AuditAndSyncTests(_ConfMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("root", password="x"))

    def test_audit_rows_and_category_filter(self):
        SettingsAudit.objects.create(username="a", scope="devices", target="sw-1", action="update")
        SettingsAudit.objects.create(username="a", scope="ldap", target="подключение", action="update")
        SettingsAudit.objects.create(username="a", scope="import", target="3 записей", action="update")
        request = mock.Mock(GET={"cat": "access"})
        rows = vs._tab_context(request, "audit")["audit_rows"]
        self.assertEqual([r["scope_label"] for r in rows], ["Вход и LDAP"])
        rows = vs._tab_context(mock.Mock(GET={}), "audit")["audit_rows"]
        self.assertEqual({r["action_kind"] for r in rows}, {"change", "import"})

    def test_sync_run_is_a_stub(self):
        r = self.client.post("/settings/sync/run/")
        self.assertEqual(r.status_code, 204)
        self.assertIn("пока не подключён", json.loads(r["HX-Trigger"])["show-toast"]["message"])

    def test_changed_devices_banner(self):
        started = sync_overview.now() - timedelta(hours=1)
        (Path(self.tmp.name) / "logs" / "full_sync_2026-10-02_0300.log").write_text(
            _log([(n, "OK", 10) for n in sync_log.STEP_NAMES],
                 started=started.strftime("%Y-%m-%d %H:%M:%S")), encoding="utf-8")
        SettingsAudit.objects.create(username="a", scope="devices", target="sw-2", action="update")
        SettingsAudit.objects.create(username="a", scope="devices", target="gone", action="update")
        self.assertEqual(vs._changed_devices({"sw-1", "sw-2"}), ["sw-2"])
