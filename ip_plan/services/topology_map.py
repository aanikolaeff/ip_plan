"""Карта сети (макет test_des/Topo*): граф L1–L2 одной площадки.

Источник истины — NetBox (устройства, интерфейсы, кабели); как каждая связь
доказана и что с ней не так — сводка последнего прогона
`netbox_lldp_topology.py` (`lldp_snapshots/topology.json`: линки со статусами,
конфликты, подтверждённые кабели, владение) и история «без подтверждения»
(`unconfirmed_cables.json`). Каталог монтируется в контейнер read-only
(`settings.TOPOLOGY_DIR`).

Что отдаётся фронтенду (React-остров static_src/topo_map) — см. `build_map`:
узлы (устройства и свёрнутые группы хостов), логические связи (агрегат или
бонд — одна связь, внутри кабели), кабели (физический слой), сводки.
Хосты (роль Server / Hypervisor / VM) сворачиваются в группы по кластеру
NetBox, а без кластера — по общему набору соседей-коммутаторов; группа
раскрывается на фронтенде, данные членов приходят сразу.
"""
import json
import logging
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from django.conf import settings

from .netbox_client import NetBoxAPIError, NetBoxClient

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
_cache = {}

# Описание кабеля (его ставит netbox_lldp_topology.py) → ключ источника.
SOURCE_BY_DESCRIPTION = {
    "LLDP": "lldp",
    "LACP": "lacp",
    "LLDP (с гипервизора)": "hv",
    "MAC-таблица": "mac",
    "ARP": "arp",
    "MAC-таблица + ARP": "macarp",
}
SOURCE_LABELS = {
    "lldp": ("LLDP", "сосед назвал себя сам"),
    "lacp": ("LACP", "порт в агрегате, партнёр и номер порта"),
    "hv": ("С гипервизора", "ESXi сам слышит соседа"),
    "mac": ("MAC-таблица", "LLDP на порту нет"),
    "arp": ("ARP", "криптошлюзы, стыки точка-точка"),
    "macarp": ("MAC-таблица + ARP", "MAC за портом, IP по ARP ядра"),
    "manual": ("Вручную", "кабель заведён не синком"),
}
SOURCE_ORDER = ["lldp", "lacp", "hv", "mac", "arp", "macarp", "manual"]

# Роль NetBox (slug) → иконка (kind). Иконка модели/устройства (map_icon)
# важнее; это запасной путь.
KIND_BY_ROLE = {
    "router": "router",
    "firewall": "firewall",
    "crypto-gateway": "crypto",
    "crypto-management-center": "crypto",
    "l3-switch": "core",
    "switch": "access",
    "server": "server",
    "hypervisor": "hypervisor",
    "vm": "vm",
}
FAMILY_BY_KIND = {
    "router": "net", "core": "net", "access": "net", "chassis": "net", "ap": "net",
    "firewall": "sec", "crypto": "sec", "tls": "sec",
    "hypervisor": "cmp", "server": "cmp", "vm": "cmp", "storage": "cmp",
    "workstation": "oth", "cloud": "oth", "power": "oth", "unknown": "oth",
}
# Полосы раскладки «по ролям» (макет): Граница, Защита, Ядро, Доступ, Серверы.
BANDS = ["Граница", "Защита", "Ядро", "Доступ", "Серверы"]
BAND_BY_KIND = {
    "router": 0, "cloud": 0,
    "firewall": 1, "crypto": 1, "tls": 1,
    "core": 2,
    "access": 3, "chassis": 3, "ap": 3, "power": 3, "workstation": 3, "unknown": 3,
    "hypervisor": 4, "server": 4, "vm": 4, "storage": 4,
}
HOST_KINDS = {"server", "hypervisor", "vm", "storage"}
ROLE_TITLES = {
    "router": "Маршрутизатор", "core": "Коммутатор ядра", "access": "Коммутатор доступа",
    "chassis": "Шасси, блейд", "ap": "Точка доступа", "firewall": "Межсетевой экран",
    "crypto": "Криптошлюз", "tls": "Шлюз доступа TLS", "hypervisor": "Гипервизор",
    "server": "Сервер", "vm": "Виртуальная машина", "storage": "СХД",
    "workstation": "Рабочее место", "cloud": "Провайдер", "power": "ИБП, PDU", "unknown": "Роль не задана",
}


