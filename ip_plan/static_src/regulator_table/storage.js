// Копия ip_table/storage.js с собственным префиксом ключей — это ОТДЕЛЬНЫЙ
// React-island (своя таблица, свои колонки), общий префикс с ip_table
// столкнул бы, например, columnSizing одной таблицы с другой (у них разные
// наборы колонок, но одинаковые ключи 'columnSizing' и т.п.).
const PREFIX = 'regulator_table_react_v1_';

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

export function loadSession(key, fallback) {
  try {
    const raw = sessionStorage.getItem(PREFIX + key);
    if (raw === null) return fallback;
    return JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
}

export function saveSession(key, value) {
  try {
    sessionStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch (e) { /* см. выше */ }
}
