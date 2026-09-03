import { useState } from 'react';
import { patchIp } from '../api.js';

export function showToast(message, kind) {
  window.dispatchEvent(new CustomEvent('show-toast', { detail: { message, kind } }));
}

export function CopyButton({ value }) {
  if (!value) return null;
  return (
    <button
      type="button"
      className="copy-btn"
      title="Копировать"
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(value);
        showToast('Скопировано: ' + value, 'success');
      }}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="9" y="9" width="13" height="13" rx="2" />
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
      </svg>
    </button>
  );
}

export function StatusBadge({ status }) {
  return (
    <span className="status-chip" data-status={status?.value}>
      <span className="status-dot" aria-hidden="true" />
      {status?.label || status?.value}
    </span>
  );
}

export function RoleBadge({ role }) {
  if (!role) return <span className="cell-muted">—</span>;
  return <span className="badge-role" data-role={role.slug}>{role.name}</span>;
}

export const IP_STATUS_OPTIONS = [
  { value: 'active', label: 'Active' },
  { value: 'reserved', label: 'Reserved' },
  { value: 'deprecated', label: 'Deprecated' },
  { value: 'dhcp', label: 'DHCP' },
  { value: 'slaac', label: 'SLAAC' },
];

/** Двойной клик по ячейке — инлайн-редактирование (текст или select),
 * PATCH на сервер, оптимистичное обновление строки через meta.updateRow —
 * тот же паттерн, что startInlineEdit()/saveInlineEdit() в app.js, но как
 * управляемый React-компонент вместо ручного DOM. */
export function EditableCell({ id, field, value, display, editable, type = 'text', maxLen, table }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? '');
  const [saving, setSaving] = useState(false);

  if (!editable) {
    return <span className="cell-value">{display ?? value ?? '—'}</span>;
  }

  if (!editing) {
    return (
      <span
        className="cell-display"
        onDoubleClick={() => { setDraft(value ?? ''); setEditing(true); }}
        title="Двойной клик — редактировать"
      >
        <span className="cell-value">
          {maxLen && (display ?? value) ? String(display ?? value).slice(0, maxLen) : ((display ?? value) || '—')}
        </span>
      </span>
    );
  }

  async function save(newValue) {
    setSaving(true);
    try {
      const response = await patchIp(id, field, newValue);
      if (response.ok) {
        table.options.meta?.updateRow(id, { [field]: newValue });
        showToast('Сохранено', 'success');
        setEditing(false);
      } else {
        showToast(response.status === 403 ? 'Сессия истекла — обновите страницу (F5)' : 'Не удалось сохранить изменение', 'error');
      }
    } catch (e) {
      showToast('NetBox недоступен', 'error');
    } finally {
      setSaving(false);
    }
  }

  if (type === 'select') {
    return (
      <select
        autoFocus
        disabled={saving}
        defaultValue={value}
        onBlur={() => setEditing(false)}
        onChange={(e) => save(e.target.value)}
      >
        {IP_STATUS_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
    );
  }

  return (
    <input
      autoFocus
      type="text"
      disabled={saving}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => { if (draft !== value) save(draft); else setEditing(false); }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') save(draft);
        if (e.key === 'Escape') setEditing(false);
      }}
    />
  );
}
