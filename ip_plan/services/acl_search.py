"""Раздел «ACL» v2: ответ на вопрос «где встречается адрес или подсеть».

Поверх разобранных снимков (acl_store / acl_model) считает то, чего в
модели нет, но что требует макет (test_des/AclWhere, AclList):

- запрос — одиночный IPv4 или подсеть CIDR (`parse_query`);
- операнд правила раскладывается на части: литералы, объекты, узлы ViPNet,
  any (`_parts`) — `Endpoint.networks` плоский, и без этого не узнать, какая
  именно часть операнда дала попадание;
- **категория совпадения**: exact (адрес указан явно) → net (входит в
  подсеть правила) → obj (через объект/группу) → broad (через сеть короче
  `WIDE_PREFIX`, например @PrivateNetworkIP 10/8 или 0/0) → any (any с обеих
  сторон). От неё зависят сортировка, цвет точки и свёртки внизу;
- **цепочка «почему совпало»**: адрес → подсеть → объект → вложенный объект
  (обход `ParsedDevice.object_group_includes`), для узлов ViPNet — id и имя;
- «явный запрет» = drop/deny в категориях exact/net/obj; запреты через
  широкие наборы и any явными не считаются (решение по макету 01.10.2026).

Сюда же вынесены каталог списков и групп, меню устройств, сводка по датам
снимков, строки списка правил с фрагментами (text / any / group / hit) и
содержимое панели группы — всё в виде словарей, готовых для шаблонов
(контракт — scratchpad/design/acl/CONTRACT-acl.md на время разработки,
затем CLAUDE.md раздел «ACL v2»).
"""
import ipaddress
import logging
import re
from datetime import datetime

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from . import acl_store
from .vipnet_firewall import _norm_name, _split_refs, _value_networks

logger = logging.getLogger(__name__)

# Сети короче этого префикса считаются «широким набором»: адрес попадает в
# них не потому, что его кто-то указал, а потому что правило охватывает все
# частные сети или весь интернет. Настраивается через settings.ACL_WIDE_PREFIX.
WIDE_PREFIX = int(getattr(settings, "ACL_WIDE_PREFIX", 16) or 16)

ANY_NET = ipaddress.ip_network("0.0.0.0/0")

CATEGORY_RANK = {"exact": 0, "net": 1, "obj": 2, "broad": 3, "any": 4}

ACTION_LABELS = {"permit": "Разрешает", "deny": "Запрещает", "nat": "NAT", "remark": "Комментарий"}

# Секции межсетевых экранов и типы Cisco: подпись вкладки и назначение
# человеческим языком (подпись группы в результате и в каталоге).
SECTION_LABELS = {
    "local": ("Local", "трафик к самому устройству"),
    "forward": ("Forward", "транзитный трафик"),
    "tunnel": ("Tunnel", "трафик туннелей"),
    "vpn": ("VPN", "трафик VPN"),
    "nat": ("NAT", "трансляция адресов"),
    "standard": ("Standard", ""),
    "extended": ("Extended", ""),
}

ROLE_WORDS = {"src": "источник", "dst": "назначение"}

_VPN_NODE_RE = re.compile(r"(0x[0-9a-fA-F]{4,8})\s+([^;]*)")
_IPV4_IN_TEXT_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


class QueryError(ValueError):
    """Строка запроса — не IPv4-адрес и не подсеть."""


# ---------------------------------------------------------------- запрос

def parse_query(text: str):
    """«10.1.141.166» → (IPv4Network 10.1.141.166/32, False);
    «10.1.141.0/24» → (сеть, True). IPv6 и мусор — QueryError."""
    q = (text or "").strip()
    if not q:
        raise QueryError("Пустой запрос")
    try:
        net = ipaddress.ip_network(q, strict=False)
    except ValueError:
        raise QueryError(f"«{q}» — не похоже на IPv4-адрес или подсеть") from None
    if net.version != 4:
        raise QueryError(f"«{q}» — IPv6 в снимках не ищем, только IPv4")
    return net, net.prefixlen != 32


# ---------------------------------------------------------------- части операнда

class Part:
    """Кусок операнда: литерал (адрес/сеть/диапазон), объект, узел ViPNet,
    any или нераспознанное (FQDN, iface, @local)."""

    __slots__ = ("text", "kind", "networks", "object", "title", "hit_parts", "node_id")

    def __init__(self, text, kind, networks=(), obj=None, title=None):
        self.text = text
        self.kind = kind
        self.title = title   # подсказка: у узла ViPNet — его идентификатор и адреса
        self.hit_parts = ()  # у чипа «N узлов» — исходные части-узлы (для подсветки)
        self.node_id = None  # у узла ViPNet — его идентификатор 0x…
        # Самые узкие сети первыми: у объекта с 10/8 и 10.1.141.0/24 попадание
        # должно объясняться через /24, а не через /8.
        self.networks = sorted(networks, key=lambda n: -n.prefixlen)
        self.object = obj


def _object_text(device, name):
    """Как объект пишется в правиле у этого вендора: ViPNet — «@имя»,
    Cisco и коммутаторы — просто имя."""
    return f"@{name}" if device.parser == "xfw" else name


def _find_object(device, name):
    if name in device.object_groups:
        return name
    want = _norm_name(name)
    for known in device.object_groups:
        if _norm_name(known) == want:
            return known
    return None


def _node_networks(rule, side, node_id):
    """Адреса узла ViPNet из extra["{side}_vpn_nodes»] («0x4f260038 имя
    (7.0.0.45, 11.0.0.6)») — как /32. Имя узла — вторым элементом."""
    extra = rule.extra or {}
    # Точные сети узла (с туннельными диапазонами) парсер кладёт в
    # «{side}_node_nets»: «0x…: 10.1.141.160/29, 7.0.0.45/32; 0x…: …».
    for chunk in (extra.get(f"{side}_node_nets") or "").split(";"):
        nid, _, nets_text = chunk.strip().partition(":")
        if nid.lower() != node_id.lower():
            continue
        nets = []
        for token in nets_text.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                nets.append(ipaddress.ip_network(token, strict=False))
            except ValueError:
                continue
        name = ""
        for m in _VPN_NODE_RE.finditer(extra.get(f"{side}_vpn_nodes") or ""):
            if m.group(1).lower() == node_id.lower():
                name = _node_name(m.group(2))
                break
        return nets, name
    text = extra.get(f"{side}_vpn_nodes") or ""
    for m in _VPN_NODE_RE.finditer(text):
        if m.group(1).lower() != node_id.lower():
            continue
        rest = m.group(2)
        nets = []
        for ip in _IPV4_IN_TEXT_RE.findall(rest):
            try:
                nets.append(ipaddress.ip_network(f"{ip}/32"))
            except ValueError:
                continue
        return nets, _node_name(rest)
    return [], ""


