"""acl_search: категории совпадения, цепочка «почему совпало», широкие
наборы и any, поиск по подсети, каталог, список с фрагментами и свёртками,
панель группы — на синтетическом устройстве ViPNet."""
import ipaddress
from datetime import datetime
from unittest import mock

from django.test import SimpleTestCase

from ip_plan.services import acl_search, acl_store
from ip_plan.services.acl_model import Acl, AclRule, Application, Endpoint, ParsedDevice


def N(s):
    return ipaddress.ip_network(s)


def _xfw():
    d = ParsedDevice(device_name="xfw-test", parser="xfw")
    d.object_groups = {
        "mdc-lan": [N("10.1.141.0/24")],
        "MDC networks": [N("10.1.141.0/24"), N("10.2.0.0/16")],
        "PrivateNetworkIP": [N("10.0.0.0/8"), N("172.16.0.0/12"), N("192.168.0.0/16")],
        "Excl": [N("10.1.141.0/24")],
    }
    d.object_group_includes = {"MDC networks": ["mdc-lan"]}
    d.object_group_exclusions = {"Excl": [N("10.1.141.166/32")]}
    any_ep = Endpoint(raw="@any", kind="any", networks=[N("0.0.0.0/0")])

    def obj(name):
        return Endpoint(raw=f"@{name}", kind="object-group", networks=list(d.object_groups[name]),
                        object_group=name, object_groups=[name])
    rules = [
        AclRule(1, "permit", protocol="@SSH (TCP), @HTTPS (TCP), @DNS (UDP)", rule_id="4000161", name="ACCESS",
                action_raw="pass", source=Endpoint(raw="10.1.141.166", kind="host", networks=[N("10.1.141.166/32")]),
                destination=any_ep),
        AclRule(2, "permit", protocol="any", rule_id="4000013", name="VPN Admins", action_raw="pass",
                source=Endpoint(raw="172.31.255.19", kind="host", networks=[N("172.31.255.19/32")]),
                destination=obj("MDC networks")),
        AclRule(3, "permit", protocol="any", rule_id="4000307", name="TO_RDC", action_raw="pass",
                source=Endpoint(raw="10.1.0.0/16", kind="network", networks=[N("10.1.0.0/16")]), destination=any_ep),
        AclRule(4, "deny", protocol="@Telnet (TCP)", rule_id="6", name="Запрет_TELNET", action_raw="drop",
                source=obj("PrivateNetworkIP"), destination=any_ep),
        AclRule(5, "deny", protocol="any", rule_id="99", name="DROP_ALL", action_raw="drop",
                source=any_ep, destination=any_ep),
        AclRule(6, "permit", protocol="any", rule_id="4000500", name="MIXED", action_raw="pass",
                source=Endpoint(raw="@MDC networks, 172.31.1.1", kind="mixed",
                                networks=[N("10.1.141.0/24"), N("10.2.0.0/16"), N("172.31.1.1/32")],
                                object_group="MDC networks", object_groups=["MDC networks"]),
                destination=any_ep),
        AclRule(7, "permit", protocol="any", rule_id="4000600", name="EXCLUDED", action_raw="pass",
                source=obj("Excl"), destination=any_ep),
        AclRule(8, "remark", remark="комментарий"),
    ]
    d.acls = [Acl(name="forward", acl_type="forward", rules=rules)]
    d.applications = [Application(acl_name="forward", kind="section", location="Forward Rules")]
    return d


SNAP = [{"device": "xfw-test", "model": "ViPNet xFW 5000", "site": "mdc", "acl_count": 1, "rule_count": 8,
         "updated": datetime(2026, 9, 28, 3, 0), "changed": None, "rules_set_id": None, "files": [],
         "parser": "xfw", "warnings": []}]


