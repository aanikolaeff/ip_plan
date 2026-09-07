// CSRF/fetch-обвязка — копия ip_table/api.js (см. комментарий там же и в
// storage.js про причину дублирования вместо общего модуля: два независимых
// React-island'а, каждый сам себе бандл).

const URL_PREFIX = window.URL_PREFIX || '';
const CSRF_COOKIE_NAME = window.CSRF_COOKIE_NAME || 'csrftoken';

function getCookie(name) {
  const match = document.cookie.match(new RegExp('(^|; )' + name + '=([^;]*)'));
  return match ? decodeURIComponent(match[2]) : null;
}

function csrfTokenLooksValid(token) {
  return !!token && (token.length === 32 || token.length === 64);
}

async function refreshCsrfToken() {
  try {
    await fetch(URL_PREFIX + '/csrf/', { credentials: 'same-origin', cache: 'no-store' });
  } catch (e) { /* сети нет — попробуем по следующему тику/событию */ }
  return getCookie(CSRF_COOKIE_NAME);
}

const CSRF_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
setInterval(refreshCsrfToken, CSRF_REFRESH_INTERVAL_MS);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refreshCsrfToken();
});

export async function fetchWithCsrf(url, method, body) {
  const buildOptions = (csrfToken) => {
    const options = { method, headers: { 'X-CSRFToken': csrfToken } };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    return options;
  };

  let csrfToken = getCookie(CSRF_COOKIE_NAME);
  if (!csrfTokenLooksValid(csrfToken)) {
    csrfToken = await refreshCsrfToken();
  }
  let response = await fetch(url, buildOptions(csrfToken));
  if (response.status === 403) {
    csrfToken = await refreshCsrfToken();
    response = await fetch(url, buildOptions(csrfToken));
  }
  return response;
}

export async function fetchRegulatorEntries() {
  const response = await fetch(`${URL_PREFIX}/regulator/entries.json`, { credentials: 'same-origin' });
  if (!response.ok) {
    let message = `Не удалось загрузить список записей (HTTP ${response.status})`;
    try {
      message = (await response.json()).error || message;
    } catch (e) { /* ответ без тела */ }
    throw new Error(message);
  }
  return response.json();
}

export function reactivateEntry(id) {
  return fetchWithCsrf(`${URL_PREFIX}/regulator/entry/${id}/reactivate.json`, 'POST');
}

export function deleteEntry(id) {
  return fetchWithCsrf(`${URL_PREFIX}/regulator/entry/${id}/delete.json`, 'POST');
}
