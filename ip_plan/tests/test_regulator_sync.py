"""regulator_sync: статусы записи «на xFW», сверка, проверка значения и
diff для мастера команд — на синтетическом снимке объектов."""
import ipaddress
from datetime import date
from unittest import mock

from django.test import TestCase

from ip_plan.models import FirewallObjectName, RegulatorDocument, RegulatorEntry
from ip_plan.services import acl_store, regulator_sync
from ip_plan.services.acl_model import ParsedDevice


def _device():
    d = ParsedDevice(device_name="xfw5000-mdc", parser="xfw")
    ip_vals = ["1.1.1.1", "2.2.2.2", "9.9.9.9", "198.18.4.0/255.255.255.0"]
    d.object_groups = {"BLOCK_BY_IP_2": [ipaddress.ip_network(v.replace("/255.255.255.0", "/24")) for v in ip_vals],
                       "BBDNS_1": []}
    d.object_group_members = {"BLOCK_BY_IP_2": ip_vals, "BBDNS_1": ["bad.example", "only.fw"]}
    return d


class RegulatorSyncTests(TestCase):
    def setUp(self):
        FirewallObjectName.objects.update_or_create(kind="ip", defaults={"current_name": "BLOCK_BY_IP_2"})
        FirewallObjectName.objects.update_or_create(kind="fqdn", defaults={"current_name": "BBDNS_1"})
        self.d1 = RegulatorDocument.objects.create(reference_no="1", document_date=date(2026, 9, 1))
        self.d2 = RegulatorDocument.objects.create(reference_no="2", document_date=date(2026, 9, 2))
        E = RegulatorEntry.objects.create
        self.applied = E(document=self.d1, kind="ip", value="1.1.1.1")
        self.pending = E(document=self.d1, kind="ip", value="3.3.3.3")
        self.revoked_present = E(document=self.d1, kind="ip", value="2.2.2.2", status="revoked")
        self.removed = E(document=self.d1, kind="ip", value="4.4.4.4", status="revoked")
        self.subnet = E(document=self.d1, kind="ip", value="198.18.4.7")
        self.url = E(document=self.d1, kind="url", value="https://bad.example/path")
        self.hash = E(document=self.d1, kind="hash", value="a" * 32)
        # 9.9.9.9 снят в первом письме, но активен во втором — не «снят»
        self.mixed_rev = E(document=self.d1, kind="ip", value="9.9.9.9", status="revoked")
        self.mixed_act = E(document=self.d2, kind="ip", value="9.9.9.9")
        dev = _device()
        for p in (mock.patch.object(acl_store, "get_device", return_value=dev),
                  mock.patch.object(acl_store, "get_meta", return_value={"updated": "2026-10-01T03:00:00", "model": "xFW 5000"}),
                  mock.patch.object(acl_store, "get_object_group", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def states(self):
        snap = regulator_sync.snapshot()
        rows = regulator_sync.annotate(RegulatorEntry.objects.select_related("document").all(), snap)
        return {r["id"]: (r["fw_state"], r["fw_via"]) for r in rows}

    def test_fw_states(self):
        st = self.states()
        self.assertEqual(st[self.applied.id][0], "applied")
        self.assertEqual(st[self.pending.id][0], "pending")
        self.assertEqual(st[self.revoked_present.id][0], "revoked_present")
        self.assertEqual(st[self.removed.id][0], "removed")
        self.assertEqual(st[self.subnet.id], ("applied", "198.18.4.0/255.255.255.0"))
        self.assertEqual(st[self.url.id][0], "applied", "URL сверяется по хосту в объекте доменов")
        self.assertEqual(st[self.hash.id][0], "not_applicable")
        self.assertEqual(st[self.mixed_rev.id][0], "applied", "активно в другом письме — не снято")

    def test_sync_lists_and_summary(self):
        snap = regulator_sync.snapshot()
        lists = regulator_sync.sync_lists(snap)
        self.assertEqual([x["value"] for x in lists["pending"]], ["3.3.3.3"])
        self.assertEqual([x["value"] for x in lists["revoked_present"]], ["2.2.2.2"])
        # подсеть на xFW в письмах не указана (указан адрес внутри неё) — тоже «только на xFW»
        self.assertEqual([x["value"] for x in lists["only_fw"]], ["only.fw", "198.18.4.0/255.255.255.0"])
        s = regulator_sync.summary(snap, lists=lists)
        self.assertEqual((s["pending_count"], s["revoked_present_count"], s["only_fw_count"]), (1, 1, 2))
        self.assertEqual(s["active_hash"], 1)
        self.assertEqual(s["letters_count"], 2)

    def test_letter_progress(self):
        rows = {r["reference_no"]: r for r in regulator_sync.letters()}
        one = rows["1"]
        self.assertEqual((one["applied"], one["pending"]), (3, 1))   # 1.1.1.1, 198.18.4.7, url; 3.3.3.3
        self.assertEqual(one["revoked_present"], 1)

    def test_check(self):
        self.assertEqual(regulator_sync.check("1.1.1.1")["outcome"], "blocked")
        self.assertEqual(regulator_sync.check("3.3.3.3")["outcome"], "pending")
        self.assertEqual(regulator_sync.check("2.2.2.2")["outcome"], "revoked_present")
        self.assertEqual(regulator_sync.check("only[.]fw")["outcome"], "only_fw", "дефанг понимается")
        self.assertEqual(regulator_sync.check("8.8.8.8")["outcome"], "not_found")
        c = regulator_sync.check("hxxps://bad[.]example/x")
        self.assertEqual((c["kind"], c["normalized"], c["outcome"]), ("url", "bad.example", "blocked"))

    def test_commands_diff(self):
        cmd = regulator_sync.commands()
        ip = next(b for b in cmd["blocks"] if b["kind"] == "ip")
        self.assertEqual((ip["from"], ip["to"]), ("BLOCK_BY_IP_2", "BLOCK_BY_IP_3"))
        self.assertEqual(ip["add"], ["3.3.3.3"])
        self.assertEqual(ip["remove"], ["2.2.2.2"])
        self.assertIn("198.18.4.0/255.255.255.0", ip["command"], "подсеть с xFW сохраняется")
        self.assertNotIn("2.2.2.2", ip["command"])
        self.assertIn("9.9.9.9", ip["command"], "значение, активное в другом письме, не убирается")
        fq = next(b for b in cmd["blocks"] if b["kind"] == "fqdn")
        self.assertFalse(fq["changed"])
        self.assertIn("only.fw", fq["command"], "«только на xFW» не убирается")
        self.assertFalse(cmd["blocked"])

    def test_no_snapshot_blocks_commands(self):
        with mock.patch.object(acl_store, "get_device", return_value=None), \
             mock.patch.object(regulator_sync.firewall_blocklist, "get_current_values", return_value=None):
            cmd = regulator_sync.commands()
        self.assertTrue(cmd["blocked"])
        with mock.patch.object(acl_store, "get_device", return_value=None), \
             mock.patch.object(regulator_sync.firewall_blocklist, "get_current_values", return_value=None):
            self.assertEqual(self.states()[self.applied.id][0], "unknown")

    def test_fallback_after_apply(self):
        FirewallObjectName.objects.filter(kind="ip").update(current_name="BLOCK_BY_IP_3")
        snap = regulator_sync.snapshot()
        self.assertTrue(snap["waiting_snapshot"])
        self.assertEqual(snap["objects"]["ip"].name, "BLOCK_BY_IP_2")
        self.assertEqual(regulator_sync.commands(snap)["blocks"][0]["to"], "BLOCK_BY_IP_4")
