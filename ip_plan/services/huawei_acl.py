"""Парсер ACL из конфигурации Huawei VRP (`display current-configuration`,
коммутаторы CX912 / CloudEngine / S-серии).

Чистая обработка текста: ни сети, ни NetBox. Вызывается из acl_store через
parse_snapshot(files, device_name), где files["config"] — текст снимка,
снятого netbox_config_backup.py (терминальная сессия: `screen-length 0
temporary`, эхо команды, приглашение `<host>` в конце; конфигурация
заканчивается строкой `return`, блоки разделены строками `#`).

Синтаксис — по документации Huawei:
  - Command Reference CloudEngine 8800/7800/6800/5800 V200R019C10, раздел
    «ACL Configuration Commands» (acl, acl name, description, step,
    rule (basic ACL view), rule (advanced ACL view), acl (user interface
    view)): https://support.huawei.com/enterprise/en/doc/EDOC1100137933/f3ca4a6c/acl-configuration-commands
  - Command Reference WLAN AC V200R022C00, «rule (advanced ACL view)»:
    https://support.huawei.com/enterprise/en/doc/EDOC1100279155?section=j2uc
  - «Huawei Access-Lists» (обзор basic/advanced/L2, traffic-filter):
    https://ipcisco.com/lesson/huawei-access-lists-acl/
  - Ansible community.network.ce_acl (диапазоны и поля правила basic ACL):
    https://docs.ansible.com/projects/ansible/2.10/collections/community/network/ce_acl_module.html
  - локально: netbox-sync/mibs/huaweiCX912/docs/snmp-agent_community.md,
    snmp-agent_group.md, snmp-agent_usm-user.md — привязка ACL к SNMP.

Что покрывается:
  - `acl [number] N [match-order …]` и `acl name NAME [basic|advance|
    advanced|link|user] [[number] N]`; тип по номеру: 2000–2999 basic,
    3000–3999 advanced; 4000–4999 (layer-2), 5000–5999 (user-defined) и
    прочие — пропуск с предупреждением; именованный ACL без типа и номера
    — advanced (так VRP создаёт его по умолчанию). `acl ipv6 …` — пропуск с
    предупреждением. `description` → Acl.description, `step` игнорируется.
  - `rule [id] [name NAME] permit|deny [proto] [source A W|any]
    [destination A W|any] [source-port|destination-port eq|gt|lt|range|neq …]
    [logging] [time-range T] [vpn-instance V] [fragment|none-first-fragment]
    [dscp|precedence|tos|icmp-type|tcp-flag …]`. Ключевые слова могут идти
    в любом порядке. Маска — WILDCARD, как у Cisco: `0` и `0.0.0.0` — хост,
    `0.0.0.255` — /24. Голое число 1–32 после адреса (форма NE/CX
    `source 10.1.1.0 24`) читается как длина префикса — допущение,
    фиксируется в warnings при встрече. Правило без явного id получает
    синтетический номер 10, 20, …; `name` → AclRule.label; `logging` →
    log; `time-range` → schedule; всё остальное → extra.
  - применение: `traffic-filter inbound|outbound acl {N|name X}` на
    интерфейсе; `traffic-policy P inbound|outbound` на интерфейсе через
    `traffic policy P` → `classifier C` → `traffic classifier C` →
    `if-match acl …` (location «iface (traffic-policy P)»); `acl {N|name X}
    inbound|outbound` в `user-interface vty …` (kind line); `snmp-agent
    community read|write … acl|acl-ipv4 …` (kind snmp, location «community
    (read)» — community никогда не попадает в location), `snmp-agent group
    v3 G … acl …`, `snmp-agent usm-user v3 U … acl …`; `ssh|telnet|ftp|http|
    https server acl …` и `ntp-service access … N` (kind line); `nat
    outbound N` на интерфейсе (kind nat, только у маршрутизаторов AR).

Чего нет: object-group / `ip address-set` (это синтаксис межсетевых
экранов USG, у VRP-коммутаторов его нет — в документации CX912 не
встречается), IPv6-варианты всех команд.
"""

