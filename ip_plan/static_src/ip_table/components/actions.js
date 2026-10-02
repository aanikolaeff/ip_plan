// Сетевые действия над адресом (доступность / порты / DNS) и удаление —
// общая логика для кебаб-меню одной строки и массовых действий из
// плавающей панели выделения. Контракты эндпоинтов — см. api.js.
//
// interactive=true (одна строка): как раньше — тосты по ходу и
// window.confirm на предложения сервера (сменить статус на reserved,
// заменить DNS-имя). interactive=false (массово): никаких диалогов, лишь
// результат для сводного тоста; предложения сервера пропускаются.
import { deleteIp, patchIp, runNetworkAction } from '../api.js';
import { STATUS_FALLBACK_LABELS } from '../constants.js';
import { showToast } from './cells.jsx';

export const ACTION_START = {
  check: (addr) => `Проверка доступности ${addr}…`,
  'scan-ports': (addr) => `Сканирование портов ${addr} (1–1000)…`,
  'resolve-dns': (addr) => `Резолв DNS для ${addr}…`,
};

function statusObj(value, labelOf) {
  return { value, label: (labelOf && labelOf(value)) || STATUS_FALLBACK_LABELS[value] || value };
}

/** Выполнить одно сетевое действие над строкой. Возвращает объект
 * результата (для сводки): {ok, alive?, ports?, dnsAction?, error?}. */
export async function performNetworkAction(action, row, { updateRow, interactive = true, labelOf } = {}) {
  const { id } = row;
  const address = (row.address || '').split('/')[0];
  if (interactive) showToast(ACTION_START[action](address), 'info');

  let response;
  try {
    response = await runNetworkAction(action, id);
  } catch (e) {
    if (interactive) showToast('NetBox недоступен', 'error');
    return { ok: false, error: 'NetBox недоступен' };
  }
  let data = {};
  try { data = await response.json(); } catch (e) { /* ответ без тела */ }
  if (!response.ok) {
    if (interactive) showToast(data.error || 'Ошибка операции', 'error');
    return { ok: false, error: data.error || `HTTP ${response.status}` };
  }

  if (action === 'check') {
    if (data.status_changed) updateRow?.(id, { status: statusObj('active', labelOf) });
    if (interactive) {
      const latency = data.latency_ms != null ? ` (${data.latency_ms} мс)` : '';
      const suffix = data.status_changed ? ` — статус → ${statusObj('active', labelOf).label}` : '';
      showToast(`${address}: ${data.alive ? 'доступен' + latency : 'не отвечает'}${suffix}`, data.alive ? 'success' : 'error');
      if (data.suggest_status) {
        const target = statusObj(data.suggest_status, labelOf);
        const ok = window.confirm(`${address} не отвечает на пинг, хотя статус сейчас «${statusObj('active', labelOf).label}». Изменить статус на «${target.label}»?`);
        if (ok) {
          const patchResponse = await patchIp(id, 'status', data.suggest_status);
          if (patchResponse.ok) {
            updateRow?.(id, { status: target });
            showToast(`${address}: статус изменён на «${target.label}»`, 'success');
          } else {
            showToast('Не удалось изменить статус', 'error');
          }
        }
      }
    }
    return { ok: true, alive: !!data.alive, suggested: !!data.suggest_status, statusChanged: !!data.status_changed };
  }

  if (action === 'scan-ports') {
    updateRow?.(id, { open_ports: data.ports || '' });
    if (interactive) {
      showToast(data.ports ? `${address}: открытые порты — ${data.ports}` : `${address}: открытых портов не найдено`, 'success');
    }
    return { ok: true, ports: data.ports || '' };
  }

  if (action === 'resolve-dns') {
    if (data.action === 'saved') updateRow?.(id, { dns_name: data.resolved });
    if (interactive) {
      if (data.action === 'none') {
        showToast(
          data.resolved ? `${address}: имя уже совпадает с PTR (${data.resolved})` : `${address}: PTR-запись не найдена`,
          data.resolved ? 'success' : 'error',
        );
      } else if (data.action === 'saved') {
        showToast(`${address}: DNS-имя сохранено — ${data.resolved}`, 'success');
      } else if (data.action === 'confirm') {
        const ok = window.confirm(`Текущее DNS-имя «${data.current}» отличается от PTR «${data.resolved}». Заменить на значение из PTR?`);
        if (ok) {
          const patchResponse = await patchIp(id, 'dns_name', data.resolved);
          if (patchResponse.ok) {
            updateRow?.(id, { dns_name: data.resolved });
            showToast(`${address}: DNS-имя обновлено — ${data.resolved}`, 'success');
          } else {
            showToast('Не удалось сохранить DNS-имя', 'error');
          }
        }
      }
    }
    return { ok: true, dnsAction: data.action, resolved: data.resolved || '' };
  }
  return { ok: true };
}

/** Удалить один адрес (с подтверждением, если confirm=true). */
export async function performDelete(row, { removeRow, confirm = true } = {}) {
  const address = (row.address || '').split('/')[0];
  if (confirm && !window.confirm(`Удалить IP-адрес ${address}? Это действие нельзя отменить.`)) return { ok: false, cancelled: true };
  try {
    const response = await deleteIp(row.id);
    if (response.ok) {
      removeRow?.(row.id);
      if (confirm) showToast(`IP-адрес ${address} удалён`, 'success');
      return { ok: true };
    }
    let message = 'Не удалось удалить IP-адрес';
    try { message = (await response.json()).error || message; } catch (e) { /* ответ без тела */ }
    if (confirm) showToast(message, 'error');
    return { ok: false, error: message };
  } catch (e) {
    if (confirm) showToast('NetBox недоступен', 'error');
    return { ok: false, error: 'NetBox недоступен' };
  }
}

/** PATCH одного поля с тостом. */
export async function patchField(row, field, value, { updateRow, patch, silent = false } = {}) {
  try {
    const response = await patchIp(row.id, field, value);
    if (response.ok) {
      updateRow?.(row.id, patch || { [field]: value });
      if (!silent) showToast('Сохранено', 'success');
      return { ok: true };
    }
    if (!silent) showToast(response.status === 403 ? 'Сессия истекла — обновите страницу (F5)' : 'Не удалось сохранить изменение', 'error');
    return { ok: false };
  } catch (e) {
    if (!silent) showToast('NetBox недоступен', 'error');
    return { ok: false };
  }
}

/** Пул: не более `limit` задач одновременно, порядок результатов — как у
 * items. onProgress(done, total) — для индикатора в панели. */
export async function runPool(items, limit, worker, onProgress) {
  const results = new Array(items.length);
  let next = 0;
  let done = 0;
  const lanes = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
    while (next < items.length) {
      const idx = next;
      next += 1;
      try {
        results[idx] = await worker(items[idx], idx);
      } catch (e) {
        results[idx] = { ok: false, error: String(e) };
      }
      done += 1;
      onProgress?.(done, items.length);
    }
  });
  await Promise.all(lanes);
  return results;
}
