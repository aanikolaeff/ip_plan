"""Парсер ACL из running-config коммутаторов Maipu (MyPower OS, серии
NSS5930 / S3330 / S4330 и т.п. — `show running-config`).

Чистая обработка текста: ни сети, ни NetBox. Вызывается из acl_store через
parse_snapshot(files, device_name), где files["config"] — текст снимка,
снятого netbox_config_backup.py (терминальная сессия: `terminal length 0`
может не приниматься, эхо команды, `Building Configuration...done`,
приглашение `host#` в конце, артефакты пейджера — см. eltex_acl.clean_lines).

Синтаксис — по документации Maipu (Maipu Switch Configuration Guide,
зеркало cnttshop.vn) и шаблону настройки коммутатора Maipu:
  - «Configure IP Standard ACL»: `ip access-list standard {N|NAME}`,
    правила `[seq] permit|deny {any | A W | host A} [time-range T] [log]
    [pbr-|l3-|egr-|vfp-action-group X]`, номера 1–1000, одностроч. форма
    `access-list N permit|deny …`: https://cnttshop.vn/docs/configure-ip-standard-acl
  - «Configure IP Extended ACL»: `ip access-list extended {N|NAME}`, правила
    `[seq] permit|deny PROTO {any|A W|host A} [op sport] {any|A W|host A}
    [op dport] [ack|fin|psh|rst|syn|urg] [precedence P] [tos T] [dscp D]
    [fragments] [log] [time-range T] [*-action-group X]`, `[seq] remark
    TEXT`, номера 1001–2000: https://cnttshop.vn/docs/configure-ip-extended-acl
  - «Configure ACL Application»: `ip access-group {N|NAME} {in|out|self}`
    на интерфейсе, `ip access-group {N|NAME} {in|out|vfp}` в `vlan N`,
    `global ip access-group {N|NAME} in`:
    https://cnttshop.vn/docs/configure-acl-application
  - Шаблон «等保二级 交换机安全配置模板（迈普）» (реальный вывод: `ip
    access-list standard 1` / `10 permit host … log` / `exit`, `ip access-group
    2 out` в `vlan 10`, `line vty 0 15`): https://blog.csdn.net/normanhere/article/details/140832552

Маски — WILDCARD, порты — Cisco-подобные `eq|gt|lt|neq|range`; здесь
переиспользуются _parse_endpoint/_parse_port_spec из cisco_acl.

Допущения (в документации Maipu не найдено; при встрече такой строки
парсер пишет запись в ParsedDevice.warnings):
  - `access-class {N|NAME} in|out` внутри `line vty …` читается по-Cisco как
    ограничение доступа к терминалу (kind line);
  - хвостовой токен после `ro|rw` в `snmp-server community … {ro|rw} X`
    читается как ссылка на ACL (kind snmp, location «community (ro)»;
    community в location не попадает);
  - второй адрес в правиле standard ACL (`deny any 172.18.0.0 0.0.255.255`
    в шаблоне выше) читается как destination;
  - `ip nat inside|outside source|destination list X …` — как у Cisco (kind nat).

Прочее: ipv6/mac/hybrid access-list пропускаются с предупреждением;
object-group у Maipu нет; `*-action-group`, tcp-флаги, precedence/tos/dscp,
fragments → AclRule.extra; `time-range` → schedule; `log` → log.
"""

from __future__ import annotations

import re

from .acl_model import Acl, AclRule, Application, Endpoint, ParsedDevice
from .cisco_acl import _NAT_LIST, _parse_endpoint, _parse_port_spec
from .eltex_acl import clean_lines, warn_once

PARSER_NAME = "maipu"

