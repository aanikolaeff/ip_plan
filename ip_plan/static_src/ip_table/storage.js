// Персистентность настроек вида в localStorage. Новый префикс ключей
// (v2) — набор и смысл сохранённого изменились вместе с макетом
// (плотность, порядок/видимость колонок, свободные диапазоны, размер
// страницы), миграция со старых ключей ip_table_react_v1_* не нужна:
// там хранились ширины колонок и видимость под другой состав колонок.
// Фильтры и поиск сознательно не сохраняются.
const PREFIX = 'ip_table_v2_';

export const STORAGE_KEYS = ['density', 'columnVisibility', 'columnOrder', 'showGaps', 'pageSize'];

export function loadJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (raw === null) return fallback;
    return JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
}

export function saveJSON(key, value) {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch (e) {
    /* localStorage недоступен (приватный режим и т.п.) — просто не сохраняем */
  }
}

export function removeKey(key) {
  try {
    localStorage.removeItem(PREFIX + key);
  } catch (e) { /* см. выше */ }
}