def _node_name(rest):
    """«Имя Фамилия VPN-20262 (адреса)» → «Имя Фамилия»: номер сети ViPNet
    пользователю в строке правила не нужен (он остаётся в подсказке).
    «(нет в iplir)» имени не содержит — пустая строка."""
    rest = rest.strip()
    if rest.startswith("("):
        return ""
    name = rest.split(" (")[0].strip()
    return re.sub(r"\s+VPN-\d+$", "", name).strip()


def _split_members(text):
    """Состав «0x… имя (пары; пары); 0x… имя (…)» → список узлов: «;»
    внутри скобок разделяет пары адресов одного узла, а не узлы."""
    out, buf, depth = [], [], 0
    for ch in text or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def _parts(rule, side, device):
    """Операнд → список Part. Для простых операндов (один литерал, один
    объект, any) — одна часть; смешанные операнды ViPNet режутся по тому же
    правилу, что и в парсере (_split_refs), и каждая часть разрешается
    заново по словарю объектов устройства."""
    ep = getattr(rule, side, None)
    if ep is None:
        return []
    if ep.kind == "any":
        return [Part(ep.raw or "any", "any", [ANY_NET])]
    objects = acl_store._endpoint_objects(ep)
    if ep.kind == "object-group" and len(objects) == 1:
        name = objects[0]
        return [Part(_object_text(device, name), "object", device.object_groups.get(name) or [], name)]
    if ep.kind in ("host", "network", "wildcard", "range") and not objects and device.parser != "xfw":
        return [Part(ep.raw, "literal", ep.networks)]

    known = {_norm_name(n) for n in device.object_groups}
    parts = []
    for item in _split_refs(ep.raw or "", known):
        item = item.strip()
        if not item:
            continue
        if item.startswith("@"):
            name = item[1:].rstrip()
            if name == "any":
                parts.append(Part("@any", "any", [ANY_NET]))
                continue
            found = _find_object(device, name)
            if found:
                parts.append(Part(f"@{found}", "object", device.object_groups.get(found) or [], found))
            else:
                parts.append(Part(item, "other"))
            continue
        parsed = _value_networks(item)
        if parsed is not None:
            parts.append(Part(item, "literal", parsed[1]))
            continue
        if re.match(r"^0x[0-9a-fA-F]{4,8}$", item):
            # Узел ViPNet показываем именем из iplir (как в старом теге
            # acl_endpoint), идентификатор — в подсказке.
            nets, name = _node_networks(rule, side, item)
            addrs = ", ".join(str(n.network_address) if n.prefixlen == 32 else str(n) for n in nets[:6])
            title = " · ".join(x for x in (item, addrs) if x)
            if not name:
                title = f"{item} · нет в iplir show config"
            part = Part(name or item, "node", nets, title=title)
            part.node_id = item.lower()
            parts.append(part)
            continue
        parts.append(Part(item, "other"))
    if not parts:
        parts = [Part(ep.raw, "literal" if ep.resolved else "other", ep.networks if ep.resolved else [])]
    # Сети, которые парсер нашёл в операнде, а переразбор по частям — нет
    # (туннельные диапазоны узлов ViPNet из iplir, адреса узлов с
    # маской): отдаём их узлам, иначе совпадение по ним потеряется.
    covered = {n for part in parts for n in part.networks}
    leftover = [n for n in ep.networks if n not in covered]
    if leftover:
        nodes = [part for part in parts if part.kind == "node"]
        if len(nodes) == 1:
            nodes[0].networks = sorted(nodes[0].networks + leftover, key=lambda n: -n.prefixlen)
        elif nodes:
            parts.append(Part(", ".join(n.text for n in nodes), "node", leftover))
        else:
            parts.append(Part(ep.raw, "literal", leftover))
    return parts


# ---------------------------------------------------------------- совпадение

class SideMatch:
    __slots__ = ("part", "network", "category", "relation", "tunnel")

    def __init__(self, part, network, category, relation, tunnel=None):
        self.part, self.network, self.category, self.relation = part, network, category, relation
        self.tunnel = tunnel   # «lo-hi» — туннелируемый диапазон координатора, через который совпало


def _node_tunnels(device, node_id):
    """[(«lo-hi», [сети])] из строк tunnel= узла iplir (часть до « to »)."""
    info = (getattr(device, "vpn_nodes", None) or {}).get((node_id or "").lower())
    out = []
    for rng in (info or {}).get("tunnels") or ():
        real = rng.split(" to ")[0].strip()
        parsed = _value_networks(real)
        if parsed is not None:
            out.append((real, parsed[1]))
    return out


def _tunnel_hit(device, part, target):
    """Диапазон туннеля узла, содержащий запрос, либо None."""
    if part.kind != "node" or not part.node_id:
        return None
    for rng, nets in _node_tunnels(device, part.node_id):
        if any(target.subnet_of(n) for n in nets):
            return rng
    return None


def _excluded(device, part, target):
    """Адрес из исключения объекта (ViPNet Exclusion) совпадением не считается."""
    if part.kind != "object" or not part.object:
        return False
    excl = device.object_group_exclusions.get(part.object) or ()
    return any(target.subnet_of(n) for n in excl)


def _match_part(part, target, is_network, device):
    """SideMatch либо None. relation: exact — сеть правила равна запросу или
    это хост; within — запрос внутри сети правила; inside — сеть правила
    внутри искомой подсети (правило упоминает адрес из неё)."""
    if part.kind == "other" or not part.networks:
        return None
    for net in part.networks:
        if target.subnet_of(net):
            relation = "exact" if (net.prefixlen == 32 or net == target) else "within"
        elif is_network and net.subnet_of(target):
            relation = "inside"
        else:
            continue
        if _excluded(device, part, target):
            return None
        if part.kind == "any":
            category = "any"
        elif net.prefixlen < WIDE_PREFIX:
            category = "broad"
        elif part.kind == "object":
            category = "obj"
        elif relation in ("exact", "inside"):
            category = "exact"
        else:
            category = "net"
        tunnel = None
        if part.kind == "node" and category in ("exact", "net"):
            # Узел-координатор: адрес мог попасть не как адрес самого узла, а
            # как часть туннелируемого за ним диапазона — это «подсеть», не «точно».
            tunnel = _tunnel_hit(device, part, target)
            if tunnel and not any(target.subnet_of(n) for n in part.networks if n.prefixlen == 32 and n not in
                                  {x for _r, ns in _node_tunnels(device, part.node_id) for x in ns}):
                category, relation = "net", "tunnel"
        return SideMatch(part, net, category, relation, tunnel)
    return None


def _match_side(rule, side, device, target, is_network, parts=None):
    """Лучшее совпадение стороны. parts — уже разложенный операнд: тот же
    список потом идёт в токены, иначе «часть, которая совпала» не найдётся
    по identity."""
    best = None
    for part in (parts if parts is not None else _parts(rule, side, device)):
        m = _match_part(part, target, is_network, device)
        if m and (best is None or CATEGORY_RANK[m.category] < CATEGORY_RANK[best.category]):
            best = m
    return best


