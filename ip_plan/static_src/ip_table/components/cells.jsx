import { useEffect, useRef, useState } from 'react';
import { patchIp } from '../api.js';
import { STATUS_COLOR_VAR, STATUS_FALLBACK_LABELS, STATUS_ORDER, statusLabel } from '../constants.js';
import { IconCheck, IconCopy } from './icons.jsx';
import { Popover } from './popover.jsx';

export function showToast(message, kind) {
  window.dispatchEvent(new CustomEvent('show-toast', { detail: { message, kind } }));
}

export function copyText(value, label) {
  if (!value) return;
  navigator.clipboard?.writeText(value);
  showToast(`Скопировано: ${label || value}`, 'success');
}

/** Кнопка «копировать» — видна при наведении на строку (.ipt-reveal). */
export function CopyButton({ value, label }) {
  if (!value) return null;
  return (
    <button
      type="button"
      className="ipt-ibtn ipt-reveal"
      title="Копировать"
      aria-label={`Копировать ${label || value}`}
      onClick={(e) => { e.stopPropagation(); copyText(value); }}
    >
      <IconCopy size={13} />
    </button>
  );
}

/** Точка статуса с «гало». */
export function StatusDot({ value, className = '' }) {
  const color = STATUS_COLOR_VAR[value] || 'var(--off)';
  return <span className={`ipt-dot ${className}`} style={{ '--dot': color }} aria-hidden="true" />;
}

/** Пустое значение — «—» бледным. */
export function Empty() {
  return <span className="ipt-faint">—</span>;
}

/** Текстовая ячейка с инлайн-редактированием по двойному клику:
 * Enter — сохранить, Escape — отмена, потеря фокуса — сохранить, если
 * значение изменилось. PATCH одного поля → onSaved(id, patch). */
export function EditableText({ id, field, value, display, editable = true, mono = false, muted = false, title, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? '');
  const [saving, setSaving] = useState(false);
  const inputRef = useRef(null);

  useEffect(() => { if (editing) inputRef.current?.select(); }, [editing]);

  const shown = display ?? value;
  const cls = ['ipt-txt', mono ? 'ipt-mono' : '', muted ? 'ipt-muted' : ''].filter(Boolean).join(' ');

  if (!editing) {
    return (
      <span
        className={cls}
        title={title ?? (shown ? String(shown) : undefined)}
        onDoubleClick={editable ? (e) => { e.preventDefault(); setDraft(value ?? ''); setEditing(true); } : undefined}
        data-editable={editable ? '' : undefined}
      >
        {shown ? shown : <Empty />}
      </span>
    );
  }

  async function save() {
    if (draft === (value ?? '')) { setEditing(false); return; }
    setSaving(true);
    try {
      const response = await patchIp(id, field, draft);
      if (response.ok) {
        onSaved?.(id, { [field]: draft });
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

  return (
    <input
      ref={inputRef}
      autoFocus
      type="text"
      className={`ipt-inline-input${mono ? ' ipt-mono' : ''}`}
      disabled={saving}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={save}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === 'Enter') save();
        if (e.key === 'Escape') setEditing(false);
      }}
    />
  );
}

/** Список статусов для выбора: из choices сервера, в нашем порядке;
 * чего нет в choices — добираем из запасной карты. */
export function statusOptions(choices) {
  const map = new Map();
  (choices || []).forEach(([v, l]) => map.set(v, l));
  STATUS_ORDER.forEach((v) => { if (!map.has(v)) map.set(v, STATUS_FALLBACK_LABELS[v]); });
  return STATUS_ORDER.filter((v) => map.has(v)).map((v) => ({ value: v, label: map.get(v) }))
    .concat([...map.keys()].filter((v) => !STATUS_ORDER.includes(v)).map((v) => ({ value: v, label: map.get(v) })));
}

/** Маленький поповер выбора статуса (5 вариантов). */
export function StatusPicker({ anchorRef, open, onClose, current, choices, onPick }) {
  return (
    <Popover anchorRef={anchorRef} open={open} onClose={onClose} className="ipt-pop-status" ariaLabel="Выбрать статус">
      {statusOptions(choices).map((opt) => (
        <button
          key={opt.value}
          type="button"
          role="menuitemradio"
          aria-checked={opt.value === current}
          className="ipt-mi"
          onClick={() => { onPick(opt.value, opt.label); onClose(); }}
        >
          <StatusDot value={opt.value} />
          <span className="ipt-mi-label">{opt.label}</span>
          {opt.value === current && <IconCheck size={13} className="ipt-mi-check" />}
        </button>
      ))}
    </Popover>
  );
}

/** Ячейка статуса: точка + слово; клик — поповер выбора, PATCH. */
export function StatusCell({ id, status, choices, onSaved }) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const ref = useRef(null);
  const value = status?.value;

  async function pick(newValue, label) {
    if (newValue === value) return;
    setSaving(true);
    try {
      const response = await patchIp(id, 'status', newValue);
      if (response.ok) {
        onSaved?.(id, { status: { value: newValue, label } });
        showToast('Сохранено', 'success');
      } else {
        showToast(response.status === 403 ? 'Сессия истекла — обновите страницу (F5)' : 'Не удалось сохранить изменение', 'error');
      }
    } catch (e) {
      showToast('NetBox недоступен', 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <button
        ref={ref}
        type="button"
        className={`ipt-status${open ? ' is-open' : ''}`}
        title="Изменить статус"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={saving}
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
      >
        <StatusDot value={value} />
        <span className="ipt-ellipsis">{statusLabel(status, choices)}</span>
      </button>
      <StatusPicker anchorRef={ref} open={open} onClose={() => setOpen(false)} current={value} choices={choices} onPick={pick} />
    </>
  );
}
