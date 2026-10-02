"""Парсер ACL (access-list) из Cisco IOS running-config.

Не обращается ни к сети, ни к NetBox — чистая обработка текста, которую уже
сохранил netbox-sync/netbox_config_backup.py в config_backups/<device>/latest.cfg
(см. acl_store.py, который читает эти файлы и вызывает parse_config()).

Покрывает ровно то, что реально встречается в конфигурациях Cisco ASR этого
проекта (проверено построчно на обоих running-config перед написанием
парсера — см. docs/plans, если есть):
  - именованные ACL:      ip access-list standard|extended NAME  + отступ-тело
  - однострочные numbered: access-list N permit|deny ...
  - object-group network:  для резолва "object-group NAME" в источнике/назначении
  - применение ACL:        ip access-group / access-class / snmp-server community
                            RO/RW N / ip nat ... list NAME

Специально не поддерживается (не встречалось в данных, не пытаемся угадать):
  range/gt/lt/neq для портов, discontiguous wildcard-маски, ACL с explicit
  sequence-номерами в теле, object-group nesting (group-object).
"""

from __future__ import annotations

import ipaddress
import re

# Cisco 'eq <port>' допускает как номер, так и mnemonic-имя порта — в данных
# встречаются ftp/domain, но список расширен на другие частые mnemonics.
_PORT_NAMES = {
    "ftp": 21, "ssh": 22, "telnet": 23, "smtp": 25, "domain": 53,
    "www": 80, "pop3": 110, "ntp": 123, "https": 443, "syslog": 514,
    "snmp": 161, "bgp": 179, "ldap": 389,
}

_STANDARD_RANGES = ((1, 99), (1300, 1999))
_EXTENDED_RANGES = ((100, 199), (2000, 2699))

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _looks_like_ipv4(tok: str) -> bool:
    return bool(_IPV4_RE.match(tok)) and all(0 <= int(o) <= 255 for o in tok.split("."))


def classify_numbered_acl(number: int) -> str | None:
    """standard | extended | None (номер вне известных диапазонов ACL)."""
    for lo, hi in _STANDARD_RANGES:
        if lo <= number <= hi:
            return "standard"
    for lo, hi in _EXTENDED_RANGES:
        if lo <= number <= hi:
            return "extended"
    return None


def _port_to_text(tok: str) -> str:
    return tok  # отображаем как в конфиге; резолв в номер не нужен для поиска


# Датаклассы вынесены в acl_model.py (общая модель для всех вендорских
# парсеров); здесь они реэкспортируются, чтобы старые импорты
# `from .cisco_acl import Endpoint` не ломались.
from .acl_model import Acl, AclRule, Application, Endpoint, ParsedDevice  # noqa: F401


# ── Разбор одного endpoint'а (source/destination) ──────────────────────────

def _wildcard_to_network(ip: str, wildcard: str):
    """Cisco wildcard-маска (инверсия netmask) -> ipaddress.IPv4Network.
    Возвращает None для несмежных ("discontiguous") масок — крайне редкая
    ACL-техника, которую одной сетью не выразить; в данных не встречалась."""
    try:
        base_i = int(ipaddress.IPv4Address(ip))
        wc_i = int(ipaddress.IPv4Address(wildcard))
    except ValueError:
        return None
    netmask_i = (~wc_i) & 0xFFFFFFFF
    ones = bin(netmask_i).count("1")
    expected = (0xFFFFFFFF << (32 - ones)) & 0xFFFFFFFF if ones else 0
    if netmask_i != expected:
        return None
    net_i = base_i & netmask_i
    try:
        return ipaddress.ip_network((net_i, ones), strict=False)
    except ValueError:
        return None


def _parse_endpoint(tokens: list, i: int, object_groups: dict, unresolved: set):
    """Возвращает (Endpoint, следующий_индекс) или (None, i), если распознать
    не удалось (грамматика не покрывает этот случай)."""
    if i >= len(tokens):
        return None, i
    tok = tokens[i]

    if tok == "any":
        return Endpoint(raw="any", kind="any", networks=[ipaddress.ip_network("0.0.0.0/0")]), i + 1

    if tok == "host":
        if i + 1 >= len(tokens):
            return None, i
        ip = tokens[i + 1]
        try:
            net = ipaddress.ip_network(f"{ip}/32")
        except ValueError:
            return Endpoint(raw=f"host {ip}", kind="host", resolved=False), i + 2
        return Endpoint(raw=f"host {ip}", kind="host", networks=[net]), i + 2

    if tok == "object-group":
        if i + 1 >= len(tokens):
            return None, i
        name = tokens[i + 1]
        nets = object_groups.get(name)
        if nets is None:
            unresolved.add(name)
            return Endpoint(raw=f"object-group {name}", kind="object-group",
                             object_group=name, object_groups=[name], resolved=False), i + 2
        return Endpoint(raw=f"object-group {name}", kind="object-group",
                         object_group=name, object_groups=[name], networks=list(nets)), i + 2

    # Голый IP — либо "IP WILDCARD" (сеть), либо просто IP (неявный host,
    # стандартная запись для ACL типа 'access-list 20 deny 178.162.194.210').
    if _looks_like_ipv4(tok):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt and _looks_like_ipv4(nxt):
            net = _wildcard_to_network(tok, nxt)
            ep = Endpoint(raw=f"{tok} {nxt}", kind="wildcard",
                           networks=[net] if net else [], resolved=net is not None)
            return ep, i + 2
        try:
            net = ipaddress.ip_network(f"{tok}/32")
        except ValueError:
            return Endpoint(raw=tok, kind="host", resolved=False), i + 1
        return Endpoint(raw=tok, kind="host", networks=[net]), i + 1

    return None, i