def _object_records(device, name):
    n = len(device.object_groups.get(name) or ()) or len(device.object_group_members.get(name) or ())
    return n


def _chain(device, rule, side, match, target_text):
    """Цепочка «почему совпало»: [{value, note}, …]; между элементами UI
    рисует «входит в»."""
    part, net = match.part, match.network
    role_note = "указан в источнике" if side == "source" else "указан в назначении"
    net_note = "подсеть в источнике" if side == "source" else "подсеть в назначении"
    if part.kind == "any":
        return [{"value": target_text, "note": ""}, {"value": part.text, "note": "любой адрес"}]
    if part.kind == "literal":
        if match.relation in ("exact", "inside"):
            return [{"value": part.text if match.relation == "inside" else target_text, "note": role_note}]
        return [{"value": target_text, "note": ""}, {"value": str(net), "note": net_note}]
    if part.kind == "node":
        chain = [{"value": target_text, "note": ""}]
        if match.relation == "tunnel":
            chain.append({"value": f"туннель {match.tunnel}", "note": "за координатором"})
        elif str(net) != target_text:
            chain.append({"value": str(net), "note": ""})
        chain.append({"value": part.node_id or part.text, "note": f"узел ViPNet · {part.text}"})
        return chain
    # объект: спускаемся по вложенным объектам до того, который реально
    # содержит совпавшую сеть
    path = [part.object]
    cur = part.object
    while True:
        children = device.object_group_includes.get(cur) or []
        nxt = next((c for c in children if net in (device.object_groups.get(c) or ()) and c not in path), None)
        if not nxt:
            break
        path.append(nxt)
        cur = nxt
    chain = [{"value": target_text, "note": ""}]
    if str(net) != target_text:
        chain.append({"value": str(net), "note": ""})
    for name in reversed(path):
        n = _object_records(device, name)
        chain.append({"value": _object_text(device, name), "note": _plural(n, "запись", "записи", "записей")})
    return chain


# ---------------------------------------------------------------- форматирование

def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{ru_number(n)} {word}"


def ru_number(n):
    """2340 → «2 340» (тонкий неразрывный пробел)."""
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def _shorten(items, keep=2):
    items = [i for i in items if i]
    if len(items) <= keep:
        return ", ".join(items)
    return f"{', '.join(items[:keep])} +{len(items) - keep}"


def _service_items(rule):
    """Сервисы правила списком: у ViPNet — элементы через запятую
    («tcp to 443», «@PING»), у Cisco — протокол и порты одной записью."""
    proto = (rule.protocol or "").strip()
    ports = " ".join(p for p in (rule.source_port, rule.destination_port) if p)
    if not proto and not ports:
        return ["any"]
    if "," in proto:
        return [i.strip() for i in proto.split(",") if i.strip()]
    return [f"{proto} {ports}".strip()]


def _service(rule):
    """(кратко, полностью): ViPNet хранит сервисы текстом («@SMTP (TCP), …»),
    Cisco — протокол и порты."""
    proto = (rule.protocol or "").strip()
    ports = " ".join(p for p in (rule.source_port, rule.destination_port) if p)
    if not proto and not ports:
        return "any", "any"
    full = proto if not ports else f"{proto} {ports}".strip()
    if "," in proto:
        items = [i.strip().lstrip("@") for i in proto.split(",")]
        items = [re.sub(r"\s*\((TCP|UDP|TCP/UDP|ICMP)\)\s*$", "", i, flags=re.I) for i in items]
        return _shorten(items), full
    short = full.lstrip("@")
    return (short if len(short) <= 28 else short[:26] + "…"), full


def _rule_panel_url(device_name, acl_name, rule_id, side, query):
    url = reverse("ip_plan:acl_rule_nodes_panel", args=[device_name, acl_name, rule_id, side])
    return f"{url}?q={query}" if query else url


def _collapse_nodes(parts, match, device, rule, acl, side, query, label=None):
    """Узлы ViPNet, перечисленные прямо в операнде (HW пишет в правило
    идентификаторы, а не объект), ведут себя как группа, а имя этой группы —
    имя правила («Отдел-РИСППР»). Сворачиваем их в одну часть-чип
    «Отдел-РИСППР +5 узлов» (label — имя; без имени — «5 узлов») со ссылкой
    на панель узлов правила. Одиночный узел остаётся именем сотрудника.
    Литералы и объекты остаются как есть, на месте первого узла."""
    nodes = [p for p in parts if p.kind == "node"]
    if not nodes:
        return parts
    out, done = [], False
    for p in parts:
        if p.kind != "node":
            out.append(p)
            continue
        if done:
            continue
        done = True
        names = [n.text for n in nodes]
        count = _plural(len(names), "узел", "узла", "узлов")
        if len(names) == 1:
            text = names[0]
        elif label:
            text = f"{label} +{count}"
        else:
            text = count
        chip = Part(text, "nodes", [n for node in nodes for n in node.networks],
                    title=f"{count} ViPNet: {', '.join(names[:4])}{' …' if len(names) > 4 else ''} · открыть список")
        chip.object = (acl.name, _rule_id(rule), side)
        chip.hit_parts = nodes
        out.append(chip)
    return out


def _tokens(parts, match, device, query, rule=None, acl=None, side=None):
    tokens = []
    for p in parts:
        hit = bool(match and (match.part is p or (p.kind == "nodes" and match.part in getattr(p, "hit_parts", ()))))
        token = {"text": p.text, "kind": p.kind if p.kind in ("object", "literal", "any", "node", "nodes") else "literal",
                 "hit": hit, "panel_url": None, "title": p.title or "", "panel_title": "Группа адресов · открыть"}
        if p.kind == "object" and p.object:
            token["panel_url"] = _panel_url(device.device_name, p.object, query)
        elif p.kind == "nodes" and rule is not None and acl is not None:
            token["panel_url"] = _rule_panel_url(device.device_name, acl.name, _rule_id(rule), side, query)
            token["panel_title"] = "Узлы ViPNet в правиле · открыть"
        tokens.append(token)
    return tokens


def _panel_url(device_name, obj, query):
    url = reverse("ip_plan:acl_group_panel", args=[device_name, obj])
    return f"{url}?q={query}" if query else url


def _list_url(device_name, acl_name, query, anchor=None):
    url = reverse("ip_plan:acl_detail", args=[device_name, acl_name])
    if query:
        url += f"?q={query}"
    if anchor:
        url += f"#{anchor}"
    return url


def _action(rule, acl):
    if rule.action == "remark":
        return "remark"
    if (acl.acl_type or "").lower() == "nat" or (rule.action_raw or "").upper() == "NAT":
        return "nat"
    return "permit" if rule.action == "permit" else "deny"


