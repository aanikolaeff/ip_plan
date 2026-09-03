// Персистентность состояния колонок в localStorage — те же принципы, что
// были в app.js (COLUMN_VISIBILITY_STORAGE_KEY и т.д.): версия в ключе
// растёт при изменении набора/смысла колонок, чтобы устаревшая запись не
// применилась к другой структуре (см. историю версий v1..v6 в app.js).
const PREFIX = 'ip_table_react_v1_';

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
