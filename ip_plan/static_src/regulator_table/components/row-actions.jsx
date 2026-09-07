import { deleteEntry, reactivateEntry } from '../api.js';
import { showToast } from '../toast.js';

/** Действия строки — только «Восстановить» (для уже снятой записи, без
 * подтверждения — обратимо и не разрушительно) и «Удалить» (с
 * window.confirm, тот же паттерн, что deleteRow() в ip_table/components/
 * kebab-menu.jsx). «Снять» сюда сознательно не перенесена — по ТЗ вкладка
 * «Записи» её не показывает вообще (снятие статуса остаётся на детальной
 * странице письма). */
export function RowActions({ entry, table }) {
  async function handleReactivate() {
    try {
      const response = await reactivateEntry(entry.id);
      if (response.ok) {
        const data = await response.json();
        table.options.meta?.updateEntry(entry.id, data.entry);
        showToast('Запись восстановлена', 'success');
      } else {
        showToast('Не удалось восстановить запись', 'error');
      }
    } catch (e) {
      showToast('NetBox недоступен', 'error');
    }
  }

  async function handleDelete() {
    const ok = window.confirm(
      `Удалить запись «${entry.value}»? Только для исправления ошибки ввода — если блокировку с регулятора сняли, отметьте это на странице письма, не удаляйте запись.`,
    );
    if (!ok) return;
    try {
      const response = await deleteEntry(entry.id);
      if (response.ok) {
        table.options.meta?.removeEntry(entry.id);
        showToast('Запись удалена', 'success');
      } else {
        showToast('Не удалось удалить запись', 'error');
      }
    } catch (e) {
      showToast('NetBox недоступен', 'error');
    }
  }

  return (
    <span className="regulator-entry-actions">
      {entry.status === 'revoked' && (
        <button type="button" className="btn-modal-secondary btn-sm" onClick={handleReactivate}>Восстановить</button>
      )}
      <button type="button" className="btn-danger btn-sm" onClick={handleDelete}>Удалить</button>
    </span>
  );
}
