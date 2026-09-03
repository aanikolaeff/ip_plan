// CSRF/fetch-обвязка — почти дословный перенос из ip_plan/static/ip_plan/js/app.js
// (getCookie/csrfTokenLooksValid/refreshCsrfToken/fetchWithCsrf/patchJson),
// см. docs/plans/2026-09-03-ip-table-react-design.md. window.URL_PREFIX и
// window.CSRF_COOKIE_NAME по-прежнему выставляются в base.html — эта часть
// инфраструктуры общая с HTMX-модалками и не менялась.

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

// Запрос с автоматическим обновлением CSRF-токена: если cookie выглядит
// протухшей ИЛИ сервер ответил 403 — обновляем токен и пробуем ещё раз,
// прозрачно для пользователя, без перезагрузки страницы.
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

export function patchIp(id, field, value) {
  return fetchWithCsrf(`${URL_PREFIX}/ip/${id}/patch/`, 'PATCH', { [field]: value });
}

export function deleteIp(id) {
  return fetchWithCsrf(`${URL_PREFIX}/ip/${id}/delete/`, 'DELETE');
}

const NETWORK_ACTION_PATHS = {
  check: 'check',
  'scan-ports': 'scan-ports',
  'resolve-dns': 'resolve-dns',
};

export function runNetworkAction(action, id) {
  return fetchWithCsrf(`${URL_PREFIX}/ip/${id}/${NETWORK_ACTION_PATHS[action]}/`, 'POST');
}

export async function fetchIpList() {
  const response = await fetch(`${URL_PREFIX}/ip/list.json`, { credentials: 'same-origin' });
  if (!response.ok) {
    let message = `Не удалось загрузить список (HTTP ${response.status})`;
    try {
      message = (await response.json()).error || message;
    } catch (e) { /* ответ без тела */ }
    throw new Error(message);
  }
  return response.json();
}
