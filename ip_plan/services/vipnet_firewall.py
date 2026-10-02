"""Парсер политики межсетевого экрана ViPNet xFirewall / HW (снимки
``firewall rules show`` и ``firewall object show``).

Чистая обработка текста: ни сети, ни NetBox. На вход — то, что netbox-sync
снял с криптошлюза по SSH (терминальная сессия, возможно с ``\\r``,
ANSI-последовательностями, эхом команды и приглашением ``host> `` в конце);
на выход — общая модель ``acl_model`` (Acl / AclRule / Endpoint /
ParsedDevice), с которой работает страница «ACL».

Формат «firewall rules show»
============================

Шесть секций: ``Service Vpn Rules``, ``Vpn Rules``, ``Nat Rules``,
``Service Local Rules``, ``Local Rules``, ``Forward Rules``. Пустая секция
печатается одной строкой ``empty rule for  Vpn Rules:``. В каждой секции —
``Current Rules Set Id: N`` и таблица фиксированной ширины::

    Num   Rule ID-Name                                             Option  Schedule
    Act   Protocol               Source                          ->Destination
          DpiProtocol            [G]DpiGroup, DpiApp               DomainUser   (только Forward)

Правила разделены строками из дефисов. Позиции колонок берутся из строк
заголовка (в реальном снимке: Num 0, Rule ID-Name 6, Option 63, Schedule 71;
Act 0, Protocol 6, Source 29, ``->`` 61, Destination 63, ширина таблицы 86),
поэтому другая прошивка с иной шириной колонок не ломает разбор.

Как устройство переносит текст в ячейках (проверено на реальном снимке):

* В Source/Destination **каждый пробел исходного текста даёт перенос** —
  одна строка = одно «слово»: ``@Vpn`` / ``Admins,`` — это объект «Vpn
  Admins»; ``Change:`` / ``10.1.2.3`` — трансляция NAT; ``iface`` / ``eth0``.
  Маска всегда уходит на отдельную строку (``10.1.112.0/`` /
  ``255.255.255.0,``), диапазон рвётся по ``-`` если не влезает
  (``10.1.117.10-`` / ``10.1.117.12,``). Поэтому при сборке колонки строки
  склеиваются без разделителя, если предыдущая кончается на ``/`` или ``-``
  либо заполнена на всю ширину колонки (жёсткий перенос посреди токена), и
  через пробел во всех остальных случаях. Затем текст режется по запятым.
* В Protocol — обычная жадная укладка с переносом по пробелам и дефисам
  (``@SMTP Submission`` / ``Service (TCP) ,``), подстроки портов ``to N`` /
  ``from N`` начинают строку с пробела. Собирается тем же правилом.
* Имя неактивного правила (``4000297(inactive) - Tel``) устройство обрезает
  по ширине 23 символа, а хвост (``net_BLOCK``) печатает **в колонке Source
  строки действия** — ниже строки Num, выше строки со стрелкой ``->``.
  Строка действия без ``->`` поэтому трактуется как продолжение имени, куски
  склеиваются без пробела: «Telnet_BLOCK».
* Имена с кириллицей в снимке, снятом без корректной кодировки, приходят с
  символами U+FFFD и сдвинутыми колонками — такие правила разбираются по
  возможности и помечаются предупреждением.
* Третья строка Forward-правила (DpiProtocol / DpiGroup / DomainUser) — это
  последняя строка блока, у которой заполнена колонка Protocol не с
  пробела и без ``:``/``,`` на конце. Значение DomainUser устройство иногда
  печатает на строку выше остальных двух колонок (когда Destination длинный)
  — это распознаётся и не попадает в адреса назначения.

Отображение в модель
====================

* Один ``Acl`` на непустую секцию; ``name`` = ``acl_type`` = заголовок без
  слова Rules в lower-kebab (``forward``, ``local``, ``nat``, ``vpn``,
  ``service-local``, ``service-vpn``); порядок в ``ParsedDevice.acls`` —
  forward, local, nat, vpn, service-local, service-vpn.
* ``AclRule.sequence`` = Num (порядок в секции), ``rule_id`` = Rule ID
  (4000123), ``name`` = имя правила, ``label`` = «ID - Имя» без «(inactive)»,
  ``inactive`` по пометке, ``action_raw`` = pass/drop/reject/NAT,
  ``action`` = permit для pass и NAT, deny для drop и reject.
* ``protocol`` — человекочитаемый текст: ``tcp``, ``udp``, ``icmp 8``,
  ``ip 47``, ``any``, ``@HTTPS (TCP)``; при одном tcp/udp-элементе порты
  уходят в ``destination_port``/``source_port`` («to 5432» / «from 67»),
  при нескольких элементах всё остаётся в ``protocol``
  («tcp to 5432, tcp to 5433»).
* ``Endpoint``: ``@any`` → any (0.0.0.0/0); ``@local`` → local, resolved=False;
  ``@Obj`` → object-group c сетями включений объекта (вложенные ``@`` ссылки
  раскрываются рекурсивно, циклы отсекаются); адрес → host; адрес/маска →
  network; диапазон → range (summarize_address_range). Несколько значений
  через запятую → ОДИН Endpoint: kind общий, если он у всех одинаков, иначе
  ``mixed``; ``networks`` — объединение; ``object_group`` — первый объект.
  FQDN (``updateids.infotecs.ru``) и интерфейс (``iface eth0``) сетей не
  дают: kind ``unresolved``, resolved=False, сам текст — в
  ``extra["…_fqdn"]`` / ``extra["…_interface"]``.
* ``AclRule.extra`` (подпись → текст, UI показывает как есть):
  ``option`` (User/Generated), ``dpi_protocol`` / ``dpi_group`` /
  ``domain_user`` (только если не ``@any``), ``nat_source_change`` /
  ``nat_destination_change`` / ``nat_port_change`` (трансляция NAT),
  ``source_objects`` / ``destination_objects`` (все объекты, если в
  операнде их несколько), ``source_fqdn`` / ``destination_fqdn``,
  ``source_interface`` / ``destination_interface``, ``unparsed`` (то, что
  классифицировать не удалось — дублируется в ``ParsedDevice.warnings``).
* ``ParsedDevice.object_groups[name]`` — только сети включений объекта,
  ``object_group_exclusions[name]`` — сети исключений; значения, которые
  адресами не являются (FQDN в списках блокировок), в сети не попадают, о
  них одно предупреждение на объект. ``service_objects[name]`` — текст
  сервисного объекта («tcp: to 443»). Объекты интерфейсов и расписаний в
  модели не представлены — их возвращает ``parse_objects``.

Координатор HW (ViPNet Coordinator HW5000)
==========================================

Тот же парсер, отличия берутся из заголовков таблиц:

* ширина 80, заголовок ``Num   Name … Option   Schedule`` — у правил нет ID,
  стрелка ``> `` вместо ``->``, строки ``Current Rules Set Id`` нет, строки
  DpiProtocol нет ни в одной секции;
* ``(inactive)`` — префикс перед именем (``(inactive) To all tunnel nodes``):
  ``rule_id`` = None, ``name`` = ``label`` = имя без префикса;
* секции: Service Vpn, Vpn (непустая), ``empty rule for  Nat Rules:``,
  **Tunnel** (slug ``tunnel``), Service Local, Local, Forward;
* VPN-объекты (``Vpn Objects``: admin, users, Сотрудники_СИЦ…) состоят из
  идентификаторов узлов и пользователей ViPNet (``0x4f260022``), а не из
  адресов: в ``object_groups`` они есть с пустым списком сетей, предупреждения
  «не являются адресами» для них не выдаются. Те же идентификаторы бывают
  прямо в Source/Destination туннельных правил — они перечисляются в
  ``extra["source_vpn_nodes"]`` / ``["destination_vpn_nodes"]``, операнд
  получает kind ``unresolved``;
* ``@tunneledip`` — встроенный операнд «все туннелируемые адреса»: как
  ``@local``, адресов из снимка не видно (kind ``unresolved``,
  resolved=False, в неразрешённые объекты не попадает);
* имена объектов координатор хранит в UTF-8, а в колонках Source /
  Destination печатает байтами, так что в KOI8-R-сессии ``@Запрет_Telnet``
  приходит как ``@п≈п╟п©я─п╣я┌_Telnet`` и сдвигает стрелку вправо. Текст
  ячеек перекодируется обратно (``_fix_mojibake``), позиция стрелки берётся
  из самой строки.

Узлы ViPNet по ``iplir show config``
====================================

Третий файл снимка (``files["iplir_config"]``) — секции ``[id]`` с полями
``id=``, ``name=`` (имя — всё до прогона из 2+ пробелов перед меткой сети
``VPN-2870``; в имени бывают пробелы, точки, скобки, кириллица), ``ip=``,
``tunnel=``, ``virtualip=``, ``accessip=``. Прочие секции (``[adapter]``,
``[misc]``, …) игнорируются. ``parse_iplir`` отдаёт словарь «id (как
напечатан, в нижнем регистре) → IplirNode».

Семантика адресов: ``ip= A, B`` — пара «реальный адрес узла → его
виртуальный адрес, каким он виден с этого координатора» (строк с одним
адресом почти нет, строк на узел бывает несколько — по одной на интерфейс);
``tunnel= R1-R2 to V1-V2`` — реальный туннелируемый диапазон за чужим
координатором и его виртуальное отображение, ``tunnel= R1-R2`` — только
реальный диапазон; узлы без ``ip=`` вовсе (клиенты за NAT, 124 из 300 в
живом снимке) видны координатору по ``virtualip=``.

Адреса узла для фильтра = реальные + виртуальные из пар ``ip=`` +
``virtualip`` + обе стороны ``tunnel=`` (без дублей). Правило по
идентификатору узла срабатывает на узел, как бы его трафик ни был виден —
реальным адресом или виртуальным 11.x, — поэтому и поиск по IP обязан
находить правило по любому из них. Так VPN-объекты (``0x…``) и
идентификаторы прямо в Source/Destination туннельных правил получают сети;
состав объекта «как есть» — в ``ParsedDevice.object_group_members``:
``0x4f26007c Иванова И. И. (Офис) VPN-20262 (192.168.1.65 → 11.0.1.6)``,
узел только с virtualip — ``… (virt 11.0.0.214)``, координатор с туннелями
— ``…, туннели: N``, неизвестный id — ``0x… (нет в iplir)``. Короткий id
``0x06e1`` (4 hex-цифры) — номер сети ViPNet, а не узла: в iplir его нет по
определению, он помечается «сеть ViPNet» без предупреждения. Без файла
iplir сети VPN-объектов пусты, участники — голые id, одно предупреждение.

Точки входа: ``parse_snapshot(files, device_name)`` (``files["firewall_rules"]``,
``files["firewall_objects"]``, ``files["iplir_config"]`` — любой может
отсутствовать), а также ``parse_rules(text)``, ``parse_objects(text)``,
``parse_iplir(text)`` и ``rules_set_id(text)``.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

from .acl_model import Acl, AclRule, Application, Endpoint, ParsedDevice

PARSER_NAME = "xfw"

# ── очистка терминального вывода ────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\x1b\][^\x07]*\x07")
# эхо команды («mdc-xfw2> firewall rules show») и голое приглашение в конце
_PROMPT_RE = re.compile(r"^[\w.\-]*[>#]\s*(?:firewall\b.*)?$")

_SET_ID_RE = re.compile(r"Current Rules Set Id:\s*(\d+)")
_SEP_RE = re.compile(r"^-{5,}\s*$")
_HDR_RE = re.compile(r"^={5,}\s*$")
_SECTION_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?) Rules:\s*$")
_EMPTY_SECTION_RE = re.compile(r"^\s*empty rule for\s+([A-Za-z][A-Za-z ]*?) Rules:\s*$")
_OBJ_SECTION_RE = re.compile(r"^\s*(Ip|Vpn|Interface|Service|Schedule) Objects\s*$")

_OPTION_WORDS = ("User", "Generated", "Policy")
_ACTIONS = {"pass": "permit", "drop": "deny", "reject": "deny", "NAT": "permit"}
_ACL_ORDER = ["local", "forward", "tunnel", "vpn", "nat", "service-local", "service-vpn"]

# Какие секции становятся списками доступа на странице ACL. Local и Forward —
# сетевые фильтры (категории пользовательских правил), NAT — трансляции, их
# тоже смотрят рядом. Tunnel и Vpn — правила координатора HW (туннели и
# защищённый трафик между узлами ViPNet); у xFW эти секции пусты, так что для
# него список тот же. Service Local / Service Vpn целиком Generated: их пишет
# сама платформа для своих служб, руками они не меняются, для контроля
# доступа это шум. Полный набор остаётся в файле снимка (история/diff видят
# всё), а sections=None разбирает всё.
DEFAULT_SECTIONS = ("local", "forward", "tunnel", "vpn", "nat")

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# идентификатор узла/пользователя ViPNet в Source/Destination координатора
_VPN_NODE_RE = re.compile(r"^0x[0-9a-fA-F]{4,8}$")
# 4 hex-цифры — номер сети ViPNet, а не узла (у узла ещё 4 цифры после них)
_VPN_NET_ID_RE = re.compile(r"^0x[0-9a-fA-F]{4}$")
# встроенные операнды без адресов в снимке: @local — сам узел, @tunneledip —
# все адреса, которые координатор туннелирует
_BUILTIN_UNRESOLVED = {"tunneledip"}
_FQDN_RE = re.compile(r"^[A-Za-z0-9*_][A-Za-z0-9*_\-]*(?:\.[A-Za-z0-9_\-]+)+\.?$")
_PROTO_ITEM_RE = re.compile(r"^(tcp/udp|tcp|udp|icmp|ip|gre|esp|ah)\s*:\s*(.*)$", re.I)
_PORT_CHANGE_RE = re.compile(r"\s*Change\s+(\w*Port):\s*(\S+)")
_CHANGE_RE = re.compile(r"\s*Change:\s*")


def _clean_lines(text: str) -> list:
    """Убирает ANSI, ``\\r``, эхо команды и приглашение; возвращает строки
    с сохранёнными ведущими пробелами (позиции колонок важны)."""
    text = _ANSI_RE.sub("", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in text.split("\n"):
        line = line.rstrip()
        if _PROMPT_RE.match(line.strip()):
            continue
        out.append(line)
    return out


def rules_set_id(text: str):
    """Значение ``Current Rules Set Id`` из любого из двух снимков или None."""
    m = _SET_ID_RE.search(_ANSI_RE.sub("", text or ""))
    return m.group(1) if m else None


# Байты 0x80–0xBF в KOI8-R — псевдографика и символы; в UTF-8 это байты
# продолжения любой кириллицы. Если они встретились в тексте, снятом как
# KOI8-R, перед нами UTF-8, показанный терминалом как KOI8-R.
_KOI8_GRAPHICS_RE = re.compile(r"[\u2500-\u25ff\u2219\u221a\u2248\u2264\u2265\u2320\u2321\u00b0\u00b2\u00b7\u00f7]")


def _fix_mojibake(text: str) -> str:
    """Координатор HW хранит имена объектов в UTF-8, но колонки Source /
    Destination печатает байтами в KOI8-R-сессии: «@Запрет_Telnet» приходит
    как «@п≈п╟п©я─п╣я┌_Telnet». Перекодировка koi8-r → utf-8 возвращает
    исходное имя; если текст не таков, возвращается как есть."""
    if not _KOI8_GRAPHICS_RE.search(text):
        return text
    try:
        return text.encode("koi8-r").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _looks_like_ipv4(tok: str) -> bool:
    return bool(_IPV4_RE.match(tok)) and all(int(o) <= 255 for o in tok.split("."))


def _norm_name(name: str) -> str:
    """Ключ для поиска объекта по имени: устройство рвёт имена по пробелам
    и печатает ведущие/концевые пробелы отдельной строкой, так что
    сравниваем без пробелов вообще."""
    return re.sub(r"\s+", "", name)


# ── сборка колонки из перенесённых строк ────────────────────────────────

def _join_column(lines: list, width: int) -> str:
    """Собирает текст ячейки из строк одной колонки. Склейка без разделителя
    — если предыдущая строка кончается на ``/`` или ``-`` (маска, диапазон,
    дефис в имени) либо занята на всю ширину колонки и не кончается запятой
    (жёсткий перенос посреди токена); иначе — через пробел (перенос по
    пробелу исходного текста, сам пробел устройство не печатает)."""
    out = ""
    prev_raw = None
    for raw in lines:
        piece = raw.strip()
        if not piece:
            prev_raw = raw
            continue
        if not out:
            out = piece
        else:
            prev = prev_raw.rstrip() if prev_raw is not None else ""
            glue = (
                prev.endswith(("/", "-")) and not prev.endswith((" -", " /"))
            ) or (
                width > 0 and len(prev.lstrip()) >= width and not prev.endswith(",")
            )
            out += piece if glue else " " + piece
        prev_raw = raw
    return out


def _split_values(text: str) -> list:
    return [v.strip() for v in text.split(",") if v.strip()]


def _split_refs(text: str, known: set) -> list:
    """Как _split_values, но куски, разрезанные запятой внутри имени объекта
    (``@mdc-temp,sw-fw``), склеиваются обратно, если склейка даёт известное
    имя (known — нормализованные имена, см. _norm_name)."""
    pieces = _split_values(text)
    if not known:
        return pieces
    out: list = []
    i = 0
    while i < len(pieces):
        cur = pieces[i]
        if cur.startswith("@") and _norm_name(cur[1:]) not in known:
            j = i + 1
            merged = cur
            while j < len(pieces) and j - i <= 3:
                merged = merged + "," + pieces[j]
                if _norm_name(merged[1:]) in known:
                    cur, i = merged, j
                    break
                j += 1
        out.append(cur)
        i += 1
    return out


# ── объекты (firewall object show) ──────────────────────────────────────

@dataclass
class FwObject:
    """Один объект таблицы ``firewall object show``. kind: ip | vpn |
    interface | service | schedule. inclusion/exclusion — исходные строки
    значений (адреса, ``@ссылки``, FQDN, ``iface eth0``, ``tcp: to 443``)."""

    name: str
    kind: str
    creation: str = ""
    inclusion: list = field(default_factory=list)
    exclusion: list = field(default_factory=list)
    raw: str = ""


@dataclass
class FirewallObjects:
    ip: dict = field(default_factory=dict)
    vpn: dict = field(default_factory=dict)
    interface: dict = field(default_factory=dict)
    service: dict = field(default_factory=dict)
    schedule: dict = field(default_factory=dict)
    set_id: str | None = None
    warnings: list = field(default_factory=list)

    def table(self, kind: str) -> dict:
        return getattr(self, kind)

    def lookup(self, kind: str, name: str):
        """Объект по имени: сначала точное совпадение (устройство различает
        « mdc-asutk-test» с ведущим пробелом и «mdc-asutk-test», и в правилах
        печатает их по-разному — «@ mdc-asutk-test» и «@mdc-asutk-test»),
        и только потом без учёта пробелов (см. _norm_name) — для имён,
        собранных из переносов строк, где пробел мог потеряться или
        появиться лишний."""
        table = self.table(kind)
        if name in table:
            return table[name]
        for obj in table.values():
            if obj.name == name:
                return obj
        key = _norm_name(name)
        for obj in table.values():
            if _norm_name(obj.name) == key:
                return obj
        return None


def _object_header(line: str, name_col: int, type_col: int):
    """(имя, тип создания) из строки-заголовка объекта либо None."""
    m = re.match(r"^(\d+)\s", line)
    if not m:
        return None
    tail = line[type_col:].strip()
    if tail.split(" ", 1)[0] in _OPTION_WORDS:
        # имя как напечатано: ведущий пробел — часть имени (устройство
        # различает « mdc-asutk-test» и «mdc-asutk-test»), концевые
        # пробелы от отступа колонки не отличить
        return line[name_col:type_col].rstrip(), tail.split(" ", 1)[0]
    # имя с кириллицей (в снимке — U+FFFD) сдвигает колонку типа вправо
    m = re.match(r"^\d+\s+(.*\S)\s+(User|Generated|Policy)\s*$", line)
    if m:
        return re.sub(r"\s{2,}", "", m.group(1)).strip(), m.group(2)
    return None


def parse_objects(text: str) -> FirewallObjects:
    """Разбирает ``firewall object show`` целиком: все пять таблиц объектов
    (Vpn Objects может отсутствовать)."""
    lines = _clean_lines(text)
    result = FirewallObjects(set_id=rules_set_id(text))

    kind = None
    name_col, type_col, excl_col, width = 4, 53, 38, 77
    in_header = False
    block: list = []

    def flush():
        nonlocal block
        if kind is None or not any(l.strip() for l in block):
            block = []
            return
        first_idx = next((i for i, l in enumerate(block) if re.match(r"^\d+\s", l)), None)
        if first_idx is None:
            result.warnings.append(
                f"{kind} Objects: строки вне таблицы не разобраны: "
                + " | ".join(l.strip() for l in block if l.strip()))
            block = []
            return
        hdr = _object_header(block[first_idx], name_col, type_col)
        if hdr is None:
            result.warnings.append(
                f"{kind} Objects: заголовок объекта не распознан: «{block[first_idx].strip()}»")
            block = []
            return
        name, creation = hdr
        body = block[first_idx + 1:]
        incl = _fix_mojibake(_join_column([l[:excl_col] for l in body], excl_col))
        excl = _fix_mojibake(_join_column([l[excl_col:] for l in body], width - excl_col))
        obj = FwObject(name=name, kind=kind, creation=creation,
                       inclusion=_split_values(incl), exclusion=_split_values(excl),
                       raw="\n".join(block))
        if "�" in name:
            result.warnings.append(
                f"{kind} Objects: имя объекта «{name}» повреждено при снятии "
                "(не-ASCII символы заменены), ссылки на него из правил не разрешатся")
        table = result.table(kind)
        if name in table:
            # два объекта с одинаково напечатанным именем (в живом снимке —
            # «SMTP Submission Service (TCP)» и то же имя с концевым
            # пробелом): второй сохраняем с суффиксом, чтобы не потерять
            result.warnings.append(
                f"{kind} Objects: имя «{name}» встречается дважды, второй объект "
                f"сохранён как «{name} (2)»")
            name = f"{name} (2)"
            obj.name = name
        table[name] = obj
        block = []

    for line in lines:
        m = _OBJ_SECTION_RE.match(line)
        if m:
            flush()
            kind = m.group(1).lower()
            in_header = False
            continue
        if kind is None:
            continue
        if _SET_ID_RE.search(line):
            continue
        if _HDR_RE.match(line):
            # первая линия «=» открывает шапку, вторая закрывает
            in_header = not in_header
            width = len(line.strip())
            continue
        if in_header:
            if "Name" in line and "Creation type" in line:
                name_col = line.index("Name")
                type_col = line.index("Creation type")
            elif "Inclusion" in line:
                excl_col = line.index("Exclusion") if "Exclusion" in line else 38
                width = max(len(line), excl_col + 1)
            continue
        if _SEP_RE.match(line):
            flush()
            continue
        if _SECTION_RE.match(line) or _EMPTY_SECTION_RE.match(line):
            # начался вывод другой команды
            flush()
            kind = None
            continue
        block.append(line)
    flush()
    return result


# ── узлы ViPNet (iplir show config) ─────────────────────────────────────

_IPLIR_SECTION_RE = re.compile(r"^\s*\[(\w+)\]\s*$")
_IPLIR_KV_RE = re.compile(r"^\s*([a-z_]+)\s*=\s*(.*?)\s*$")
_IPLIR_NAME_RE = re.compile(r"^(.*?)\s+(VPN-\S+)$")
_IPLIR_TUNNEL_RE = re.compile(
    r"^(\S+?)-(\S+?)(?:\s+to\s+(\S+?)-(\S+?))?$")


@dataclass
class IplirNode:
    """Секция ``[id]`` из ``iplir show config``. ips — пары (реальный,
    виртуальный | None) из строк ``ip=``; tunnels — строки ``tunnel=`` как
    напечатаны."""

    id: str
    name: str = ""
    network: str | None = None           # метка сети, «VPN-2870»
    ips: list = field(default_factory=list)
    tunnels: list = field(default_factory=list)
    virtualip: str | None = None
    accessip: str | None = None

    def networks(self) -> list:
        """Все адреса, которыми узел виден координатору (см. докстринг
        модуля): реальные и виртуальные из пар ip=, virtualip, обе стороны
        туннелей."""
        nets: list = []
        for real, virt in self.ips:
            for ip in (real, virt):
                if ip and _looks_like_ipv4(ip):
                    nets.append(ipaddress.ip_network(f"{ip}/32"))
        if self.virtualip and _looks_like_ipv4(self.virtualip):
            nets.append(ipaddress.ip_network(f"{self.virtualip}/32"))
        for rng in self.tunnels:
            m = _IPLIR_TUNNEL_RE.match(rng)
            if not m:
                continue
            for lo, hi in ((m.group(1), m.group(2)), (m.group(3), m.group(4))):
                if lo and hi:
                    parsed = _value_networks(f"{lo}-{hi}")
                    if parsed is not None:
                        nets.extend(parsed[1])
        return _dedupe(nets)

    def member_text(self) -> str:
        """«0x… имя VPN-… (реальный → виртуальный[; …][, туннели: N])»."""
        head = " ".join(p for p in (self.id, self.name, self.network) if p)
        if self.ips:
            body = "; ".join(f"{real} → {virt}" if virt else real for real, virt in self.ips)
        elif self.virtualip:
            body = f"virt {self.virtualip}"
        else:
            body = "адресов нет"
        if self.tunnels:
            body += f", туннели: {len(self.tunnels)}"
        return f"{head} ({body})"


def parse_iplir(text: str) -> dict:
    """``iplir show config`` → {id в нижнем регистре: IplirNode}. Терпит
    терминальный мусор, ``\r`` и отсутствующие поля; секции кроме ``[id]``
    пропускаются."""
    nodes: dict = {}
    node: IplirNode | None = None
    in_id = False
    for line in _clean_lines(text):
        m = _IPLIR_SECTION_RE.match(line)
        if m:
            if node is not None and node.id:
                nodes.setdefault(node.id.lower(), node)
            node = None
            in_id = m.group(1) == "id"
            if in_id:
                node = IplirNode(id="")
            continue
        if not in_id or node is None:
            continue
        m = _IPLIR_KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key == "id":
            node.id = value.split()[0].lower() if value else ""
        elif key == "name" and not node.name:
            nm = _IPLIR_NAME_RE.match(value)
            if nm:
                node.name, node.network = nm.group(1).strip(), nm.group(2)
            else:
                node.name = value.strip()
        elif key == "ip":
            parts = [v.strip() for v in value.split(",") if v.strip()]
            if parts:
                node.ips.append((parts[0], parts[1] if len(parts) > 1 else None))
        elif key == "tunnel":
            node.tunnels.append(value)
        elif key == "virtualip" and not node.virtualip:
            node.virtualip = value.split(",")[0].strip() or None
        elif key == "accessip" and not node.accessip:
            node.accessip = value.split(",")[0].strip() or None
    if node is not None and node.id:
        nodes.setdefault(node.id.lower(), node)
    return nodes


# ── разрешение адресов ──────────────────────────────────────────────────

def _value_networks(value: str):
    """Сети для литерала: host, a.b.c.d/маска, a.b.c.d/nn, диапазон.
    Возвращает (kind, [IPv4Network]) либо None, если это не адрес."""
    v = value.strip()
    if "-" in v and not v.startswith("@"):
        lo, hi = (p.strip() for p in v.split("-", 1))
        if _looks_like_ipv4(lo) and _looks_like_ipv4(hi):
            a, b = ipaddress.IPv4Address(lo), ipaddress.IPv4Address(hi)
            if a > b:
                a, b = b, a
            return "range", list(ipaddress.summarize_address_range(a, b))
        return None
    if "/" in v:
        ip, mask = v.split("/", 1)
        if _looks_like_ipv4(ip) and (_looks_like_ipv4(mask) or mask.isdigit()):
            try:
                return "network", [ipaddress.ip_network(f"{ip}/{mask}", strict=False)]
            except ValueError:
                return None
        return None
    if _looks_like_ipv4(v):
        return "host", [ipaddress.ip_network(f"{v}/32")]
    return None


class _Resolver:
    """Раскрывает объекты в сети (рекурсивно по ``@ссылкам``, с защитой от
    циклов), считает пропущенные не-адресные значения и запоминает
    неизвестные имена."""

    def __init__(self, objects: FirewallObjects | None, device: ParsedDevice,
                 nodes: dict | None = None):
        self.objects = objects
        self.device = device
        self.nodes = nodes                 # parse_iplir() или None (файла нет)
        self.node_refs_seen = False        # встречались ли 0x… вообще
        self.unknown_ids: list = []        # id, которых нет в iplir (все)
        self._memo: dict = {}
        self._cycles: set = set()
        self.known: set = set()
        if objects is not None:
            for kind in ("ip", "vpn"):
                self.known.update(_norm_name(n) for n in objects.table(kind))

    def split(self, text: str) -> list:
        return _split_refs(text, self.known)

    def find(self, name: str):
        if self.objects is None:
            return None
        return self.objects.lookup("ip", name) or self.objects.lookup("vpn", name)

    def node(self, node_id: str):
        """(сети узла, IplirNode | None). Неизвестный id запоминается;
        номер сети (0x06e1) узлом не является и неизвестным не считается."""
        self.node_refs_seen = True
        if self.nodes is None or _VPN_NET_ID_RE.match(node_id):
            return [], None
        found = self.nodes.get(node_id.lower())
        if found is None:
            if node_id.lower() not in self.unknown_ids:
                self.unknown_ids.append(node_id.lower())
            return [], None
        return found.networks(), found

    def is_unknown(self, node_id: str) -> bool:
        return (self.nodes is not None and not _VPN_NET_ID_RE.match(node_id)
                and node_id.lower() not in self.nodes)

    def member_text(self, node_id: str) -> str:
        _, found = self.node(node_id)
        if found is not None:
            return found.member_text()
        if _VPN_NET_ID_RE.match(node_id):
            return f"{node_id} (сеть ViPNet)"
        return node_id if self.nodes is None else f"{node_id} (нет в iplir)"

    def resolve_values(self, values: list, stack: tuple = ()):
        """(сети, число пропущенных не-адресных значений). Идентификаторы
        узлов ViPNet (0x…) разрешаются через iplir; без iplir они считаются
        пропущенными (как и прочие не-адреса)."""
        nets: list = []
        skipped = 0
        for value in values:
            if _VPN_NODE_RE.match(value):
                sub, found = self.node(value)
                if found is None:
                    skipped += 1
                nets.extend(sub)
                continue
            if value.startswith("@"):
                ref = value[1:].rstrip()   # ведущий пробел — часть имени
                if ref == "any":
                    nets.append(ipaddress.ip_network("0.0.0.0/0"))
                    continue
                if ref == "local" or ref in _BUILTIN_UNRESOLVED:
                    skipped += 1
                    continue
                sub, exists = self.networks(ref, stack)
                if not exists:
                    self.device.unresolved_object_groups.add(ref)
                nets.extend(sub)
                continue
            parsed = _value_networks(value)
            if parsed is None:
                skipped += 1
            else:
                nets.extend(parsed[1])
        return _dedupe(nets), skipped

    def networks(self, name: str, stack: tuple = ()):
        """(сети включений объекта, существует ли объект). Ключ кэша и стека
        циклов — имя найденного объекта как есть, а не нормализованное:
        « mdc-asutk-test» и «mdc-asutk-test» — разные объекты устройства."""
        obj = self.find(name)
        key = obj.name if obj is not None else _norm_name(name)
        if key in self._memo:
            return self._memo[key]
        if key in stack:
            cycle = stack[stack.index(key):] + (key,)
            if frozenset(cycle) not in self._cycles:
                self._cycles.add(frozenset(cycle))
                self.device.warnings.append(
                    f"Объект {name}: циклическая ссылка {' → '.join(cycle)}, цикл отброшен")
            return [], True
        if obj is None:
            return [], False
        cycles_before = len(self._cycles)
        nets, _ = self.resolve_values(self.split(", ".join(obj.inclusion)), stack + (key,))
        # результат, посчитанный внутри разорванного цикла, неполон — его
        # запоминаем только у объекта верхнего уровня (там он окончателен)
        if not stack or len(self._cycles) == cycles_before:
            self._memo[key] = (nets, True)
        return nets, True


def _dedupe(nets: list) -> list:
    seen: set = set()
    out = []
    for n in nets:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _fill_object_groups(objects: FirewallObjects, device: ParsedDevice, resolver: _Resolver):
    single_fqdn: list = []
    for kind in ("ip", "vpn"):
        for obj in objects.table(kind).values():
            name = obj.name
            nets, _ = resolver.networks(name)
            _, skipped = resolver.resolve_values(
                resolver.split(", ".join(obj.inclusion)), (name,))
            device.object_groups[name] = nets
            excl, _ = resolver.resolve_values(
                resolver.split(", ".join(obj.exclusion)), (name,))
            if excl:
                device.object_group_exclusions[name] = excl
            # вложенные объекты: «@child» среди включений — чтобы объект,
            # который используется только внутри другого объекта, не выглядел
            # на странице неиспользуемым
            children = []
            for value in resolver.split(", ".join(obj.inclusion)):
                if value.startswith("@") and value[1:].rstrip() not in ("any", "local"):
                    child = resolver.find(value[1:].rstrip())
                    children.append(child.name if child is not None else value[1:].rstrip())
            if children:
                device.object_group_includes[name] = children
            # состав объекта как есть: идентификаторы узлов — с именем и
            # адресами из iplir, остальное — исходным текстом
            members = []
            unknown = []
            for value in resolver.split(", ".join(obj.inclusion)):
                if _VPN_NODE_RE.match(value):
                    members.append(resolver.member_text(value))
                    if resolver.is_unknown(value):
                        unknown.append(value)
                else:
                    members.append(value)
            device.object_group_members[name] = members
            if unknown:
                device.warnings.append(
                    f"Объект {name}: {len(unknown)} идентификаторов узлов нет в iplir: "
                    + ", ".join(unknown))
            if kind == "vpn":
                # значения VPN-объекта — идентификаторы узлов/пользователей
                # ViPNet (0x4f260022): адреса у них только через iplir, и об
                # отсутствии iplir сказано одним общим предупреждением
                continue
            if skipped > 1 or (skipped and nets):
                device.warnings.append(
                    f"Объект {name}: {skipped} значений не являются адресами (FQDN и т.п.), "
                    "в поиск по IP не входят")
            elif skipped:
                single_fqdn.append(name)
    if single_fqdn:
        # объектов «одно доменное имя» в списках блокировок сотни — одной
        # строкой, чтобы не утопить остальные предупреждения
        device.warnings.append(
            f"{len(single_fqdn)} объектов состоят из одного значения не-адреса (FQDN), "
            "в поиск по IP не входят: " + ", ".join(single_fqdn))
    for obj in objects.service.values():
        device.service_objects[obj.name] = ", ".join(obj.inclusion)
    device.warnings.extend(objects.warnings)


# ── таблица правил ──────────────────────────────────────────────────────

@dataclass
class _Cols:
    """Позиции колонок (из строк заголовка секции). xFW: «Rule ID-Name»,
    стрелка «->», ширина 86; координатор HW: «Name» без ID, стрелка «> »,
    ширина 80."""

    name: int = 6
    opt: int = 63
    sched: int = 71
    proto: int = 6
    src: int = 29
    arrow: int = 61
    width: int = 86
    has_dpi: bool = False
    has_rule_id: bool = True
    arrow_token: str = "->"

    @property
    def dst(self):
        return self.arrow + len(self.arrow_token)


@dataclass
class _Section:
    title: str
    set_id: str | None = None
    cols: _Cols = field(default_factory=_Cols)
    blocks: list = field(default_factory=list)


def _section_slug(title: str) -> str:
    return re.sub(r"\s+", "-", title.strip().lower())


def _split_sections(lines: list) -> list:
    """Секции ``firewall rules show`` с блоками правил (сырые строки)."""
    sections: list = []
    current: _Section | None = None
    in_header = False
    header_seen = False
    block: list = []

    def flush_block():
        nonlocal block
        if current is not None and any(l.strip() for l in block):
            current.blocks.append(block)
        block = []

    for line in lines:
        m = _SECTION_RE.match(line)
        if m and not line.startswith("      "):
            flush_block()
            current = _Section(title=m.group(1).strip())
            sections.append(current)
            in_header = header_seen = False
            continue
        if _EMPTY_SECTION_RE.match(line):
            flush_block()
            current = None
            continue
        if _OBJ_SECTION_RE.match(line):
            flush_block()
            current = None
            continue
        if current is None:
            continue
        m = _SET_ID_RE.search(line)
        if m and not header_seen:
            current.set_id = m.group(1)
            continue
        if _HDR_RE.match(line):
            if not header_seen:
                in_header = not in_header
                if not in_header:
                    header_seen = True
                current.cols.width = len(line.strip())
            continue
        if in_header:
            c = current.cols
            if line.startswith("Num") and "Name" in line:
                c.has_rule_id = "Rule ID" in line
                c.name = line.index("Rule ID-Name") if c.has_rule_id else line.index("Name")
                c.opt = line.index("Option") if "Option" in line else c.opt
                c.sched = line.index("Schedule") if "Schedule" in line else c.sched
            elif "Destination" in line and line.startswith("Act"):
                c.proto = line.index("Protocol") if "Protocol" in line else c.proto
                c.src = line.index("Source") if "Source" in line else c.src
                if "->Destination" in line:
                    c.arrow_token, c.arrow = "->", line.index("->Destination")
                elif "> Destination" in line:
                    c.arrow_token, c.arrow = "> ", line.index("> Destination")
            elif "DpiProtocol" in line:
                c.has_dpi = True
            continue
        if _SEP_RE.match(line):
            flush_block()
            continue
        block.append(line)
    flush_block()
    return sections


_NUM_ROW_RE = re.compile(r"^(\d+)\s")
_LABEL_RE = re.compile(r"^(\d+)\s*(\(inactive\))?\s*-\s*(.*)$")


def _parse_num_row(line: str, cols: _Cols):
    """(num, ячейка имени, option, schedule)."""
    num = int(_NUM_ROW_RE.match(line).group(1))
    tail = line[cols.opt:].strip() if len(line) > cols.opt else ""
    first = tail.split(" ", 1)[0] if tail else ""
    if first in _OPTION_WORDS:
        name_cell = line[cols.name:cols.opt].strip()
        if not name_cell.isascii():
            # не-ASCII имя устройство считает в байтах: обрезанный хвост
            # допечатан в той же строке через много пробелов — склеиваем
            name_cell = re.sub(r"\s{2,}", "", name_cell)
        schedule = tail[len(first):].strip() or None
        return num, name_cell, first, schedule
    # колонки сдвинуты (кириллица в имени): берём последнее слово-опцию
    m = re.match(r"^\d+\s+(.*\S)\s+(User|Generated|Policy)(?:\s+(\S.*?))?\s*$", line)
    if m:
        # обрезанное имя устройство допечатывает в той же строке через
        # много пробелов — куски склеиваем
        name_cell = re.sub(r"\s{2,}", "", m.group(1)).strip()
        return num, name_cell, m.group(2), (m.group(3) or None)
    return num, line[cols.name:].strip(), None, None


def _cell(line: str, start: int, end: int | None = None) -> str:
    return line[start:end] if end is not None else line[start:]


def _parse_protocol(text: str, device: ParsedDevice, ctx: str, extra: dict):
    """(protocol, source_port, destination_port). Сервисные объекты
    остаются ``@Имя``; порты выносятся только при единственном элементе."""
    if not text.strip():
        return None, None, None
    m = _PORT_CHANGE_RE.search(text)
    if m:
        extra["nat_port_change"] = f"{m.group(1)}: {m.group(2)}"
        text = _PORT_CHANGE_RE.sub("", text)
    items = _split_values(text)
    parts = []
    single = None
    for item in items:
        if item.startswith("@"):
            name = item[1:].rstrip()   # ведущий пробел — часть имени (см. _Resolver.lookup)
            if name == "any":
                parts.append("any")
            else:
                parts.append("@" + name)
                if device.service_objects and _norm_name(name) not in {
                        _norm_name(k) for k in device.service_objects}:
                    device.warnings.append(
                        f"{ctx}: сервисный объект @{name} не найден в firewall object show")
            continue
        pm = _PROTO_ITEM_RE.match(item)
        if not pm:
            parts.append(item)
            device.warnings.append(f"{ctx}: протокол «{item}» не распознан")
            extra.setdefault("unparsed", [])
            extra["unparsed"].append(f"protocol: {item}")
            continue
        proto, rest = pm.group(1).lower(), pm.group(2).strip()
        if proto in ("icmp",) and rest.lower() in ("", "any"):
            rest = ""
        src_port = dst_port = None
        if proto in ("tcp", "udp", "tcp/udp"):
            fm = re.search(r"\bfrom\s+(\S+)", rest)
            tm = re.search(r"\bto\s+(\S+)", rest)
            if fm:
                src_port = f"from {fm.group(1)}"
            if tm:
                dst_port = f"to {tm.group(1)}"
            human = " ".join(p for p in (proto, src_port, dst_port) if p)
            if not fm and not tm and rest:
                human = f"{proto} {rest}"
        else:
            human = f"{proto} {rest}".strip()
        parts.append(human)
        # у tcp/udp порты выносятся в отдельные поля, у остальных (icmp 8,
        # ip 47) уточнение остаётся в самом тексте протокола
        single = (proto if src_port or dst_port else human, src_port, dst_port)
    if "unparsed" in extra:
        extra["unparsed"] = "; ".join(extra["unparsed"])
    if len(items) == 1 and single is not None:
        proto, src_port, dst_port = single
        return proto, src_port, dst_port
    return ", ".join(parts), None, None


def _build_endpoint(text: str, resolver: _Resolver, device: ParsedDevice,
                    ctx: str, side: str, extra: dict):
    """Endpoint из текста колонки Source/Destination (уже без ``Change:``)."""
    items = resolver.split(text)
    if not items:
        return None
    kinds: list = []
    nets: list = []
    objects: list = []
    fqdns: list = []
    ifaces: list = []
    nodes: list = []
    node_nets: list = []   # «0x…: сеть, сеть» — сети каждого узла отдельно (для acl_search)
    resolved = True
    for item in items:
        if item.startswith("@"):
            name = item[1:].rstrip()   # ведущий пробел — часть имени (см. _Resolver.lookup)
            if name == "any":
                kinds.append("any")
                nets.append(ipaddress.ip_network("0.0.0.0/0"))
            elif name == "local":
                kinds.append("local")
                resolved = False
            elif name in _BUILTIN_UNRESOLVED:
                kinds.append("unresolved")
                resolved = False
            else:
                kinds.append("object-group")
                obj = resolver.find(name)
                display = obj.name if obj else name
                objects.append(display)
                sub, exists = resolver.networks(name)
                if not exists:
                    device.unresolved_object_groups.add(name)
                    resolved = False
                nets.extend(sub)
            continue
        parsed = _value_networks(item)
        if parsed is not None:
            kinds.append(parsed[0])
            nets.extend(parsed[1])
            continue
        low = item.lower()
        if low.startswith("iface "):
            kinds.append("unresolved")
            ifaces.append(item[6:].strip())
            resolved = False
            continue
        if _FQDN_RE.match(item):
            kinds.append("unresolved")
            fqdns.append(item)
            resolved = False
            continue
        if _VPN_NODE_RE.match(item):
            sub, found = resolver.node(item)
            nodes.append(resolver.member_text(item))
            if sub:
                node_nets.append(f"{item}: {', '.join(str(n) for n in sub)}")
            if found is None:
                kinds.append("unresolved")
                resolved = False
            else:
                kinds.append("host" if all(n.prefixlen == 32 for n in sub) else "network")
                nets.extend(sub)
            continue
        kinds.append("unresolved")
        resolved = False
        device.warnings.append(f"{ctx}: значение «{item}» в {side} не распознано")
        extra["unparsed"] = "; ".join(filter(None, [extra.get("unparsed"), f"{side}: {item}"]))

    if "any" in kinds and len(items) > 1:
        device.warnings.append(f"{ctx}: @any перечислен вместе с другими значениями в {side}")
    kind = kinds[0] if len(set(kinds)) == 1 else "mixed"
    if objects and len(items) > 1:
        extra[f"{side}_objects"] = ", ".join(objects)
    if fqdns:
        extra[f"{side}_fqdn"] = ", ".join(fqdns)
    if ifaces:
        extra[f"{side}_interface"] = ", ".join(ifaces)
    if nodes:
        extra[f"{side}_vpn_nodes"] = "; ".join(nodes)
    if node_nets:
        # Сети по узлам нужны поиску (acl_search), чтобы объяснить, через
        # какой именно узел адрес попал в правило; UI это поле не показывает.
        extra[f"{side}_node_nets"] = "; ".join(node_nets)
    return Endpoint(raw=", ".join(items), kind=kind, networks=_dedupe(nets),
                    object_group=objects[0] if objects else None,
                    object_groups=list(objects), resolved=resolved)


def _split_change(text: str):
    """«адреса Change: X [Reverse Change: Y]» → (адреса, текст трансляции)."""
    if "Change:" not in text:
        return text, None
    parts = _CHANGE_RE.split(text)
    head = parts[0].strip()
    changes = [p.strip() for p in parts[1:] if p.strip()]
    return head, "; ".join(changes) if changes else None


def _parse_block(block: list, section: _Section, resolver: _Resolver,
                 device: ParsedDevice):
    cols = section.cols
    title = section.title
    num_idx = next((i for i, l in enumerate(block) if _NUM_ROW_RE.match(l)), None)
    if num_idx is None:
        device.warnings.append(
            f"{title}: блок без строки Num не разобран: "
            + " | ".join(l.strip() for l in block if l.strip()))
        return None

    num, name_cell, option, schedule = _parse_num_row(block[num_idx], cols)
    body = block[num_idx + 1:]
    ctx = f"{title} #{num}"

    tok = cols.arrow_token
    arrow_idx = next((i for i, l in enumerate(body)
                      if l[cols.arrow:cols.arrow + len(tok)] == tok), None)
    if arrow_idx is None:
        # стрелка сдвинута вправо: имя объекта с кириллицей устройство
        # считает в байтах (см. _fix_mojibake)
        arrow_idx = next((i for i, l in enumerate(body) if l.find(tok, cols.src) > 0), None)
    act_idx = next((i for i, l in enumerate(body) if l[:1].strip()), None)

    action_raw = body[act_idx][:cols.proto].strip() if act_idx is not None else ""
    # хвост имени: строки между строкой действия (включительно) и стрелкой
    if act_idx is not None:
        end = arrow_idx if arrow_idx is not None else len(body)
        for l in body[act_idx:end]:
            rest = l[cols.proto:]
            if not rest.strip():
                continue
            segs = [s for s in re.split(r"\s{2,}", rest.strip()) if s]
            # устройство печатает хвост имени в колонке Source, а его
            # перенос — левее, в колонке Protocol: порядок восстанавливаем
            if len(segs) == 2 and rest[cols.src - cols.proto:].strip() and \
                    rest[:cols.src - cols.proto].strip():
                segs = [segs[1], segs[0]]
            name_cell += "".join(segs) if len(segs) > 1 else segs[0]

    m = _LABEL_RE.match(name_cell) if cols.has_rule_id else None
    if m:
        rule_id, inactive, name = m.group(1), bool(m.group(2)), m.group(3).strip()
        label = f"{rule_id} - {name}" if name else rule_id
    else:
        # координатор HW: ID нет, «(inactive)» — префикс перед именем
        rule_id = None
        inactive = name_cell.startswith("(inactive)")
        name = name_cell[len("(inactive)"):].strip() if inactive else name_cell.strip()
        label = name
        if cols.has_rule_id:
            device.warnings.append(f"{ctx}: ячейка «Rule ID-Name» не распознана: «{name_cell}»")
    if "�" in label:
        device.warnings.append(
            f"{ctx}: имя правила повреждено при снятии (не-ASCII символы заменены): «{label}»")

    action = _ACTIONS.get(action_raw)
    if action is None:
        device.warnings.append(f"{ctx} ({label}): действие «{action_raw}» не распознано")
        action = "deny" if not action_raw else action_raw

    extra: dict = {}
    if option:
        extra["option"] = option
    rule = AclRule(sequence=num, action=action, action_raw=action_raw or None,
                   label=label, rule_id=rule_id, name=name or None,
                   inactive=inactive, schedule=schedule,
                   raw="\n".join(l.rstrip() for l in block), extra=extra)

    if arrow_idx is None:
        device.warnings.append(f"{ctx} ({label}): строка «{tok}Destination» не найдена, "
                               "адреса не разобраны")
        return rule

    psd = body[arrow_idx:]
    dpi_line = None
    if cols.has_dpi and len(psd) > 1:
        last = psd[-1]
        pc = _cell(last, cols.proto, cols.src)
        if pc.strip() and pc[:1] != " " and not pc.rstrip().endswith((":", ",")):
            dpi_line = last
            psd = psd[:-1]
            # DomainUser, напечатанный строкой выше (см. докстринг модуля)
            if not _cell(last, cols.dst).strip() and len(psd) > 1:
                prev, before = psd[-1], psd[-2]
                only_dst = (not _cell(prev, cols.proto, cols.arrow).strip()
                            and _cell(prev, cols.dst).strip())
                bd = _cell(before, cols.dst).rstrip()
                bd_open = bd.endswith(("/", "-", ",")) or len(bd.strip()) >= cols.width - cols.dst
                if only_dst and bd.strip() and not bd_open:
                    dpi_line = last[:cols.dst].ljust(cols.dst) + _cell(prev, cols.dst).strip()
                    psd = psd[:-1]
        else:
            device.warnings.append(f"{ctx} ({label}): строка DPI не найдена")

    # у строки со стрелкой её фактическая позиция может отличаться от
    # заголовка (сдвиг байтами), у остальных строк колонки стандартные
    apos = psd[0].find(tok, cols.src)
    apos = apos if apos > 0 else cols.arrow
    src_cells = [_cell(psd[0], cols.src, apos)] + [_cell(l, cols.src, cols.arrow) for l in psd[1:]]
    dst_cells = [_cell(psd[0], apos + len(tok))] + [_cell(l, cols.dst) for l in psd[1:]]
    proto_text = _fix_mojibake(_join_column([_cell(l, cols.proto, cols.src) for l in psd],
                                            cols.src - cols.proto))
    src_text = _fix_mojibake(_join_column(src_cells, cols.arrow - cols.src))
    dst_text = _fix_mojibake(_join_column(dst_cells, cols.width - cols.dst))

    rule.protocol, rule.source_port, rule.destination_port = _parse_protocol(
        proto_text, device, f"{ctx} ({label})", extra)

    src_text, src_change = _split_change(src_text)
    dst_text, dst_change = _split_change(dst_text)
    if src_change:
        extra["nat_source_change"] = src_change
    if dst_change:
        extra["nat_destination_change"] = dst_change
    if (src_change and ";" in src_change) or (dst_change and ";" in dst_change):
        device.warnings.append(f"{ctx} ({label}): несколько трансляций Change: в одной колонке")

    rule.source = _build_endpoint(src_text, resolver, device, f"{ctx} ({label})", "source", extra)
    rule.destination = _build_endpoint(dst_text, resolver, device, f"{ctx} ({label})",
                                       "destination", extra)
    if rule.source is None or rule.destination is None:
        device.warnings.append(f"{ctx} ({label}): пустой Source или Destination")

    if dpi_line is not None:
        for key, value in (("dpi_protocol", _cell(dpi_line, cols.proto, cols.src)),
                           ("dpi_group", _cell(dpi_line, cols.src, cols.arrow)),
                           ("domain_user", _cell(dpi_line, cols.dst))):
            value = value.strip()
            if value and value != "@any":
                extra[key] = value
    return rule


def parse_rules(text: str, objects: FirewallObjects | None = None,
                device: ParsedDevice | None = None,
                sections=DEFAULT_SECTIONS, nodes: dict | None = None,
                resolver: "_Resolver | None" = None) -> list:
    """Разбирает ``firewall rules show`` в список Acl (по непустой секции).
    objects — результат parse_objects для разрешения ``@ссылок``; device —
    ParsedDevice, в который складываются предупреждения и неразрешённые
    имена (если не передан, создаётся временный); sections — какие секции
    брать (см. DEFAULT_SECTIONS), None — все."""
    if device is None:
        device = ParsedDevice(device_name="")
    if resolver is None:
        resolver = _Resolver(objects, device, nodes)
    wanted = set(sections) if sections is not None else None
    sections = _split_sections(_clean_lines(text))
    acls = []
    for section in sections:
        slug = _section_slug(section.title)
        if wanted is not None and slug not in wanted:
            continue
        acl = Acl(name=slug, acl_type=slug,
                  description=f"{section.title} Rules" + (
                      f", Rules Set Id {section.set_id}" if section.set_id else ""))
        for block in section.blocks:
            rule = _parse_block(block, section, resolver, device)
            if rule is not None:
                acl.rules.append(rule)
        if acl.rules:
            acls.append(acl)
    order = {name: i for i, name in enumerate(_ACL_ORDER)}
    acls.sort(key=lambda a: order.get(a.name, len(order)))
    return acls


# ── точка входа ─────────────────────────────────────────────────────────

def parse_snapshot(files: dict, device_name: str, sections=DEFAULT_SECTIONS) -> ParsedDevice:
    """files — {"firewall_rules": текст, "firewall_objects": текст,
    "iplir_config": текст}; любой может отсутствовать: без объектов правила
    разбираются, но ``@ссылки`` остаются неразрешёнными; без iplir узлы
    ViPNet (0x…) остаются без адресов. sections — см. parse_rules."""
    device = ParsedDevice(device_name=device_name, parser=PARSER_NAME)
    rules_text = files.get("firewall_rules") or ""
    objects_text = files.get("firewall_objects") or ""
    iplir_text = files.get("iplir_config") or ""
    nodes = parse_iplir(iplir_text) if iplir_text.strip() else None

    objects = None
    if objects_text.strip():
        objects = parse_objects(objects_text)
    else:
        device.warnings.append("Снимок firewall object show отсутствует: объекты не разрешены")
    resolver = _Resolver(objects, device, nodes)
    # Узлы iplir — на устройство: UI объясняет совпадения через туннелируемые
    # диапазоны координаторов и показывает их в панелях.
    if nodes:
        device.vpn_nodes = {
            nid: {"name": n.name, "network": n.network, "ips": list(n.ips),
                  "virtualip": n.virtualip, "tunnels": list(n.tunnels)}
            for nid, n in nodes.items()}
    if objects is not None:
        _fill_object_groups(objects, device, resolver)

    if rules_text.strip():
        device.acls = parse_rules(rules_text, objects, device, sections=sections,
                                  resolver=resolver)
        titles = {_section_slug(s.title): f"{s.title} Rules"
                  for s in _split_sections(_clean_lines(rules_text))}
        for acl in device.acls:
            device.applications.append(Application(
                acl_name=acl.name, kind="section", location=titles.get(acl.name, acl.name)))
    else:
        device.warnings.append("Снимок firewall rules show отсутствует")

    if objects is not None and rules_text.strip():
        rid, oid = rules_set_id(rules_text), objects.set_id
        if rid and oid and rid != oid:
            device.warnings.append(
                f"Rules Set Id снимков не совпадает: правила {rid}, объекты {oid}")
    if nodes is None and (resolver.node_refs_seen or (objects is not None and objects.vpn)):
        device.warnings.append("iplir_config отсутствует: узлы VPN-объектов не разрешены")
    return device
