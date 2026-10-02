"""Раздел «Блокировки» v2: сверка реестра регулятора со снимком объектов xFW.

Источник объектов — тот же снимок, что у страницы ACL
(`config_backups/<firewall>/latest.firewall_objects.txt`, разбирает
`acl_store` / `vipnet_firewall`): так сверка не зависит от
`blocklist_objects` в devices.json netbox-sync, который после «Отметить
применённым» отставал от нового имени объекта. Если разобранного снимка
нет — запасной путь через старый `firewall_blocklist` (latest.json).

Что считается (контракт — scratchpad/design/reg/CONTRACT-reg.md на время
разработки, затем CLAUDE.md раздел «Блокировки v2»):

- статус записи «на xFW» (`fw_state`): applied (активна и стоит), pending
  (активна, на xFW нет), revoked_present (снята везде, где встречается, а на
  xFW ещё стоит), removed (снята и убрана), not_applicable (хэш — не для
  firewall), unknown (снимка нет). Значение может быть в нескольких письмах:
  оно «снято», только если снято во всех;
- IP сверяется и с подсетями объекта (на xFW бывают «a.b.c.0/255.255.255.0»):
  адрес внутри подсети — applied «через подсеть»;
- метрики для карточек, прогресс писем, три списка сверки (ждут применения /
  сняты, но стоят / только на xFW), проверка одного значения;
- diff для мастера команд: новый список = снимок + добавить − убрать
  (снятые везде); значения «только на xFW» сохраняются.
"""
import ipaddress
import re
from collections import defaultdict
from datetime import datetime

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from . import acl_store, firewall_blocklist
from .ioc_parser import refang
from ..models import FirewallObjectName, RegulatorDocument, RegulatorEntry

FIREWALL = firewall_blocklist.DEVICE   # firewall с блокировками (сейчас один)

DEFAULT_NAMES = {"ip": "BLOCK_BY_IP_1", "fqdn": "BBDNS_0"}
OBJECT_KIND = {"ip": "ip", "fqdn": "fqdn", "url": "fqdn"}   # тип записи → объект xFW

STATE_LABELS = {
    "applied": "применена",
    "pending": "ждёт применения",
    "revoked_present": "снята, но ещё стоит",
    "removed": "убрана",
    "not_applicable": "не для firewall",
    "unknown": "нет снимка",
}
KIND_LABELS = {"ip": "IP", "fqdn": "Домен", "url": "URL", "hash": "Хэш"}

# Правила ViPNet, где применяются объекты блокировок (шаг 3 мастера) — если в
# снимке не нашлось, кто их использует.
DEFAULT_RULES = ["BLOCK_OUTSIDE_IN_BY_LIST", "BLOCK_INSIDE_OUT_BY_LIST"]


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        w = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        w = few
    else:
        w = many
    return f"{n:,}".replace(",", " ") + f" {w}"


def _fmt_when(dt):
    if dt is None:
        return ""
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    today = timezone.localdate()
    if dt.date() == today:
        return f"сегодня, {dt:%H:%M}"
    if (today - dt.date()).days == 1:
        return f"вчера, {dt:%H:%M}"
    return f"{dt:%d.%m.%Y %H:%M}"


# ---------------------------------------------------------------- снимок

def _parse_value(raw):
    """Строка из объекта xFW → (нормализованное, IPv4Network | None)."""
    v = (raw or "").strip()
    if "/" in v:
        ip, mask = v.split("/", 1)
        try:
            return v, ipaddress.ip_network(f"{ip}/{mask}", strict=False)
        except ValueError:
            return v.lower(), None
    try:
        return v, ipaddress.ip_network(f"{v}/32")
    except ValueError:
        return v.lower(), None


class FwObject:
    """Один объект блокировки в снимке: имя, найден ли, значения."""

    def __init__(self, kind, name, values, found, wanted, fallback=False):
        self.kind = kind
        self.name = name          # имя объекта, по которому сверяем
        self.wanted = wanted      # current_name из FirewallObjectName
        self.found = found
        self.fallback = fallback  # current_name в снимке нет, сверка по предыдущему
        self.raw = list(values)
        self.exact = set()
        self.networks = []        # [(raw, IPv4Network)] — только настоящие подсети
        for raw in self.raw:
            norm, net = _parse_value(raw)
            self.exact.add(norm)
            if net is not None and net.prefixlen < 32:
                self.networks.append((raw, net))

    def contains(self, match_value):
        """(есть ли, через подсеть raw | None)."""
        if match_value in self.exact:
            return True, None
        if self.kind == "ip":
            try:
                ip = ipaddress.ip_network(f"{match_value}/32")
            except ValueError:
                return False, None
            for raw, net in self.networks:
                if ip.subnet_of(net):
                    return True, raw
        return False, None