_PORT_OPS = {"eq", "range", "gt", "lt", "neq"}


def _parse_port_spec(tokens: list, i: int):
    """'eq 22' / 'eq domain' / 'range 1 100' -> (текст, next_i) либо (None, i)."""
    if i >= len(tokens) or tokens[i] not in _PORT_OPS:
        return None, i
    op = tokens[i]
    if op == "range" and i + 2 < len(tokens):
        return f"range {tokens[i + 1]}-{tokens[i + 2]}", i + 3
    if i + 1 < len(tokens):
        return f"{op} {_port_to_text(tokens[i + 1])}", i + 2
    return None, i


def _parse_rule_body(action: str, tokens: list, seq: int, acl_type: str,
                      object_groups: dict, unresolved: set, raw_line: str) -> AclRule:
    if action == "remark":
        return AclRule(sequence=seq, action="remark", remark=" ".join(tokens), raw=raw_line)

    rule = AclRule(sequence=seq, action=action, raw=raw_line)
    i = 0

    if acl_type == "extended":
        if not tokens:
            return rule
        rule.protocol = tokens[0]
        i = 1

    src, i = _parse_endpoint(tokens, i, object_groups, unresolved)
    rule.source = src

    if acl_type == "extended" and rule.protocol in ("tcp", "udp"):
        port, i2 = _parse_port_spec(tokens, i)
        if port:
            rule.source_port = port
            i = i2

    if acl_type == "extended":
        dst, i = _parse_endpoint(tokens, i, object_groups, unresolved)
        rule.destination = dst
        if rule.protocol in ("tcp", "udp"):
            port, i2 = _parse_port_spec(tokens, i)
            if port:
                rule.destination_port = port
                i = i2

    remainder = tokens[i:]
    rule.log = "log" in remainder
    return rule


# ── object-group network ────────────────────────────────────────────────

_OBJ_GROUP_HDR = re.compile(r"^object-group network (\S+)\s*$")


def _parse_object_groups(lines: list) -> dict:
    groups: dict = {}
    current = None
    for line in lines:
        m = _OBJ_GROUP_HDR.match(line)
        if m:
            current = m.group(1)
            groups[current] = []
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped or stripped == "!":
            current = None
            continue
        if not line.startswith(" "):
            current = None
            continue
        parts = stripped.split()
        if parts[0] == "host" and len(parts) >= 2:
            try:
                groups[current].append(ipaddress.ip_network(f"{parts[1]}/32"))
            except ValueError:
                pass
        elif len(parts) >= 2 and _looks_like_ipv4(parts[0]) and _looks_like_ipv4(parts[1]):
            # object-group network использует обычную netmask, НЕ wildcard
            try:
                groups[current].append(ipaddress.ip_network(f"{parts[0]}/{parts[1]}", strict=False))
            except ValueError:
                pass
        # group-object (вложенность в другую группу) — не встречается в
        # данных проекта, сознательно не поддерживаем (см. докстринг модуля)
    return groups


# ── named + numbered ACL ────────────────────────────────────────────────

_NAMED_ACL_HDR = re.compile(r"^ip access-list (standard|extended) (\S+)\s*$")
_NUMBERED_ACL = re.compile(r"^access-list (\d+) (remark|permit|deny)\s*(.*)$")


