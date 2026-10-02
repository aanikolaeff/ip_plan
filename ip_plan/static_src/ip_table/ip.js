// IPv4-арифметика: числовая сортировка адресов, принадлежность префиксу,
// свободные диапазоны и заполненность подсети.

export function ipToInt(address) {
  const parts = (address || '').split('.').map(Number);
  if (parts.length !== 4 || parts.some((p) => Number.isNaN(p) || p < 0 || p > 255)) return null;
  return parts.reduce((acc, p) => acc * 256 + p, 0);
}

export function ipIntToStr(intVal) {
  return [(intVal >>> 24) & 0xff, (intVal >>> 16) & 0xff, (intVal >>> 8) & 0xff, intVal & 0xff].join('.');
}

/** Адрес без маски: "10.0.0.5/24" → "10.0.0.5". */
export function stripMask(address) {
  return (address || '').split('/')[0];
}

export function maskOf(address) {
  const parts = (address || '').split('/');
  return parts.length > 1 ? parts[1] : '';
}

/** Разбор префикса "10.20.30.0/24" → границы и ёмкость. Для /31 и /32
 * network/broadcast не выделяются (RFC 3021), хостов — вся сеть; для /30
 * и короче — минус два адреса. null — если это не IPv4-префикс. */
export function parsePrefix(prefixStr) {
  if (!prefixStr || typeof prefixStr !== 'string') return null;
  const [netStr, maskStr] = prefixStr.split('/');
  const mask = Number(maskStr);
  const netInt = ipToInt(netStr);
  if (netInt === null || !Number.isInteger(mask) || mask < 0 || mask > 32) return null;
  const size = 2 ** (32 - mask);
  const shift = 32 - mask;
  const base = shift >= 32 ? 0 : ((netInt >>> shift) << shift) >>> 0;
  const hostOnly = mask >= 31;
  return {
    prefix: prefixStr,
    netInt: base,
    mask,
    size,
    firstHost: hostOnly ? base : base + 1,
    lastHost: hostOnly ? base + size - 1 : base + size - 2,
    hostCount: hostOnly ? size : Math.max(size - 2, 0),
  };
}

export function ipInPrefix(addrInt, parsed) {
  if (addrInt === null || !parsed) return false;
  return addrInt >= parsed.netInt && addrInt < parsed.netInt + parsed.size;
}

/** Свободные диапазоны между занятыми адресами (отсортированы по
 * возрастанию). Возвращает [{afterIpInt, start, end, count}] — start/end
 * как числа. includeEdges: добавить диапазон от начала подсети до первого
 * адреса и от последнего до конца (на первой/последней странице). */
export function computeGaps(sortedIpInts, parsed, { leading = true, trailing = true } = {}) {
  const gaps = [];
  if (!parsed) return gaps;
  const push = (start, end) => {
    if (end >= start) gaps.push({ afterIpInt: start - 1, start, end, count: end - start + 1 });
  };
  let prev = null;
  sortedIpInts.forEach((ipInt, idx) => {
    if (idx === 0 && leading) push(parsed.firstHost, ipInt - 1);
    else if (prev !== null) push(prev + 1, ipInt - 1);
    prev = ipInt;
  });
  if (trailing) {
    if (prev === null) push(parsed.firstHost, parsed.lastHost);
    else push(prev + 1, parsed.lastHost);
  }
  return gaps;
}

/** Первые N свободных адресов подсети (строками, без маски). */
export function firstFreeAddresses(sortedIpInts, parsed, limit = 8) {
  const out = [];
  for (const gap of computeGaps(sortedIpInts, parsed)) {
    for (let a = gap.start; a <= gap.end && out.length < limit; a += 1) out.push(ipIntToStr(a));
    if (out.length >= limit) break;
  }
  return out;
}