from __future__ import annotations

import ipaddress
import re

from .acl_model import Acl, AclRule, Application, Endpoint, ParsedDevice
from .cisco_acl import _looks_like_ipv4, _parse_port_spec, _wildcard_to_network
from .eltex_acl import clean_lines, warn_once

PARSER_NAME = "huawei_vrp"

_ACL_NUMBER_HDR = re.compile(r"^acl (?:number )?(\d+)(?:\s+match-order \S+)?\s*$")
_ACL_NAME_HDR = re.compile(
    r"^acl name (\S+)(?:\s+(basic|advance|advanced|link|user|ucl|arp|mpls))?"
    r"(?:\s+(?:number\s+)?(\d+))?(?:\s+match-order \S+)?\s*$")
_ACL_IPV6_HDR = re.compile(r"^acl ipv6 ")
_RULE = re.compile(r"^rule(?:\s+(\d+))?(?:\s+name\s+(\S+))?\s+(permit|deny)(?:\s+(.*))?$")
_IFACE_HDR = re.compile(r"^interface (\S+)\s*$")
_VTY_HDR = re.compile(r"^user-interface (.+)$")
_CLASSIFIER_HDR = re.compile(r"^traffic classifier (\S+)")
_POLICY_HDR = re.compile(r"^traffic policy (\S+)")
_TRAFFIC_FILTER = re.compile(r"^traffic-filter (inbound|outbound) (.*)$")
_TRAFFIC_POLICY = re.compile(r"^traffic-policy (\S+) (inbound|outbound)")
_VTY_ACL = re.compile(r"^acl (.*?)\s*(inbound|outbound)\s*$")
_IF_MATCH = re.compile(r"^if-match (.*)$")
_CLASSIFIER_REF = re.compile(r"^classifier (\S+)")
_NAT_OUTBOUND = re.compile(r"^nat outbound (\d+)")
_SNMP_COMMUNITY = re.compile(r"^snmp-agent community (read|write) (.*)$")
_SNMP_GROUP = re.compile(r"^snmp-agent group v3 (\S+) (.*)$")
_SNMP_USER = re.compile(r"^snmp-agent (?:remote-engineid \S+ )?usm-user v3 (\S+) (.*)$")
_SERVICE_ACL = re.compile(r"^((?:ssh|telnet|ftp|http|https) server) (?:ipv6 )?acl (.*)$")
_NTP_ACCESS = re.compile(r"^ntp-service access (\S+) (\S+)")
_DIRECTION = {"inbound": "in", "outbound": "out"}

# Ключевые слова правила advanced ACL, у которых значения читаются до
# следующего ключевого слова (icmp-type и tcp-flag берут несколько токенов).
_RULE_KEYWORDS = {
    "source", "destination", "source-port", "destination-port", "logging",
    "time-range", "vpn-instance", "fragment", "none-first-fragment", "dscp",
    "precedence", "tos", "icmp-type", "tcp-flag", "established", "ttl",
    "packet-length", "syn-flag", "igmp-type", "vlan", "ttl-expired",
}


def classify_acl_number(number: int):
    """basic | advanced | None (L2/user-defined/неизвестный диапазон)."""
    if 2000 <= number <= 2999:
        return "basic"
    if 3000 <= number <= 3999:
        return "advanced"
    return None


def _acl_ref(tokens: list, i: int):
    """`acl 2001` / `acl name X` / `acl-ipv4 …` -> (имя ACL, следующий индекс);
    (None, i) — если это не ссылка на IPv4 ACL (например `acl ipv6 …`)."""
    if i >= len(tokens) or tokens[i] not in ("acl", "acl-ipv4"):
        return None, i
    if i + 1 >= len(tokens):
        return None, i
    nxt = tokens[i + 1]
    if nxt == "name" and i + 2 < len(tokens):
        return tokens[i + 2], i + 3
    if nxt == "ipv6":
        return None, i
    return nxt, i + 2