def _object_values_from_store(device, name):
    if device is None or name not in device.object_groups:
        return None
    members = device.object_group_members.get(name)
    if members:
        return [m for m in members if not m.startswith("@")]
    return [str(n) for n in device.object_groups[name]]


def _prefix_fallback(device, name):
    """Объект с тем же префиксом и наибольшим номером из снимка — когда
    current_name уже продвинут «Отметить применённым», а новый снимок ещё
    не снят."""
    m = re.match(r"^(.*?)(\d+)$", name or "")
    if not m or device is None:
        return None
    prefix = m.group(1)
    best = None
    for n in device.object_groups:
        mm = re.match(rf"^{re.escape(prefix)}(\d+)$", n)
        if mm and (best is None or int(mm.group(1)) > best[0]):
            best = (int(mm.group(1)), n)
    return best[1] if best else None


def snapshot():
    """Текущая картина xFW: {available, at, at_text, device, model,
    objects: {ip: FwObject, fqdn: FwObject}, waiting_snapshot}."""
    names = {}
    for kind, default in DEFAULT_NAMES.items():
        obj, _ = FirewallObjectName.objects.get_or_create(kind=kind, defaults={"current_name": default})
        names[kind] = obj
    device = acl_store.get_device(FIREWALL)
    meta = acl_store.get_meta(FIREWALL) if device is not None else {}
    at = None
    if meta.get("updated"):
        try:
            at = datetime.fromisoformat(meta["updated"])
            if timezone.is_naive(at):
                at = timezone.make_aware(at)
        except ValueError:
            at = None
    objects = {}
    for kind, rec in names.items():
        values = _object_values_from_store(device, rec.current_name)
        if values is not None:
            objects[kind] = FwObject(kind, rec.current_name, values, True, rec.current_name)
            continue
        alt = _prefix_fallback(device, rec.current_name)
        if alt:
            objects[kind] = FwObject(kind, alt, _object_values_from_store(device, alt), True, rec.current_name, True)
            continue
        legacy = firewall_blocklist.get_current_values(rec.current_name)
        if legacy is not None:
            objects[kind] = FwObject(kind, rec.current_name, legacy, True, rec.current_name)
            if at is None:
                lm = firewall_blocklist.snapshot_mtime()
                at = timezone.make_aware(lm) if lm else None
        else:
            objects[kind] = FwObject(kind, rec.current_name, [], False, rec.current_name)
    available = any(o.found for o in objects.values())
    waiting = any(o.fallback for o in objects.values())
    return {
        "available": available, "at": at, "at_text": _fmt_when(at), "device": FIREWALL,
        "model": meta.get("model") or "", "objects": objects, "names": names,
        "waiting_snapshot": waiting,
    }


# ---------------------------------------------------------------- статусы

def _value_index():
    """match_value → {'active': bool, 'kinds': set, 'entries': [...]} по всему реестру."""
    idx = defaultdict(lambda: {"active": False, "entries": []})
    for e in RegulatorEntry.objects.select_related("document").all():
        rec = idx[(OBJECT_KIND.get(e.kind, e.kind), e.match_value)]
        rec["entries"].append(e)
        if e.status == "active":
            rec["active"] = True
    return idx


def fw_state(entry, snap, index):
    """(state, via_subnet | None) для одной записи."""
    if entry.kind == "hash":
        return "not_applicable", None
    okind = OBJECT_KIND[entry.kind]
    obj = snap["objects"].get(okind)
    if not snap["available"] or obj is None or not obj.found:
        return "unknown", None
    present, via = obj.contains(entry.match_value)
    active_anywhere = index[(okind, entry.match_value)]["active"]
    if entry.status == "active" or active_anywhere:
        return ("applied" if present else "pending"), via
    return ("revoked_present" if present else "removed"), via


def annotate(entries, snap=None, index=None):
    """Список записей → словари для шаблонов и JSON (с fw_state)."""
    snap = snap or snapshot()
    index = index if index is not None else _value_index()
    out = []
    for e in entries:
        state, via = fw_state(e, snap, index)
        others = [x for x in index[(OBJECT_KIND.get(e.kind, e.kind), e.match_value)]["entries"]
                  if x.document_id != e.document_id]
        out.append({
            "id": e.id, "kind": e.kind, "kind_label": KIND_LABELS.get(e.kind, e.kind),
            "value": e.value, "match_value": e.match_value,
            "status": e.status, "status_label": "активна" if e.status == "active" else "снята",
            "revoked_at": e.revoked_at, "revoked_reason": e.revoked_reason,
            "created_at": e.created_at,
            "document": {"id": e.document_id, "reference_no": e.document.reference_no,
                         "document_date": e.document.document_date,
                         "url": reverse("ip_plan:regulator_document_detail", args=[e.document_id])},
            "fw_state": state, "fw_state_label": STATE_LABELS[state], "fw_via": via,
            "other_letters": [{"id": x.document_id, "reference_no": x.document.reference_no} for x in others],
        })
    return out


