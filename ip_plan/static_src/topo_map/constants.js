// Справочники карты: семейства, виды оборудования, стили линий по источнику,
// порядок состояний. Цвета семейств — CSS-переменные из topo.css (там же
// тёмная тема), здесь только ключи и подписи.

export const FAMILY_ORDER = ['net', 'sec', 'cmp', 'oth'];
export const FAMILIES = {
  net: { label: 'Сеть' },
  sec: { label: 'Защита' },
  cmp: { label: 'Вычисления' },
  oth: { label: 'Прочее' },
};

export const KIND_ORDER = [
  'router', 'core', 'access', 'chassis', 'ap',
  'firewall', 'crypto', 'tls',
  'hypervisor', 'server', 'vm', 'storage',
  'workstation', 'cloud', 'power', 'unknown',
];
export const KINDS = {
  router: { label: 'Маршрутизатор', family: 'net' },
  core: { label: 'Коммутатор ядра', family: 'net' },
  access: { label: 'Коммутатор доступа', family: 'net' },
  chassis: { label: 'Шасси, блейд', family: 'net' },
  ap: { label: 'Точка доступа Wi‑Fi', family: 'net' },
  firewall: { label: 'Межсетевой экран', family: 'sec' },
  crypto: { label: 'Криптошлюз', family: 'sec' },
  tls: { label: 'Шлюз доступа TLS', family: 'sec' },
  hypervisor: { label: 'Гипервизор', family: 'cmp' },
  server: { label: 'Сервер', family: 'cmp' },
  vm: { label: 'Виртуальная машина', family: 'cmp' },
  storage: { label: 'СХД', family: 'cmp' },
  workstation: { label: 'Рабочее место', family: 'oth' },
  cloud: { label: 'Провайдер, внешняя сеть', family: 'oth' },
  power: { label: 'ИБП, PDU', family: 'oth' },
  unknown: { label: 'Роль не задана', family: 'oth' },
};
export const kindOf = (k) => (KINDS[k] ? k : 'unknown');
export const familyOf = (fam, kind) => (FAMILIES[fam] ? fam : KINDS[kindOf(kind)].family);

// Штрих и толщина линии по источнику доказательства (легенда макета TopoIcons
// + два своих: macarp — длинный штрих с точкой, manual — серая тонкая).
export const SOURCE_STYLE = {
  lldp: { dash: '', w: 1.5 },
  lacp: { dash: '', w: 2.6 },
  hv: { dash: '6 4', w: 1.5 },
  mac: { dash: '0.5 4', w: 2 },
  arp: { dash: '8 3 1.5 3', w: 1.5 },
  macarp: { dash: '14 4 0.5 4', w: 1.8 },
  manual: { dash: '', w: 1.1, manual: true },
};
export const sourceStyle = (key) => SOURCE_STYLE[key] || { dash: '2 3', w: 1.4 };

// Запасные подписи, если бэкенд почему-то не прислал sources[].
export const SOURCE_FALLBACK = [
  { key: 'lldp', label: 'LLDP', hint: 'сосед назвал себя сам' },
  { key: 'lacp', label: 'LACP', hint: 'порт в агрегате, партнёр и номер порта' },
  { key: 'hv', label: 'С гипервизора', hint: 'ESXi сам слышит соседа' },
  { key: 'mac', label: 'MAC-таблица', hint: 'LLDP на порту нет' },
  { key: 'arp', label: 'ARP', hint: 'криптошлюзы, стыки точка-точка' },
  { key: 'macarp', label: 'MAC-таблица + ARP', hint: 'MAC за портом, IP по ARP ядра' },
  { key: 'manual', label: 'Вручную', hint: 'кабель заведён не синком' },
];

// Чем больше — тем хуже; у связки (свёрнутая группа) берётся худшее.
export const STATE_RANK = { confirmed: 0, partial: 1, unconfirmed: 2, conflict: 3 };
export const worstState = (states) => states.reduce(
  (acc, s) => ((STATE_RANK[s] ?? 0) > (STATE_RANK[acc] ?? 0) ? s : acc), 'confirmed');

export const HEALTH = {
  ok: { label: 'Работает', color: 'var(--ok)' },
  stale: { label: 'Данные устарели', color: 'var(--warn)' },
  down: { label: 'Не отвечает', color: 'var(--off)' },
};

// Геометрия логического слоя (px в координатах полотна).
export const DEV_W = 140;   // ширина узла (плитка по центру + подписи)
export const DEV_H = 86;    // высота узла: плитка 44 + имя + адрес
export const TILE = 44;
export const GAP_X = 20;    // между узлами в полосе
export const ROW_GAP = 18;  // между рядами внутри полосы
export const BAND_PAD_TOP = 34;
export const BAND_PAD_BOTTOM = 18;
export const MAX_ROW_UNITS = 7; // шире — ряд переносится (читаемость при «Показать всё»)
export const GROUP_HEAD = 40;
export const GROUP_PAD = 16;
export const MEMBER_GAP_X = 16;
export const MEMBER_GAP_Y = 14;

// Физический слой.
export const CARD_W = 228;
export const CARD_HEAD = 36;
export const CARD_PAD = 4;
export const PORT_ROW = 22;
export const LAG_ROW = 18;
export const PHYS_SCALE_X = (CARD_W + 60) / (DEV_W + GAP_X);