_NAMED_HDR = re.compile(r"^ip access-list (standard|extended) (\S+)\s*$")
_LEGACY = re.compile(r"^access-list (\d+) (permit|deny|remark)(?:\s+(.*))?$")
_SKIP_HDR = re.compile(r"^(ipv6 access-list|mac access-list|hybrid access-list|mac-ip access-list)\b.*?(\S+)\s*$")
_RULE = re.compile(r"^(?:(\d+)\s+)?(permit|deny|remark)(?:\s+(.*))?$")
_IFACE_HDR = re.compile(r"^interface (\S+)\s*$")
_VLAN_HDR = re.compile(r"^vlan (\d+)\s*$")
_LINE_HDR = re.compile(r"^line (.+?)\s*$")
_ACCESS_GROUP = re.compile(r"^ip access-group (\S+) (in|out|self|vfp)\b")
_GLOBAL_ACCESS_GROUP = re.compile(r"^global ip access-group (\S+) (in|out)\b")
_ACCESS_CLASS = re.compile(r"^access-class (\S+) (in|out)\b")
_SNMP_COMMUNITY = re.compile(r"^snmp-server community (\S+)(?: view (\S+))? (ro|rw)(?: (\S+))?\s*$")
_BLOCK_END = {"exit", "!", "end", "!end"}

_TCP_FLAGS = {"ack", "fin", "psh", "rst", "syn", "urg"}
_VALUE_FLAGS = {"precedence", "tos", "dscp", "time-range"}
_ACTION_GROUP_SUFFIX = "-action-group"


def classify_acl_number(number: int):
    """standard | extended | None по диапазонам Maipu (1–1000 / 1001–2000)."""
    if 1 <= number <= 1000:
        return "standard"
    if 1001 <= number <= 2000:
        return "extended"
    return None


def _parse_tail(rule: AclRule, tokens: list, i: int, device: ParsedDevice, raw_line: str) -> None:
    """Флаги после адресов/портов: log, time-range, tcp-флаги, precedence…"""
    unknown = []
    while i < len(tokens):
        tok = tokens[i]
        if tok == "log":
            rule.log = True
            i += 1
        elif tok == "fragments":
            rule.extra["fragments"] = "да"
            i += 1
        elif tok in _TCP_FLAGS:
            flags = rule.extra.get("tcp-flags", "")
            rule.extra["tcp-flags"] = (flags + " " + tok).strip()
            i += 1
        elif tok in _VALUE_FLAGS and i + 1 < len(tokens):
            if tok == "time-range":
                rule.schedule = tokens[i + 1]
            else:
                rule.extra[tok] = tokens[i + 1]
            i += 2
        elif tok.endswith(_ACTION_GROUP_SUFFIX) and i + 1 < len(tokens):
            rule.extra[tok] = tokens[i + 1]
            i += 2
        else:
            unknown.append(tok)
            i += 1
    if unknown:
        rule.extra["не разобрано"] = " ".join(unknown)
        warn_once(device, f"Не разобранные токены в правиле: {raw_line}")


def _parse_rule(acl_type: str, action: str, tokens: list, seq, device: ParsedDevice,
                raw_line: str) -> AclRule:
    if action == "remark":
        return AclRule(sequence=seq, action="remark", remark=" ".join(tokens), raw=raw_line)
    rule = AclRule(sequence=seq, action=action, raw=raw_line)
    i = 0
    if acl_type == "extended":
        if not tokens:
            return rule
        rule.protocol = tokens[0]
        i = 1
    src, i = _parse_endpoint(tokens, i, {}, set())
    if src is None:
        warn_once(device, f"Не удалось разобрать адрес источника: {raw_line}")
        _parse_tail(rule, tokens, i, device, raw_line)
        return rule
    rule.source = src
    if acl_type == "extended":
        port, i2 = _parse_port_spec(tokens, i)
        if port:
            rule.source_port = port
            i = i2
        dst, i2 = _parse_endpoint(tokens, i, {}, set())
        if dst is None:
            warn_once(device, f"Не удалось разобрать адрес назначения: {raw_line}")
        else:
            rule.destination = dst
            i = i2
            port, i2 = _parse_port_spec(tokens, i)
            if port:
                rule.destination_port = port
                i = i2
            if rule.protocol == "icmp" and i < len(tokens) and tokens[i].isdigit():
                # icmp-type [icmp-code]
                j = i
                while j < len(tokens) and tokens[j].isdigit():
                    j += 1
                rule.extra["icmp-type/code"] = " ".join(tokens[i:j])
                i = j
    else:
        dst, i2 = _parse_endpoint(tokens, i, {}, set())
        if dst is not None:
            warn_once(device, "Второй адрес в правиле standard ACL прочитан как "
                              f"destination: {raw_line}")
            rule.destination = dst
            i = i2
    _parse_tail(rule, tokens, i, device, raw_line)
    return rule