# ---------------------------------------------------------------- снимок синка

def _topology_dir():
    return Path(getattr(settings, "TOPOLOGY_DIR", ""))


def load_snapshot():
    """topology.json и unconfirmed_cables.json; отсутствие — пустые структуры."""
    base = _topology_dir()
    topo, unconfirmed = {}, {}
    try:
        topo = json.loads((base / "topology.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        unconfirmed = json.loads((base / "unconfirmed_cables.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return topo, unconfirmed


# ---------------------------------------------------------------- NetBox

def _netbox_public_url():
    url = getattr(settings, "NETBOX_PUBLIC_URL", "") or settings.NETBOX_URL
    return url.replace("host.docker.internal", "localhost").rstrip("/")


def _fetch(site_slug):
    """Устройства, интерфейсы, кабели, модели и площадки из NetBox (кэш на минуту)."""
    key = site_slug or "*"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    client = NetBoxClient()
    params = {"site": site_slug} if site_slug else {}
    data = {
        "devices": client._list_all("/api/dcim/devices/", dict(params)),
        "interfaces": client._list_all("/api/dcim/interfaces/", {
            **params, "fields": "id,name,type,lag,cable,device,custom_fields"}),
        "cables": client._list_all("/api/dcim/cables/", dict(params)),
        "types": client._list_all("/api/dcim/device-types/", {}),
        "sites": client._list_all("/api/dcim/sites/", {}),
    }
    _cache[key] = (time.time(), data)
    return data


def _members_by_lag(interfaces):
    """{lag_interface_id: [члены]} — все члены агрегатов, в том числе без
    кабеля (для «доказано k из N»)."""
    out = defaultdict(list)
    for i in interfaces:
        lag = i.get("lag")
        if lag:
            out[lag["id"]].append({"id": i["id"], "name": i["name"], "cabled": bool(i.get("cable"))})
    return out


# ---------------------------------------------------------------- узлы

def _kind(device, types_by_id):
    cf = device.get("custom_fields") or {}
    if cf.get("map_icon"):
        return cf["map_icon"], "устройство"
    t = types_by_id.get((device.get("device_type") or {}).get("id")) or {}
    tcf = t.get("custom_fields") or {}
    if tcf.get("map_icon"):
        return tcf["map_icon"], "модель"
    role = (device.get("role") or {}).get("slug")
    if role in KIND_BY_ROLE:
        return KIND_BY_ROLE[role], "роль"
    return "unknown", "—"


def _health(device):
    """(ok | stale | down, текст) — состояние NetBox и свежесть проверок синка."""
    status = (device.get("status") or {}).get("value")
    if status and status != "active":
        return "down", f"Статус в NetBox: {(device.get('status') or {}).get('label')}"
    cf = device.get("custom_fields") or {}
    for key, label in (("ssh_local", "SSH"), ("snmp_check", "SNMP"), ("lldp_check", "LLDP")):
        val = (cf.get(key) or "").strip()
        if val.startswith("ошибка"):
            return "stale", f"{label}: {val}"
    return "ok", "Работает"


def _node(device, types_by_id, sites_by_id):
    kind, kind_from = _kind(device, types_by_id)
    health, health_text = _health(device)
    ip = ((device.get("primary_ip4") or {}).get("address") or "").split("/")[0]
    site = sites_by_id.get((device.get("site") or {}).get("id")) or {}
    return {
        "id": f"d{device['id']}", "device_id": device["id"], "name": device.get("name") or f"#{device['id']}",
        "model": (device.get("device_type") or {}).get("model") or "",
        "role": (device.get("role") or {}).get("name") or "", "role_title": ROLE_TITLES.get(kind, ""),
        "kind": kind, "kind_from": kind_from, "family": FAMILY_BY_KIND.get(kind, "oth"),
        "band": BAND_BY_KIND.get(kind, 3), "site": site.get("slug"), "site_name": site.get("name"),
        "ip": ip, "health": health, "health_text": health_text,
        "cluster": (device.get("cluster") or {}).get("name"),
        "netbox_url": f"{_netbox_public_url()}/dcim/devices/{device['id']}/",
        "group": None,
    }


# ---------------------------------------------------------------- доказательства

def _proof(cable_src, link, topo_devices_by_name):
    """Шаги «Чем доказано» для кабеля по записи линка из topology.json."""
    if not link:
        if cable_src == "manual":
            return ["Кабель заведён в NetBox вручную, синк его не строил."]
        return [SOURCE_LABELS.get(cable_src, ("", ""))[1].capitalize() + "."] if cable_src in SOURCE_LABELS else []
    a, ap, b, bp = link.get("a_device"), link.get("a_port"), link.get("b_device"), link.get("b_port")
    seen = sorted(set(link.get("seen_from") or []))
    if cable_src == "lldp":
        who = " и ".join(seen) if seen else a
        return [f"{who} по LLDP видит соседа: {a} {ap} ↔ {b} {bp}.",
                f"Сосед опознан: {link.get('matched_by') or 'по имени'}."]
    if cable_src == "lacp":
        snap = topo_devices_by_name.get(a) or {}
        partner = (snap.get("lacp") or {}).get(ap)
        steps = []
        if partner:
            steps.append(f"{a} видит на {ap} LACP-партнёра: системный MAC {partner[0]}, порт партнёра {partner[1]}.")
        steps.append(f"Этот MAC в NetBox принадлежит {b} — значит, на той стороне агрегат.")
        steps.append(f"Номер порта указывает на член агрегата: {bp} ({link.get('matched_by') or 'LACP'}).")
        return steps
    if cable_src in ("mac", "arp", "macarp"):
        steps = [f"За портом {a} {ap} виден единственный чужой адрес {link.get('b_chassis_id') or ''}.".replace("  ", " ")]
        if cable_src == "macarp":
            steps.append(f"ARP L3-коммутатора даёт его IP: {link.get('matched_by', '')}.")
        steps.append(f"Этот адрес — интерфейс {b} {bp} в NetBox.")
        return steps
    if cable_src == "hv":
        return [f"{a} сам слышит соседа по LLDP/CDP: {b} {bp} (подсказка гипервизора)."]
    return []


# ---------------------------------------------------------------- граф

def build_map(site_slug=None):
    """Полные данные карты площадки (или всех площадок при site_slug=None)."""
    nb = _fetch(site_slug)
    topo, unconfirmed = load_snapshot()
    lag_members = _members_by_lag(nb["interfaces"])
    types_by_id = {t["id"]: t for t in nb["types"]}
    sites_by_id = {s["id"]: s for s in nb["sites"]}
    dev_by_id = {d["id"]: d for d in nb["devices"]}
    iface_by_id = {i["id"]: i for i in nb["interfaces"]}

    conflicts_by_cable = {c["cable_id"]: c for c in topo.get("conflicts") or []}
    confirmed = set(topo.get("confirmed_cables") or [])
    ownership = topo.get("cable_ownership") or {}
    partial_run = bool(topo.get("partial"))
    topo_devices_by_name = {d.get("device"): d for d in topo.get("devices") or []}
    link_by_pair = {}
    for l in topo.get("links") or []:
        if l.get("status") == "ready" and l.get("a_interface_id") and l.get("b_interface_id"):
            link_by_pair[frozenset((l["a_interface_id"], l["b_interface_id"]))] = l

    # --- кабели (физический слой)
    cables = []
    used_devices = set()
    for c in nb["cables"]:
        a = (c.get("a_terminations") or [{}])[0]
        b = (c.get("b_terminations") or [{}])[0]
        if a.get("object_type") != "dcim.interface" or b.get("object_type") != "dcim.interface":
            continue
        ai, bi = a["object_id"], b["object_id"]
        ao, bo = a.get("object") or {}, b.get("object") or {}
        ad, bd = (ao.get("device") or {}).get("id"), (bo.get("device") or {}).get("id")
        if ad is None or bd is None:
            continue
        aif, bif = iface_by_id.get(ai) or {}, iface_by_id.get(bi) or {}
        desc = (c.get("description") or "").strip()
        tags = {t.get("slug") for t in c.get("tags") or []}
        src = SOURCE_BY_DESCRIPTION.get(desc)
        if src is None:
            src = "manual" if "sync-topology" not in tags else "lldp"
        own = (ownership.get(str(c["id"])) or {}).get("owner") or ("ours" if "sync-topology" in tags else "manual")
        unc = unconfirmed.get(str(c["id"]))
        conflict = conflicts_by_cable.get(c["id"])
        state = "conflict" if conflict else ("unconfirmed" if unc else "confirmed")
        link = link_by_pair.get(frozenset((ai, bi)))
        cables.append({
            "id": c["id"], "source": src, "description": desc, "owner": own,
            "editors": (ownership.get(str(c["id"])) or {}).get("editors") or [],
            "state": state,
            "confirmed_last_run": c["id"] in confirmed,
            "unconfirmed": {"runs": unc.get("runs"), "since": unc.get("since")} if unc else None,
            "conflict": ({"observed_peer": conflict.get("observed_peer"),
                          "observed_source": conflict.get("observed_source"),
                          "busy": conflict.get("busy")} if conflict else None),
            "a": {"device_id": ad, "node": f"d{ad}", "interface_id": ai, "interface": ao.get("name"),
                  "lag": (aif.get("lag") or {}).get("name"), "lag_id": (aif.get("lag") or {}).get("id"),
                  "type": (aif.get("type") or {}).get("value")},
            "b": {"device_id": bd, "node": f"d{bd}", "interface_id": bi, "interface": bo.get("name"),
                  "lag": (bif.get("lag") or {}).get("name"), "lag_id": (bif.get("lag") or {}).get("id"),
                  "type": (bif.get("type") or {}).get("value")},
            "created": c.get("created"),
            "netbox_url": f"{_netbox_public_url()}/dcim/cables/{c['id']}/",
            "proof": _proof(src, link, topo_devices_by_name),
        })
        used_devices.update((ad, bd))

    # Устройства на концах кабелей, которых нет в выборке площадки (кабель на
    # другую площадку) — подтягиваем, чтобы связь было куда рисовать.
    missing = {d for d in used_devices if d not in dev_by_id}
    if missing:
        client = NetBoxClient()
        for did in missing:
            try:
                dev_by_id[did] = client.get_device(did)
            except NetBoxAPIError:
                pass

    # --- узлы: устройства с кабелями + устройства синка площадки без кабелей
    nodes = {}
    for did in used_devices:
        if did in dev_by_id:
            nodes[f"d{did}"] = _node(dev_by_id[did], types_by_id, sites_by_id)
    loose = []
    for d in nb["devices"]:
        sync_key = (d.get("custom_fields") or {}).get("sync_key")
        if d["id"] in used_devices or not sync_key:
            continue
        n = _node(d, types_by_id, sites_by_id)
        nodes[n["id"]] = n
        loose.append({"node": n["id"], "name": n["name"], "kind": n["kind"],
                      "reason": _loose_reason(n["name"], topo)})

    # --- логические связи: агрегат/бонд или одиночный порт — одна связь
    links = {}
    for cab in cables:
        a, b = cab["a"], cab["b"]
        ka = ("lag", a["lag_id"]) if a["lag_id"] else ("if", a["interface_id"])
        kb = ("lag", b["lag_id"]) if b["lag_id"] else ("if", b["interface_id"])
        # Агрегат с одной стороны — одна связь «агрегат ↔ устройство», как бы
        # ни назывались порты на стороне без агрегата (блейд-коммутатор,
        # хост с бондом в два разных коммутатора).
        if a["lag_id"] or b["lag_id"]:
            ka = ka if a["lag_id"] else ("dev", 0)
            kb = kb if b["lag_id"] else ("dev", 0)
        key = tuple(sorted([(a["node"],) + ka, (b["node"],) + kb]))
        lk = links.get(key)
        if lk is None:
            first, second = (a, b) if (a["node"],) + ka == key[0] else (b, a)
            lk = links[key] = {
                "id": f"l{len(links) + 1}", "a": {"node": first["node"], "lag": first["lag"], "ports": []},
                "b": {"node": second["node"], "lag": second["lag"], "ports": []},
                "cables": [], "sources": Counter(), "states": Counter(),
                "lag_ids": [first["lag_id"], second["lag_id"]],
            }
        side_a = lk["a"] if lk["a"]["node"] == a["node"] and lk["a"]["lag"] == a["lag"] else lk["b"]
        side_b = lk["b"] if side_a is lk["a"] else lk["a"]
        side_a["ports"].append(a["interface"])
        side_b["ports"].append(b["interface"])
        lk["cables"].append(cab["id"])
        lk["sources"][cab["source"]] += 1
        lk["states"][cab["state"]] += 1
        cab["link_id"] = lk["id"]
    # к какому устройству ведёт кабель с интерфейса — чтобы не ждать в связи
    # «агрегат ↔ B» членов, которые кабелем ушли к C (бонд в два коммутатора)
    peer_of_iface = {}
    for cab in cables:
        peer_of_iface[cab["a"]["interface_id"]] = cab["b"]["node"]
        peer_of_iface[cab["b"]["interface_id"]] = cab["a"]["node"]

    def _expected(lag_id, other_node):
        members = lag_members.get(lag_id) or []
        return sum(1 for m in members if peer_of_iface.get(m["id"]) in (None, other_node))

    out_links = []
    for lk in links.values():
        exp = []
        for lag_id, other in ((lk["lag_ids"][0], lk["b"]["node"]), (lk["lag_ids"][1], lk["a"]["node"])):
            if lag_id:
                exp.append(_expected(lag_id, other))
        # Агрегаты с обеих сторон — берём меньшую оценку: член без кабеля у
        # бонда «в два коммутатора» неизвестно куда идёт, а агрегат
        # коммутатора, все члены которого доказаны, полон.
        expected = max(min(exp) if exp else 0, len(lk["cables"]))
        proven = len(lk["cables"])
        state = ("conflict" if lk["states"].get("conflict") else
                 "unconfirmed" if lk["states"].get("unconfirmed") else
                 "partial" if expected > proven else "confirmed")
        # Члены агрегатов этой связи, у которых кабеля нет вовсе: куда они
        # идут, неизвестно (часто — ко второму коммутатору пары), поэтому
        # это пометка агрегата, а не неполнота самой связи.
        gaps = []
        for side, lag_id in ((lk["a"], lk["lag_ids"][0]), (lk["b"], lk["lag_ids"][1])):
            members = lag_members.get(lag_id) or [] if lag_id else []
            loose_m = [m["name"] for m in members if m["id"] not in peer_of_iface]
            if loose_m:
                gaps.append({"node": side["node"], "lag": side["lag"], "members": len(members),
                             "cabled": len(members) - len(loose_m), "without_cable": loose_m})
        out_links.append({
            "id": lk["id"], "a": lk["a"], "b": lk["b"], "cables": lk["cables"], "lag_gaps": gaps,
            "source": lk["sources"].most_common(1)[0][0], "sources": dict(lk["sources"]),
            "members_expected": expected, "members_proven": proven, "is_lag": bool(lk["a"]["lag"] or lk["b"]["lag"]),
            "state": state,
        })

    # --- группы хостов
    groups = _groups(nodes, out_links)

    # --- сводки
    by_source_links = Counter(l["source"] for l in out_links)
    by_source_cables = Counter(c["source"] for c in cables)
    # Неполные агрегаты: связь доказана не всеми ожидаемыми членами
    # (state=partial) или у агрегата есть члены без кабеля (lag_gaps).
    partial_lags, seen_lags = [], set()
    for l in out_links:
        if l["state"] == "partial":
            side = l["a"] if l["a"]["lag"] else l["b"]
            seen_lags.add((side["node"], side["lag"]))
            partial_lags.append({"link": l["id"], "node": side["node"], "device": nodes.get(side["node"], {}).get("name"),
                                 "lag": side["lag"], "members": l["members_expected"], "proven": l["members_proven"],
                                 "without_cable": []})
        for g in l["lag_gaps"]:
            if (g["node"], g["lag"]) in seen_lags:
                continue
            seen_lags.add((g["node"], g["lag"]))
            partial_lags.append({"link": l["id"], "node": g["node"], "device": nodes.get(g["node"], {}).get("name"),
                                 "lag": g["lag"], "members": g["members"], "proven": g["cabled"],
                                 "without_cable": g["without_cable"]})
    rejected = _rejected(topo, {n["name"] for n in nodes.values()})
    site = next((s for s in nb["sites"] if s.get("slug") == site_slug), None) if site_slug else None
    return {
        "site": {"slug": site_slug, "name": (site or {}).get("name") if site else "Все площадки"},
        "collected_at": topo.get("collected_at"), "partial_run": partial_run,
        "bands": BANDS,
        "sources": [{"key": k, "label": SOURCE_LABELS[k][0], "hint": SOURCE_LABELS[k][1],
                     "links": by_source_links.get(k, 0), "cables": by_source_cables.get(k, 0)} for k in SOURCE_ORDER],
        "nodes": list(nodes.values()), "groups": groups, "links": out_links, "cables": cables,
        "summary": {
            "devices": len(nodes), "links": len(out_links), "cables": len(cables),
            "cables_in_lag": sum(1 for c in cables if c["a"]["lag"] or c["b"]["lag"]),
            "loose": loose, "rejected": rejected, "partial_lags": partial_lags,
            "conflicts": [c for c in cables if c["state"] == "conflict"],
            "unconfirmed": [c for c in cables if c["state"] == "unconfirmed"],
            "manual": sum(1 for c in cables if c["owner"] == "manual"),
            "edited": sum(1 for c in cables if c["owner"] == "edited"),
        },
    }


def _loose_reason(name, topo):
    reasons = Counter()
    for l in topo.get("links") or []:
        if l.get("a_device") == name and l.get("status") != "ready":
            reasons[l.get("status")] += 1
    if reasons:
        return reasons.most_common(1)[0][0]
    if not any(d.get("device") == name for d in topo.get("devices") or []):
        return "LLDP не собирается — связи только по таблицам адресов"
    return "Соседей не найдено"


def _rejected(topo, names, limit=40):
    """Отброшено как сомнительное: линки снимка, которые не стали кабелем,
    кроме «достроен другим источником» (это не отказ)."""
    out = Counter()
    sample = {}
    for l in topo.get("links") or []:
        st = l.get("status") or ""
        if st == "ready" or st.startswith("достроен"):
            continue
        if l.get("a_device") not in names:
            continue
        key = (l.get("a_device"), st)
        out[key] += 1
        sample.setdefault(key, f"{l.get('a_device')} · {l.get('a_port')}")
    rows = [{"where": sample[k], "device": k[0], "reason": k[1], "count": n} for k, n in out.most_common(limit)]
    return rows


def _groups(nodes, links):
    """Свернуть хосты в группы: по кластеру NetBox, иначе по одинаковому
    набору соседей-не-хостов. Группа — от двух хостов."""
    neighbors = defaultdict(set)
    for l in links:
        neighbors[l["a"]["node"]].add(l["b"]["node"])
        neighbors[l["b"]["node"]].add(l["a"]["node"])
    buckets = defaultdict(list)
    for nid, n in nodes.items():
        if n["kind"] not in HOST_KINDS:
            continue
        if n.get("cluster"):
            key = ("cluster", n["cluster"])
        else:
            ups = sorted(nodes[x]["name"] for x in neighbors[nid] if x in nodes and nodes[x]["kind"] not in HOST_KINDS)
            if not ups:
                continue
            key = ("upstream", " + ".join(ups))
        buckets[key].append(nid)
    groups = []
    for (how, label), members in sorted(buckets.items(), key=lambda kv: kv[0][1]):
        if len(members) < 2:
            continue
        gid = "g" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] + f"-{len(groups) + 1}"
        kinds = Counter(nodes[m]["kind"] for m in members)
        for m in members:
            nodes[m]["group"] = gid
        groups.append({
            "id": gid, "title": label if how == "cluster" else f"Серверы за {label}",
            "how": how, "members": sorted(members, key=lambda m: nodes[m]["name"]),
            "kind": kinds.most_common(1)[0][0], "family": "cmp", "band": 4, "count": len(members),
        })
    return groups
