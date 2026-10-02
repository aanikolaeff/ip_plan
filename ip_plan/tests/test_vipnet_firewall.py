"""Тесты парсера ViPNet xFW/HW (ip_plan/services/vipnet_firewall.py).

Без Django: запуск на хосте
    python3 -m unittest ip_plan.tests.test_vipnet_firewall -v

Фикстуры в fixtures/vipnet_firewall_*.txt — синтетические, но повторяют
раскладку живого вывода (позиции колонок, переносы посреди токена, хвост
имени неактивного правила в строке действия, строка DPI, DomainUser
строкой выше). Адреса и имена вымышленные.
"""

import ipaddress
import unittest
from pathlib import Path

from ip_plan.services import vipnet_firewall as vf

FIXTURES = Path(__file__).parent / "fixtures"


def _net(s):
    return ipaddress.ip_network(s)


def _rule(acl, seq):
    for r in acl.rules:
        if r.sequence == seq:
            return r
    raise AssertionError(f"нет правила {seq} в {acl.name}")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules_text = (FIXTURES / "vipnet_firewall_rules.txt").read_text()
        cls.objects_text = (FIXTURES / "vipnet_firewall_objects.txt").read_text()
        cls.dev = vf.parse_snapshot(
            {"firewall_rules": cls.rules_text, "firewall_objects": cls.objects_text},
            "lab-xfw1", sections=None)
        cls.by_name = {a.name: a for a in cls.dev.acls}


class TestSections(_Base):
    def test_counts_and_order(self):
        self.assertEqual([a.name for a in self.dev.acls],
                         ["local", "forward", "nat", "service-local", "service-vpn"])
        self.assertEqual({a.name: len(a.rules) for a in self.dev.acls},
                         {"forward": 13, "local": 2, "nat": 3,
                          "service-local": 1, "service-vpn": 2})
        for acl in self.dev.acls:
            self.assertEqual(acl.acl_type, acl.name)
        self.assertNotIn("vpn", self.by_name)  # «empty rule for  Vpn Rules:»

    def test_parser_name_and_set_id(self):
        self.assertEqual(self.dev.parser, "xfw")
        self.assertEqual(vf.rules_set_id(self.rules_text), "4242")
        self.assertEqual(vf.rules_set_id(self.objects_text), "4242")
        self.assertIsNone(vf.rules_set_id("nothing here"))

    def test_applications(self):
        apps = {(a.acl_name, a.kind, a.location) for a in self.dev.applications}
        self.assertIn(("forward", "section", "Forward Rules"), apps)
        self.assertIn(("service-vpn", "section", "Service Vpn Rules"), apps)
        self.assertEqual(len(apps), 5)
        self.assertTrue(self.dev.is_applied("nat"))

    def test_every_rule_has_action(self):
        for acl in self.dev.acls:
            for r in acl.rules:
                self.assertIn(r.action, ("permit", "deny"), (acl.name, r.sequence))
                self.assertTrue(r.action_raw, (acl.name, r.sequence))
                self.assertIn(r.extra.get("option"), ("User", "Generated"))
                self.assertTrue(r.raw)

    def test_no_unexpected_warnings(self):
        bad = [w for w in self.dev.warnings if "не распознан" in w or "не найден" in w]
        self.assertEqual(bad, [])


class TestRuleHeader(_Base):
    def test_label_and_actions(self):
        fwd = self.by_name["forward"]
        self.assertEqual(_rule(fwd, 1).label, "5000101 - BLOCK_LIST_IN")
        self.assertEqual(_rule(fwd, 1).action, "deny")
        self.assertEqual(_rule(fwd, 1).action_raw, "drop")
        self.assertEqual(_rule(fwd, 4).action, "permit")
        self.assertEqual(_rule(fwd, 4).action_raw, "pass")
        self.assertEqual(_rule(fwd, 13).action, "deny")
        self.assertEqual(_rule(fwd, 13).action_raw, "reject")
        nat = _rule(self.by_name["nat"], 1)
        self.assertEqual((nat.action, nat.action_raw), ("permit", "NAT"))

    def test_inactive_name_wrapped_into_act_row(self):
        r = _rule(self.by_name["forward"], 3)
        self.assertTrue(r.inactive)
        self.assertEqual(r.label, "5000103 - Telnet_BLOCK")
        self.assertEqual(r.source.kind, "any")
        self.assertEqual(r.destination.kind, "any")
        r = _rule(self.by_name["nat"], 2)
        self.assertTrue(r.inactive)
        self.assertEqual(r.label, "5000002 - OFFICE_sNAT")
        self.assertFalse(_rule(self.by_name["forward"], 1).inactive)

    def test_schedule_and_option(self):
        r = _rule(self.by_name["forward"], 11)
        self.assertEqual(r.schedule, "Workdays")
        self.assertEqual(r.extra["option"], "User")
        self.assertIsNone(_rule(self.by_name["forward"], 10).schedule)
        self.assertEqual(_rule(self.by_name["service-vpn"], 1).extra["option"], "Generated")