# ---------------------------------------------------------------- сводка и сверка

def sync_lists(snap=None, index=None):
    """Три списка сверки: pending, revoked_present, only_fw."""
    snap = snap or snapshot()
    index = index if index is not None else _value_index()
    pending, revoked_present = [], []
    seen = set()
    for (okind, mv), rec in index.items():
        if okind == "hash":
            continue
        obj = snap["objects"].get(okind)
        if obj is None or not obj.found:
            continue
        present, via = obj.contains(mv)
        e = rec["entries"][0]
        item = {"kind": e.kind, "kind_label": KIND_LABELS.get(e.kind, e.kind), "value": mv,
                "letters": [{"id": x.document_id, "reference_no": x.document.reference_no,
                             "url": reverse("ip_plan:regulator_document_detail", args=[x.document_id])}
                            for x in rec["entries"]],
                "via": via}
        if rec["active"] and not present:
            pending.append(item)
        elif not rec["active"] and present and via is None:
            item["revoked_at"] = max((x.revoked_at for x in rec["entries"] if x.revoked_at), default=None)
            revoked_present.append(item)
        seen.add((okind, mv))
    only_fw = []
    for okind, obj in snap["objects"].items():
        if not obj.found:
            continue
        for raw in obj.raw:
            norm, _net = _parse_value(raw)
            if (okind, norm) not in index:
                only_fw.append({"kind": okind, "kind_label": "IP" if okind == "ip" else "Домен",
                                "value": raw, "object": obj.name})
    key = lambda x: x["value"]
    return {"pending": sorted(pending, key=key), "revoked_present": sorted(revoked_present, key=key),
            "only_fw": sorted(only_fw, key=lambda x: (x["kind"], x["value"]))}


def summary(snap=None, index=None, lists=None):
    """Метрики главной."""
    snap = snap or snapshot()
    index = index if index is not None else _value_index()
    lists = lists or sync_lists(snap, index)
    qs = RegulatorEntry.objects.all()
    active = qs.filter(status="active")
    by_kind = dict(active.values_list("kind").annotate(n=Count("id")))
    return {
        "active_total": sum(v for k, v in by_kind.items() if k != "hash"),
        "active_ip": by_kind.get("ip", 0), "active_fqdn": by_kind.get("fqdn", 0),
        "active_url": by_kind.get("url", 0), "active_hash": by_kind.get("hash", 0),
        "active_domains": by_kind.get("fqdn", 0) + by_kind.get("url", 0),
        "entries_total": qs.count(), "entries_no_hash": qs.exclude(kind="hash").count(),
        "pending_count": len(lists["pending"]),
        "revoked_present_count": len(lists["revoked_present"]),
        "only_fw_count": len(lists["only_fw"]),
        "sync_issues_count": len(lists["pending"]) + len(lists["revoked_present"]) + len(lists["only_fw"]),
        "letters_count": RegulatorDocument.objects.count(),
    }


def letters(snap=None, index=None):
    """Письма со счётчиками и прогрессом применения."""
    snap = snap or snapshot()
    index = index if index is not None else _value_index()
    rows = []
    docs = RegulatorDocument.objects.prefetch_related("entries").all()
    for d in docs:
        entries = list(d.entries.all())
        counts = defaultdict(int)
        applied = pending = revoked_present = 0
        for e in entries:
            counts[e.kind] += 1
            if e.kind == "hash" or e.status != "active":
                if e.kind != "hash" and e.status != "active":
                    st, _ = fw_state(e, snap, index)
                    revoked_present += st == "revoked_present"
                continue
            e.document = d
            st, _ = fw_state(e, snap, index)
            applied += st == "applied"
            pending += st == "pending"
        applicable = applied + pending
        rows.append({
            "id": d.id, "reference_no": d.reference_no, "document_date": d.document_date,
            "note": d.note, "created_by": d.created_by, "created_at": d.created_at,
            "url": reverse("ip_plan:regulator_document_detail", args=[d.id]),
            "total": len(entries), "ip": counts["ip"], "fqdn": counts["fqdn"], "url_count": counts["url"],
            "hash": counts["hash"], "revoked": sum(1 for e in entries if e.status != "active"),
            "applicable": applicable, "applied": applied, "pending": pending,
            "revoked_present": revoked_present,
            "pct": round(100 * applied / applicable) if applicable else 100,
            "all_applied": applicable > 0 and pending == 0,
            "known": snap["available"],
        })
    return rows


