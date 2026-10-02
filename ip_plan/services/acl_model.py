"""Общая модель списка доступа для всех вендорских парсеров (cisco_acl,
vipnet_firewall, huawei_acl, eltex_acl, maipu_acl).

Родилась из датаклассов cisco_acl.py, когда страница «ACL» перестала быть
только про Cisco: поиск «куда попадает IP» и обзорные таблицы работают с
этими структурами и не знают, с какого устройства и какой командой они
сняты. Всё вендорское, что не ложится в общие поля, парсер складывает в
AclRule.extra (словарь «подпись → текст») — UI показывает его как есть.

Словарь действий един: permit | deny | remark. Исходное слово вендора
(pass/drop/reject у ViPNet) хранится в action_raw только для отображения.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Endpoint:
    """Один операнд правила (source или destination).

    kind: any | host | wildcard | network | range | object-group | local | unresolved.
    'local' — адреса самого устройства (ViPNet @local): что это за адреса,
    из снимка не видно, поэтому resolved=False и в поиск по IP не попадает."""

    raw: str
    kind: str
    networks: list = field(default_factory=list)  # список ipaddress.IPv4Network
    object_group: str | None = None   # первый (или единственный) объект операнда
    # Все объекты операнда: у ViPNet в одном Source могут стоять «@Admins nets,
    # 10.1.141.166, …» — такой операнд получает kind «mixed», и считать его
    # использованием объекта можно только по этому списку, не по object_group.
    object_groups: list = field(default_factory=list)
    resolved: bool = True  # False — networks посчитать не смогли


@dataclass
class AclRule:
    sequence: int
    action: str  # permit | deny | remark
    protocol: str | None = None
    source: Endpoint | None = None
    destination: Endpoint | None = None
    source_port: str | None = None
    destination_port: str | None = None
    log: bool = False
    remark: str | None = None
    raw: str = ""
    # Ниже — поля, которых нет у Cisco IOS; парсеры других вендоров заполняют
    # их, UI показывает, если заполнены.
    label: str | None = None       # подпись целиком (ViPNet: «4000123 - BLOCK_OUTSIDE_IN_BY_LIST»)
    rule_id: str | None = None     # идентификатор правила у вендора (ViPNet Rule ID 4000123, Huawei rule N)
    name: str | None = None        # имя правила (ViPNet BLOCK_OUTSIDE_IN_BY_LIST, Huawei rule … name X)
    action_raw: str | None = None  # слово вендора: pass/drop/reject
    inactive: bool = False         # правило выключено (ViPNet «(inactive)»)
    schedule: str | None = None    # расписание действия правила
    extra: dict = field(default_factory=dict)  # прочие колонки: DPI, DomainUser, Option…


@dataclass
class Acl:
    """Список правил. acl_type — тип у Cisco (standard/extended) либо секция
    у межсетевого экрана (forward/local/nat/service-local/service-vpn/vpn):
    в обоих случаях это то, что UI показывает бейджем рядом с именем."""

    name: str
    acl_type: str
    rules: list = field(default_factory=list)
    description: str | None = None


@dataclass
class Application:
    """Где список применён. kind: interface | line | snmp | nat | section.
    'section' — у межсетевых экранов список применён самим фактом
    существования секции (Forward Rules действуют на весь транзит)."""

    acl_name: str
    kind: str
    location: str
    direction: str | None = None


@dataclass
class ParsedDevice:
    device_name: str
    acls: list = field(default_factory=list)
    object_groups: dict = field(default_factory=dict)  # имя -> [IPv4Network] (включения)
    applications: list = field(default_factory=list)
    unresolved_object_groups: set = field(default_factory=set)
    parser: str | None = None
    # Исключения из объектов (ViPNet Exclusion) — отдельно, чтобы не путать с
    # включениями: при поиске по IP попадание в исключение снимает совпадение.
    object_group_exclusions: dict = field(default_factory=dict)  # имя -> [IPv4Network]
    # Вложенность объектов: родитель -> [имена объектов среди его включений].
    object_group_includes: dict = field(default_factory=dict)
    # Состав объекта «как есть», для показа: имя -> [строки]. У VPN-объектов
    # ViPNet это узлы («0x4f260038 i.perevalov (7.0.0.45, 11.0.0.6)»), у
    # прочих может быть пусто — тогда UI показывает только networks.
    object_group_members: dict = field(default_factory=dict)
    # Сервисные объекты межсетевого экрана: имя -> текст («tcp: to 443»).
    service_objects: dict = field(default_factory=dict)
    # Узлы ViPNet из iplir show config: id (нижний регистр) -> {name, network,
    # ips [(real, virt)], virtualip, tunnels [строки tunnel=]}. Нужны UI, чтобы
    # объяснить совпадение через туннелируемый диапазон координатора.
    vpn_nodes: dict = field(default_factory=dict)
    # Что парсер не смог разобрать — UI показывает, чтобы молчаливых
    # пропусков не было («лишний кабель хуже отсутствующего» и здесь).
    warnings: list = field(default_factory=list)

    def acl_by_name(self, name: str):
        for acl in self.acls:
            if acl.name == name:
                return acl
        return None

    def is_applied(self, acl_name: str) -> bool:
        return any(app.acl_name == acl_name for app in self.applications)