def _find_acl_ref(tokens: list):
    """Первая ссылка `acl …` / `acl-ipv4 …` в списке токенов (для команд, где
    она стоит среди других параметров: snmp-agent community, traffic-filter)."""
    for i, tok in enumerate(tokens):
        if tok in ("acl", "acl-ipv4"):
            if i > 0 and tokens[i - 1] == "ipv6":
                return None   # `traffic-filter inbound ipv6 acl N` — не наш
            name, _ = _acl_ref(tokens, i)
            return name
    return None


def _endpoint(tokens: list, i: int, device: ParsedDevice, raw_line: str):
    """Значение после `source`/`destination`: `any` | `A W` | `A 0` | `A N`."""
    if i >= len(tokens):
        return None, i
    tok = tokens[i]
    if tok == "any":
        return Endpoint(raw="any", kind="any", networks=[ipaddress.ip_network("0.0.0.0/0")]), i + 1
    if not _looks_like_ipv4(tok):
        return None, i
    mask = tokens[i + 1] if i + 1 < len(tokens) else None
    if mask is None or mask in _RULE_KEYWORDS or not (_looks_like_ipv4(mask) or mask.isdigit()):
        # адрес без маски (Cisco-подобная запись хоста)
        return Endpoint(raw=tok, kind="host", networks=[ipaddress.ip_network(f"{tok}/32")]), i + 1
    raw = f"{tok} {mask}"
    if _looks_like_ipv4(mask):
        net = _wildcard_to_network(tok, mask)
        kind = "host" if mask == "0.0.0.0" else "wildcard"
        return Endpoint(raw=raw, kind=kind, networks=[net] if net else [],
                        resolved=net is not None), i + 2
    length = int(mask)
    if length == 0:
        return Endpoint(raw=raw, kind="host", networks=[ipaddress.ip_network(f"{tok}/32")]), i + 2
    if 1 <= length <= 32:
        warn_once(device, f"Число после адреса прочитано как длина префикса: {raw_line}")
        net = ipaddress.ip_network(f"{tok}/{length}", strict=False)
        return Endpoint(raw=raw, kind="network", networks=[net]), i + 2
    warn_once(device, f"Не удалось разобрать маску: {raw_line}")
    return Endpoint(raw=raw, kind="unresolved", resolved=False), i + 2


def _parse_rule(acl_type: str, tokens: list, seq: int, device: ParsedDevice,
                raw_line: str, rule_id, rule_name, action: str) -> AclRule:
    rule = AclRule(sequence=rule_id if rule_id is not None else seq, action=action,
                   raw=raw_line, label=rule_name)
    i = 0
    if acl_type == "advanced" and tokens and tokens[0] not in _RULE_KEYWORDS:
        rule.protocol = tokens[0]
        i = 1
    while i < len(tokens):
        kw = tokens[i]
        if kw == "source" or kw == "destination":
            ep, i2 = _endpoint(tokens, i + 1, device, raw_line)
            if ep is None:
                warn_once(device, f"Не удалось разобрать адрес в правиле: {raw_line}")
                i += 1
                continue
            if kw == "source":
                rule.source = ep
            else:
                rule.destination = ep
            i = i2
            continue
        if kw in ("source-port", "destination-port"):
            port, i2 = _parse_port_spec(tokens, i + 1)
            if port is None:
                warn_once(device, f"Не удалось разобрать порт в правиле: {raw_line}")
                i += 1
                continue
            if kw == "source-port":
                rule.source_port = port
            else:
                rule.destination_port = port
            i = i2
            continue
        # Прочие ключевые слова: значения — все токены до следующего ключевого
        j = i + 1
        while j < len(tokens) and tokens[j] not in _RULE_KEYWORDS:
            j += 1
        values = " ".join(tokens[i + 1:j])
        if kw == "logging":
            rule.log = True
        elif kw == "time-range":
            rule.schedule = values or None
        elif kw in _RULE_KEYWORDS:
            rule.extra[kw] = values or "да"
        else:
            rule.extra["не разобрано"] = (rule.extra.get("не разобрано", "") + " " + kw).strip()
            warn_once(device, f"Неизвестное слово '{kw}' в правиле: {raw_line}")
            j = i + 1
        i = j
    any_ep = Endpoint(raw="any", kind="any", networks=[ipaddress.ip_network("0.0.0.0/0")])
    if rule.source is None:
        rule.source = any_ep
    if acl_type == "advanced" and rule.destination is None:
        rule.destination = Endpoint(raw="any", kind="any",
                                    networks=[ipaddress.ip_network("0.0.0.0/0")])
    return rule