def _type_label(acl_type):
    label, purpose = SECTION_LABELS.get((acl_type or "").lower(), ((acl_type or "").capitalize(), ""))
    return label, purpose


def _rule_id(rule):
    return str(rule.rule_id or rule.sequence)


def _where_text(applications):
    """«Vlan30, вход» / «NAT inside source» / «доступ по SSH (vty 0 4)» /
    «SNMP community» / «применён» (секция МЭ) / «не применён»."""
    if not applications:
        return "не применён"
    parts = []
    for a in applications:
        if a.kind == "interface":
            d = {"in": "вход", "out": "выход"}.get((a.direction or "").lower(), a.direction or "")
            parts.append(f"{a.location}, {d}" if d else a.location)
        elif a.kind == "line":
            parts.append(f"доступ по SSH ({a.location})")
        elif a.kind == "snmp":
            parts.append("SNMP community")
        elif a.kind == "section":
            parts.append("применён")
        else:
            parts.append(a.location or a.kind)
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return _shorten(seen, keep=2)


# ---------------------------------------------------------------- поиск

def _device_meta(devices):
    return {r["device"]: r for r in acl_store.snapshot_info(devices=devices)}


def _hits_for_device(device, target, is_network, query, model=""):
    """Все совпадения правил устройства с запросом — список словарей HIT."""
    hits = []
    target_text = str(target.network_address) if not is_network else str(target)
    for acl in device.acls:
        applied = device.is_applied(acl.name)
        type_label, _purpose = _type_label(acl.acl_type)
        for rule in acl.rules:
            if rule.action == "remark":
                continue
            src_parts = _parts(rule, "source", device)
            dst_parts = _parts(rule, "destination", device)
            src = _match_side(rule, "source", device, target, is_network, src_parts)
            dst = _match_side(rule, "destination", device, target, is_network, dst_parts)
            if not src and not dst:
                continue
            specific = [(s, m) for s, m in (("src", src), ("dst", dst)) if m and m.category != "any"]
            if specific:
                best_side, best = min(specific, key=lambda sm: CATEGORY_RANK[sm[1].category])
                role = "both" if len(specific) == 2 else best_side
            else:
                best_side, best = ("src", src) if src else ("dst", dst)
                role = "both"
            category = best.category
            side_attr = "source" if best_side == "src" else "destination"
            src_has_nodes = any(p.kind == "node" for p in src_parts)
            src_tokens = _tokens(_collapse_nodes(src_parts, src, device, rule, acl, "source", query, rule.name), src, device, query, rule, acl, "source")
            dst_tokens = _tokens(_collapse_nodes(dst_parts, dst, device, rule, acl, "destination", query, None if src_has_nodes else rule.name), dst, device, query, rule, acl, "destination")
            src_short = _shorten([t["text"] for t in src_tokens]) or "—"
            dst_short = _shorten([t["text"] for t in dst_tokens]) or "—"
            if best.relation == "tunnel":
                via, via_text = f"{best.part.text} · туннель {best.tunnel}", None
            elif category == "exact":
                via, via_text = target_text, "указан явно"
            elif best.part.kind == "object":
                via, via_text = (f"{best.part.text} · {best.network}" if category == "broad" else best.part.text), None
            else:
                via, via_text = str(best.network), None
            obj = None
            if best.part.kind == "object" and best.part.object:
                obj = {"name": best.part.text, "device": device.device_name,
                       "panel_url": _panel_url(device.device_name, best.part.object, query),
                       "detail_url": reverse("ip_plan:acl_object_group_detail", args=[device.device_name, best.part.object])}
            service_short, service_full = _service(rule)
            rid = _rule_id(rule)
            hits.append({
                "key": f"{device.device_name}|{acl.name}|{rid}",
                "rule_id": rid, "name": rule.name or rule.label or "", "seq": rule.sequence,
                "action": _action(rule, acl), "action_label": ACTION_LABELS.get(_action(rule, acl), ""),
                "inactive": bool(rule.inactive), "schedule": rule.schedule,
                "role": role, "category": category, "via": via, "via_text": via_text,
                "device": device.device_name, "model": model, "acl_name": acl.name, "type_label": type_label,
                "applied": applied,
                "list_url": _list_url(device.device_name, acl.name, query, f"rule-{rid}"),
                "src": {"tokens": src_tokens, "short": src_short},
                "dst": {"tokens": dst_tokens, "short": dst_short},
                "other_short": src_short if role == "dst" else dst_short,
                "service_short": service_short, "service_full": service_full,
                "chain": _chain(device, rule, side_attr, best, target_text),
                "object": obj,
            })
    return hits


def search(query_text, devices=None, selected=None):
    """Результат «Где встречается адрес» для шаблона acl_where.html.
    devices — ограничение площадки (None = все), selected — устройства,
    выбранные пользователем (пусто = все доступные)."""
    target, is_network = parse_query(query_text)
    query = str(target) if is_network else str(target.network_address)
    meta = _device_meta(devices)
    wanted = set(selected or ()) or None

    main, broad, anyr = [], [], []
    for device in acl_store.get_all_devices():
        if not acl_store._wanted(device, None, devices):
            continue
        if wanted and device.device_name not in wanted:
            continue
        model = (meta.get(device.device_name) or {}).get("model") or ""
        for hit in _hits_for_device(device, target, is_network, query, model):
            if hit["category"] == "any":
                anyr.append(hit)
            elif hit["category"] == "broad":
                broad.append(hit)
            else:
                main.append(hit)

    # группы устройство → список, внутри сортировка: точные выше
    groups, index = [], {}
    for hit in main:
        key = f"{hit['device']}|{hit['acl_name']}"
        g = index.get(key)
        if g is None:
            label, purpose = _type_label(_acl_type_of(hit))
            g = index[key] = {
                "key": key, "device": hit["device"], "model": hit["model"],
                "acl_name": hit["acl_name"], "type_label": hit["type_label"], "purpose": purpose,
                "applied": hit["applied"], "where_text": "применён" if hit["applied"] else "не применён",
                "rule_count": 0, "list_url": _list_url(hit["device"], hit["acl_name"], query), "hits": [],
            }
            groups.append(g)
        g["hits"].append(hit)
        g["rule_count"] += 1
    for g in groups:
        g["hits"].sort(key=lambda h: (CATEGORY_RANK[h["category"]], h["seq"]))

    roles = ("any", "src", "dst")
    cat_counts = {}
    for cat in ("all", "exact", "net", "obj"):
        cat_counts[cat] = {}
        for role in roles:
            cat_counts[cat][role] = sum(
                1 for h in main
                if (cat == "all" or h["category"] == cat) and (role == "any" or h["role"] in (role, "both")))

    # сводка по широким наборам: через какие объекты/сети адрес попал
    by_via = {}
    for h in broad:
        by_via.setdefault(h["via"], 0)
        by_via[h["via"]] += 1
    objects = sorted(by_via.items(), key=lambda kv: -kv[1])
    broad_objects = []
    for via, count in objects[:3]:
        name, _, network = via.partition(" · ")
        broad_objects.append({"name": name, "network": network, "count": count})
    rest = objects[3:]

    total_devices = len({h["device"] for h in main})
    return {
        "query": query, "is_network": is_network,
        "total_rules": len(main), "total_devices": total_devices,
        "explicit_denies": sum(1 for h in main if h["action"] == "deny"),
        "broad_count": len(broad), "any_count": len(anyr), "hidden_count": len(broad) + len(anyr),
        "cat_counts": cat_counts,
        "groups": groups,
        "broad": {"count": len(broad), "objects": broad_objects,
                  "more_objects_count": len(rest), "more_objects_rules": sum(c for _, c in rest),
                  "hits": sorted(broad, key=lambda h: (h["device"], h["acl_name"], h["seq"]))},
        "any": {"count": len(anyr), "hits": sorted(anyr, key=lambda h: (h["device"], h["acl_name"], h["seq"]))},
        "empty": not main and not broad and not anyr,
    }


