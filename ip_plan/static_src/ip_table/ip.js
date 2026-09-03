// IPv4-арифметика, общая для columns.jsx (числовая сортировка адреса) и
// TableRoot.jsx (гэп-строки свободных диапазонов, префикс-фильтр).

export function ipToInt(address) {
  const parts = (address || '').split('.').map(Number);
  if (parts.length !== 4 || parts.some((p) => Number.isNaN(p) || p < 0 || p > 255)) return null;
  return parts.reduce((acc, p) => acc * 256 + p, 0);
}

export function ipIntToStr(intVal) {
  return [(intVal >>> 24) & 0xff, (intVal >>> 16) & 0xff, (intVal >>> 8) & 0xff, intVal & 0xff].join('.');
}