class SearchTests(SimpleTestCase):
    def setUp(self):
        self.dev = _xfw()
        patches = [
            mock.patch.object(acl_store, "get_all_devices", return_value=[self.dev]),
            mock.patch.object(acl_store, "get_device", side_effect=lambda n: self.dev if n == "xfw-test" else None),
            mock.patch.object(acl_store, "snapshot_info", return_value=SNAP),
            mock.patch.object(acl_store, "get_meta", return_value={"model": "ViPNet xFW 5000"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_parse_query(self):
        net, is_net = acl_search.parse_query(" 10.1.141.166 ")
        self.assertEqual((str(net), is_net), ("10.1.141.166/32", False))
        net, is_net = acl_search.parse_query("10.1.141.5/24")
        self.assertEqual((str(net), is_net), ("10.1.141.0/24", True))
        for bad in ("abc", "", "2001:db8::1"):
            with self.assertRaises(acl_search.QueryError):
                acl_search.parse_query(bad)

    def test_categories_and_buckets(self):
        r = acl_search.search("10.1.141.166")
        by_id = {h["rule_id"]: h for g in r["groups"] for h in g["hits"]}
        self.assertEqual(set(by_id), {"4000161", "4000013", "4000307", "4000500"})
        self.assertEqual(by_id["4000161"]["category"], "exact")
        self.assertEqual(by_id["4000161"]["role"], "src")
        self.assertEqual(by_id["4000161"]["via_text"], "указан явно")
        self.assertEqual(by_id["4000307"]["category"], "net")
        self.assertEqual(by_id["4000307"]["via"], "10.1.0.0/16")
        self.assertEqual(by_id["4000013"]["category"], "obj")
        self.assertEqual(by_id["4000013"]["role"], "dst")
        self.assertEqual(by_id["4000013"]["via"], "@MDC networks")
        self.assertEqual(by_id["4000500"]["category"], "obj", "объект внутри смешанного операнда")
        # сортировка внутри группы: точные выше
        cats = [h["category"] for h in r["groups"][0]["hits"]]
        self.assertEqual(cats, sorted(cats, key=acl_search.CATEGORY_RANK.get))
        # широкие и any — отдельно, явный запрет через 10/8 не считается
        self.assertEqual(r["broad_count"], 1)
        self.assertEqual(r["broad"]["objects"][0], {"name": "@PrivateNetworkIP", "network": "10.0.0.0/8", "count": 1})
        # any: DROP_ALL и EXCLUDED (источник исключён, но назначение any подходит кому угодно)
        self.assertEqual(r["any_count"], 2)
        self.assertEqual(r["explicit_denies"], 0)
        self.assertEqual(r["total_rules"], 4)
        self.assertEqual(r["total_devices"], 1)
        self.assertEqual(r["cat_counts"]["all"]["any"], 4)
        self.assertEqual(r["cat_counts"]["obj"]["dst"], 1)
        self.assertEqual(r["cat_counts"]["exact"]["src"], 1)
        self.assertFalse(r["empty"])
        # исключение объекта снимает совпадение
        self.assertNotIn("4000600", by_id)

    def test_chain_through_nested_object(self):
        r = acl_search.search("10.1.141.166")
        hit = next(h for g in r["groups"] for h in g["hits"] if h["rule_id"] == "4000013")
        self.assertEqual([c["value"] for c in hit["chain"]],
                         ["10.1.141.166", "10.1.141.0/24", "@mdc-lan", "@MDC networks"])
        self.assertEqual(hit["chain"][-1]["note"], "2 записи")
        self.assertEqual(hit["object"]["name"], "@MDC networks")
        self.assertIn("/panel/", hit["object"]["panel_url"])
        self.assertTrue(hit["dst"]["tokens"][0]["hit"])
        self.assertEqual(hit["other_short"], "172.31.255.19")

    def test_service_short(self):
        r = acl_search.search("10.1.141.166")
        hit = next(h for g in r["groups"] for h in g["hits"] if h["rule_id"] == "4000161")
        self.assertEqual(hit["service_short"], "SSH, HTTPS +1")
        self.assertIn("@SSH (TCP)", hit["service_full"])

    def test_subnet_query(self):
        r = acl_search.search("10.1.141.0/24")
        by_id = {h["rule_id"]: h for g in r["groups"] for h in g["hits"]}
        self.assertEqual(by_id["4000161"]["category"], "exact", "хост внутри искомой подсети")
        self.assertEqual(by_id["4000013"]["category"], "obj")
        self.assertEqual(by_id["4000307"]["category"], "net")
        self.assertTrue(r["is_network"])

    def test_selected_devices_and_empty(self):
        r = acl_search.search("10.1.141.166", selected=["other"])
        self.assertTrue(r["empty"])
        r = acl_search.search("8.8.8.8")
        self.assertEqual(r["total_rules"], 0)
        self.assertEqual(r["any_count"], 6, "все правила с any в назначении подходят чужому адресу")
        self.assertFalse(r["empty"], "any-правила всё же есть")

    def test_catalog_and_question(self):
        with mock.patch.object(acl_store, "overview", return_value=[{
            "device": "xfw-test", "acl_name": "forward", "acl_type": "forward", "rule_count": 7,
            "inactive_count": 0, "applied": True, "applications": self.dev.applications}]), \
             mock.patch.object(acl_store, "list_object_groups", return_value=[
                 {"device": "xfw-test", "name": "MDC networks", "member_count": 2, "exclusion_count": 0,
                  "used_by": ["forward"], "included_in": []},
                 {"device": "xfw-test", "name": "Old", "member_count": 1, "exclusion_count": 0, "used_by": [], "included_in": []}]):
            c = acl_search.catalog()
        self.assertEqual(c["lists"][0]["type_label"], "Forward")
        self.assertEqual(c["lists"][0]["purpose"], "транзитный трафик")
        self.assertEqual(c["lists"][0]["where_text"], "применён")
        self.assertEqual([g["used_text"] for g in c["groups"]], ["в forward", "не используется"])
        q = acl_search.question(None, "", [], True, {"mdc": "ЦОД"})
        self.assertEqual(q["sites"][0]["label"], "ЦОД")
        self.assertEqual(q["button_label"], "Все устройства")
        self.assertEqual(q["button_count"], 1)
        q = acl_search.question(None, "1.1.1.1", ["xfw-test", "ghost"], False, {})
        self.assertEqual(q["selected"], ["xfw-test"])
        self.assertEqual(q["button_label"], "xfw-test")

    def test_snapshot_summary(self):
        s = acl_search.snapshot_summary()
        self.assertEqual(s["text"], "Снимки конфигураций — 28.09 03:00")

    def test_listing_fragments_and_collapse(self):
        rows = [{"type": "rule", "is_match": i in (0, 6)} if i != 3 else {"type": "remark"} for i in range(9)]
        out = acl_search._collapse(rows)
        self.assertEqual([r["type"] for r in out], ["rule", "collapsed", "rule", "rule", "rule"])
        self.assertEqual(out[1]["count"], 4)
        lst = acl_search.listing("xfw-test", "forward", "10.1.141.166")
        # широкий набор (@PrivateNetworkIP, 10/8) совпадением в списке не считается — 4
        self.assertEqual(lst["match_count"], 4)
        broad = next(r for r in lst["rows"] if r["type"] == "rule" and r["rule_id"] == "6") if any(
            r["type"] == "rule" and r["rule_id"] == "6" for r in lst["rows"]) else None
        if broad is not None:
            self.assertFalse(broad["is_match"]); self.assertEqual(broad["match_text"], "источник · входит в /8")
        rule = next(r for r in lst["rows"] if r["type"] == "rule" and r["rule_id"] == "4000013")
        self.assertEqual(rule["match_text"], "назначение · через группу")
        grp = next(f for f in rule["fragments"] if f["kind"] == "group")
        self.assertEqual((grp["text"], grp["hit"], grp["group"]), ("@MDC networks", True, "MDC networks"))
        self.assertEqual(lst["siblings"][0]["active"], True)
        self.assertIsNone(acl_search.listing("xfw-test", "nope"))
        self.assertEqual(acl_search.listing("xfw-test", "forward")["match_count"], 0)

    def test_group_panel(self):
        with mock.patch.object(acl_store, "get_object_group", return_value={
            "device": "xfw-test", "name": "MDC networks", "networks": self.dev.object_groups["MDC networks"],
            "exclusions": [], "used_by": ["forward"], "included_in": [], "includes": ["mdc-lan"], "members": []}):
            p = acl_search.group_panel("xfw-test", "MDC networks", "10.2.5.5")
        self.assertEqual(p["members"][0]["text"], "10.2.0.0/16")
        self.assertTrue(p["members"][0]["contains"])
        self.assertEqual(p["members"][-1]["kind"], "group")
        self.assertEqual(p["used_text"], "используется в forward")


class RuleNodesTests(SimpleTestCase):
    """Узлы ViPNet прямо в операнде сворачиваются в чип и открываются панелью."""

    def setUp(self):
        self.dev = ParsedDevice(device_name="hw-test", parser="xfw")
        src = Endpoint(raw="0x4f260001, 0x4f260002, 10.9.9.9", kind="mixed",
                       networks=[N("192.168.1.1/32"), N("11.0.0.1/32"), N("11.0.0.2/32"), N("10.9.9.9/32")])
        rule = AclRule(11, "permit", protocol="any", rule_id="11", name="NODES", action_raw="pass", source=src,
                       destination=Endpoint(raw="@any", kind="any", networks=[N("0.0.0.0/0")]),
                       extra={"source_vpn_nodes": "0x4f260001 Иванов И. И. VPN-1 (192.168.1.1 → 11.0.0.1); "
                                                  "0x4f260002 Петров П. П. VPN-1 (virt 11.0.0.2)",
                              "source_node_nets": "0x4f260001: 192.168.1.1/32, 11.0.0.1/32; 0x4f260002: 11.0.0.2/32"})
        self.dev.acls = [Acl(name="tunnel", acl_type="tunnel", rules=[rule])]
        self.dev.applications = [Application(acl_name="tunnel", kind="section", location="Tunnel Rules")]
        for p in (mock.patch.object(acl_store, "get_all_devices", return_value=[self.dev]),
                  mock.patch.object(acl_store, "get_device", return_value=self.dev),
                  mock.patch.object(acl_store, "snapshot_info", return_value=[]),
                  mock.patch.object(acl_store, "get_meta", return_value={})):
            p.start(); self.addCleanup(p.stop)

    def test_nodes_collapse_into_chip_with_panel(self):
        lst = acl_search.listing("hw-test", "tunnel", "11.0.0.2")
        row = next(r for r in lst["rows"] if r["type"] == "rule")
        self.assertEqual(row["fragments"][0], {"text": "NODES", "kind": "name", "hit": False}, "имя правила — тег")
        self.assertEqual([f["icon"] for f in row["fragments"] if f["kind"] == "sep"], ["src", "dst", "svc"], "иконки-разделители")
        chip = next(f for f in row["fragments"] if f["kind"] == "set")
        self.assertEqual(chip["text"], "Петров П. П., Иванов И. И. …", "совпавший — первым, затем многоточие")
        self.assertEqual(chip["side"], "src")
        self.assertTrue(chip["hit"], "совпавший узел подсвечивает тег")
        self.assertIn("/tunnel/rule/11/source/panel/", chip["panel_url"])
        dst = next(f for f in row["fragments"] if f["kind"] == "any")
        self.assertEqual(dst["side"], "dst")
        svc = next(f for f in row["fragments"] if f["kind"] == "service")
        self.assertEqual((svc["text"], svc["panel_url"]), ("any", None), "один сервис — без карточки")

    def test_service_panel(self):
        rule = self.dev.acls[0].rules[0]
        rule.protocol = "tcp to 443, tcp to 9443, @PING, icmp 8"
        lst = acl_search.listing("hw-test", "tunnel")
        svc = next(f for f in next(r for r in lst["rows"] if r["type"] == "rule")["fragments"] if f["kind"] == "service")
        self.assertEqual(svc["text"], "tcp to 443, tcp to 9443 +2")
        self.assertIn("/rule/11/service/panel/", svc["panel_url"])
        p = acl_search.rule_nodes_panel("hw-test", "tunnel", "11", "service")
        self.assertEqual(p["member_count_text"], "4 сервиса")
        self.assertEqual([m["text"] for m in p["members"]], ["tcp to 443", "tcp to 9443", "@PING", "icmp 8"])

    def test_rule_nodes_panel(self):
        p = acl_search.rule_nodes_panel("hw-test", "tunnel", "11", "source", "11.0.0.2")
        self.assertEqual(p["member_count_text"], "3 записи", "2 узла + литерал")
        self.assertEqual([m["kind"] for m in p["members"]], ["node", "node", "network"])
        self.assertEqual(p["members"][0]["text"], "Петров П. П.")
        self.assertTrue(p["members"][0]["contains"])
        self.assertEqual(p["members"][0]["pairs"], [{"real": "", "virt": "11.0.0.2"}])
        self.assertEqual(p["members"][1]["pairs"], [{"real": "192.168.1.1", "virt": "11.0.0.1"}])
        self.assertIsNone(acl_search.rule_nodes_panel("hw-test", "tunnel", "99", "source"))
        self.assertIsNone(acl_search.rule_nodes_panel("hw-test", "tunnel", "11", "sideways"))
