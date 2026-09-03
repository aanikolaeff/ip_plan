import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { deleteIp, patchIp, runNetworkAction } from '../api.js';
import { showToast } from './cells.jsx';
import { positionNear } from './position.js';

const START_MESSAGES = {
  check: (addr) => `Проверка доступности ${addr}…`,
  'scan-ports': (addr) => `Сканирование портов ${addr} (1–1000)…`,
  'resolve-dns': (addr) => `Резолв DNS для ${addr}…`,
};

/** Три сетевых действия кебаб-меню — перенесено дословно из
 * runIpNetworkAction() в app.js (старая DataTables-версия), включая обе
 * ветки "сервер предлагает изменить поле, спросить подтверждение". */
async function runAction(action, id, address, updateRow) {
  showToast(START_MESSAGES[action](address), 'info');

  let response;
  try {
    response = await runNetworkAction(action, id);
  } catch (e) {
    showToast('NetBox недоступен', 'error');
    return;
  }
  let data = {};
  try {
    data = await response.json();
  } catch (e) { /* ответ без тела */ }
  if (!response.ok) {
    showToast(data.error || 'Ошибка операции', 'error');
    return;
  }

  if (action === 'check') {
    if (data.status_changed) updateRow(id, { status: { value: 'active', label: 'Active' } });
    const latency = data.latency_ms != null ? ` (${data.latency_ms} мс)` : '';
    const suffix = data.status_changed ? ' — статус → Active' : '';
    showToast(`${address}: ${data.alive ? 'доступен' + latency : 'не отвечает'}${suffix}`, data.alive ? 'success' : 'error');
    if (data.suggest_status) {
      const ok = window.confirm(`${address} не отвечает на пинг, хотя статус сейчас Active. Изменить статус на Reserved?`);
      if (!ok) return;
      const patchResponse = await patchIp(id, 'status', data.suggest_status);
      if (patchResponse.ok) {
        updateRow(id, { status: { value: data.suggest_status, label: 'Reserved' } });
        showToast(`${address}: статус изменён на Reserved`, 'success');
      } else {
        showToast('Не удалось изменить статус', 'error');
      }
    }
    return;
  }

  if (action === 'scan-ports') {
    updateRow(id, { open_ports: data.ports || '' });
    showToast(data.ports ? `${address}: открытые порты — ${data.ports}` : `${address}: открытых портов не найдено`, 'success');
    return;
  }

  if (action === 'resolve-dns') {
    if (data.action === 'none') {
      showToast(
        data.resolved ? `${address}: имя уже совпадает с PTR (${data.resolved})` : `${address}: PTR-запись не найдена`,
        data.resolved ? 'success' : 'error',
      );
      return;
    }
    if (data.action === 'saved') {
      updateRow(id, { dns_name: data.resolved });
      showToast(`${address}: DNS-имя сохранено — ${data.resolved}`, 'success');
      return;
    }
    if (data.action === 'confirm') {
      const ok = window.confirm(`Текущее DNS-имя «${data.current}» отличается от PTR «${data.resolved}». Заменить на значение из PTR?`);
      if (!ok) return;
      const patchResponse = await patchIp(id, 'dns_name', data.resolved);
      if (patchResponse.ok) {
        updateRow(id, { dns_name: data.resolved });
        showToast(`${address}: DNS-имя обновлено — ${data.resolved}`, 'success');
      } else {
        showToast('Не удалось сохранить DNS-имя', 'error');
      }
    }
  }
}

async function deleteRow(id, address, onDeleted) {
  if (!window.confirm(`Удалить IP-адрес ${address}? Это действие нельзя отменить.`)) return;
  try {
    const response = await deleteIp(id);
    if (response.ok) {
      onDeleted(id);
      showToast(`IP-адрес ${address} удалён`, 'success');
    } else {
      let message = 'Не удалось удалить IP-адрес';
      try {
        message = (await response.json()).error || message;
      } catch (e) { /* ответ без тела */ }
      showToast(message, 'error');
    }
  } catch (e) {
    showToast('NetBox недоступен', 'error');
  }
}

/** Кебаб-меню у адреса (⋯): проверить доступность / сканировать порты /
 * резолв DNS / удалить — перенесено из app.js (setupRowDelegation:
 * .btn-kebab/.kebab-menu/.kebab-action/.kebab-delete-ip). Портал в
 * document.body + positionNear() вместо оригинальных .kebab-menu-up/
 * .kebab-menu-fixed эвристик (там их было три — вниз по умолчанию,
 * перевернуть вверх, эвакуировать в fixed, если не помещается ни там, ни
 * там) — фиксированное позиционирование от viewport делает две первые
 * ветки не нужны, экран есть экран независимо от того, где внутри
 * прокручиваемой таблицы сидит сама кнопка. */
export function KebabMenu({ id, address, table }) {
  const [open, setOpen] = useState(false);
  const [anchorRect, setAnchorRect] = useState(null);
  const btnRef = useRef(null);
  const menuRef = useRef(null);

  useEffect(() => {
    if (open && menuRef.current) positionNear(anchorRect, menuRef.current);
  }, [open, anchorRect]);

  useEffect(() => {
    if (!open) return undefined;
    function onOutsideMouseDown(e) {
      if (menuRef.current && !menuRef.current.contains(e.target) && !btnRef.current?.contains(e.target)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', onOutsideMouseDown, true);
    return () => document.removeEventListener('mousedown', onOutsideMouseDown, true);
  }, [open]);

  function toggle() {
    setAnchorRect(btnRef.current.getBoundingClientRect());
    setOpen((v) => !v);
  }

  function runAndClose(action) {
    setOpen(false);
    runAction(action, id, address, (rowId, patch) => table.options.meta?.updateRow(rowId, patch));
  }

  function deleteAndClose() {
    setOpen(false);
    deleteRow(id, address, (deletedId) => table.options.meta?.removeRow(deletedId));
  }

  return (
    <span className="kebab-wrap">
      <button ref={btnRef} type="button" className="btn-kebab" onClick={toggle}>⋯</button>
      {open && createPortal(
        <div ref={menuRef} className="kebab-menu show kebab-menu-fixed">
          <button type="button" className="kebab-action" onClick={() => runAndClose('check')}>Проверить доступность</button>
          <button type="button" className="kebab-action" onClick={() => runAndClose('scan-ports')}>Сканировать порты</button>
          <button type="button" className="kebab-action" onClick={() => runAndClose('resolve-dns')}>Резолв DNS</button>
          <div className="kebab-divider" />
          <button type="button" className="kebab-delete-ip" onClick={deleteAndClose}>Удалить</button>
        </div>,
        document.body,
      )}
    </span>
  );
}