class TestProtocol(_Base):
    def test_single_tcp_with_port(self):
        r = _rule(self.by_name["local"], 2)
        self.assertEqual(r.protocol, "tcp")
        self.assertEqual(r.destination_port, "to 443")
        self.assertIsNone(r.source_port)

    def test_udp_from_to(self):
        r = _rule(self.by_name["forward"], 11)
        self.assertEqual((r.protocol, r.source_port, r.destination_port),
                         ("udp", "from 67", "to 68"))
        r = _rule(self.by_name["service-vpn"], 1)
        self.assertEqual((r.protocol, r.source_port, r.destination_port),
                         ("udp", "from 2060", "to 2060"))

    def test_multi_protocol_stays_in_protocol(self):
        r = _rule(self.by_name["forward"], 2)
        self.assertEqual(r.protocol, "tcp to 5432, tcp to 5433")
        self.assertIsNone(r.destination_port)
        self.assertIsNone(r.source_port)
        self.assertEqual(_rule(self.by_name["forward"], 5).protocol, "tcp to 8222, tcp to 4222")

    def test_icmp_ip_any(self):
        self.assertEqual(_rule(self.by_name["service-vpn"], 2).protocol, "icmp 8")
        self.assertEqual(_rule(self.by_name["forward"], 10).protocol, "ip 47")
        self.assertEqual(_rule(self.by_name["forward"], 1).protocol, "any")

    def test_service_objects(self):
        r = _rule(self.by_name["local"], 1)
        self.assertEqual(r.protocol, "@SSH, @HTTPS (TCP)")
        self.assertEqual(self.dev.service_objects["SSH"], "tcp: to 22")
        self.assertEqual(self.dev.service_objects["HTTPS (TCP)"], "tcp: to 443")
        self.assertEqual(_rule(self.by_name["forward"], 7).protocol, "@HTTPS (TCP)")

    def test_service_names_wrapped_by_space_and_hyphen(self):
        r = _rule(self.by_name["forward"], 13)
        self.assertEqual(r.protocol,
                         "@SMTP Submission Service (TCP), @FTPS (data) lab-extftp01")


class TestEndpoints(_Base):
    def test_any_and_local(self):
        r = _rule(self.by_name["service-vpn"], 1)
        self.assertEqual(r.source.kind, "any")
        self.assertEqual(r.source.networks, [_net("0.0.0.0/0")])
        self.assertTrue(r.source.resolved)
        self.assertEqual(r.destination.kind, "local")
        self.assertFalse(r.destination.resolved)
        self.assertEqual(r.destination.networks, [])

    def test_host_list(self):
        r = _rule(self.by_name["forward"], 2)
        self.assertEqual(r.source.kind, "host")
        self.assertEqual(r.source.networks, [_net("10.200.3.7/32")])
        self.assertEqual(r.destination.kind, "host")
        self.assertEqual(r.destination.raw, "10.200.4.11, 10.200.4.12, 10.200.4.13")
        self.assertEqual([str(n) for n in r.destination.networks],
                         ["10.200.4.11/32", "10.200.4.12/32", "10.200.4.13/32"])

    def test_network_wrapped_mid_token(self):
        r = _rule(self.by_name["forward"], 5)
        self.assertEqual(r.source.kind, "network")
        self.assertEqual(r.source.raw, "10.200.7.0/255.255.255.0, 10.200.8.0/255.255.255.0")
        self.assertEqual(r.source.networks, [_net("10.200.7.0/24"), _net("10.200.8.0/24")])
        r = _rule(self.by_name["nat"], 2)
        self.assertEqual(r.source.networks, [_net("10.200.2.0/24")])

    def test_ranges(self):
        r = _rule(self.by_name["forward"], 4)
        self.assertEqual(r.source.kind, "range")
        self.assertEqual(r.source.raw, "10.200.5.10-10.200.5.18, 10.200.5.20-10.200.5.21")
        expected = list(ipaddress.summarize_address_range(
            ipaddress.IPv4Address("10.200.5.10"), ipaddress.IPv4Address("10.200.5.18")))
        expected += [_net("10.200.5.20/31")]
        self.assertEqual(r.source.networks, expected)
        # диапазон, разорванный по дефису, плюс хост → mixed
        self.assertEqual(r.destination.kind, "mixed")
        self.assertEqual(r.destination.raw, "10.200.6.10-10.200.6.12, 10.200.6.26")
        self.assertIn(_net("10.200.6.26/32"), r.destination.networks)
        self.assertIn(_net("10.200.6.12/32"), r.destination.networks)

    def test_object_group_resolved(self):
        r = _rule(self.by_name["forward"], 1)
        self.assertEqual(r.source.kind, "object-group")
        self.assertEqual(r.source.object_group, "BLOCK_LIST")
        self.assertTrue(r.source.resolved)
        self.assertIn(_net("203.0.113.248/32"), r.source.networks)  # жёсткий перенос «…24»+«8»
        self.assertIn(_net("203.0.113.100/30"), r.source.networks)
        self.assertNotIn("source_objects", r.extra)

    def test_unknown_object(self):
        r = _rule(self.by_name["forward"], 8)
        self.assertEqual(r.source.kind, "object-group")
        self.assertEqual(r.source.object_group, "Ghost")
        self.assertFalse(r.source.resolved)
        self.assertEqual(r.source.networks, [])
        self.assertIn("Ghost", self.dev.unresolved_object_groups)
        self.assertEqual(self.dev.unresolved_object_groups, {"Ghost"})

    def test_multi_object_destination(self):
        r = _rule(self.by_name["forward"], 6)
        d = r.destination
        self.assertEqual(d.kind, "object-group")
        self.assertEqual(d.raw, "@BLOCK_LIST, @DNS_BLOCK")
        self.assertEqual(d.object_group, "BLOCK_LIST")
        self.assertEqual(r.extra["destination_objects"], "BLOCK_LIST, DNS_BLOCK")
        self.assertIn(_net("203.0.113.250/32"), d.networks)
        self.assertIn(_net("203.0.113.1/32"), d.networks)
        self.assertTrue(d.resolved)

    def test_mixed_object_and_host(self):
        r = _rule(self.by_name["forward"], 7)
        self.assertEqual(r.source.kind, "mixed")
        self.assertEqual(r.source.raw, "@Inside, 192.0.2.7")
        self.assertEqual(r.source.object_group, "Inside")
        self.assertEqual(r.extra["source_objects"], "Inside")
        self.assertEqual(r.source.networks, [_net("10.200.0.0/16"), _net("192.0.2.7/32")])

    def test_object_name_wrapped_by_space(self):
        r = _rule(self.by_name["local"], 1)
        self.assertEqual(r.source.raw, "@Admins nets, 10.200.9.5")
        self.assertEqual(r.source.object_group, "Admins nets")
        self.assertIn(_net("10.200.9.0/24"), r.source.networks)
        self.assertIn(_net("10.200.9.5/32"), r.source.networks)
        self.assertTrue(r.source.resolved)

    def test_object_name_with_comma(self):
        r = _rule(self.by_name["forward"], 12)
        self.assertEqual(r.source.raw, "@lab-temp,sw-fw")
        self.assertEqual(r.source.object_group, "lab-temp,sw-fw")
        self.assertEqual(r.source.networks, [_net("10.200.99.0/24")])
        # вложенный объект: @Inside, @Dmz, @lab-temp,sw-fw
        self.assertEqual(r.destination.object_group, "AllInside")
        self.assertEqual(r.destination.networks,
                         [_net("10.200.0.0/16"), _net("192.0.2.0/24"), _net("10.200.99.0/24")])

    def test_fqdn_and_interface_destination(self):
        r = _rule(self.by_name["local"], 2)
        self.assertEqual(r.destination.kind, "unresolved")
        self.assertFalse(r.destination.resolved)
        self.assertEqual(r.destination.raw, "updates.example.net")
        self.assertEqual(r.extra["destination_fqdn"], "updates.example.net")
        r = _rule(self.by_name["service-local"], 1)
        self.assertEqual(r.destination.raw, "iface eth0")
        self.assertEqual(r.destination.kind, "unresolved")
        self.assertEqual(r.extra["destination_interface"], "eth0")


