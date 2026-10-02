"""Тесты парсера Huawei VRP (ip_plan/services/huawei_acl.py) на синтетической
фикстуре fixtures/huawei_config.txt. Без Django — гоняются на хосте:
python3 -m unittest ip_plan.tests.test_huawei_acl -v"""

import ipaddress
import unittest
from pathlib import Path

from ip_plan.services import huawei_acl

FIXTURE = Path(__file__).parent / "fixtures" / "huawei_config.txt"


def _net(text):
    return ipaddress.ip_network(text)


class HuaweiAclTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text(encoding="utf-8")
        cls.device = huawei_acl.parse_config(cls.text, "lab-hw-sw01")

    def rule(self, acl_name, sequence):
        acl = self.device.acl_by_name(acl_name)
        self.assertIsNotNone(acl, acl_name)
        for r in acl.rules:
            if r.sequence == sequence:
                return r
        self.fail(f"{acl_name}: нет правила {sequence}")

    def test_parser_name_and_snapshot_entry(self):
        self.assertEqual(self.device.parser, "huawei_vrp")
        via_snapshot = huawei_acl.parse_snapshot({"config": self.text}, "x")
        self.assertEqual([a.name for a in via_snapshot.acls], [a.name for a in self.device.acls])
        empty = huawei_acl.parse_snapshot({}, "x")
        self.assertEqual(empty.acls, [])

    def test_acl_names_and_types(self):
        got = {a.name: a.acl_type for a in self.device.acls}
        self.assertEqual(got, {
            "2001": "basic", "2500": "basic", "2600": "basic", "3001": "advanced",
            "MGMT-IN": "basic", "VTY-ACL": "basic",
        })

    def test_l2_and_ipv6_acls_skipped_with_warning(self):
        self.assertIsNone(self.device.acl_by_name("4001"))
        self.assertTrue(any("4001" in w for w in self.device.warnings))
        self.assertTrue(any("IPv6" in w for w in self.device.warnings))
        # Правило IPv6-списка не должно попасть ни в один IPv4-список
        for acl in self.device.acls:
            for r in acl.rules:
                self.assertNotIn("2001:DB8", r.raw)

    def test_no_unexpected_warnings(self):
        for w in self.device.warnings:
            self.assertNotIn("не разобра", w.lower(), w)
            self.assertNotIn("Не удалось", w, w)

    def test_basic_acl_rules(self):
        acl = self.device.acl_by_name("2001")
        self.assertEqual(acl.description, "NMS hosts")
        self.assertEqual([r.sequence for r in acl.rules], [5, 10, 15])
        r5 = self.rule("2001", 5)
        self.assertEqual(r5.action, "permit")
        self.assertEqual(r5.source.kind, "host")
        self.assertEqual(r5.source.networks, [_net("10.200.1.10/32")])
        self.assertEqual(r5.raw, "rule 5 permit source 10.200.1.10 0")
        r10 = self.rule("2001", 10)
        self.assertEqual(r10.source.kind, "wildcard")
        self.assertEqual(r10.source.networks, [_net("10.200.1.0/24")])
        r15 = self.rule("2001", 15)
        self.assertEqual(r15.action, "deny")
        self.assertEqual(r15.source.kind, "any")
        self.assertIsNone(r15.destination)

    def test_advanced_acl_rules(self):
        r5 = self.rule("3001", 5)
        self.assertEqual(r5.protocol, "tcp")
        self.assertEqual(r5.source.networks, [_net("10.200.2.0/24")])
        self.assertEqual(r5.destination.networks, [_net("192.0.2.10/32")])
        self.assertEqual(r5.destination_port, "eq 22")
        self.assertIsNone(r5.source_port)
        r10 = self.rule("3001", 10)
        self.assertEqual(r10.source.kind, "any")
        self.assertEqual(r10.destination_port, "range 53-54")
        r15 = self.rule("3001", 15)
        self.assertTrue(r15.log)
        self.assertEqual(r15.destination.kind, "any")
        r20 = self.rule("3001", 20)
        self.assertEqual(r20.extra.get("icmp-type"), "echo")
        self.assertFalse(r20.log)

    def test_named_acls(self):
        r5 = self.rule("MGMT-IN", 5)
        self.assertEqual(r5.schedule, "WORK")
        self.assertEqual(r5.source.networks, [_net("10.200.9.1/32")])
        self.assertEqual(self.rule("MGMT-IN", 10).action, "deny")
        r10 = self.rule("VTY-ACL", 10)
        self.assertEqual(r10.label, "lab-host")
        self.assertEqual(r10.source.networks, [_net("10.200.9.77/32")])

    def test_step_line_is_not_a_rule(self):
        self.assertEqual(len(self.device.acl_by_name("3001").rules), 4)

    def _apps(self, acl_name):
        return [(a.kind, a.location, a.direction)
                for a in self.device.applications if a.acl_name == acl_name]

    def test_snmp_applications_hide_community(self):
        apps = self._apps("2001")
        self.assertIn(("snmp", "community (read)", None), apps)
        self.assertIn(("snmp", "group v3 admins", None), apps)
        self.assertIn(("snmp", "usm-user v3 nmsuser", None), apps)
        self.assertIn(("snmp", "community (write)", None), self._apps("MGMT-IN"))
        for a in self.device.applications:
            self.assertNotIn("SECRET", a.location)
            self.assertNotIn("%^%#", a.location)

    def test_interface_applications(self):
        self.assertIn(("interface", "Vlanif100", "in"), self._apps("MGMT-IN"))
        self.assertEqual(self._apps("3001"),
                         [("interface", "10GE1/0/1 (traffic-policy P-EDGE)", "in")])
        self.assertIn(("nat", "GigabitEthernet0/0/1: nat outbound 2600", None), self._apps("2600"))

    def test_vty_and_service_applications(self):
        self.assertEqual(self._apps("VTY-ACL"), [("line", "vty 0 4", "in")])
        self.assertIn(("line", "ssh server", "in"), self._apps("2001"))

    def test_unapplied_acl(self):
        self.assertFalse(self.device.is_applied("2500"))
        self.assertTrue(self.device.is_applied("2001"))

    def test_terminal_noise_tolerated(self):
        noisy = self.text.replace("\n", "\r\n").replace(
            "acl number 3001", "\x1b[0m\r\n                                     acl number 3001")
        dev = huawei_acl.parse_config(noisy, "lab-hw-sw01")
        self.assertEqual([a.name for a in dev.acls], [a.name for a in self.device.acls])
        self.assertEqual(len(dev.acl_by_name("3001").rules), 4)

    def test_prefix_length_assumption_warns_only_when_met(self):
        text = "#\nacl number 2010\n rule 5 permit source 10.200.1.0 24\n#\nreturn\n"
        dev = huawei_acl.parse_config(text, "x")
        self.assertEqual(dev.acls[0].rules[0].source.networks, [_net("10.200.1.0/24")])
        self.assertTrue(any("длина префикса" in w for w in dev.warnings))
        self.assertFalse(any("длина префикса" in w for w in self.device.warnings))

    def test_rule_without_id_gets_synthetic_sequence(self):
        text = "#\nacl number 2010\n rule permit source 10.200.1.1 0\n rule deny\n#\nreturn\n"
        dev = huawei_acl.parse_config(text, "x")
        self.assertEqual([r.sequence for r in dev.acls[0].rules], [10, 20])


if __name__ == "__main__":
    unittest.main()
