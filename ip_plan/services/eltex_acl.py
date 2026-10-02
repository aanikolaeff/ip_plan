"""Парсер ACL из running-config коммутаторов Eltex MES (прошивка ROS,
серии MES23xx/33xx/35xx/53xx/54xx — `show running-config`).

Чистая обработка текста: ни сети, ни NetBox. Вызывается из acl_store через
parse_snapshot(files, device_name), где files["config"] — текст снимка,
снятого netbox_config_backup.py (терминальная сессия: эхо команды сверху,
приглашение снизу, возможны артефакты пейджера — см. clean_lines()).

Синтаксис взят из официальной документации Eltex:
  - «Коммутаторы Ethernet MES23xx, MES33xx, MES35xx, MES5324. Руководство по
    эксплуатации» v4.0.22, разделы «Списки контроля доступа (ACL)» (таблицы
    «ip access-list extended», «permit/deny», «service-acl») и «Настройка
    Management ACL» (таблицы 218–219):
    https://old.eltex-co.com/upload/iblock/1dd/3ovqyknd95b2akcx5yhd04ege21okl15/MES_Series_user_manual_4.0.22_en.pdf
  - База знаний Eltex, «Настройка Management ACL на MES2300-xx/MES3300-xx/…»:
    https://docs.eltex-co.ru/pages/viewpage.action?pageId=52495865
  - База знаний Eltex, PBR на MES23xx/33xx/35xx/5324 (примеры extended ACL с
    ace-priority): https://docs.eltex-co.ru/pages/viewpage.action?pageId=58240841
  - Форум Eltex «Extended ACL на MES5324» (service-acl input, только вход):
    https://forum.eltex-co.ru/viewtopic.php?t=12109

Что покрывается:
  - `ip access-list extended NAME` + тело `permit|deny …` (acl_type extended).
    Грамматика ROS отличается от Cisco IOS: у tcp/udp ЧЕТЫРЕ позиционных
    слота `{any|src wc} {any|sport} {any|dst wc} {any|dport}`, порт — голое
    число/имя либо `range A B`; у прочих протоколов два слота `{any|src wc}
    {any|dst wc}`, у icmp/igmp дальше тип/код. Флаги: ace-priority (номер
    правила → AclRule.sequence), dscp/precedence, time-range → schedule,
    log-input → log, disable-port, match-all, offset-list, vlan → extra.
  - однострочный standard: `ip access-list NAME permit|deny {any|A [M]}`
    (acl_type standard). Маска M считается WILDCARD-маской, как во всём
    остальном синтаксисе ACL Eltex; руководство называет её ip_address_mask
    без уточнения — допущение записано и в warnings при встрече.
  - `management access-list NAME` + `permit|deny [ip-source A [mask M|/N]]
    [<интерфейс>|port-channel N|oob|vlan N] [service S] [ace-priority N]`
    (acl_type management; здесь маска — обычная netmask, по документации);
    `management access-class NAME` → Application(kind line).
  - применение: `service-acl input|output NAME [NAME2]` и `ip access-group
    NAME in|out` внутри блока interface → Application(kind interface).
  - ipv6/mac access-list пропускаются с предупреждением.

Чего нет у Eltex (и в парсере): object-group, remark внутри ACL, ACL на
snmp-server community (эту роль выполняет management access-list), NAT.

Примеры из базы знаний Eltex вида `permit ip any any any 192.0.2.1 0.0.0.0`
(три `any` и адрес) документированной грамматике не соответствуют — такие
строки разбираются по четырёхслотовой схеме, а не уложившееся в неё
попадает в extra["не разобрано"] с записью в warnings, чтобы молчаливых
пропусков не было.
"""

from __future__ import annotations

import ipaddress
import re

from .acl_model import Acl, AclRule, Application, Endpoint, ParsedDevice
from .cisco_acl import _looks_like_ipv4, _parse_endpoint, _parse_port_spec

PARSER_NAME = "eltex_status"

# ── Терминальный шум ─────────────────────────────────────────────────────
#
# Снимок — это сессия SSH, а не файл конфигурации: в нём эхо команды, строка
# приглашения, а у Eltex и Maipu ещё артефакты пейджера — управляющие
# последовательности (`\x1b[0m`) и строки, сдвинутые терминалом вправо на
# десятки пробелов. Из-за последнего на отступ в этих парсерах полагаться
# нельзя: границы блоков определяются ключевыми словами и `exit`/`!`/`#`.
# Этот же чистильщик импортируют huawei_acl и maipu_acl.

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MORE_RE = re.compile(r"-+\s*\(?more\)?\s*-+", re.IGNORECASE)
_PAGER_SHIFT = 8  # реальная вложенность у всех трёх вендоров — 1–2 пробела