class TestNatAndDpi(_Base):
    def test_nat_changes(self):
        nat = self.by_name["nat"]
        r = _rule(nat, 1)
        self.assertEqual(r.destination.raw, "198.51.100.10")
        self.assertEqual(r.destination.networks, [_net("198.51.100.10/32")])
        self.assertEqual(r.extra["nat_destination_change"], "10.200.1.10")
        self.assertNotIn("nat_source_change", r.extra)
        r = _rule(nat, 2)
        self.assertEqual(r.extra["nat_source_change"], "198.51.100.20")
        self.assertEqual(r.source.raw, "10.200.2.0/255.255.255.0")
        r = _rule(nat, 3)
        self.assertEqual(r.extra["nat_port_change"], "DstPort: 21")
        self.assertEqual((r.protocol, r.destination_port), ("tcp", "to 6521"))
        self.assertEqual(r.extra["nat_destination_change"], "10.200.1.11")

    def test_dpi_row(self):
        fwd = self.by_name["forward"]
        self.assertEqual(_rule(fwd, 3).extra["dpi_protocol"], "Telnet")
        self.assertEqual(_rule(fwd, 7).extra["dpi_protocol"], "FTP")
        r = _rule(fwd, 1)
        for key in ("dpi_protocol", "dpi_group", "domain_user"):
            self.assertNotIn(key, r.extra)  # @any — умолчание, не показываем
        # строка DPI не должна утекать в адреса
        self.assertEqual(_rule(fwd, 2).destination.raw, "10.200.4.11, 10.200.4.12, 10.200.4.13")

    def test_domain_user_printed_one_line_above(self):
        r = _rule(self.by_name["forward"], 9)
        self.assertEqual(r.destination.raw, "@Inside, @Dmz")
        self.assertEqual(r.destination.kind, "object-group")
        self.assertNotIn("domain_user", r.extra)
        self.assertFalse([w for w in self.dev.warnings if "@any перечислен" in w])

    def test_no_dpi_outside_forward(self):
        for name in ("local", "nat", "service-local", "service-vpn"):
            for r in self.by_name[name].rules:
                self.assertNotIn("dpi_protocol", r.extra)


