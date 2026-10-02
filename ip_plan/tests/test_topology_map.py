"""topology_map: логические связи из кабелей NetBox (агрегат — одна связь,
«k из N» без членов, ушедших к другому устройству), источник по описанию,
состояния из сводки прогона, группы хостов; эндпоинт раскладки."""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from ip_plan.models import MapLayout
from ip_plan.services import topology_map as tm


def _dev(i, name, role, cluster=None, site=1):
    return {"id": i, "name": name, "role": {"slug": role, "name": role}, "device_type": {"id": 1, "model": "M"},
            "site": {"id": site}, "status": {"value": "active", "label": "Активно"}, "primary_ip4": None,
            "cluster": {"name": cluster} if cluster else None, "custom_fields": {"sync_key": name}}


def _if(i, dev, name, lag=None):
    return {"id": i, "name": name, "device": {"id": dev}, "type": {"value": "10gbase-x-sfpp"},
            "lag": {"id": lag, "name": f"lag{lag}"} if lag else None, "cable": None, "custom_fields": {}}


def _cab(cid, ai, ad, bi, bd, desc="LLDP", tag=True):
    t = lambda i, d: [{"object_type": "dcim.interface", "object_id": i,
                       "object": {"name": f"if{i}", "device": {"id": d}}}]
    return {"id": cid, "a_terminations": t(ai, ad), "b_terminations": t(bi, bd), "description": desc,
            "tags": [{"slug": "sync-topology"}] if tag else [], "created": None}


def _nb():
    devices = [_dev(1, "sw1", "switch"), _dev(2, "sw2", "switch"), _dev(3, "fw", "firewall"),
               _dev(4, "h1", "hypervisor", "C1"), _dev(5, "h2", "hypervisor", "C1"), _dev(6, "lonely", "switch")]
    interfaces = [
        _if(100, 3, "bond0"), _if(101, 3, "eth1", lag=100), _if(102, 3, "eth2", lag=100),
        _if(103, 3, "eth3", lag=100), _if(104, 3, "eth4", lag=100),          # бонд в два коммутатора
        _if(110, 1, "Po1"), _if(111, 1, "te1", lag=110), _if(112, 1, "te2", lag=110),
        _if(120, 2, "te1"), _if(121, 2, "te2"),                                # без агрегата
        _if(130, 1, "te9"), _if(140, 4, "nic0"), _if(141, 2, "te9"), _if(150, 5, "nic0"),
    ]
    cables = [
        _cab(1, 111, 1, 101, 3, "LACP"), _cab(2, 112, 1, 102, 3, "LACP"),   # sw1 Po1 ↔ fw bond0: 2 из 2
        _cab(3, 120, 2, 103, 3, "LACP"),                                       # sw2 ↔ fw bond0: 1 из 2 (eth4 без кабеля)
        _cab(4, 130, 1, 140, 4, "MAC-таблица + ARP"),
        _cab(5, 141, 2, 150, 5, "что-то своё", tag=False),
    ]
    return {"devices": devices, "interfaces": interfaces, "cables": cables,
            "types": [{"id": 1, "custom_fields": {}}], "sites": [{"id": 1, "slug": "mdc", "name": "MDC"}]}


TOPO = {"collected_at": "2026-10-02T03:14:00", "partial": False,
        "conflicts": [{"cable_id": 4, "observed_peer": "h9 nic0", "observed_source": "LLDP", "busy": "sw1 te9"}],
        "confirmed_cables": [1, 2], "cable_ownership": {"5": {"owner": "manual", "editors": []}},
        "devices": [], "links": [{"a_device": "sw1", "a_port": "te5", "status": "сосед не найден в NetBox"}]}


