// Справочники острова: статусы (порядок, русские подписи-запас, цвета) и
// описание колонок (подписи, ширины, что скрыто по умолчанию).

// Подписи приходят с сервера (status_choices / row.status.label) — это
// лишь запас на случай, если сервер отдаст только value.
export const STATUS_FALLBACK_LABELS = {
  active: 'Активен',
  reserved: 'Резерв',
  deprecated: 'Устарел',
  dhcp: 'DHCP',
  slaac: 'SLAAC',
};

// Вкладки полосы статусов — множественное число, своё, не из choices.
export const STATUS_TABS = [
  { value: 'active', label: 'Активные' },
  { value: 'dhcp', label: 'DHCP' },
  { value: 'reserved', label: 'Резерв' },
  { value: 'deprecated', label: 'Устаревшие' },
  { value: 'slaac', label: 'SLAAC' },
];

// Цвет статуса — токен из style.css.
export const STATUS_COLOR_VAR = {
  active: 'var(--ok)',
  reserved: 'var(--warn)',
  deprecated: 'var(--off)',
  dhcp: 'var(--info)',
  slaac: 'var(--accent)',
};

export const STATUS_ORDER = ['active', 'dhcp', 'reserved', 'deprecated', 'slaac'];

export function statusLabel(status, choices) {
  if (!status) return '—';
  if (typeof status === 'string') {
    const found = (choices || []).find(([v]) => v === status);
    return (found && found[1]) || STATUS_FALLBACK_LABELS[status] || status;
  }
  if (status.label) return status.label;
  return statusLabel(status.value, choices);
}

export const DENSITIES = [
  { value: 'compact', label: 'Плотно', gap: 2 },
  { value: 'normal', label: 'Обычно', gap: 4 },
  { value: 'relaxed', label: 'Свободно', gap: 6 },
];

// Колонки, переключаемые в меню «Вид» (без «Адреса» — он всегда и первый,
// и без служебных select/actions). Порядок массива — порядок по умолчанию.
// width — доля (fr) при широком экране ≈ ширина в px из макета; min —
// нижняя граница, чтобы при узком контенте колонки ужимались
// пропорционально, а не выталкивали таблицу в горизонтальный скролл.
// widthSite/minSite — вариант при показанной колонке «Площадка».
export const COLUMN_DEFS = [
  { id: 'status', label: 'Статус', width: 120, min: 104, widthSite: 108, minSite: 100, fixed: true },
  { id: 'site', label: 'Площадка', width: 138, min: 96 },
  { id: 'dns_name', label: 'DNS-имя', width: 212, min: 120, widthSite: 196 },
  { id: 'description', label: 'Описание', width: 260, min: 120, flex: true },
  { id: 'tags', label: 'Тэги', width: 150, min: 90, widthSite: 128 },
  { id: 'os_installed', label: 'Операционная система', short: 'ОС', width: 170, min: 100, widthSite: 158 },
  { id: 'open_ports', label: 'Открытые порты', short: 'Порты', width: 118, min: 84, widthSite: 104, scan: true },
  { id: 'role', label: 'Роль', width: 110, min: 80 },
  { id: 'purpose', label: 'Назначение', width: 160, min: 100 },
  { id: 'assignment', label: 'Принадлежность', width: 180, min: 110 },
  { id: 'dns_records', label: 'DNS-записи', width: 170, min: 110 },
  { id: 'comments', label: 'Комментарии', width: 180, min: 110 },
  { id: 'guest_os', label: 'Guest OS', width: 150, min: 100, scan: true },
  { id: 'os_guess', label: 'OS Guess', width: 180, min: 110, scan: true },
  { id: 'cpe', label: 'CPE', width: 150, min: 100, scan: true },
];

export const DEFAULT_VISIBLE = ['status', 'site', 'dns_name', 'description', 'tags', 'os_installed', 'open_ports'];

export const DEFAULT_COLUMN_ORDER = COLUMN_DEFS.map((c) => c.id);

export function defaultVisibility() {
  const vis = {};
  COLUMN_DEFS.forEach((c) => { vis[c.id] = DEFAULT_VISIBLE.includes(c.id); });
  return vis;
}

export const DEFAULT_PAGE_SIZE = 50;

export function pluralize(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

export const fmtNum = (n) => new Intl.NumberFormat('ru-RU').format(n);