class TestObjects(_Base):
    def test_ip_objects(self):
        og = self.dev.object_groups
        self.assertEqual(og["PrivateNets"],
                         [_net("10.0.0.0/8"), _net("172.16.0.0/12"), _net("192.168.0.0/16")])
        self.assertEqual(og["Outside"], [_net("0.0.0.0/0")])
        self.assertEqual(self.dev.object_group_exclusions["Outside"],
                         [_net("10.0.0.0/8"), _net("172.16.0.0/12"), _net("192.168.0.0/16"),
                          _net("198.51.100.0/24")])
        self.assertNotIn("PrivateNets", self.dev.object_group_exclusions)
        self.assertEqual(og["lab-temp,sw-fw"], [_net("10.200.99.0/24")])
        self.assertEqual(og["Admins nets"],
                         [_net("10.200.9.0/24")] + list(ipaddress.summarize_address_range(
                             ipaddress.IPv4Address("192.0.2.100"), ipaddress.IPv4Address("192.0.2.110"))))
        self.assertEqual(len(og), 12)

    def test_fqdn_members_skipped_with_warning(self):
        self.assertEqual(self.dev.object_groups["DNS_BLOCK"], [_net("203.0.113.250/32")])
        self.assertEqual(self.dev.object_groups["only.fqdn.example"], [])
        self.assertTrue(any(w.startswith("Объект DNS_BLOCK: 3 значений") for w in self.dev.warnings),
                        self.dev.warnings)
        summary = [w for w in self.dev.warnings if "1 объектов состоят из одного" in w]
        self.assertEqual(len(summary), 1)
        self.assertIn("only.fqdn.example", summary[0])

    def test_cycle_guard(self):
        self.assertEqual(self.dev.object_groups["Loop_A"], [_net("10.200.50.1/32")])
        self.assertEqual(self.dev.object_groups["Loop_B"], [_net("10.200.50.1/32")])
        self.assertTrue(any("циклическая ссылка" in w for w in self.dev.warnings))

    def test_service_objects(self):
        so = self.dev.service_objects
        self.assertEqual(len(so), 7)
        self.assertEqual(so["MULTI"], "tcp: to 9443-9445, tcp: to 8005, tcp: to 8009")
        self.assertEqual(so["DHCP"], "udp: from 67-68 to 67-68")
        self.assertEqual(so["GRE"], "IP: 47")

    def test_parse_objects_helper(self):
        objs = vf.parse_objects(self.objects_text)
        self.assertEqual(objs.set_id, "4242")
        self.assertEqual(len(objs.ip), 12)
        self.assertEqual(len(objs.vpn), 0)
        self.assertEqual(len(objs.interface), 2)
        self.assertEqual(len(objs.service), 7)
        self.assertEqual(len(objs.schedule), 1)
        self.assertEqual(objs.interface["Internal"].inclusion,
                         ["iface bond0.101", "iface bond0.108", "iface bond0.109"])
        self.assertEqual(objs.schedule["Workdays"].inclusion,
                         ["weekly: 00:00:00 - 23:59:00 Mon Tue Wed Thu Fri"])
        self.assertEqual(objs.ip["Outside"].exclusion, ["@PrivateNets", "198.51.100.0/255.255.255.0"])
        self.assertEqual(objs.ip["lab-temp,sw-fw"].creation, "User")
        self.assertIs(objs.lookup("ip", "Adminsnets"), objs.ip["Admins nets"])
        self.assertIsNone(objs.lookup("ip", "Nope"))


class TestIpSearch(_Base):
    """Сети должны быть пригодны для поиска «какие правила касаются IP»."""

    def _hits(self, ip):
        addr = ipaddress.ip_address(ip)
        hits = []
        for acl in self.dev.acls:
            for r in acl.rules:
                for side in (r.source, r.destination):
                    if side and any(addr in n for n in side.networks if n.prefixlen > 0):
                        hits.append((acl.name, r.label))
                        break
        return hits

    def test_search(self):
        self.assertIn(("forward", "5000102 - DB_PORTS"), self._hits("10.200.4.12"))
        self.assertIn(("forward", "5000101 - BLOCK_LIST_IN"), self._hits("203.0.113.248"))
        self.assertIn(("forward", "5000106 - MULTI_OBJ"), self._hits("203.0.113.250"))
        self.assertIn(("forward", "5000104 - RANGES"), self._hits("10.200.5.15"))
        self.assertNotIn(("forward", "5000104 - RANGES"), self._hits("10.200.5.19"))
        self.assertIn(("local", "5000010 - MGMT_SSH"), self._hits("192.0.2.105"))


