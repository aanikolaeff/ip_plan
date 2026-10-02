"""Тесты парсера Maipu (ip_plan/services/maipu_acl.py) на синтетической
фикстуре fixtures/maipu_config.txt. Без Django — гоняются на хосте:
python3 -m unittest ip_plan.tests.test_maipu_acl -v"""

import ipaddress
import unittest
from pathlib import Path

from ip_plan.services import maipu_acl

FIXTURE = Path(__file__).parent / "fixtures" / "maipu_config.txt"


def _net(text):
    return ipaddress.ip_network(text)


class MaipuAclTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text(encoding="utf-8")
        cls.device = maipu_acl.parse_config(cls.text, "lab-maipu-sw01")

    def rule(self, acl_name, sequence):
        acl = self.device.acl_by_name(acl_name)
        self.assertIsNotNone(acl, acl_name)
        for r in acl.rules:
            if r.sequence == sequence:
                return r
        self.fail(f"{acl_name}: нет правила {sequence}")

    def test_parser_name_and_snapshot_entry(self):
        self.assertEqual(self.device.parser, "maipu")
        via_snapshot = maipu_acl.parse_snapshot({"config": self.text}, "x")
        self.assertEqual([a.name for a in via_snapshot.acls], [a.name for a in self.device.acls])
        self.assertEqual(maipu_acl.parse_snapshot({}, "x").acls, [])

    def test_acl_names_and_types(self):
        got = {a.name: a.acl_type for a in self.device.acls}
        self.assertEqual(got, {
            "1": "standard", "NMS-HOSTS": "standard", "1001": "extended",
            "UNUSED-EXT": "extended", "5": "standard", "1500": "extended",
        })

    def test_ipv6_acl_skipped_with_warning(self):
        self.assertIsNone(self.device.acl_by_name("V6-ONLY"))
        self.assertTrue(any("V6-ONLY" in w for w in self.device.warnings))

    def test_only_documented_assumption_warnings(self):
        for w in self.device.warnings:
            self.assertNotIn("не разобра", w.lower(), w)
            self.assertNotIn("Не удалось", w, w)
        self.assertTrue(any("snmp-server community" in w for w in self.device.warnings))
        self.assertTrue(any("access-class" in w for w in self.device.warnings))
        self.assertEqual(len(self.device.warnings), 3)

    def test_standard_acl(self):
        acl = self.device.acl_by_name("1")
        self.assertEqual([r.sequence for r in acl.rules], [10, 20, 30])
        r10 = acl.rules[0]
        self.assertEqual(r10.source.networks, [_net("10.200.9.10/32")])
        self.assertTrue(r10.log)
        self.assertEqual(r10.raw, "10 permit host 10.200.9.10 log")
        self.assertEqual(acl.rules[1].source.networks, [_net("10.200.9.0/24")])
        self.assertEqual(acl.rules[2].action, "deny")
        self.assertEqual(acl.rules[2].source.kind, "any")
        self.assertIsNone(acl.rules[2].destination)

    def test_extended_acl(self):
        acl = self.device.acl_by_name("1001")
        self.assertEqual([r.sequence for r in acl.rules], [10, 20, 30, 40, 50, 60])
        r10 = self.rule("1001", 10)
        self.assertEqual(r10.action, "remark")
        self.assertEqual(r10.remark, "bastion to rdp")
        r20 = self.rule("1001", 20)
        self.assertEqual(r20.protocol, "tcp")
        self.assertEqual(r20.source.networks, [_net("10.200.9.10/32")])
        self.assertEqual(r20.destination.networks, [_net("10.200.1.100/32")])
        self.assertEqual(r20.destination_port, "eq 3389")
        self.assertIsNone(r20.source_port)
        r30 = self.rule("1001", 30)
        self.assertTrue(r30.log)
        self.assertEqual((r30.source.kind, r30.destination.kind), ("any", "any"))
        r40 = self.rule("1001", 40)
        self.assertEqual(r40.source.networks, [_net("10.200.2.0/24")])
        self.assertEqual(r40.source_port, "range 1024-65535")
        self.assertEqual(r40.destination.networks, [_net("192.0.2.53/32")])
        self.assertEqual(r40.destination_port, "eq 53")
        r50 = self.rule("1001", 50)
        self.assertEqual(r50.extra.get("icmp-type/code"), "8 0")
        r60 = self.rule("1001", 60)
        self.assertEqual(r60.schedule, "WORK")
        self.assertEqual(r60.extra.get("l3-action-group"), "g1")
        self.assertFalse(r60.log)

    def test_synthetic_sequence_and_unapplied(self):
        acl = self.device.acl_by_name("UNUSED-EXT")
        self.assertEqual([r.sequence for r in acl.rules], [10, 20])
        self.assertEqual(acl.rules[0].source.networks, [_net("192.0.2.0/24")])
        self.assertFalse(self.device.is_applied("UNUSED-EXT"))

    def test_legacy_one_liners(self):
        self.assertEqual(self.device.acl_by_name("5").rules[0].source.networks,
                         [_net("10.200.5.0/24")])
        r = self.device.acl_by_name("1500").rules[0]
        self.assertEqual((r.action, r.protocol), ("deny", "ip"))
        self.assertEqual(r.destination.kind, "any")

    def _apps(self, acl_name):
        return [(a.kind, a.location, a.direction)
                for a in self.device.applications if a.acl_name == acl_name]

    def test_applications(self):
        self.assertEqual(self._apps("1001"), [("interface", "vlan 10", "out")])
        self.assertEqual(self._apps("NMS-HOSTS"), [
            ("interface", "vlan10", "in"),
            ("snmp", "community (ro)", None),
        ])
        # строка `ip access-group 1 in` в фикстуре сдвинута пейджером
        self.assertEqual(self._apps("1"), [
            ("interface", "25ge0/0/1", "in"),
            ("line", "vty 0 15", "in"),
        ])
        self.assertEqual(self._apps("1500"), [("interface", "global", "in")])
        for a in self.device.applications:
            self.assertNotIn("XXXX", a.location)
            self.assertNotIn("YYYY", a.location)

    def test_terminal_noise_tolerated(self):
        noisy = self.text.replace("\n", "\r\n").replace(
            " 40 permit udp", "\x1b[0m\r\n              40 permit udp")
        dev = maipu_acl.parse_config(noisy, "lab-maipu-sw01")
        self.assertEqual([a.name for a in dev.acls], [a.name for a in self.device.acls])
        self.assertEqual(len(dev.acl_by_name("1001").rules), 6)

    def test_standard_second_address_read_as_destination(self):
        text = "ip access-list standard 2\n 10 deny any 10.200.0.0 0.0.255.255 log\n exit\n"
        dev = maipu_acl.parse_config(text, "x")
        r = dev.acls[0].rules[0]
        self.assertEqual(r.source.kind, "any")
        self.assertEqual(r.destination.networks, [_net("10.200.0.0/16")])
        self.assertTrue(r.log)
        self.assertTrue(any("destination" in w for w in dev.warnings))


if __name__ == "__main__":
    unittest.main()