def clean_lines(text: str) -> list:
    """Список строк без `\\r`, ANSI-кодов, `--More--` и сдвига пейджера."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_RE.sub("", text).replace("\x08", "")
    out = []
    for line in text.split("\n"):
        line = _MORE_RE.sub("", line).rstrip()
        stripped = line.lstrip()
        if len(line) - len(stripped) >= _PAGER_SHIFT:
            line = stripped
        out.append(line)
    return out


def warn_once(device: ParsedDevice, message: str) -> None:
    """Одно и то же предупреждение — один раз на устройство."""
    if message not in device.warnings:
        device.warnings.append(message)


# ── Разбор правил ────────────────────────────────────────────────────────

_EXT_HDR = re.compile(r"^ip access-list extended (\S+)\s*$")
_STD_LINE = re.compile(r"^ip access-list (?!extended\b)(\S+) (permit|deny)(?:\s+(.*))?$")
_SKIP_HDR = re.compile(r"^(ipv6 access-list|mac access-list) (?:extended )?(\S+)")
_MGMT_HDR = re.compile(r"^management access-list (\S+)\s*$")
_MGMT_CLASS = re.compile(r"^management access-class (\S+)\s*$")
_IFACE_HDR = re.compile(r"^interface (.+?)\s*$")
_SERVICE_ACL = re.compile(r"^service-acl (input|output) (\S+)(?:\s+(\S+))?")
_ACCESS_GROUP = re.compile(r"^ip access-group (\S+) (in|out)\b")
_BLOCK_END = {"exit", "!", "end"}

# Флаги хвоста правила: ключевое слово -> сколько значений за ним следует.
_FLAG_ARGS = {
    "ace-priority": 1, "dscp": 1, "precedence": 1, "ip-precedence": 1,
    "time-range": 1, "match-all": 1, "offset-list": 1, "vlan": 1,
    "log-input": 0, "disable-port": 0,
}
_PORT_OPS = {"eq", "gt", "lt", "neq"}


def _any_endpoint() -> Endpoint:
    return Endpoint(raw="any", kind="any", networks=[ipaddress.ip_network("0.0.0.0/0")])


def _group_items(tokens: list) -> list:
    """Позиционные токены -> список элементов ('any',) | ('net', Endpoint) |
    ('port', текст) | ('scalar', токен). Слоты адресов и портов у ROS
    перемешаны в одной последовательности, поэтому сначала группируем, а
    интерпретируем по протоколу уже в _assign_slots()."""
    items = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "any":
            items.append(("any",))
            i += 1
        elif tok == "host" or _looks_like_ipv4(tok):
            ep, i2 = _parse_endpoint(tokens, i, {}, set())
            if ep is None:
                items.append(("scalar", tok))
                i += 1
            else:
                items.append(("net", ep))
                i = i2
        elif tok == "range" and i + 2 < len(tokens):
            items.append(("port", f"range {tokens[i + 1]}-{tokens[i + 2]}"))
            i += 3
        elif tok in _PORT_OPS and i + 1 < len(tokens):
            items.append(("port", f"{tok} {tokens[i + 1]}"))
            i += 2
        else:
            items.append(("scalar", tok))
            i += 1
    return items


def _as_endpoint(item, device: ParsedDevice, raw_line: str):
    if item[0] == "any":
        return _any_endpoint()
    if item[0] == "net":
        return item[1]
    warn_once(device, f"Не удалось разобрать адрес в правиле: {raw_line}")
    return Endpoint(raw=" ".join(item[1:]), kind="unresolved", resolved=False)


def _as_port(item, device: ParsedDevice, raw_line: str, rule: AclRule):
    if item[0] == "any":
        return None
    if item[0] in ("port", "scalar"):
        return item[1]
    # В слоте порта оказалась сеть — как в спорных примерах базы знаний.
    rule.extra.setdefault("не разобрано", []).append(item[1].raw)
    warn_once(device, f"Неоднозначный формат правила (адрес в слоте порта): {raw_line}")
    return None


def _assign_slots(rule: AclRule, items: list, device: ParsedDevice, raw_line: str) -> None:
    """Раскладывает элементы по source/source_port/destination/destination_port."""
    four_slot = rule.protocol in ("tcp", "udp") or (
        len(items) >= 4 and items[1][0] in ("any", "port", "scalar")
    )
    if not items:
        return
    rule.source = _as_endpoint(items[0], device, raw_line)
    rest = items[1:]
    if four_slot:
        if rest:
            rule.source_port = _as_port(rest[0], device, raw_line, rule)
        if len(rest) > 1:
            rule.destination = _as_endpoint(rest[1], device, raw_line)
        if len(rest) > 2:
            rule.destination_port = _as_port(rest[2], device, raw_line, rule)
        leftover = rest[3:]
    else:
        if rest:
            rule.destination = _as_endpoint(rest[0], device, raw_line)
        leftover = rest[1:]
    if leftover:
        # icmp: {any|тип} {any|код}; igmp: [тип] — оставляем текстом
        words = []
        for item in leftover:
            if item[0] == "any":
                words.append("any")
            elif item[0] == "net":
                words.append(item[1].raw)
            else:
                words.append(item[1])
        key = "icmp-type/code" if rule.protocol == "icmp" else (
            "igmp-type" if rule.protocol == "igmp" else "не разобрано")
        if key == "не разобрано":
            rule.extra.setdefault("не разобрано", []).extend(words)
            warn_once(device, f"Лишние токены в правиле: {raw_line}")
        else:
            rule.extra[key] = " ".join(words)
    if isinstance(rule.extra.get("не разобрано"), list):
        rule.extra["не разобрано"] = " ".join(rule.extra["не разобрано"])


def _parse_extended_rule(action: str, tokens: list, seq: int, device: ParsedDevice,
                         raw_line: str) -> AclRule:
    rule = AclRule(sequence=seq, action=action, raw=raw_line)
    if not tokens:
        return rule
    rule.protocol = tokens[0]
    positional = []
    i = 1
    while i < len(tokens) and tokens[i] not in _FLAG_ARGS:
        positional.append(tokens[i])
        i += 1
    _assign_slots(rule, _group_items(positional), device, raw_line)

    while i < len(tokens):
        kw = tokens[i]
        nargs = _FLAG_ARGS.get(kw)
        if nargs is None:
            rule.extra["не разобрано"] = (rule.extra.get("не разобрано", "") + " " + kw).strip()
            warn_once(device, f"Неизвестный флаг '{kw}' в правиле: {raw_line}")
            i += 1
            continue
        values = tokens[i + 1:i + 1 + nargs]
        i += 1 + nargs
        if kw == "ace-priority" and values and values[0].isdigit():
            rule.sequence = int(values[0])
        elif kw == "log-input":
            rule.log = True
        elif kw == "time-range" and values:
            rule.schedule = values[0]
        elif nargs == 0:
            rule.extra[kw] = "да"
        else:
            rule.extra[kw] = " ".join(values)
    return rule


def _parse_standard_rule(action: str, tokens: list, seq: int, device: ParsedDevice,
                         raw_line: str) -> AclRule:
    """`ip access-list NAME permit {any | A [M]}` — второй адрес читаем как
    wildcard (допущение, см. докстринг модуля)."""
    rule = AclRule(sequence=seq, action=action, raw=raw_line)
    if not tokens:
        rule.source = _any_endpoint()
        return rule
    if len(tokens) >= 2 and _looks_like_ipv4(tokens[0]) and _looks_like_ipv4(tokens[1]):
        warn_once(device, "Маска в standard ACL прочитана как wildcard "
                          "(документация Eltex тип маски не уточняет)")
    ep, i = _parse_endpoint(tokens, 0, {}, set())
    if ep is None:
        warn_once(device, f"Не удалось разобрать адрес в правиле: {raw_line}")
        ep = Endpoint(raw=" ".join(tokens), kind="unresolved", resolved=False)
    rule.source = ep
    leftover = tokens[i:]
    if leftover:
        rule.extra["не разобрано"] = " ".join(leftover)
        warn_once(device, f"Лишние токены в правиле: {raw_line}")
    return rule


_MGMT_IFACE_KW = {"gigabitethernet", "tengigabitethernet", "fortygigabitethernet",
                  "twentyfivegigabitethernet", "hundredgigabitethernet",
                  "port-channel", "vlan"}


def _parse_management_rule(action: str, tokens: list, seq: int, device: ParsedDevice,
                           raw_line: str) -> AclRule:
    rule = AclRule(sequence=seq, action=action, raw=raw_line)
    rule.source = _any_endpoint()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "ip-source" and i + 1 < len(tokens):
            addr = tokens[i + 1]
            i += 2
            mask = None
            if i + 1 < len(tokens) and tokens[i] == "mask":
                mask = tokens[i + 1]
                i += 2
            rule.source = _management_endpoint(addr, mask, device, raw_line)
        elif tok == "service" and i + 1 < len(tokens):
            rule.protocol = tokens[i + 1]
            i += 2
        elif tok == "ace-priority" and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            rule.sequence = int(tokens[i + 1])
            i += 2
        elif tok.lower() in _MGMT_IFACE_KW and i + 1 < len(tokens):
            rule.extra["интерфейс"] = f"{tok} {tokens[i + 1]}"
            i += 2
        elif tok == "oob":
            rule.extra["интерфейс"] = "oob"
            i += 1
        else:
            rule.extra["не разобрано"] = (rule.extra.get("не разобрано", "") + " " + tok).strip()
            warn_once(device, f"Неизвестный токен '{tok}' в management ACL: {raw_line}")
            i += 1
    return rule


def _management_endpoint(addr: str, mask, device: ParsedDevice, raw_line: str) -> Endpoint:
    raw = addr if mask is None else f"{addr} mask {mask}"
    if ":" in addr or (mask is None and "/" in addr and ":" in addr):
        warn_once(device, f"IPv6-адрес в management ACL пропущен: {raw_line}")
        return Endpoint(raw=raw, kind="unresolved", resolved=False)
    try:
        if mask is None:
            if "/" in addr:
                net = ipaddress.ip_network(addr, strict=False)
                return Endpoint(raw=raw, kind="network", networks=[net])
            return Endpoint(raw=raw, kind="host", networks=[ipaddress.ip_network(f"{addr}/32")])
        prefix = mask.lstrip("/")
        net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
        return Endpoint(raw=raw, kind="network" if net.prefixlen < 32 else "host", networks=[net])
    except ValueError:
        warn_once(device, f"Не удалось разобрать адрес в management ACL: {raw_line}")
        return Endpoint(raw=raw, kind="unresolved", resolved=False)


# ── Проход по конфигурации ──────────────────────────────────────────────

def parse_config(text: str, device_name: str) -> ParsedDevice:
    """Разбирает полный `show running-config` Eltex MES и возвращает ACL и
    места их применения."""
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

    ctx = None       # ("acl", name) | ("mgmt", name) | ("interface", name) | ("skip",) | None
    for raw in clean_lines(text):
        line = raw.strip()
        if not line:
            continue
        if line in _BLOCK_END:
            ctx = None
            continue

        m = _EXT_HDR.match(line)
        if m:
            get_acl(m.group(1), "extended")
            ctx = ("acl", m.group(1))
            continue
        m = _STD_LINE.match(line)
        if m:
            name, action, rest = m.group(1), m.group(2), (m.group(3) or "")
            acl = get_acl(name, "standard")
            seq[name] += 10
            acl.rules.append(_parse_standard_rule(action, rest.split(), seq[name], device, line))
            ctx = None
            continue
        m = _SKIP_HDR.match(line)
        if m:
            warn_once(device, f"{m.group(1)} {m.group(2)} пропущен: парсер разбирает только IPv4 ACL")
            ctx = ("skip",)
            continue
        m = _MGMT_HDR.match(line)
        if m:
            get_acl(m.group(1), "management")
            ctx = ("mgmt", m.group(1))
            continue
        m = _MGMT_CLASS.match(line)
        if m:
            if m.group(1) != "console-only":
                device.applications.append(Application(
                    acl_name=m.group(1), kind="line", location="management access-class"))
            ctx = None
            continue
        m = _IFACE_HDR.match(line)
        if m:
            ctx = ("interface", m.group(1))
            continue

        if ctx is None:
            continue
        tokens = line.split()
        if ctx[0] in ("acl", "mgmt") and tokens[0] in ("permit", "deny"):
            name = ctx[1]
            acl = acls[name]
            seq[name] += 10
            if ctx[0] == "acl":
                rule = _parse_extended_rule(tokens[0], tokens[1:], seq[name], device, line)
            else:
                rule = _parse_management_rule(tokens[0], tokens[1:], seq[name], device, line)
            acl.rules.append(rule)
            continue
        if ctx[0] == "interface":
            m = _SERVICE_ACL.match(line)
            if m:
                direction = "in" if m.group(1) == "input" else "out"
                names = [m.group(2)]
                if m.group(3) and m.group(3) != "default-action":
                    names.append(m.group(3))
                for acl_name in names:
                    device.applications.append(Application(
                        acl_name=acl_name, kind="interface", location=ctx[1], direction=direction))
                continue
            m = _ACCESS_GROUP.match(line)
            if m:
                device.applications.append(Application(
                    acl_name=m.group(1), kind="interface", location=ctx[1], direction=m.group(2)))
                continue

    device.acls = [acls[name] for name in order]
    return device


def parse_snapshot(files: dict, device_name: str) -> ParsedDevice:
    """Единый вход для acl_store: files — {имя снимка: текст}; Eltex читает
    только полный running-config («config»)."""
    return parse_config(files.get("config", ""), device_name)