class TestRobustness(unittest.TestCase):
    def setUp(self):
        self.rules_text = (FIXTURES / "vipnet_firewall_rules.txt").read_text()
        self.objects_text = (FIXTURES / "vipnet_firewall_objects.txt").read_text()

    def test_terminal_noise_is_stripped(self):
        noisy = "\x1b[?1h\x1b=" + self.rules_text.replace("\n", "\r\n") + "\x1b[0m\r\nlab-xfw1> "
        noisy_obj = "\x1b[?1h\x1b=" + self.objects_text.replace("\n", "\r\n") + "lab-xfw1> "
        dev = vf.parse_snapshot({"firewall_rules": noisy, "firewall_objects": noisy_obj}, "x", sections=None)
        self.assertEqual({a.name: len(a.rules) for a in dev.acls},
                         {"forward": 13, "local": 2, "nat": 3, "service-local": 1, "service-vpn": 2})
        self.assertEqual(len(dev.object_groups), 12)
        self.assertEqual(vf.rules_set_id(noisy), "4242")

    def test_missing_objects_snapshot(self):
        dev = vf.parse_snapshot({"firewall_rules": self.rules_text}, "x", sections=None)
        self.assertEqual(len(dev.acls), 5)
        self.assertEqual(dev.object_groups, {})
        self.assertTrue(any("firewall object show отсутствует" in w for w in dev.warnings))
        fwd = dev.acl_by_name("forward")
        r = _rule(fwd, 1)
        self.assertEqual(r.source.kind, "object-group")
        self.assertFalse(r.source.resolved)
        self.assertIn("BLOCK_LIST", dev.unresolved_object_groups)
        # имя с запятой без таблицы объектов склеить не по чему — две ссылки
        self.assertIn("lab-temp", dev.unresolved_object_groups)

    def test_missing_rules_snapshot(self):
        dev = vf.parse_snapshot({"firewall_objects": self.objects_text}, "x", sections=None)
        self.assertEqual(dev.acls, [])
        self.assertEqual(len(dev.object_groups), 12)
        self.assertTrue(any("firewall rules show отсутствует" in w for w in dev.warnings))

    def test_empty_input(self):
        dev = vf.parse_snapshot({}, "x", sections=None)
        self.assertEqual(dev.acls, [])
        self.assertEqual(dev.parser, "xfw")
        self.assertEqual(len(dev.warnings), 2)

    def test_parse_rules_helper_without_objects(self):
        acls = vf.parse_rules(self.rules_text, sections=None)
        self.assertEqual([a.name for a in acls],
                         ["local", "forward", "nat", "service-local", "service-vpn"])
        fwd = next(a for a in acls if a.name == "forward")
        self.assertEqual(fwd.description, "Forward Rules, Rules Set Id 4242")

    def test_set_id_mismatch_warning(self):
        objs = self.objects_text.replace("Current Rules Set Id: 4242", "Current Rules Set Id: 4243")
        dev = vf.parse_snapshot({"firewall_rules": self.rules_text, "firewall_objects": objs}, "x", sections=None)
        self.assertTrue(any("Rules Set Id снимков не совпадает" in w for w in dev.warnings))

    def test_unparsed_value_is_reported_not_dropped(self):
        broken = self.rules_text.replace("->192.0.2.2", "->something??")
        dev = vf.parse_snapshot({"firewall_rules": broken, "firewall_objects": self.objects_text}, "x", sections=None)
        r = _rule(dev.acl_by_name("forward"), 10)
        self.assertEqual(r.destination.kind, "unresolved")
        self.assertEqual(r.destination.raw, "something??")
        self.assertEqual(r.extra["unparsed"], "destination: something??")
        self.assertTrue(any("Forward #10" in w and "something??" in w for w in dev.warnings))


if __name__ == "__main__":
    unittest.main()


class DefaultSectionsTests(unittest.TestCase):
    """По умолчанию на страницу ACL идут только local / forward / nat; ID и
    имя правила — отдельные поля."""

    @classmethod
    def setUpClass(cls):
        cls.dev = vf.parse_snapshot(
            {"firewall_rules": (FIXTURES / "vipnet_firewall_rules.txt").read_text(),
             "firewall_objects": (FIXTURES / "vipnet_firewall_objects.txt").read_text()}, "fw")

    def test_only_default_sections_in_order(self):
        self.assertEqual([a.name for a in self.dev.acls], ["local", "forward", "nat"])
        self.assertEqual({a.acl_name for a in self.dev.applications}, {"local", "forward", "nat"})

    def test_rule_id_and_name_split(self):
        fwd = next(a for a in self.dev.acls if a.name == "forward")
        r = fwd.rules[0]
        self.assertEqual((r.rule_id, r.name, r.label), ("5000101", "BLOCK_LIST_IN", "5000101 - BLOCK_LIST_IN"))
        self.assertEqual(r.sequence, 1)


class ObjectGroupsListTests(unittest.TestCase):
    def test_mixed_endpoint_lists_all_objects(self):
        dev = vf.parse_snapshot(
            {"firewall_rules": (FIXTURES / "vipnet_firewall_rules.txt").read_text(),
             "firewall_objects": (FIXTURES / "vipnet_firewall_objects.txt").read_text()}, "fw",
            sections=None)
        mixed = [ep for a in dev.acls for r in a.rules for ep in (r.source, r.destination)
                 if ep is not None and ep.kind == "mixed" and ep.object_groups]
        self.assertTrue(mixed, "в фикстуре должен быть смешанный операнд с объектом")
        for ep in mixed:
            self.assertEqual(ep.object_group, ep.object_groups[0])
            for name in ep.object_groups:
                self.assertIn("@" + name.split()[0], ep.raw)
        # kind «object-group» бывает и у операнда из нескольких объектов —
        # первый из них в object_group, все — в object_groups
        single = [ep for a in dev.acls for r in a.rules for ep in (r.source, r.destination)
                  if ep is not None and ep.kind == "object-group"]
        self.assertTrue(single)
        self.assertTrue(all(ep.object_groups and ep.object_groups[0] == ep.object_group for ep in single))

    def test_exact_name_beats_space_insensitive_match(self):
        """Два объекта, различающиеся ведущим пробелом, — разные объекты
        устройства; «@a-b» не должен уходить к « a-b»."""
        objects = (
            "Ip Objects\nCurrent Rules Set Id: 7\n"
            "=============================================================================\n"
            "Num Name                                             Creation type          \n"
            "Inclusion                             Exclusion                              \n"
            "=============================================================================\n"
            "1    net-x                                           User                   \n"
            "10.200.1.0/255.255.255.0                                                     \n"
            "-----------------------------------------------------------------------------\n"
            "2   net-x                                            User                   \n"
            "10.200.2.0/255.255.255.0                                                     \n"
            "-----------------------------------------------------------------------------\n"
        )
        rules = (
            " Forward Rules:\nCurrent Rules Set Id: 7\n"
            "======================================================================================\n"
            "Num   Rule ID-Name                                             Option  Schedule       \n"
            "Act   Protocol               Source                          ->Destination            \n"
            "      DpiProtocol            [G]DpiGroup, DpiApp               DomainUser             \n"
            "======================================================================================\n"
            "1     5000001 - A                                              User                   \n"
            "pass  @any                   @net-x                          ->@ net-x                \n"
            "      @any                   @any                              @any                   \n"
            "--------------------------------------------------------------------------------------\n"
        )
        dev = vf.parse_snapshot({"firewall_rules": rules, "firewall_objects": objects}, "fw")
        r = dev.acls[0].rules[0]
        self.assertEqual(r.source.object_groups, ["net-x"])
        self.assertEqual(r.destination.object_groups, [" net-x"])
        self.assertEqual([str(n) for n in r.source.networks], ["10.200.2.0/24"])
        self.assertEqual([str(n) for n in r.destination.networks], ["10.200.1.0/24"])