def parse_config(text: str, device_name: str) -> ParsedDevice:
    """Разбирает полный `display current-configuration` и возвращает ACL и
    места их применения."""
    device = ParsedDevice(device_name=device_name, parser=PARSER_NAME)
    acls: dict = {}
    order: list = []
    seq: dict = {}
    classifier_acls: dict = {}   # classifier -> [acl]
    policy_classifiers: dict = {}  # policy -> [classifier]
    iface_policies: list = []    # (iface, policy, direction)

    def get_acl(name: str, acl_type: str) -> Acl:
        if name not in acls:
            acls[name] = Acl(name=name, acl_type=acl_type)
            order.append(name)
            seq[name] = 0
        return acls[name]

    ctx = None  # ("acl", name) | ("skip",) | ("interface", x) | ("vty", x) | ("classifier", c) | ("policy", p)
    for raw in clean_lines(text):
        line = raw.strip()
        if not line:
            continue
        if line == "#":
            ctx = None
            continue
        if line == "return":
            break

        if _ACL_IPV6_HDR.match(line):
            warn_once(device, f"IPv6 ACL пропущен: {line}")
            ctx = ("skip",)
            continue
        m = _ACL_NUMBER_HDR.match(line)
        if m:
            number = int(m.group(1))
            acl_type = classify_acl_number(number)
            if acl_type is None:
                warn_once(device, f"ACL {number} пропущен: не basic/advanced (L2 или user-defined)")
                ctx = ("skip",)
                continue
            get_acl(str(number), acl_type)
            ctx = ("acl", str(number))
            continue
        m = _ACL_NAME_HDR.match(line)
        if m:
            name, type_kw, number = m.group(1), m.group(2), m.group(3)
            if type_kw in ("basic",):
                acl_type = "basic"
            elif type_kw in ("advance", "advanced"):
                acl_type = "advanced"
            elif type_kw is not None:
                warn_once(device, f"ACL {name} пропущен: тип {type_kw} не разбирается")
                ctx = ("skip",)
                continue
            elif number is not None:
                acl_type = classify_acl_number(int(number))
                if acl_type is None:
                    warn_once(device, f"ACL {name} ({number}) пропущен: не basic/advanced")
                    ctx = ("skip",)
                    continue
            else:
                acl_type = "advanced"
            acl = get_acl(name, acl_type)
            if number is not None:
                acl.description = acl.description or f"number {number}"
            ctx = ("acl", name)
            continue
        m = _IFACE_HDR.match(line)
        if m:
            ctx = ("interface", m.group(1))
            continue
        m = _VTY_HDR.match(line)
        if m:
            ctx = ("vty", m.group(1).strip())
            continue
        m = _CLASSIFIER_HDR.match(line)
        if m:
            ctx = ("classifier", m.group(1))
            classifier_acls.setdefault(m.group(1), [])
            continue
        m = _POLICY_HDR.match(line)
        if m:
            ctx = ("policy", m.group(1))
            policy_classifiers.setdefault(m.group(1), [])
            continue

        # Команды системного вида, не зависящие от контекста
        m = _SNMP_COMMUNITY.match(line)
        if m:
            acl_name = _find_acl_ref(m.group(2).split())
            if acl_name:
                device.applications.append(Application(
                    acl_name=acl_name, kind="snmp", location=f"community ({m.group(1)})"))
            continue
        m = _SNMP_GROUP.match(line)
        if m:
            acl_name = _find_acl_ref(m.group(2).split())
            if acl_name:
                device.applications.append(Application(
                    acl_name=acl_name, kind="snmp", location=f"group v3 {m.group(1)}"))
            continue
        m = _SNMP_USER.match(line)
        if m:
            acl_name = _find_acl_ref(m.group(2).split())
            if acl_name:
                device.applications.append(Application(
                    acl_name=acl_name, kind="snmp", location=f"usm-user v3 {m.group(1)}"))
            continue
        m = _SERVICE_ACL.match(line)
        if m:
            acl_name, _ = _acl_ref(["acl"] + m.group(2).split(), 0)
            if acl_name:
                device.applications.append(Application(
                    acl_name=acl_name, kind="line", location=m.group(1), direction="in"))
            continue
        m = _NTP_ACCESS.match(line)
        if m:
            device.applications.append(Application(
                acl_name=m.group(2), kind="line", location=f"ntp-service access {m.group(1)}",
                direction="in"))
            continue

        if ctx is None:
            continue
        kind = ctx[0]
        if kind == "acl":
            name = ctx[1]
            acl = acls[name]
            m = _RULE.match(line)
            if m:
                rule_id = int(m.group(1)) if m.group(1) else None
                seq[name] += 10
                acl.rules.append(_parse_rule(acl.acl_type, (m.group(4) or "").split(), seq[name],
                                             device, line, rule_id, m.group(2), m.group(3)))
                continue
            if line.startswith("description "):
                acl.description = line[len("description "):].strip()
                continue
            if line.startswith("step ") or line.startswith("undo "):
                continue
            if line.startswith("rule "):
                warn_once(device, f"Не удалось разобрать правило: {line}")
            continue
        if kind == "interface":
            m = _TRAFFIC_FILTER.match(line)
            if m:
                acl_name = _find_acl_ref(m.group(2).split())
                if acl_name:
                    device.applications.append(Application(
                        acl_name=acl_name, kind="interface", location=ctx[1],
                        direction=_DIRECTION[m.group(1)]))
                continue
            m = _TRAFFIC_POLICY.match(line)
            if m:
                iface_policies.append((ctx[1], m.group(1), _DIRECTION[m.group(2)]))
                continue
            m = _NAT_OUTBOUND.match(line)
            if m:
                device.applications.append(Application(
                    acl_name=m.group(1), kind="nat", location=f"{ctx[1]}: {line}"))
                continue
            continue
        if kind == "vty":
            m = _VTY_ACL.match(line)
            if m:
                acl_name, _ = _acl_ref(["acl"] + m.group(1).split(), 0)
                if acl_name:
                    device.applications.append(Application(
                        acl_name=acl_name, kind="line", location=ctx[1],
                        direction=_DIRECTION[m.group(2)]))
                continue
            continue
        if kind == "classifier":
            m = _IF_MATCH.match(line)
            if m:
                acl_name = _find_acl_ref(m.group(1).split())
                if acl_name:
                    classifier_acls[ctx[1]].append(acl_name)
            continue
        if kind == "policy":
            m = _CLASSIFIER_REF.match(line)
            if m:
                policy_classifiers[ctx[1]].append(m.group(1))
            continue

    for iface, policy, direction in iface_policies:
        for classifier in policy_classifiers.get(policy, []):
            for acl_name in classifier_acls.get(classifier, []):
                device.applications.append(Application(
                    acl_name=acl_name, kind="interface",
                    location=f"{iface} (traffic-policy {policy})", direction=direction))

    device.acls = [acls[name] for name in order]
    return device


def parse_snapshot(files: dict, device_name: str) -> ParsedDevice:
    """Единый вход для acl_store: files — {имя снимка: текст}; Huawei читает
    только полный running-config («config»)."""
    return parse_config(files.get("config", ""), device_name)
