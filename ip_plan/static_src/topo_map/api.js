// Обмен с сервером: /map/data.json (граф) и /map/layout.json (раскладка
// пользователя). CSRF-токен — из hx-headers на <body> (base.html кладёт туда
// {"X-CSRFToken": "..."}), запасной вариант — cookie (имя может быть
// переопределено, window.CSRF_COOKIE_NAME).

const URL_PREFIX = window.URL_PREFIX || '';
const CSRF_COOKIE_NAME = window.CSRF_COOKIE_NAME || 'csrftoken';

function getCookie(name) {
  const match = document.cookie.match(new RegExp('(^|; )' + name + '=([^;]*)'));
  return match ? decodeURIComponent(match[2]) : null;
}

function csrfToken() {
  try {
    const raw = document.querySelector('body')?.getAttribute('hx-headers');
    const token = raw ? JSON.parse(raw)['X-CSRFToken'] : null;
    if (token) return token;
  } catch (e) { /* битый JSON — берём cookie */ }
  return getCookie(CSRF_COOKIE_NAME);
}

async function refreshCsrf() {
  try {
    await fetch(`${URL_PREFIX}/csrf/`, { credentials: 'same-origin', cache: 'no-store' });
  } catch (e) { /* сети нет */ }
  return getCookie(CSRF_COOKIE_NAME);
}

async function send(url, method, body, keepalive = false) {
  const build = (token) => {
    const opts = { method, credentials: 'same-origin', keepalive, headers: { 'X-CSRFToken': token || '' } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return opts;
  };
  let res = await fetch(url, build(csrfToken()));
  if (res.status === 403) {
    // токен в hx-headers мог устареть (страница открыта давно) — берём свежий из cookie
    res = await fetch(url, build(await refreshCsrf()));
  }
  return res;
}

const withSite = (url, site, extra = '') => {
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}site=${encodeURIComponent(site)}${extra}`;
};

async function readError(res, fallback) {
  try {
    const body = await res.json();
    if (body && body.error) return body.error;
  } catch (e) { /* без тела */ }
  return fallback;
}

export async function fetchMapData(dataUrl, site) {
  let res;
  try {
    res = await fetch(withSite(dataUrl, site), { credentials: 'same-origin', cache: 'no-store' });
  } catch (e) {
    throw new Error('Сервер не отвечает — проверьте сеть и повторите.');
  }
  if (!res.ok) throw new Error(await readError(res, `Не удалось загрузить карту (HTTP ${res.status})`));
  return res.json();
}

export async function fetchLayout(layoutUrl, site) {
  try {
    const res = await fetch(withSite(layoutUrl, site, '&layer=l2'), { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) return {};
    return await res.json();
  } catch (e) {
    return {}; // без сохранённой раскладки карта всё равно работает
  }
}

export async function putLayout(layoutUrl, site, payload, keepalive = false) {
  const res = await send(withSite(layoutUrl, site, '&layer=l2'), 'PUT', payload, keepalive);
  if (!res.ok) throw new Error(await readError(res, `HTTP ${res.status}`));
  return res.json();
}

export async function deleteLayout(layoutUrl, site) {
  const res = await send(withSite(layoutUrl, site, '&layer=l2'), 'DELETE');
  if (!res.ok) throw new Error(await readError(res, `HTTP ${res.status}`));
  return res.json();
}