# ---------------------------------------------------------------- проверка значения

def _classify(value):
    v = refang((value or "").strip())
    if not v:
        return None, ""
    try:
        net = ipaddress.ip_network(v, strict=False)
        if net.version == 4:
            return "ip", str(net.network_address) if net.prefixlen == 32 else str(net)
    except ValueError:
        pass
    if "://" in v or "/" in v:
        return "url", RegulatorEntry.compute_match_value("url", v)
    return "fqdn", v.lower().rstrip(".")


def check(value, snap=None, index=None):
    """Проверка одного значения для плашки на главной."""
    kind, mv = _classify(value)
    if not kind:
        return None
    snap = snap or snapshot()
    index = index if index is not None else _value_index()
    okind = OBJECT_KIND[kind]
    rec = index.get((okind, mv))
    obj = snap["objects"].get(okind)
    present, via = (obj.contains(mv) if obj is not None and obj.found else (False, None))
    letters_ = []
    if rec:
        for e in rec["entries"]:
            letters_.append({"id": e.document_id, "reference_no": e.document.reference_no,
                             "document_date": e.document.document_date, "status": e.status,
                             "url": reverse("ip_plan:regulator_document_detail", args=[e.document_id])})
    if rec and rec["active"]:
        outcome = "blocked" if present else ("pending" if snap["available"] else "unknown")
    elif rec:
        outcome = "revoked_present" if present else "removed"
    else:
        outcome = "only_fw" if present else "not_found"
    titles = {"blocked": "Заблокирован", "pending": "В реестре, на xFW ещё нет",
              "revoked_present": "Снят, но ещё стоит на xFW", "removed": "Снят и убран с xFW",
              "only_fw": "Стоит на xFW, в письмах нет", "not_found": "Не найден",
              "unknown": "В реестре; снимка xFW нет"}
    return {"query": value, "kind": kind, "kind_label": KIND_LABELS.get(kind, kind), "normalized": mv,
            "outcome": outcome, "title": titles[outcome], "letters": letters_,
            "object": obj.name if (obj is not None and present) else None, "via": via}


# ---------------------------------------------------------------- мастер команд

def commands(snap=None, index=None):
    """Шаги мастера: по каждому объекту — что изменится и готовая команда."""
    snap = snap or snapshot()
    index = index if index is not None else _value_index()
    blocks = []
    for okind, title in (("ip", "IP-адреса"), ("fqdn", "Домены и хосты из URL")):
        obj = snap["objects"][okind]
        rec = snap["names"][okind]
        to = rec.next_name()
        add, remove = [], []
        for (k, mv), r in index.items():
            if k != okind:
                continue
            present, via = obj.contains(mv) if obj.found else (False, None)
            if r["active"] and not present:
                add.append(mv)
            elif not r["active"] and present and via is None:
                remove.append(mv)
        remove_set = set(remove)
        keep = [raw for raw in obj.raw if _parse_value(raw)[0] not in remove_set]
        final = keep + sorted(add)
        blocks.append({
            "kind": okind, "title": title, "from": obj.name, "to": to, "wanted": obj.wanted,
            "found": obj.found, "fallback": obj.fallback,
            "keep_count": len(obj.raw) - len(remove), "add": sorted(add), "remove": sorted(remove),
            "count": len(final), "changed": bool(add or remove),
            "command": f"firewall ip-object add name @{to} {','.join(final)}" if final else "",
        })
    device = acl_store.get_device(FIREWALL)
    rules = []
    if device is not None:
        for b in blocks:
            info = acl_store.get_object_group(FIREWALL, b["from"])
            if info:
                for acl_name in info["used_by"]:
                    for acl in device.acls:
                        if acl.name != acl_name:
                            continue
                        for r in acl.rules:
                            ep = [r.source, r.destination]
                            if any(x and b["from"] in (x.object_groups or [x.object_group]) for x in ep):
                                if r.name and r.name not in rules:
                                    rules.append(r.name)
    blocked = not snap["available"] or any(not b["found"] for b in blocks)
    return {"blocks": blocks, "rules": rules or DEFAULT_RULES,
            "any_changes": any(b["changed"] for b in blocks),
            "blocked": blocked,
            "blocked_reason": ("Снимка объектов xFW нет — список собрать нельзя: значения, "
                               "которые сейчас стоят на firewall, выпали бы из нового списка.") if blocked else "",
            "snapshot": {"at_text": snap["at_text"], "waiting": snap["waiting_snapshot"]}}