def parse_config(text: str, device_name: str) -> ParsedDevice:
    """Разбирает полный `show running-config` Maipu и возвращает ACL и места
    их применения."""
    device = ParsedDevice(device_name=device_name, parser=PARSER_NAME)
    acls: dict = {}
    order: list = []
    seq: dict = {}

    def get_acl(name: str, acl_type: str) -> Acl:
        if name not in acls:
            acls[name] = Acl(name=name, acl_type=acl_type)
            order.append(name)
            seq[name] = 0
        return acls[name]

    ctx = None  # ("acl", name) | ("skip",) | ("interface", x) | ("vlan", x) | ("line", x)
    for raw in clean_lines(text):
        line = raw.strip()
        if not line:
            continue
        if line in _BLOCK_END:
            ctx = None
            continue

        m = _NAMED_HDR.match(line)
        if m:
            get_acl(m.group(2), m.group(1))
            ctx = ("acl", m.group(2))
            continue
        m = _SKIP_HDR.match(line)
        if m:
            warn_once(device, f"{m.group(1)} {m.group(2)} пропущен: парсер разбирает только IPv4 ACL")
            ctx = ("skip",)
            continue
        m = _LEGACY.match(line)
        if m:
            number, action, rest = int(m.group(1)), m.group(2), (m.group(3) or "")
            acl_type = classify_acl_number(number)
            if acl_type is None:
                warn_once(device, f"access-list {number} пропущен: номер вне диапазонов Maipu")
                ctx = None
                continue
            acl = get_acl(str(number), acl_type)
            seq[str(number)] += 10
            acl.rules.append(_parse_rule(acl_type, action, rest.split(), seq[str(number)],
                                         device, line))
            ctx = None
            continue
        m = _GLOBAL_ACCESS_GROUP.match(line)
        if m:
            device.applications.append(Application(
                acl_name=m.group(1), kind="interface", location="global", direction=m.group(2)))
            ctx = None
            continue
        m = _IFACE_HDR.match(line)
        if m:
            ctx = ("interface", m.group(1))
            continue
        m = _VLAN_HDR.match(line)
        if m:
            ctx = ("vlan", f"vlan {m.group(1)}")
            continue
        m = _LINE_HDR.match(line)
        if m:
            ctx = ("line", m.group(1))
            continue
        m = _SNMP_COMMUNITY.match(line)
        if m:
            if m.group(4):
                warn_once(device, "Хвостовой токен snmp-server community прочитан как ACL "
                                  "(в документации Maipu привязка не найдена)")
                device.applications.append(Application(
                    acl_name=m.group(4), kind="snmp", location=f"community ({m.group(3)})"))
            continue
        m = _NAT_LIST.match(line)
        if m:
            warn_once(device, "ip nat … list прочитан по-Cisco (в документации Maipu не проверено)")
            device.applications.append(Application(acl_name=m.group(1), kind="nat", location=line))
            continue

        if ctx is None:
            continue
        kind = ctx[0]
        if kind == "acl":
            name = ctx[1]
            m = _RULE.match(line)
            if not m:
                continue
            acl = acls[name]
            seq[name] += 10
            explicit = int(m.group(1)) if m.group(1) else seq[name]
            acl.rules.append(_parse_rule(acl.acl_type, m.group(2), (m.group(3) or "").split(),
                                         explicit, device, line))
            continue
        if kind in ("interface", "vlan"):
            m = _ACCESS_GROUP.match(line)
            if m:
                device.applications.append(Application(
                    acl_name=m.group(1), kind="interface", location=ctx[1], direction=m.group(2)))
            continue
        if kind == "line":
            m = _ACCESS_CLASS.match(line)
            if m:
                warn_once(device, "access-class в line прочитан по-Cisco "
                                  "(в документации Maipu не найден)")
                device.applications.append(Application(
                    acl_name=m.group(1), kind="line", location=ctx[1], direction=m.group(2)))
            continue

    device.acls = [acls[name] for name in order]
    return device


def parse_snapshot(files: dict, device_name: str) -> ParsedDevice:
    """Единый вход для acl_store: files — {имя снимка: текст}; Maipu читает
    только полный running-config («config»)."""
    return parse_config(files.get("config", ""), device_name)