class TestHwFormat(unittest.TestCase):
    """Координатор HW5000: без Rule ID, стрелка «> », префикс «(inactive)»,
    секции Tunnel и Vpn, VPN-объекты из идентификаторов узлов, кириллические
    ссылки мождибейком (UTF-8, показанный как KOI8-R)."""

    @classmethod
    def setUpClass(cls):
        cls.rules_text = (FIXTURES / "vipnet_hw_rules.txt").read_text(encoding="utf-8")
        cls.objects_text = (FIXTURES / "vipnet_hw_objects.txt").read_text(encoding="utf-8")
        cls.iplir_text = (FIXTURES / "vipnet_hw_iplir.txt").read_text(encoding="utf-8")
        cls.files = {"firewall_rules": cls.rules_text, "firewall_objects": cls.objects_text,
                     "iplir_config": cls.iplir_text}
        cls.dev = vf.parse_snapshot(cls.files, "lab-hw1", sections=None)
        cls.by_name = {a.name: a for a in cls.dev.acls}

    def test_sections_default_and_all(self):
        self.assertEqual({a.name: len(a.rules) for a in self.dev.acls},
                         {"local": 2, "forward": 1, "tunnel": 6, "vpn": 2,
                          "service-local": 1, "service-vpn": 1})
        self.assertEqual([a.name for a in self.dev.acls],
                         ["local", "forward", "tunnel", "vpn", "service-local", "service-vpn"])
        default = vf.parse_snapshot(self.files, "lab-hw1")
        self.assertEqual([a.name for a in default.acls], ["local", "forward", "tunnel", "vpn"])
        self.assertEqual(vf.DEFAULT_SECTIONS, ("local", "forward", "tunnel", "vpn", "nat"))
        self.assertIn(("tunnel", "section", "Tunnel Rules"),
                      {(a.acl_name, a.kind, a.location) for a in self.dev.applications})
        self.assertIsNone(vf.rules_set_id(self.rules_text))
        self.assertEqual(self.by_name["tunnel"].description, "Tunnel Rules")

    def test_only_unknown_node_warning(self):
        # единственное предупреждение — про id, которого нет в iplir
        self.assertEqual(self.dev.warnings,
                         ["Объект Analysts: 1 идентификаторов узлов нет в iplir: 0x1a2b007c"])
        self.assertEqual(self.dev.unresolved_object_groups, set())

    def test_name_without_rule_id(self):
        r = _rule(self.by_name["tunnel"], 1)
        self.assertIsNone(r.rule_id)
        self.assertEqual(r.name, "To all tunnel nodes")
        self.assertEqual(r.label, "To all tunnel nodes")
        self.assertTrue(r.inactive)
        r = _rule(self.by_name["tunnel"], 2)
        self.assertEqual((r.rule_id, r.name, r.label, r.inactive),
                         (None, "Analysts_access", "Analysts_access", False))
        r = _rule(self.by_name["tunnel"], 6)
        self.assertTrue(r.inactive)
        self.assertEqual(r.name, "Группа_тест")
        self.assertEqual(r.schedule, "Workdays")
        self.assertEqual(r.extra["option"], "User")

    def test_arrow_and_wrapped_destination(self):
        r = _rule(self.by_name["tunnel"], 2)
        self.assertEqual(r.source.raw, "@Analysts")
        self.assertEqual(r.destination.raw, "10.200.1.0/255.255.255.0, 10.200.2.0/255.255.255.0")
        self.assertEqual(r.destination.networks, [_net("10.200.1.0/24"), _net("10.200.2.0/24")])
        r = _rule(self.by_name["vpn"], 2)
        self.assertEqual((r.protocol, r.destination_port), ("tcp/udp", "to 53"))
        self.assertEqual(_rule(self.by_name["vpn"], 1).protocol, "icmp 5")
        r = _rule(self.by_name["service-local"], 1)
        self.assertEqual(r.destination.raw, "iface eth0")
        self.assertEqual(r.extra["destination_interface"], "eth0")

    def test_tunneledip_builtin(self):
        r = _rule(self.by_name["tunnel"], 1)
        self.assertEqual(r.destination.kind, "unresolved")
        self.assertFalse(r.destination.resolved)
        self.assertEqual(r.destination.networks, [])
        self.assertNotIn("tunneledip", self.dev.unresolved_object_groups)

    def test_vpn_node_ids_in_operand(self):
        r = _rule(self.by_name["tunnel"], 3)
        self.assertEqual(r.source.raw, "0x1a2b0060, 0x1a2b005e")
        # известный узел даёт host-сети, неизвестный — unresolved → mixed
        self.assertEqual(r.source.kind, "mixed")
        self.assertFalse(r.source.resolved)
        self.assertEqual(
            r.extra["source_vpn_nodes"],
            "0x1a2b0060 remote-hw VPN-9999 (203.0.113.60 → 11.0.9.60, туннели: 2); "
            "0x1a2b005e (нет в iplir)")
        for ip in ("203.0.113.60", "11.0.9.60", "172.16.0.24", "11.0.9.167", "172.16.0.133", "11.0.9.168"):
            self.assertIn(_net(f"{ip}/32"), r.source.networks)
        self.assertNotIn("unparsed", r.extra)
        self.assertEqual(r.protocol, "tcp to 443, tcp to 80")
        self.assertEqual(r.destination.networks, [_net("10.200.1.10/32")])
        # операнд целиком из известных узлов — разрешён
        r = _rule(self.by_name["tunnel"], 2)
        self.assertEqual(r.source.object_group, "Analysts")
        self.assertIn(_net("10.200.61.38/32"), r.source.networks)
        self.assertTrue(r.source.resolved)

    def test_mojibake_object_refs(self):
        r = _rule(self.by_name["tunnel"], 4)
        self.assertEqual(r.label, "Блок_TELNET")
        self.assertEqual(r.source.raw, "@Блок_Telnet")
        self.assertEqual(r.source.object_group, "Блок_Telnet")
        self.assertTrue(r.source.resolved)
        self.assertEqual(r.source.networks, [_net("0.0.0.0/0")])
        self.assertEqual(r.destination.kind, "any")
        self.assertEqual(r.protocol, "@Telnet")
        r = _rule(self.by_name["tunnel"], 5)
        self.assertEqual(r.source.object_group, "Сотрудники_Лаб")
        self.assertEqual([str(n) for n in r.source.networks],
                         ["10.200.62.70/32", "11.0.9.70/32", "10.200.62.72/32", "11.0.9.72/32"])
        self.assertTrue(r.source.resolved)
        self.assertEqual(r.destination.object_group, "Серверы_Лаб")
        self.assertEqual([str(n) for n in r.destination.networks],
                         ["10.200.4.71/32", "10.200.4.87/32", "10.200.4.88/32", "10.200.4.28/32"])

    def test_vpn_objects_resolved_through_iplir(self):
        objs = vf.parse_objects(self.objects_text)
        self.assertEqual(len(objs.ip), 4)
        self.assertEqual(len(objs.vpn), 4)
        self.assertEqual(objs.vpn["operators"].inclusion,
                         ["0x1a2b0022", "0x1a2b0086", "0x1a2b0017", "0x1a2b0019"])
        self.assertEqual(objs.ip["Серверы_Лаб"].creation, "User")
        og = self.dev.object_groups
        ops = og["operators"]
        # одиночный ip= + virtualip; координатор с ip= и туннелями; пара
        # «реальный, виртуальный»; узел без ip= — только virtualip
        for ip in ("10.200.60.11", "11.0.9.11", "172.30.30.1", "172.31.0.1", "198.51.100.5",
                   "11.0.9.1", "10.200.60.12", "11.0.9.12", "11.0.9.19"):
            self.assertIn(_net(f"{ip}/32"), ops, ip)
        self.assertIn(_net("10.200.101.128/26"), ops)        # tunnel= 10.200.101.1-254 → summarize
        self.assertIn(_net("10.200.100.56/30"), ops)         # tunnel= 10.200.100.1-60
        self.assertTrue(any(ipaddress.ip_address("10.200.101.200") in n for n in ops))
        self.assertFalse(any(ipaddress.ip_address("10.200.100.61") in n for n in ops))
        self.assertEqual(len(ops), len(set(ops)))
        self.assertEqual([str(n) for n in og["Analysts"]],
                         ["10.200.61.38/32", "11.0.9.38/32", "10.200.61.109/32", "11.0.9.109/32"])
        members = self.dev.object_group_members
        self.assertEqual(members["operators"][0], "0x1a2b0022 operator.one VPN-4242 (10.200.60.11)")
        self.assertEqual(members["operators"][1],
                         "0x1a2b0086 lab-hw01 VPN-4242 (172.30.30.1; 172.31.0.1; 198.51.100.5, туннели: 2)")
        self.assertEqual(members["operators"][2], "0x1a2b0017 operator.two VPN-4242 (10.200.60.12 → 11.0.9.12)")
        self.assertEqual(members["operators"][3],
                         "0x1a2b0019 АП Тестов Т.Т. (лаборатория) VPN-7777 (virt 11.0.9.19)")
        self.assertEqual(members["Analysts"][2], "0x1a2b007c (нет в iplir)")
        self.assertEqual(members["Блок_Telnet"], ["@any"])
        self.assertEqual(members["Admins"], ["10.200.9.0/255.255.255.0"])
        self.assertEqual(og["Блок_Telnet"], [_net("0.0.0.0/0")])
        # исключение @operators (VPN-объект) тоже разрешается через iplir
        self.assertEqual(self.dev.object_group_exclusions["Блок_Telnet"], self.dev.object_groups["operators"])
        self.assertEqual(self.dev.object_group_exclusions["InternetIP"],
                         [_net("10.0.0.0/8"), _net("172.16.0.0/12"), _net("192.168.0.0/16")])
        self.assertEqual(len(og), 8)
        self.assertEqual(self.dev.service_objects["Telnet"], "tcp: to 23")
        self.assertEqual(objs.schedule["Workdays"].inclusion,
                         ["weekly: 00:00:00 - 23:59:00 Mon Tue Wed Thu Fri"])

    def test_parse_iplir(self):
        nodes = vf.parse_iplir(self.iplir_text)
        self.assertEqual(len(nodes), 9)               # секции [adapter]/[misc]/[dynamic] не узлы
        n = nodes["0x1a2b0019"]
        self.assertEqual((n.name, n.network), ("АП Тестов Т.Т. (лаборатория)", "VPN-7777"))
        self.assertEqual(n.ips, [])
        self.assertEqual((n.virtualip, n.accessip), ("11.0.9.19", "11.0.9.19"))
        self.assertEqual(n.networks(), [_net("11.0.9.19/32")])
        n = nodes["0x1a2b0017"]
        self.assertEqual(n.ips, [("10.200.60.12", "11.0.9.12")])
        self.assertEqual(n.networks(), [_net("10.200.60.12/32"), _net("11.0.9.12/32")])
        n = nodes["0x1a2b0086"]
        self.assertEqual(n.ips, [("172.30.30.1", None), ("172.31.0.1", None), ("198.51.100.5", None)])
        self.assertEqual(n.tunnels, ["10.200.100.1-10.200.100.60", "10.200.101.1-10.200.101.254"])
        n = nodes["0x1a2b0060"]
        self.assertEqual(n.tunnels[0], "172.16.0.24-172.16.0.24 to 11.0.9.167-11.0.9.167")
        self.assertIn(_net("172.16.0.24/32"), n.networks())   # обе стороны «to»
        self.assertIn(_net("11.0.9.167/32"), n.networks())
        # мусор терминала и \r, регистр id
        noisy = "\x1b[?1h" + self.iplir_text.replace("\n", "\r\n").replace("0x1a2b0022", "0X1A2B0022") + "lab-hw1> "
        nodes2 = vf.parse_iplir(noisy)
        self.assertEqual(set(nodes2), set(nodes))
        self.assertEqual(nodes2["0x1a2b0022"].name, "operator.one")

    def test_without_iplir(self):
        files = {k: v for k, v in self.files.items() if k != "iplir_config"}
        dev = vf.parse_snapshot(files, "lab-hw1", sections=None)
        self.assertEqual(dev.warnings, ["iplir_config отсутствует: узлы VPN-объектов не разрешены"])
        self.assertEqual(dev.object_groups["operators"], [])
        self.assertEqual(dev.object_group_members["operators"],
                         ["0x1a2b0022", "0x1a2b0086", "0x1a2b0017", "0x1a2b0019"])
        r = _rule(dev.acl_by_name("tunnel"), 3)
        self.assertEqual(r.source.kind, "unresolved")
        self.assertFalse(r.source.resolved)
        self.assertEqual(r.extra["source_vpn_nodes"], "0x1a2b0060; 0x1a2b005e")
        # xFW-снимок без VPN-объектов и без id узлов предупреждения про iplir не получает
        xfw = vf.parse_snapshot({"firewall_rules": (FIXTURES / "vipnet_firewall_rules.txt").read_text(),
                                 "firewall_objects": (FIXTURES / "vipnet_firewall_objects.txt").read_text()}, "x")
        self.assertFalse([w for w in xfw.warnings if "iplir" in w])

    def test_vipnet_network_id_is_not_a_node(self):
        objects = self.objects_text.replace("0x1a2b0070, 0x1a2b0072", "0x06e1, 0x1a2b0070")
        dev = vf.parse_snapshot(dict(self.files, firewall_objects=objects), "lab-hw1", sections=None)
        self.assertEqual(dev.object_group_members["Сотрудники_Лаб"],
                         ["0x06e1 (сеть ViPNet)", "0x1a2b0070 staff.one VPN-4242 (10.200.62.70)"])
        self.assertEqual(dev.object_groups["Сотрудники_Лаб"], [_net("10.200.62.70/32"), _net("11.0.9.70/32")])
        self.assertFalse([w for w in dev.warnings if "Сотрудники_Лаб" in w])

    def test_fix_mojibake_helper(self):
        self.assertEqual(vf._fix_mojibake("@Блок_Telnet".encode("utf-8").decode("koi8-r")), "@Блок_Telnet")
        self.assertEqual(vf._fix_mojibake("@any, 10.200.1.1"), "@any, 10.200.1.1")
        self.assertEqual(vf._fix_mojibake("Имя без мождибейка"), "Имя без мождибейка")