def _acl_type_of(hit):
    # type_label обратно в ключ SECTION_LABELS — для purpose
    for key, (label, _p) in SECTION_LABELS.items():
        if label == hit["type_label"]:
            return key
    return ""


# ---------------------------------------------------------------- каталог, устройства, снимки

def catalog(devices=None):
    meta = _device_meta(devices)
    lists = []
    for row in acl_store.overview(devices=devices):
        label, purpose = _type_label(row["acl_type"])
        lists.append({
            "device": row["device"], "model": (meta.get(row["device"]) or {}).get("model") or "",
            "acl_name": row["acl_name"], "acl_type": row["acl_type"], "type_label": label, "purpose": purpose,
            "where_text": _where_text(row["applications"]), "applied": row["applied"],
            "rule_count": row["rule_count"], "list_url": _list_url(row["device"], row["acl_name"], ""),
        })
    lists.sort(key=lambda r: (not r["applied"], -r["rule_count"], r["device"], r["acl_name"]))

    groups = []
    for row in acl_store.list_object_groups(devices=devices):
        used = bool(row["used_by"])
        groups.append({
            "device": row["device"], "name": row["name"], "used_by": row["used_by"],
            "used_text": f"в {_shorten(row['used_by'], keep=3)}" if used else "не используется", "used": used,
            "member_count": row["member_count"],
            "detail_url": reverse("ip_plan:acl_object_group_detail", args=[row["device"], row["name"]]),
            "panel_url": _panel_url(row["device"], row["name"], ""),
            "list_url": _list_url(row["device"], row["used_by"][0], "") if used else None,
        })
    groups.sort(key=lambda r: (not r["used"], -r["member_count"], r["device"], r["name"]))
    return {
        "lists_total": len(lists), "groups_total": len(groups), "lists": lists, "groups": groups,
        "empty_text": "" if lists else "На площадке нет устройств со снимками списков доступа",
    }


def question(devices=None, query="", selected=None, show_site_headers=True, site_labels=None, mode="find"):
    """Блок вопроса: поле, выбранные устройства, меню устройств по площадкам."""
    selected = [s for s in (selected or []) if s]
    # В меню — только устройства, у которых в снимке есть списки: пункт так и
    # называется «Все устройства со списками доступа», а коммутатор без ACL
    # в поиске всё равно ничего не даст.
    rows = [r for r in acl_store.snapshot_info(devices=devices) if r.get("acl_count")]
    site_labels = site_labels or {}
    by_site = {}
    for r in rows:
        by_site.setdefault(r.get("site") or None, []).append(r)
    sites = []
    for site, items in sorted(by_site.items(), key=lambda kv: (kv[0] is None, kv[0] or "")):
        sites.append({
            "site": site, "label": site_labels.get(site, (site or "").upper()) if site else "Без площадки",
            "devices": [{
                "name": r["device"], "model": r.get("model") or "", "acl_count": r["acl_count"],
                "acl_count_text": _plural(r["acl_count"], "список", "списка", "списков"),
                "selected": r["device"] in selected,
            } for r in sorted(items, key=lambda r: r["device"])],
        })
    total = len(rows)
    known = {r["device"] for r in rows}
    selected = [s for s in selected if s in known]
    if not selected:
        label, count, footer = "Все устройства", total, f"Поиск по всем {_plural(total, 'устройству', 'устройствам', 'устройствам')}"
    elif len(selected) == 1:
        label, count, footer = selected[0], None, "Выбрано: 1"
    else:
        label, count, footer = f"Выбрано устройств: {len(selected)}", None, f"Выбрано: {len(selected)}"
    return {
        "query": query or "", "selected": selected, "devices_total": total,
        "button_label": label, "button_count": count, "footer_text": footer,
        "show_site_headers": show_site_headers, "sites": sites, "mode": mode,
        "where_url": reverse("ip_plan:acl_where"),
    }


def snapshot_summary(devices=None):
    rows = acl_store.snapshot_info(devices=devices)
    dates = [r["updated"] for r in rows if r.get("updated")]
    if not dates:
        return {"text": "Снимков нет", "oldest": None, "newest": None, "count": len(rows)}
    oldest, newest = min(dates), max(dates)
    today = timezone.localtime().date() if timezone.is_aware(newest) else datetime.now().date()

    def fmt(d):
        if d.date() == today:
            return f"сегодня, {d:%H:%M}"
        return f"{d:%d.%m %H:%M}"

    if oldest.date() == newest.date() and oldest.strftime("%H:%M") == newest.strftime("%H:%M"):
        text = f"Снимки конфигураций — {fmt(newest)}"
    elif oldest.date() == newest.date():
        text = f"Снимки конфигураций — {fmt(oldest)} … {newest:%H:%M}"
    else:
        text = f"Снимки конфигураций — {fmt(oldest)} … {fmt(newest)}"
    return {"text": text, "oldest": oldest, "newest": newest, "count": len(rows)}


# ---------------------------------------------------------------- список правил

def _match_text(role, match, both):
    if both:
        return "оба направления"
    if match is None:
        return ""
    if match.category == "any":
        return "подходит любой адрес"
    word = ROLE_WORDS.get(role, role)
    if match.relation == "tunnel":
        return f"{word} · туннель узла"
    if match.category == "exact":
        return f"{word} · точно"
    if match.category == "obj":
        return f"{word} · через группу"
    if match.category == "broad" or match.network.prefixlen < 24:
        return f"{word} · входит в /{match.network.prefixlen}"
    return f"{word} · подсеть"


