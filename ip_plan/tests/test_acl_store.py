"""acl_store: выбор парсера по meta.json, кэш по mtime, история и diff.
Django нужен только ради settings.ACL_CONFIG_BACKUPS_DIR — запускать
внутри контейнера: python manage.py test ip_plan.tests.test_acl_store."""

import json
import tempfile
import unittest
from pathlib import Path

try:
    from django.test import SimpleTestCase, override_settings
except ImportError:   # на хосте Django нет — discover остальных парсерных тестов не должен падать
    raise unittest.SkipTest("Django недоступен: test_acl_store запускается в контейнере")

from ip_plan.services import acl_store

CISCO_CFG = """!
hostname asr
!
object-group network Proxy
 host 10.200.0.10
!
ip access-list extended SNMP
 permit udp 10.200.1.0 0.0.0.255 any eq snmp
 deny ip any any
!
access-list 11 permit 10.200.1.5
!
interface GigabitEthernet0/0/1
 ip access-group SNMP in
!
snmp-server community S3CR3T view V RO 11
!
end
"""


class AclStoreTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.override = override_settings(ACL_CONFIG_BACKUPS_DIR=self.tmp.name)
        self.override.enable()
        acl_store._CACHE.clear()

    def tearDown(self):
        self.override.disable()
        self.tmp.cleanup()

    def _device(self, name, meta=None, files=None):
        d = self.root / name
        d.mkdir()
        if meta is not None:
            (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        for fname, text in (files or {}).items():
            (d / fname).write_text(text, encoding="utf-8")
        return d

    def test_legacy_dir_without_meta_is_cisco(self):
        self._device("asr1", files={"latest.cfg": CISCO_CFG})
        devices = acl_store.get_all_devices()
        self.assertEqual([d.device_name for d in devices], ["asr1"])
        self.assertEqual(devices[0].parser, "cisco_ios")
        self.assertEqual({a.name for a in devices[0].acls}, {"SNMP", "11"})

    def test_snmp_community_not_exposed(self):
        self._device("asr1", files={"latest.cfg": CISCO_CFG})
        rows = acl_store.overview()
        locations = " ".join(app.location for r in rows for app in r["applications"])
        self.assertNotIn("S3CR3T", locations)
        self.assertIn("community (RO)", locations)

    def test_unknown_parser_gives_warning_not_crash(self):
        self._device("weird", meta={"parser": "nope", "files": {}}, files={"latest.cfg": "x" * 30})
        # parser «nope» не знает, какие файлы ему нужны → каталог пропущен
        self.assertEqual(acl_store.get_all_devices(), [])

    def test_meta_parser_selects_files(self):
        self._device("fw", meta={"parser": "xfw", "model": "xFW 5000", "updated": "2026-10-01T00:12:06",
                                 "files": {"firewall_rules": {"rules_set_id": "1764", "changed": "2026-10-01T00:12:06"}}},
                     files={"latest.firewall_rules.txt": " Forward Rules:\nCurrent Rules Set Id: 1764\n",
                            "latest.firewall_objects.txt": "Ip Objects\nCurrent Rules Set Id: 1764\n"})
        devices = acl_store.get_all_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].parser, "xfw")
        info = acl_store.snapshot_info()[0]
        self.assertEqual(info["rules_set_id"], "1764")
        self.assertEqual(info["model"], "xFW 5000")
        self.assertEqual(info["files"], ["firewall_rules"])

    def test_cache_invalidates_on_mtime(self):
        d = self._device("asr1", files={"latest.cfg": CISCO_CFG})
        self.assertEqual(len(acl_store.get_all_devices()[0].acls), 2)
        cfg = d / "latest.cfg"
        cfg.write_text(CISCO_CFG + "access-list 12 permit 10.200.1.6\n", encoding="utf-8")
        import os
        st = cfg.stat()
        os.utime(cfg, (st.st_atime + 10, st.st_mtime + 10))
        self.assertEqual(len(acl_store.get_all_devices()[0].acls), 3)

    def test_search_ip_and_exclusions(self):
        self._device("asr1", files={"latest.cfg": CISCO_CFG})
        hits = acl_store.search_ip("10.200.1.7")
        # permit udp 10.200.1.0/24 any — source и any-destination; deny ip any any — catch-all, ниже
        self.assertEqual([(h["acl_name"], h["matched_as"], h["specific"]) for h in hits],
                         [("SNMP", "source+destination", True), ("SNMP", "source+destination", False)])
        self.assertEqual([h["acl_name"] for h in acl_store.search_ip("10.200.1.5")], ["SNMP", "11", "SNMP"])
        # исключение объекта снимает совпадение
        dev = acl_store.get_device("asr1")
        import ipaddress
        dev.object_group_exclusions["Proxy"] = [ipaddress.ip_network("10.200.0.10/32")]
        from ip_plan.services.acl_model import Endpoint
        ep = Endpoint(raw="object-group Proxy", kind="object-group", object_group="Proxy",
                      networks=[ipaddress.ip_network("10.200.0.0/24")])
        self.assertTrue(acl_store._endpoint_matches(ipaddress.ip_address("10.200.0.11"), ep, dev))
        self.assertFalse(acl_store._endpoint_matches(ipaddress.ip_address("10.200.0.10"), ep, dev))

    def test_object_used_inside_mixed_endpoint(self):
        """Объект в смешанном операнде («@Admins nets, 10.1.141.166») должен
        считаться использованным — именно так он и пропадал из «используется в»."""
        self._device("asr1", files={"latest.cfg": CISCO_CFG})
        dev = acl_store.get_device("asr1")
        import ipaddress
        from ip_plan.services.acl_model import Acl, AclRule, Endpoint
        dev.object_groups["Admins nets"] = [ipaddress.ip_network("10.200.5.0/24")]
        ep = Endpoint(raw="@Admins nets, 10.200.9.9", kind="mixed", object_group="Admins nets",
                      object_groups=["Admins nets"],
                      networks=[ipaddress.ip_network("10.200.5.0/24"), ipaddress.ip_network("10.200.9.9/32")])
        dev.acls.append(Acl(name="forward", acl_type="forward",
                            rules=[AclRule(sequence=1, action="permit", source=ep,
                                           destination=Endpoint(raw="@any", kind="any",
                                                                networks=[ipaddress.ip_network("0.0.0.0/0")]))]))
        self.assertEqual(acl_store._object_group_users(dev, "Admins nets"), ["forward"])
        dev.object_groups["Child"] = []
        dev.object_group_includes["Admins nets"] = ["Child"]
        rows = {r["name"]: r for r in acl_store.list_object_groups("asr1")}
        self.assertEqual(rows["Child"]["included_in"], ["Admins nets"])
        self.assertEqual(acl_store.get_object_group("asr1", "Admins nets")["includes"], ["Child"])
        # VPN-объект без адресов, но с узлами: счётчик берётся по элементам
        dev.object_group_members["Child"] = ["0x4f260038 user VPN-1 (7.0.0.45)"]
        rows = {r["name"]: r for r in acl_store.list_object_groups("asr1")}
        self.assertEqual(rows["Child"]["member_count"], 1)
        self.assertEqual(acl_store.get_object_group("asr1", "Child")["members"],
                         ["0x4f260038 user VPN-1 (7.0.0.45)"])
        rows = {r["name"]: r["used_by"] for r in acl_store.list_object_groups("asr1")}
        self.assertEqual(rows["Admins nets"], ["forward"])

    def test_history_and_diff_scrub_secrets(self):
        d = self._device("asr1", files={"latest.cfg": CISCO_CFG})
        (d / "asr1_20260930_010000.cfg").write_text(CISCO_CFG, encoding="utf-8")
        (d / "asr1_20261001_010000.cfg").write_text(
            CISCO_CFG.replace("S3CR3T", "N3WS3CR3T").replace("deny ip any any", "deny ip any any log"),
            encoding="utf-8")
        (d / "asr1_20261001_010000.firewall_rules.txt").write_text("x\n", encoding="utf-8")
        history = acl_store.list_history("asr1")
        self.assertEqual([h["snapshot"] for h in history], ["firewall_rules", "config", "config"])
        diff = acl_store.diff_snapshot("asr1", "asr1_20261001_010000.cfg")
        self.assertEqual(diff["previous"], "asr1_20260930_010000.cfg")
        text = "\n".join(diff["diff"])
        self.assertNotIn("S3CR3T", text)
        self.assertIn("deny ip any any log", text)
        self.assertEqual((diff["added"], diff["removed"]), (1, 1))
        # первый снимок — сравнивать не с чем, но не падаем
        first = acl_store.diff_snapshot("asr1", "asr1_20260930_010000.cfg")
        self.assertIsNone(first["previous"])
        # защита от произвольных имён
        self.assertIsNone(acl_store.diff_snapshot("asr1", "../../etc/passwd"))
        self.assertIsNone(acl_store.diff_snapshot("asr1", "latest.cfg"))
