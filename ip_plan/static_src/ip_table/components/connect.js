// Подключение к хосту из таблицы — локальными клиентами пользователя, без
// шлюза и без хранения учёток на сервере:
//  - SSH и VNC — URL-схемы ssh:// и vnc://. macOS открывает Terminal и
//    «Общий экран» сам; на Windows/Linux нужен зарегистрированный обработчик
//    (PuTTY/KiTTY/Windows Terminal, RealVNC/TigerVNC, Remmina).
//  - RDP — на macOS схема rdp:// приложения Windows App (бывший Microsoft
//    Remote Desktop; после «=» двоеточие экранируется %3A), иначе файл .rdp,
//    который открывают mstsc, Windows App и Remmina.
//  - Консоль ВМ — сервер (/ip/<id>/console/) спрашивает гипервизор: RedVirt/
//    oVirt отдаёт файл .vv с билетом для remote-viewer (SPICE либо VNC —
//    что настроено у машины), vCenter — ссылку vmrc:// для VMware Remote
//    Console (вход под своей учёткой).
// Имена пользователей для SSH/RDP — настройка браузера (меню «Вид»).
//
// Действия web-* — то же самое в браузере, через шлюз Guacamole (если он
// настроен, см. services/remote_gateway.py): сервер (/ip/<id>/connect/)
// выпускает токен на одно соединение и отдаёт адрес, сессия открывается в
// новой вкладке. Логин и пароль спрашивает сам шлюз.
import { fetchWithCsrf } from '../api.js';
import { showToast } from './cells.jsx';

const URL_PREFIX = window.URL_PREFIX || '';

export const CONNECT_DEFAULTS = { sshUser: '', rdpUser: '' };

/** Пункты «Подключиться» в меню строки. hint — что показываем в колонке
 * горячей клавиши: порт по умолчанию или тип файла. */
export const CONNECT_ACTIONS = [
  'ssh', 'vnc', 'rdp', 'console', 'web-ssh', 'web-ssh-key', 'web-vnc', 'web-rdp', 'web-console',
];

const GATEWAY_TITLES = { console: 'Консоль ВМ', 'ssh-key': 'SSH по ключу' };

export function isMac() {
  const p = navigator.userAgentData?.platform || navigator.platform || '';
  return /mac/i.test(p);
}

function openUrl(url) {
  // Переход по кастомной схеме: страница остаётся, браузер зовёт обработчик
  // (или спрашивает, чем открыть). Если обработчика нет — молча ничего не
  // произойдёт, поэтому рядом всегда есть тост с подсказкой.
  window.location.assign(url);
}

export function downloadFile(filename, content, mime = 'application/octet-stream') {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** Содержимое .rdp-файла: минимум, который понимают mstsc, Windows App и
 * Remmina. Учётные данные не кладём — клиент спросит сам. */
export function rdpFileContent(host, user) {
  const lines = [
    `full address:s:${host}`,
    'prompt for credentials:i:1',
    'screen mode id:i:2',
    'use multimon:i:0',
    'authentication level:i:2',
  ];
  if (user) lines.push(`username:s:${user}`);
  return `${lines.join('\r\n')}\r\n`;
}

export function sshUrl(host, user) {
  return `ssh://${user ? `${encodeURIComponent(user)}@` : ''}${host}`;
}

export function vncUrl(host) {
  return `vnc://${host}`;
}

/** rdp://-ссылка для Windows App на macOS: атрибуты .rdp через «&», двоеточие
 * после «=» — %3A, иначе новое приложение не разбирает адрес. */
export function rdpUrl(host, user) {
  let url = `rdp://full%20address=s%3A${encodeURIComponent(host)}`;
  if (user) url += `&username=s%3A${encodeURIComponent(user)}`;
  return url;
}

function safeName(s) {
  return (s || 'host').replace(/[^\w.-]+/g, '_');
}

/** Сессия в браузере через шлюз. Вкладку открываем сразу, ещё в обработчике
 * клика: после await браузер счёл бы window.open всплывающим окном и
 * заблокировал. Адрес в неё подставляем, когда сервер выпустит токен. */
async function openGatewaySession(proto, row, host, prefs) {
  const tab = window.open('', '_blank');
  if (tab) {
    tab.opener = null;
    tab.document.title = `${GATEWAY_TITLES[proto] || proto.toUpperCase()} → ${host}`;
    tab.document.body.textContent = 'Открываю сессию…';
  }
  const username = { ssh: prefs.sshUser, rdp: prefs.rdpUser }[proto] || '';
  let response;
  try {
    response = await fetchWithCsrf(`${URL_PREFIX}/ip/${row.id}/connect/`, 'POST', { proto, username });
  } catch (e) {
    tab?.close();
    showToast('Сервер недоступен', 'error');
    return;
  }
  let data = {};
  try { data = await response.json(); } catch (e) { /* без тела */ }
  if (!response.ok || !data.url) {
    tab?.close();
    showToast(data.error || `Шлюз недоступен (HTTP ${response.status})`, 'error');
    return;
  }
  if (tab) {
    tab.location.replace(data.url);
    showToast(data.message || 'Сессия открыта в новой вкладке', 'success');
  } else {
    showToast('Браузер не дал открыть вкладку — разрешите всплывающие окна для этого сайта', 'error');
  }
}

/** Открыть подключение к строке таблицы. prefs — {sshUser, rdpUser}. */
export async function openConnection(kind, row, prefs = CONNECT_DEFAULTS) {
  const host = (row.address || '').split('/')[0];
  if (!host) return;
  if (kind.startsWith('web-')) {
    await openGatewaySession(kind.slice(4), row, host, prefs);
    return;
  }
  if (kind === 'ssh') {
    openUrl(sshUrl(host, prefs.sshUser));
    showToast(`SSH → ${host}: открываю терминал${prefs.sshUser ? ` (${prefs.sshUser})` : ''}`, 'info');
    return;
  }
  if (kind === 'vnc') {
    openUrl(vncUrl(host));
    showToast(`VNC → ${host}: открываю клиент экрана`, 'info');
    return;
  }
  if (kind === 'rdp') {
    if (isMac()) {
      openUrl(rdpUrl(host, prefs.rdpUser));
      showToast(`RDP → ${host}: открываю Windows App`, 'info');
    } else {
      downloadFile(`${safeName(row.dns_name || host)}.rdp`, rdpFileContent(host, prefs.rdpUser), 'application/x-rdp');
      showToast(`RDP → ${host}: файл .rdp скачан, откройте его клиентом удалённого рабочего стола`, 'info');
    }
    return;
  }
  if (kind === 'console') {
    showToast(`Запрашиваю консоль ВМ у гипервизора для ${host}…`, 'info');
    let response;
    try {
      response = await fetchWithCsrf(`${URL_PREFIX}/ip/${row.id}/console/`, 'POST');
    } catch (e) {
      showToast('Сервер недоступен', 'error');
      return;
    }
    let data = {};
    try { data = await response.json(); } catch (e) { /* без тела */ }
    if (!response.ok) {
      showToast(data.error || `Консоль недоступна (HTTP ${response.status})`, 'error');
      return;
    }
    if (data.kind === 'file') {
      downloadFile(data.filename, data.content, data.mime || 'application/x-virt-viewer');
    } else if (data.kind === 'url') {
      openUrl(data.url);
    }
    showToast(data.message || 'Консоль открыта', 'success');
  }
}
