"""Тесты парсера Eltex MES (ip_plan/services/eltex_acl.py) на синтетической
фикстуре fixtures/eltex_config.txt. Без Django — гоняются на хосте:
python3 -m unittest ip_plan.tests.test_eltex_acl -v"""

import ipaddress
import unittest
from pathlib import Path

from ip_plan.services import eltex_acl

FIXTURE = Path(__file__).parent / "fixtures" / "eltex_config.txt"


def _net(text):
    return ipaddress.ip_network(text)


class EltexAclTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text(encoding="utf-8")
        cls.device = eltex_acl.parse_config(cls.text, "lab-mes-sw01")

    def rule(self, acl_name, sequence):
        acl = self.device.acl_by_name(acl_name)
        self.assertIsNotNone(acl, acl_name)
        for r in acl.rules:
            if r.sequence == sequence:
                return r
        self.fail(f"{acl_name}: нет правила {sequence}")

    def test_parser_name_and_snapshot_entry(self):
        self.assertEqual(self.device.parser, "eltex_status")
        via_snapshot = eltex_acl.parse_snapshot({"config": self.text}, "x")
        self.assertEqual([a.name for a in via_snapshot.acls], [a.name for a in self.device.acls])
        self.assertEqual(eltex_acl.parse_snapshot({}, "x").acls, [])

    def test_acl_names_and_types(self):
        got = {a.name: a.acl_type for a in self.device.acls}
        self.assertEqual(got, {
            "SRV-IN": "extended", "UNUSED": "extended",
            "LEGACY": "standard", "MGMT": "management",
        })

    def test_mac_acl_skipped_with_warning(self):
        self.assertIsNone(self.device.acl_by_name("L2-ONLY"))
        self.assertTrue(any("L2-ONLY" in w for w in self.device.warnings))

    def test_only_expected_warnings(self):
        for w in self.device.warnings:
            self.assertNotIn("не разобра", w.lower(), w)
            self.assertNotIn("Не удалось", w, w)
            self.assertNotIn("Лишние", w, w)
        # допущение про wildcard в standard ACL — ровно одна запись
        self.assertEqual(sum("wildcard" in w for w in self.device.warnings), 1)

    def test_extended_rules_ace_priority_is_sequence(self):
        acl = self.device.acl_by_name("SRV-IN")
        self.assertEqual([r.sequence for r in acl.rules], [10, 20, 30, 40, 50])

    def test_tcp_udp_four_slots(self):
        r10 = self.rule("SRV-IN", 10)
        self.assertEqual(r10.protocol, "tcp")
        self.assertEqual(r10.source.kind, "any")
        self.assertIsNone(r10.source_port)
        self.assertEqual(r10.destination.networks, [_net("10.200.10.0/24")])
        self.assertEqual(r10.destination_port, "22")
        self.assertEqual(r10.raw, "permit tcp any any 10.200.10.0 0.0.0.255 22 ace-priority 10")
        r20 = self.rule("SRV-IN", 20)
        self.assertEqual(r20.source.networks, [_net("10.200.20.5/32")])
        self.assertEqual(r20.destination.networks, [_net("10.200.10.53/32")])
        self.assertEqual(r20.destination_port, "range 53-54")

    def test_other_protocols(self):
        r30 = self.rule("SRV-IN", 30)
        self.assertEqual(r30.action, "deny")
        self.assertTrue(r30.log)
        self.assertEqual(r30.source.networks, [_net("192.0.2.0/24")])
        self.assertEqual(r30.destination.kind, "any")
        r40 = self.rule("SRV-IN", 40)
        self.assertEqual(r40.destination.networks, [_net("10.200.10.0/24")])
        self.assertEqual(r40.extra.get("icmp-type/code"), "8 any")
        r50 = self.rule("SRV-IN", 50)
        self.assertEqual((r50.source.kind, r50.destination.kind), ("any", "any"))
        self.assertIsNone(r50.source_port)

    def test_unused_acl_with_time_range(self):
        r = self.rule("UNUSED", 10)
        self.assertEqual(r.schedule, "NIGHT")
        self.assertFalse(self.device.is_applied("UNUSED"))

    def test_standard_one_line_acl(self):
        acl = self.device.acl_by_name("LEGACY")
        self.assertEqual([r.sequence for r in acl.rules], [10, 20])
        self.assertEqual(acl.rules[0].source.networks, [_net("10.200.30.0/24")])
        self.assertEqual(acl.rules[1].action, "deny")
        self.assertEqual(acl.rules[1].source.kind, "any")

    def test_management_acl(self):
        r10 = self.rule("MGMT", 10)
        self.assertEqual(r10.source.networks, [_net("10.200.9.10/32")])
        self.assertEqual(r10.protocol, "ssh")
        self.assertEqual(self.rule("MGMT", 20).source.networks, [_net("10.200.9.0/24")])
        r30 = self.rule("MGMT", 30)
        self.assertEqual(r30.source.networks, [_net("192.0.2.0/24")])
        self.assertIsNone(r30.protocol)
        r40 = self.rule("MGMT", 40)
        self.assertEqual(r40.action, "deny")
        self.assertEqual(r40.source.kind, "any")

    def _apps(self, acl_name):
        return [(a.kind, a.location, a.direction)
                for a in self.device.applications if a.acl_name == acl_name]

    def test_applications(self):
        self.assertEqual(self._apps("SRV-IN"), [
            ("interface", "TwentyFiveGigaEthernet1/0/1", "in"),
            ("interface", "vlan 100", "in"),
        ])
        self.assertEqual(self._apps("LEGACY"), [
            ("interface", "vlan 100", "in"),
            ("interface", "vlan 200", "in"),
        ])
        self.assertEqual(self._apps("MGMT"), [("line", "management access-class", None)])
        for a in self.device.applications:
            self.assertNotIn("XXXX", a.location)

    def test_terminal_noise_tolerated(self):
        noisy = self.text.replace("\n", "\r\n").replace(
            " permit udp", "\x1b[0m\r\n                                                       permit udp")
        dev = eltex_acl.parse_config(noisy, "lab-mes-sw01")
        self.assertEqual([a.name for a in dev.acls], [a.name for a in self.device.acls])
        self.assertEqual(len(dev.acl_by_name("SRV-IN").rules), 5)
        self.assertEqual(dev.applications[-1].location, "vlan 200")

    def test_clean_lines(self):
        lines = eltex_acl.clean_lines("a\r\n\x1b[0m\r\n         shifted\r\n --More-- b\r\n")
        self.assertEqual(lines[:3], ["a", "", "shifted"])
        self.assertEqual(lines[3].strip(), "b")
        self.assertEqual(lines[4], "")

    def test_ambiguous_rule_is_reported_not_dropped(self):
        text = "ip access-list extended ODD\n permit ip any any any 192.0.2.1 0.0.0.0 ace-priority 20\nexit\n"
        dev = eltex_acl.parse_config(text, "x")
        r = dev.acls[0].rules[0]
        self.assertEqual(r.sequence, 20)
        self.assertIn("192.0.2.1 0.0.0.0", r.extra.get("не разобрано", ""))
        self.assertTrue(dev.warnings)


if __name__ == "__main__":
    unittest.main()