def _fragments(rule, acl, device, src, dst, query, src_parts=None, dst_parts=None):
    """Правило как фрагменты для строки списка.

    ViPNet: «имя · источник → назначение · сервис», где имя — тег (kind
    name), а каждая сторона — теги со стороной (side = src|dst): объект →
    кликабельное имя группы (group), набор адресов/узлов → первые два и
    многоточие, кликабельный (set, открывает карточку стороны), any → any.
    Cisco и коммутаторы: синтаксис устройства, стороны — теми же тегами
    (lit — некликабельный литерал)."""
    frags = []
    given = {"source": src_parts, "destination": dst_parts}

    def text(t):
        if t:
            frags.append({"text": t, "kind": "text", "hit": False})

    def parts_of(side_name):
        parts = given.get(side_name)
        return parts if parts is not None else _parts(rule, side_name, device)

    def side_tags(side_name, match):
        side = "src" if side_name == "source" else "dst"
        parts = parts_of(side_name)
        if not parts:
            text("—")
            return
        objs = [p for p in parts if p.kind == "object"]
        rest = [p for p in parts if p.kind != "object"]
        first = True
        for p in objs:
            if not first:
                text(", ")
            first = False
            frags.append({"text": p.text, "kind": "group", "side": side, "group": p.object,
                          "hit": bool(match and match.category in ("exact", "net", "obj") and match.part is p),
                          "panel_url": _panel_url(device.device_name, p.object, query)})
        if rest:
            if not first:
                text(", ")
            # Подсветка — только осмысленное совпадение: any подходит кому
            # угодно и находкой не считается.
            hit = bool(match and match.category in ("exact", "net", "obj") and any(match.part is p for p in rest))
            if all(p.kind == "any" for p in rest):
                frags.append({"text": rest[0].text, "kind": "any", "side": side, "hit": False})
            elif len(rest) == 1 and device.parser != "xfw":
                frags.append({"text": rest[0].text, "kind": "lit", "side": side, "hit": hit, "title": rest[0].title or ""})
            else:
                # Совпавший элемент — первым, иначе он прячется за «…» и
                # непонятно, почему строка подсвечена.
                ordered = sorted(rest, key=lambda p: 0 if (match and match.part is p) else 1)
                names = [p.text for p in ordered]
                shown = ", ".join(names[:2]) + (" …" if len(names) > 2 else "")
                frags.append({"text": shown, "kind": "set", "side": side, "hit": hit,
                              "title": f"{_plural(len(names), 'запись', 'записи', 'записей')} · открыть список",
                              "panel_url": _rule_panel_url(device.device_name, acl.name, _rule_id(rule), side_name, query)})

    if device.parser == "xfw":
        # Разделители между блоками — иконки (kind sep): перед источником
        # стрелка «из линии», перед назначением — «в линию», перед сервисом — вилка.
        if rule.name:
            frags.append({"text": rule.name, "kind": "name", "hit": False})
        frags.append({"text": "", "kind": "sep", "icon": "src", "hit": False})
        side_tags("source", src)
        frags.append({"text": "", "kind": "sep", "icon": "dst", "hit": False})
        side_tags("destination", dst)
        frags.append({"text": "", "kind": "sep", "icon": "svc", "hit": False})
        short, full = _service(rule)
        items = _service_items(rule)
        frag = {"text": short, "kind": "service", "hit": False, "title": full, "panel_url": None}
        if len(items) > 2:
            frag["title"] = f"{_plural(len(items), 'сервис', 'сервиса', 'сервисов')} · открыть список"
            frag["panel_url"] = _rule_panel_url(device.device_name, acl.name, _rule_id(rule), "service", query)
        frags.append(frag)
    else:
        text(f"{rule.action_raw or rule.action} {rule.protocol or ''} ".replace("  ", " "))
        side_tags("source", src)
        if rule.source_port:
            text(f" {rule.source_port}")
        text(" ")
        side_tags("destination", dst)
        if rule.destination_port:
            text(f" {rule.destination_port}")
        if rule.log:
            text(" log")
    return frags


def listing(device_name, acl_name, query_text=""):
    """Контекст acl_list.html либо None, если устройства/списка нет."""
    device = acl_store.get_device(device_name)
    if device is None:
        return None
    acl = device.acl_by_name(acl_name)
    if acl is None:
        return None
    query = ""
    target = is_network = None
    if (query_text or "").strip():
        try:
            target, is_network = parse_query(query_text)
            query = str(target) if is_network else str(target.network_address)
        except QueryError:
            query = ""
    meta = acl_store.get_meta(device_name) or {}
    apps = [a for a in device.applications if a.acl_name == acl.name]
    label, purpose = _type_label(acl.acl_type)

    rows, match_count = [], 0
    for rule in acl.rules:
        if rule.action == "remark":
            rows.append({"type": "remark", "text": rule.remark or rule.raw or ""})
            continue
        src = dst = None
        src_parts = _parts(rule, "source", device)
        dst_parts = _parts(rule, "destination", device)
        if target is not None:
            src = _match_side(rule, "source", device, target, is_network, src_parts)
            dst = _match_side(rule, "destination", device, target, is_network, dst_parts)
        # Совпадение в списке — только точное: явно, подсеть, объект. Попадание
        # через 10/8, 0/0 (broad) или any подписывается, но не считается и не
        # подсвечивается — иначе «1 из 37» состоит из правил «для всех».
        sides = [(s, m) for s, m in (("src", src), ("dst", dst)) if m]
        specific = [(s, m) for s, m in sides if m.category in ("exact", "net", "obj")]
        is_match = bool(specific)
        if specific:
            role, m = min(specific, key=lambda sm: CATEGORY_RANK[sm[1].category])
            match_text = _match_text(role, m, both=len(specific) == 2)
        elif sides:
            role, m = min(sides, key=lambda sm: CATEGORY_RANK[sm[1].category])
            match_text = _match_text(role, m, both=False) if m.category == "broad" else "подходит любой адрес"
        else:
            match_text = ""
        if is_match:
            match_count += 1
        rid = _rule_id(rule)
        rows.append({
            "type": "rule", "seq": rule.sequence, "seq_text": ru_number(rule.sequence), "rule_id": rid,
            "name": rule.name, "action": _action(rule, acl), "action_word": rule.action_raw or rule.action,
            "inactive": bool(rule.inactive),
            "fragments": _fragments(rule, acl, device, src, dst, query, src_parts, dst_parts),
            "match_text": match_text, "is_match": is_match, "anchor": f"rule-{rid}",
            "match_index": match_count if is_match else None,
        })

    if query:
        rows = _collapse(rows)

    section_names = {a.acl_name for a in device.applications if a.kind == "section"}
    siblings = []
    if acl.name in section_names:
        for other in device.acls:
            if other.name in section_names:
                lbl, _p = _type_label(other.acl_type)
                siblings.append({"acl_name": other.name, "type_label": lbl, "rule_count": len(other.rules),
                                 "url": _list_url(device_name, other.name, query), "active": other.name == acl.name})
    rule_total = sum(1 for r in acl.rules if r.action != "remark")
    live = [r for r in acl.rules if r.action != "remark"]
    action_counts = {"pass": 0, "drop": 0, "nat": 0}
    for r in live:
        a = _action(r, acl)
        action_counts["pass" if a == "permit" else "drop" if a == "deny" else "nat"] += 1
    state_counts = {"on": sum(1 for r in live if not r.inactive), "off": sum(1 for r in live if r.inactive)}
    where = _where_text(apps)
    return {
        "device": device_name, "model": meta.get("model") or "", "acl_name": acl.name,
        "type_label": label, "purpose": purpose,
        "where_text": ("применён на " + where) if apps and where != "применён" else where,
        "applied": bool(apps), "rule_count": rule_total, "rule_count_text": _plural(rule_total, "правило", "правила", "правил"),
        "query": query, "match_count": match_count, "siblings": siblings,
        "history_url": reverse("ip_plan:acl_history", args=[device_name]),
        "back_url": f"{reverse('ip_plan:acl_lookup')}?device={device_name}",
        # Фильтры по действию и состоянию — только у межсетевых экранов
        # ViPNet: у ACL коммутаторов нет выключенных правил, а permit/deny там
        # и так читается по строке.
        "is_firewall": device.parser == "xfw",
        "action_counts": action_counts, "state_counts": state_counts,
        "rows": rows,
    }


