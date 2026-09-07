import { deleteEntry, reactivateEntry, revokeEntry } from '../api.js';
import { showToast } from '../toast.js';

/** Действия строки. «Снять» показывается только когда showRevoke=true
 * (детальная страница письма — см. TableRoot.jsx: documentId задан) — на
 * вкладке «Записи» общего списка её по-прежнему нет по ТЗ (сквозной обзор
 * по всем письмам, снятие — точечное действие в контексте конкретного
 * письма). Причина снятия необязательна — собирается через window.prompt(),
 * тем же паттерном, что подтверждение удаления чуть ниже (не заводим
 * отдельную модалку ради одного необязательного текстового поля). */
export function RowActions({ entry, table, showRevoke }) {
  async function handleRevoke() {
    const reason = window.prompt('Причина снятия (необязательно):', '');
    if (reason === null) return; // отмена
    try {
      const response = await revokeEntry(entry.id, reason.trim());
      if (response.ok) {
        const data = await response.json();
        table.options.meta?.updateEntry(entry.id, data.entry);
        showToast('Запись снята', 'success');
      } else {
        showToast('Не удалось снять запись', 'error');
      }
    } catch (e) {
      showToast('Не удалось выполнить запрос', 'error');
    }
  }

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
      {showRevoke && entry.status === 'active' && (
        <button type="button" className="btn-modal-secondary btn-sm" onClick={handleRevoke}>Снять</button>
      )}
      {entry.status === 'revoked' && (
        <button type="button" className="btn-modal-secondary btn-sm" onClick={handleReactivate}>Восстановить</button>
      )}
      <button type="button" className="btn-danger btn-sm" onClick={handleDelete}>Удалить</button>
    </span>
  );
}