class TopologyMapTests(TestCase):
    def build(self):
        tm._cache.clear()
        with mock.patch.object(tm, "_fetch", return_value=_nb()), \
             mock.patch.object(tm, "load_snapshot", return_value=(TOPO, {"3": {"runs": 2, "since": "2026-09-30"}})):
            return tm.build_map("mdc")

    def test_links_and_lag_counts(self):
        d = self.build()
        by_pair = {(l["a"]["node"], l["b"]["node"]): l for l in d["links"]}
        sw1 = next(l for l in d["links"] if {"d1", "d3"} == {l["a"]["node"], l["b"]["node"]})
        self.assertEqual(len(sw1["cables"]), 2, "агрегат — одна связь")
        self.assertEqual((sw1["members_proven"], sw1["members_expected"], sw1["state"]), (2, 2, "confirmed"),
                         "члены бонда, ушедшие ко второму коммутатору, не ожидаются")
        sw2 = next(l for l in d["links"] if {"d2", "d3"} == {l["a"]["node"], l["b"]["node"]})
        self.assertEqual((sw2["members_proven"], sw2["members_expected"]), (1, 2), "eth4 без кабеля — ждём его здесь")
        self.assertEqual(sw2["state"], "unconfirmed", "состояние кабеля важнее неполноты")
        self.assertEqual(sw1["lag_gaps"][0]["without_cable"], ["eth4"], "член без кабеля — пометка агрегата")
        self.assertEqual([(p["device"], p["lag"], p["proven"], p["members"]) for p in d["summary"]["partial_lags"]],
                         [("fw", "lag100", 3, 4)])
        self.assertTrue(by_pair or True)

    def test_sources_states_owner(self):
        c = {x["id"]: x for x in self.build()["cables"]}
        self.assertEqual(c[1]["source"], "lacp")
        self.assertEqual(c[4]["source"], "macarp")
        self.assertEqual(c[4]["state"], "conflict")
        self.assertEqual(c[4]["conflict"]["observed_peer"], "h9 nic0")
        self.assertEqual(c[3]["unconfirmed"], {"runs": 2, "since": "2026-09-30"})
        self.assertEqual((c[5]["source"], c[5]["owner"]), ("manual", "manual"))
        self.assertTrue(c[1]["confirmed_last_run"])

    def test_groups_kinds_loose(self):
        d = self.build()
        nodes = {n["id"]: n for n in d["nodes"]}
        self.assertEqual(nodes["d3"]["kind"], "firewall")
        self.assertEqual(nodes["d3"]["band"], 1)
        self.assertEqual([g["title"] for g in d["groups"]], ["C1"])
        self.assertEqual(nodes["d4"]["group"], d["groups"][0]["id"])
        self.assertEqual([x["name"] for x in d["summary"]["loose"]], ["lonely"])
        self.assertEqual(d["summary"]["rejected"][0]["reason"], "сосед не найден в NetBox")

    def test_icon_precedence(self):
        dev = _dev(1, "x", "switch")
        types = {1: {"custom_fields": {"map_icon": "chassis"}}}
        self.assertEqual(tm._kind(dev, types), ("chassis", "модель"))
        dev["custom_fields"]["map_icon"] = "ap"
        self.assertEqual(tm._kind(dev, types), ("ap", "устройство"))
        self.assertEqual(tm._kind(_dev(2, "y", "nope"), {}), ("unknown", "—"))


class MapLayoutViewTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("u1", password="x")
        self.client.force_login(self.u)

    def test_roundtrip_per_user(self):
        url = "/map/layout.json?site=mdc&layer=l2"
        r = self.client.put(url, json.dumps({"positions": {"d1": [10, 20.55], "bad": "x"}, "expanded": ["g1"], "locked": True}),
                            content_type="application/json", HTTP_HOST="localhost")
        self.assertEqual(r.status_code, 200, r.content)
        got = self.client.get(url, HTTP_HOST="localhost").json()
        self.assertEqual(got["positions"], {"d1": [10.0, 20.6]})
        self.assertEqual((got["expanded"], got["locked"]), (["g1"], True))
        other = User.objects.create_user("u2", password="x")
        self.client.force_login(other)
        self.assertEqual(self.client.get(url, HTTP_HOST="localhost").json()["positions"], {})
        self.client.force_login(self.u)
        self.client.delete(url, HTTP_HOST="localhost")
        self.assertFalse(MapLayout.objects.filter(user=self.u).exists())