def _collapse(rows, min_rules=3):
    """Подряд идущие строки без совпадения (правила и ремарки между ними)
    сворачиваются в одну строку-свёртку, если правил в блоке >= min_rules."""
    out, block = [], []

    def flush():
        n = sum(1 for r in block if r["type"] == "rule")
        if n >= min_rules:
            out.append({"type": "collapsed", "count": n,
                        "count_text": _plural(n, "правило без совпадений", "правила без совпадений", "правил без совпадений"),
                        "rows": list(block)})
        else:
            out.extend(block)
        block.clear()

    for r in rows:
        if r["type"] == "rule" and r["is_match"]:
            flush()
            out.append(r)
        else:
            block.append(r)
    flush()
    return out


# ---------------------------------------------------------------- панель группы

# Жадное имя: скобки могут быть и в имени («(Офис)»), адреса — всегда в
# последних скобках.
_MEMBER_RE = re.compile(r"^(0x[0-9a-fA-F]{4,8})\s+(.*)\s+\(([^()]*)\)\s*$", re.S)
_MEMBER_BARE_RE = re.compile(r"^(0x[0-9a-fA-F]{4,8})\s+(.*?)\s*$", re.S)


def _node_tunnel_info(device, node_id, target):
    """(число туннелей узла, диапазон с запросом или '') для строки панели."""
    tunnels = _node_tunnels(device, node_id)
    hit = ""
    if target is not None:
        for rng, nets in tunnels:
            if any(target.subnet_of(n) for n in nets):
                hit = rng
                break
    return len(tunnels), hit