def _parse_acls(lines: list, object_groups: dict, unresolved: set) -> list:
    acls: dict = {}  # name -> Acl
    order: list = []
    current_name = None
    seq_counters: dict = {}

    def get_acl(name, acl_type):
        if name not in acls:
            acls[name] = Acl(name=name, acl_type=acl_type)
            order.append(name)
            seq_counters[name] = 0
        return acls[name]

    for line in lines:
        m = _NAMED_ACL_HDR.match(line)
        if m:
            acl_type, name = m.group(1), m.group(2)
            get_acl(name, acl_type)
            current_name = name
            continue

        m = _NUMBERED_ACL.match(line)
        if m:
            number, action, rest = int(m.group(1)), m.group(2), m.group(3)
            acl_type = classify_numbered_acl(number)
            if acl_type is None:
                current_name = None
                continue
            name = str(number)
            acl = get_acl(name, acl_type)
            seq_counters[name] += 10
            rule = _parse_rule_body(action, rest.split(), seq_counters[name], acl_type,
                                     object_groups, unresolved, line.strip())
            acl.rules.append(rule)
            current_name = None  # однострочная форма не имеет "тела" ниже
            continue

        # Тело именованного ACL — только пока идут ведущие пробелы сразу
        # после его заголовка (или предыдущей строки того же тела).
        if current_name is not None:
            stripped = line.strip()
            if not stripped or stripped == "!":
                current_name = None
                continue
            if not line.startswith(" "):
                current_name = None
                continue
            tokens = stripped.split()
            if tokens[0] in ("permit", "deny", "remark"):
                action = tokens[0]
                acl = acls[current_name]
                seq_counters[current_name] += 10
                rule = _parse_rule_body(action, tokens[1:], seq_counters[current_name],
                                         acl.acl_type, object_groups, unresolved, stripped)
                acl.rules.append(rule)
            # прочие строки внутри тела (не встречаются в данных) — игнорируем

    return [acls[name] for name in order]


# ── применение ACL (access-group / access-class / snmp / nat) ──────────

_IFACE_HDR = re.compile(r"^interface (\S+)\s*$")
_LINE_HDR = re.compile(r"^line (.+)$")
_ACCESS_GROUP = re.compile(r"^ ip access-group (\S+) (in|out)\s*$")
_ACCESS_CLASS = re.compile(r"^ access-class (\S+) (in|out)\s*$")
_SNMP_COMMUNITY = re.compile(r"^snmp-server community (\S+)(?: view \S+)? (RO|RW) (\S+)\s*$")
_NAT_LIST = re.compile(r"^ip nat \S+ (?:source|destination) list (\S+)")


def _parse_applications(lines: list) -> list:
    apps: list = []
    context_kind = None
    context_location = None

    for line in lines:
        m = _IFACE_HDR.match(line)
        if m:
            context_kind, context_location = "interface", m.group(1)
            continue
        m = _LINE_HDR.match(line)
        if m:
            context_kind, context_location = "line", m.group(1)
            continue

        if line.strip() == "!" or (line and not line.startswith(" ") and not _NAT_LIST.match(line)
                                    and not _SNMP_COMMUNITY.match(line)):
            # непустая строка без ведущего пробела закрывает interface/line
            # контекст, кроме тех двух top-level директив, которые сами себя
            # разбирают ниже независимо от контекста
            if not line.startswith(" "):
                context_kind, context_location = None, None

        if context_kind == "interface":
            m = _ACCESS_GROUP.match(line)
            if m:
                apps.append(Application(acl_name=m.group(1), kind="interface",
                                         location=context_location, direction=m.group(2)))
                continue
        if context_kind == "line":
            m = _ACCESS_CLASS.match(line)
            if m:
                apps.append(Application(acl_name=m.group(1), kind="line",
                                         location=context_location, direction=m.group(2)))
                continue

        m = _SNMP_COMMUNITY.match(line)
        if m:
            # community намеренно не попадает в location: это секрет, а
            # location рендерится на странице ACL открытым текстом.
            mode, acl_ref = m.group(2), m.group(3)
            apps.append(Application(acl_name=acl_ref, kind="snmp",
                                     location=f"community ({mode})"))
            continue

        m = _NAT_LIST.match(line)
        if m:
            apps.append(Application(acl_name=m.group(1), kind="nat", location=line.strip()))
            continue

    return apps


# ── точка входа ─────────────────────────────────────────────────────────

def parse_config(text: str, device_name: str) -> ParsedDevice:
    """Разбирает полный running-config (как сохранён netbox_config_backup.py)
    и возвращает все ACL, object-group'ы и места применения устройства."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    object_groups = _parse_object_groups(lines)
    unresolved: set = set()
    acls = _parse_acls(lines, object_groups, unresolved)
    applications = _parse_applications(lines)

    return ParsedDevice(
        device_name=device_name,
        acls=acls,
        object_groups=object_groups,
        applications=applications,
        unresolved_object_groups=unresolved,
        parser="cisco_ios",
    )


def parse_snapshot(files: dict, device_name: str) -> ParsedDevice:
    """Единый вход для acl_store: files — {имя снимка: текст}; Cisco читает
    только полный running-config («config»)."""
    return parse_config(files.get("config", ""), device_name)
