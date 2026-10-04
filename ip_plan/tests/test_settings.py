"""Раздел «Настройки» (этап 6 редизайна, 02.10.2026): происхождение флагов, сохранение устройства и
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
from django.utils import timezone

from ip_plan import views_settings as vs
from ip_plan.models import SettingsAudit
from ip_plan.services import settings_bundle
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


# Разбор лога nmap и сводка «Синк» по заданиям — tests/test_sync_overview.py
# (с этапа 2 full_sync.sh выполняет только nmap, остальные шаги — задания).


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
        # Реестр платформ — фикстура, копия netbox-sync/registry/platforms.json
        # (сверку с настоящим держит test_platform_registry).
        registry = Path(__file__).resolve().parent / "fixtures"
        patcher = override_settings(NETBOX_SYNC_CONF_DIR=str(conf), SYNC_LOGS_DIR=str(logs),
                                    NETBOX_SYNC_LOCK_FILE=str(logs / ".lock"),
                                    NETBOX_SYNC_REGISTRY_DIR=str(registry))
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

    @override_settings(SYNC_WORKER_HEALTH_URL="")
    def test_sync_run_is_a_stub_without_worker(self):
        # Сборщик не включён (без профиля worker или на SQLite) — запуска
        # из интерфейса нет. С ним кнопки ставят задания —
        # test_jobs.SettingsSyncButtonsTests.
        r = self.client.post("/settings/sync/run/")
        self.assertEqual(r.status_code, 204)
        self.assertIn("сборщик заданий не включён", json.loads(r["HX-Trigger"])["show-toast"]["message"])
        from ip_plan.models import SyncJob
        self.assertFalse(SyncJob.objects.exists())

    def test_changed_devices_banner(self):
        # Граница — начало последнего шага устройств по заданиям сборщика.
        from ip_plan.models import SyncJob
        started = timezone.now() - timedelta(hours=1)
        SyncJob.objects.create(kind="night_after_nmap", steps=["netbox_devices_sync"], status="done",
                               step_results=[{"step": "netbox_devices_sync", "status": "done",
                                              "started_at": started.isoformat(), "duration": 60}])
        SettingsAudit.objects.create(username="a", scope="devices", target="sw-2", action="update")
        SettingsAudit.objects.create(username="a", scope="devices", target="gone", action="update")
        self.assertEqual(vs._changed_devices({"sw-1", "sw-2"}), ["sw-2"])