def _parse_member(text):
    """«0x4f26007c Хрулева С. А. (Офис) VPN-20262 (192.168.1.65 → 11.0.1.6)» →
    {id, name, network, ips (список), ips_text}. Не узел — None."""
    raw = (text or "").strip()
    m = _MEMBER_RE.match(raw)
    if m:
        node_id, head, body = m.group(1), m.group(2), m.group(3) or ""
    else:
        m = _MEMBER_BARE_RE.match(raw)
        if not m:
            return None
        node_id, head, body = m.group(1), m.group(2), ""
    network = ""
    nm = re.search(r"\s*(VPN-\d+)$", head)
    if nm:
        network, head = nm.group(1), head[:nm.start()].rstrip()
    missing = "нет в iplir" in body or "нет в iplir" in head
    if missing:
        body = ""
        head = re.sub(r"\(?нет в iplir\)?", "", head).strip()
    ips = _IPV4_IN_TEXT_RE.findall(body)
    # Пары «реальный → виртуальный» по интерфейсам узла (строки ip= в iplir);
    # «virt X» — координатор видит узел только по виртуальному адресу (за NAT);
    # «туннели: N» — у координаторов, отдельной заметкой.
    pairs, tunnels = [], ""
    for chunk in re.split(r"[;,](?![^(]*\))", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        tm = re.match(r"^туннели:\s*(\d+)$", chunk)
        if tm:
            tunnels = f"туннели: {tm.group(1)}"
            continue
        if chunk.startswith("virt "):
            pairs.append({"real": "", "virt": chunk[5:].strip()})
            continue
        if "→" in chunk:
            real, _, virt = chunk.partition("→")
            pairs.append({"real": real.strip(), "virt": virt.strip()})
        elif chunk != "адресов нет":
            pairs.append({"real": chunk, "virt": ""})
    ips_text = re.sub(r"\bvirt\s+", "", body).strip()
    return {"id": node_id, "name": head.strip() or node_id, "network": network, "ips": ips,
            "ips_text": ips_text, "pairs": pairs, "tunnels": tunnels, "missing": missing}

def group_panel(device_name, name, query_text="", limit=1000):
    info = acl_store.get_object_group(device_name, name)
    if info is None:
        return None
    device = acl_store.get_device(device_name)
    target = None
    query = ""
    if (query_text or "").strip():
        try:
            target, _is_net = parse_query(query_text)
            query = query_text.strip()
        except QueryError:
            pass
    members = []
    # Объект из узлов ViPNet: одна строка на узел (имя, адреса, идентификатор
    # тегом), а не плоский список /32 плюс те же узлы текстом ещё раз.
    covered = set()
    node_rows = []
    for text in info["members"]:
        node = _parse_member(text)
        if node is None:
            continue
        nets = []
        for ip in node["ips"]:
            try:
                nets.append(ipaddress.ip_network(f"{ip}/32"))
            except ValueError:
                continue
        covered.update(nets)
        t_count, t_hit = _node_tunnel_info(device, node["id"], target)
        contains = bool(target is not None and any(target.subnet_of(n) for n in nets)) or bool(t_hit)
        node_rows.append({"text": node["name"], "kind": "node", "contains": contains,
                          "note": "содержит адрес" if contains else "", "ips": node["ips_text"],
                          "tunnels_count": t_count, "tunnel_hit": t_hit,
                          "pairs": node["pairs"], "tunnels": node["tunnels"], "missing": node["missing"],
                          "tag": node["id"], "network": node["network"],
                          "search": f"{node['name']} {node['ips_text']} {node['id']}"})
    for net in info["networks"]:
        if net in covered:
            continue
        contains = bool(target is not None and target.subnet_of(net))
        members.append({"text": str(net), "kind": "network", "contains": contains,
                        "note": "содержит адрес" if contains else "", "search": str(net)})
    members.extend(node_rows)
    # Не-адресные члены объекта (FQDN, iface, @ссылки на неизвестное):
    # get_object_group отдаёт из состава только узлы 0x…, остальное берём из
    # ParsedDevice.object_group_members напрямую — иначе объект из одних
    # доменных имён (BBDNS_1) выглядел бы пустым.
    fqdn_rows = []
    for raw in (device.object_group_members.get(name) or ()) if device is not None else ():
        text = (raw or "").strip()
        if not text or text.lower().startswith("0x") or _value_networks(text) is not None or text.startswith("@"):
            continue
        fqdn_rows.append({"text": text, "kind": "fqdn", "contains": False, "note": "", "search": text})
    members.extend(fqdn_rows)
    for child in info["includes"]:
        members.append({"text": _object_text(device, child), "kind": "group", "contains": False,
                        "note": "вложенная группа", "panel_url": _panel_url(device_name, child, query),
                        "search": child})
    for net in info["exclusions"]:
        members.append({"text": str(net), "kind": "exclusion", "contains": False, "note": "кроме", "search": str(net)})
    order = {"network": 0, "node": 1, "fqdn": 2, "group": 3, "exclusion": 4}
    members.sort(key=lambda m: (not m["contains"], order.get(m["kind"], 9)))
    n_nodes = len(node_rows)
    n_fqdn = len(fqdn_rows)
    n_missing = sum(1 for r in node_rows if r["missing"])
    total = len(members)
    used = info["used_by"]
    return {
        "device": device_name, "name": name, "query": query, "long_name": len(name) > 24,
        "member_count": n_nodes or len(info["networks"]) or n_fqdn,
        "member_count_text": (_plural(n_nodes, "узел", "узла", "узлов") if n_nodes
                              else _plural(len(info["networks"]), "адрес", "адреса", "адресов") if info["networks"]
                              else _plural(n_fqdn, "доменное имя", "доменных имени", "доменных имён")),
        "wide": n_nodes > 0,
        "missing_count": n_missing,
        "used_by": used, "used_text": f"используется в {_shorten(used, keep=3)}" if used else "не используется",
        "includes": info["includes"],
        "detail_url": reverse("ip_plan:acl_object_group_detail", args=[device_name, name]),
        "members": members[:limit], "shown": min(total, limit), "more": max(0, total - limit),
    }


def _other_note(text):
    """Подпись для части операнда без сетей. Доменные имена, @local,
    @tunneledip и интерфейсы — штатные операнды ViPNet, подписи не нужны
    (решение 01.10.2026); пометка остаётся только у того, что парсер и
    впрямь не распознал."""
    low = (text or "").strip().lower()
    if low in ("@local", "@tunneledip") or low.startswith("iface ") or low.startswith("@"):
        return ""
    if re.match(r"^[a-z0-9*_][a-z0-9*_\-]*(?:\.[a-z0-9_\-]+)+\.?$", low):
        return ""
    return "не разобрано"


def rule_nodes_panel(device_name, acl_name, rule_id, side, query_text=""):
    """Карточка стороны правила (источник или назначение): узлы ViPNet с
    парами адресов, литералы, объекты (кликабельные), any. Тот же формат,
    что у group_panel, чтобы шаблон _acl_group_panel.html был общим."""
    device = acl_store.get_device(device_name)
    if device is None:
        return None
    acl = device.acl_by_name(acl_name)
    if acl is None or side not in ("source", "destination", "service"):
        return None
    rule = next((r for r in acl.rules if _rule_id(r) == str(rule_id)), None)
    if rule is None:
        return None
    rid = _rule_id(rule)
    if side == "service":
        items = _service_items(rule)
        return {
            "device": device_name, "name": rule.name or rid, "query": "",
            "long_name": len(rule.name or rid) > 24, "kicker": "Сервис правила",
            "member_count": len(items), "member_count_text": _plural(len(items), "сервис", "сервиса", "сервисов"),
            "missing_count": 0, "used_by": [acl.name], "used_text": f"правило {rid} в списке {acl.name}",
            "includes": [], "detail_url": _list_url(device_name, acl.name, "", f"rule-{rid}"),
            "detail_text": "Правило в списке →",
            "members": [{"text": i, "kind": "network", "contains": False, "note": "", "search": i} for i in items],
            "shown": len(items), "more": 0, "wide": False,
        }
    target, query = None, ""
    if (query_text or "").strip():
        try:
            target, _is_net = parse_query(query_text)
            query = query_text.strip()
        except QueryError:
            pass

    def contains(nets):
        return bool(target is not None and any(target.subnet_of(n) for n in nets))

    rows = []
    for chunk in _split_members((rule.extra or {}).get(f"{side}_vpn_nodes", "")):
        node = _parse_member(chunk)
        if node is None:
            continue
        nets = []
        for ip in node["ips"]:
            try:
                nets.append(ipaddress.ip_network(f"{ip}/32"))
            except ValueError:
                continue
        t_count, t_hit = _node_tunnel_info(device, node["id"], target)
        c = contains(nets) or bool(t_hit)
        rows.append({"text": node["name"], "kind": "node", "contains": c,
                     "note": "содержит адрес" if c else "", "ips": node["ips_text"],
                     "tunnels_count": t_count, "tunnel_hit": t_hit,
                     "pairs": node["pairs"], "tunnels": node["tunnels"], "missing": node["missing"],
                     "tag": node["id"], "network": node["network"],
                     "search": f"{node['name']} {node['ips_text']} {node['id']}"})
    n_nodes = len(rows)
    for p in _parts(rule, side, device):
        if p.kind == "literal":
            c = contains(p.networks)
            rows.append({"text": p.text, "kind": "network", "contains": c,
                         "note": "содержит адрес" if c else "", "search": p.text})
        elif p.kind == "object" and p.object:
            rows.append({"text": p.text, "kind": "group", "contains": contains(p.networks),
                         "note": "группа адресов", "panel_url": _panel_url(device_name, p.object, query),
                         "search": p.text})
        elif p.kind == "any":
            rows.append({"text": p.text, "kind": "network", "contains": target is not None,
                         "note": "любой адрес", "search": p.text})
        elif p.kind == "other":
            rows.append({"text": p.text, "kind": "network", "contains": False, "note": _other_note(p.text), "search": p.text})
    rows.sort(key=lambda m: not m["contains"])
    side_word = "источник" if side == "source" else "назначение"
    total = len(rows)
    return {
        "device": device_name, "name": rule.name or rid, "query": query,
        "long_name": len(rule.name or rid) > 24,
        "kicker": f"{side_word.capitalize()} правила",
        "member_count": total,
        "member_count_text": (_plural(n_nodes, "узел", "узла", "узлов") if n_nodes == total
                              else _plural(total, "запись", "записи", "записей")),
        "missing_count": sum(1 for r in rows if r.get("missing")),
        "used_by": [acl.name], "used_text": f"правило {rid} в списке {acl.name}",
        "includes": [], "detail_url": _list_url(device_name, acl.name, query, f"rule-{rid}"),
        "detail_text": "Правило в списке →",
        "members": rows, "shown": total, "more": 0, "wide": n_nodes > 0,
    }
