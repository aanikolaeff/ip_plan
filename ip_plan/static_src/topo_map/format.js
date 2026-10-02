// Плюрализация, даты, порты — всё, что превращает данные в русский текст.

export function plural(n, forms) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b === 1) return forms[0];
  if (b >= 2 && b <= 4) return forms[1];
  return forms[2];
}
export const W = {
  device: ['устройство', 'устройства', 'устройств'],
  link: ['связь', 'связи', 'связей'],
  cable: ['кабель', 'кабеля', 'кабелей'],
  host: ['хост', 'хоста', 'хостов'],
  run: ['прогон', 'прогона', 'прогонов'],
  item: ['запись', 'записи', 'записей'],
};
export const pl = (n, key) => `${n} ${plural(n, W[key])}`;

const pad = (n) => String(n).padStart(2, '0');

function dayDiff(d, now = new Date()) {
  const a = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((b - a) / 86400000);
}

function parse(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

// «сегодня в 03:14» / «вчера в 03:14» / «01.10 в 03:14» / «01.10.2025 в 03:14»
export function formatWhen(iso) {
  const d = parse(iso);
  if (!d) return '';
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const diff = dayDiff(d);
  if (diff === 0) return `сегодня в ${time}`;
  if (diff === 1) return `вчера в ${time}`;
  const now = new Date();
  const date = d.getFullYear() === now.getFullYear()
    ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`
    : `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
  return `${date} в ${time}`;
}

export function formatDate(iso) {
  const d = parse(iso);
  if (!d) return '—';
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
}

export function todayStamp() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Естественная сортировка имён портов/устройств: te1/0/2 < te1/0/10.
const collator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' });
export const natCmp = (a, b) => collator.compare(String(a ?? ''), String(b ?? ''));

// ['te1/0/21','te1/0/22'] → 'te1/0/21–22'; разнородные — через запятую.
export function compressPorts(ports) {
  if (!ports || !ports.length) return '';
  const list = [...ports].sort(natCmp);
  const out = [];
  let i = 0;
  while (i < list.length) {
    const m = /^(.*?)(\d+)$/.exec(list[i]);
    if (!m) { out.push(list[i]); i += 1; continue; }
    const prefix = m[1];
    const start = Number(m[2]);
    let end = start;
    let j = i + 1;
    while (j < list.length) {
      const n = /^(.*?)(\d+)$/.exec(list[j]);
      if (!n || n[1] !== prefix || Number(n[2]) !== end + 1) break;
      end += 1;
      j += 1;
    }
    out.push(end > start ? `${prefix}${start}–${end}` : list[i]);
    i = j;
  }
  return out.join(', ');
}

// Конец связи одной строкой: «Po1 · te1/0/21–22» / «te1/0/48».
export function endText(end) {
  if (!end) return '';
  const ports = compressPorts(end.ports);
  if (end.lag) return ports ? `${end.lag} · ${ports}` : end.lag;
  return ports || '—';
}
export function endShort(end) {
  if (!end) return '';
  return end.lag || compressPorts(end.ports) || '—';
}
