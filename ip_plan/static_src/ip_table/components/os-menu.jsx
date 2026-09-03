import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { patchIp } from '../api.js';
import { showToast } from './cells.jsx';
import { positionNear } from './position.js';

function OsSubmenu({ entry, anchorRect, onPick, onMouseEnter, onMouseLeave }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) positionNear(anchorRect, ref.current, { flyout: true });
  }, [anchorRect]);
  return createPortal(
    <div className="os-menu-submenu show" ref={ref} onMouseEnter={onMouseEnter} onMouseLeave={onMouseLeave}>
      {entry.versions.map((v) => (
        <div key={v} className="os-menu-item" onClick={(e) => { e.stopPropagation(); onPick(`${entry.name} ${v}`); }}>
          {v}
        </div>
      ))}
    </div>,
    document.body,
  );
}

function OsMenuPanel({ anchorRect, onPick, onCancel }) {
  const menuRef = useRef(null);
  const [filterText, setFilterText] = useState('');
  const [activeEntry, setActiveEntry] = useState(null); // { key, rect }
  const hoverCloseTimer = useRef(null);
  const grouped = window.OS_CATALOG_OPTIONS || {};

  useEffect(() => {
    if (menuRef.current) positionNear(anchorRect, menuRef.current);
  }, [anchorRect, filterText]);

  useEffect(() => {
    function onOutsideMouseDown(e) {
      if (menuRef.current && !menuRef.current.contains(e.target) && !e.target.closest('.os-menu-submenu')) {
        onCancel();
      }
    }
    // mousedown, не click — тот клик, которым открыли меню, успевает
    // завершиться раньше, чем навешивается этот обработчик.
    document.addEventListener('mousedown', onOutsideMouseDown, true);
    return () => document.removeEventListener('mousedown', onOutsideMouseDown, true);
  }, [onCancel]);

  const needle = filterText.trim().toLowerCase();
  let exactMatch = false;
  const categories = Object.keys(grouped)
    .map((category) => ({
      category,
      entries: grouped[category].filter((e) => !needle || e.name.toLowerCase().includes(needle)),
    }))
    .filter((g) => g.entries.length);
  categories.forEach((g) => g.entries.forEach((e) => { if (e.name.toLowerCase() === needle) exactMatch = true; }));

  function openSubmenu(entryKey, rect) {
    clearTimeout(hoverCloseTimer.current);
    setActiveEntry({ key: entryKey, rect });
  }
  function scheduleCloseSubmenu() {
    hoverCloseTimer.current = setTimeout(() => setActiveEntry(null), 250);
  }

  return createPortal(
    <div className="os-menu" ref={menuRef} onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); }}>
      <input
        autoFocus
        type="text"
        className="os-menu-filter"
        placeholder="Поиск..."
        value={filterText}
        onChange={(e) => setFilterText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); }}
      />
      <div className="os-menu-list">
        {categories.map(({ category, entries }) => (
          <div key={category}>
            <div className="os-menu-group-label">{category}</div>
            {entries.map((entry) => {
              const hasVersions = entry.versions && entry.versions.length > 0;
              const entryKey = `${category}::${entry.name}`;
              return (
                <div
                  key={entryKey}
                  className={`os-menu-item${activeEntry?.key === entryKey ? ' os-menu-item-active' : ''}`}
                  onClick={(e) => {
                    if (hasVersions) openSubmenu(entryKey, e.currentTarget.getBoundingClientRect());
                    else onPick(entry.name);
                  }}
                  onMouseEnter={(e) => { if (hasVersions) openSubmenu(entryKey, e.currentTarget.getBoundingClientRect()); }}
                  onMouseLeave={scheduleCloseSubmenu}
                >
                  <span>{entry.name}</span>
                  {hasVersions && <span className="os-menu-arrow">▸</span>}
                </div>
              );
            })}
          </div>
        ))}
        {!categories.length && <div className="os-menu-empty">Ничего не найдено</div>}
      </div>
      {needle && !exactMatch && (
        <div className="os-menu-custom-row" onClick={() => onPick(filterText.trim())}>
          Использовать «{filterText.trim()}» как есть
        </div>
      )}
      {activeEntry && (() => {
        const [category] = activeEntry.key.split('::');
        const entry = grouped[category]?.find((e) => `${category}::${e.name}` === activeEntry.key);
        if (!entry) return null;
        return (
          <OsSubmenu
            entry={entry}
            anchorRect={activeEntry.rect}
            onPick={onPick}
            onMouseEnter={() => clearTimeout(hoverCloseTimer.current)}
            onMouseLeave={scheduleCloseSubmenu}
          />
        );
      })()}
    </div>,
    document.body,
  );
}

/** Ячейка "Операционная система" — своё меню с поиском/группами/подменю
 * версий (справочник admin/os_catalog.json → window.OS_CATALOG_OPTIONS),
 * не обычный <select>: перенесено из app.js (startOsMenuEdit). Открывается
 * одним кликом (не двойным, как у обычных текстовых полей) — так же вело
 * себя data-type="select-or-text" в старой версии. */
export function OsMenuCell({ id, value, table }) {
  const [editing, setEditing] = useState(false);
  const [anchorRect, setAnchorRect] = useState(null);
  const [saving, setSaving] = useState(false);

  function open(e) {
    setAnchorRect(e.currentTarget.getBoundingClientRect());
    setEditing(true);
  }

  async function save(newValue) {
    setEditing(false);
    if (newValue === value) return;
    setSaving(true);
    try {
      const response = await patchIp(id, 'os_installed', newValue);
      if (response.ok) {
        table.options.meta?.updateRow(id, { os_installed: newValue });
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
    <span className="cell-display" onClick={open} style={{ cursor: saving ? 'wait' : 'pointer' }}>
      <span className="cell-with-copy">
        <span className="cell-value" title={value}>{value || '—'}</span>
        {value && (
          <button
            type="button"
            className="cell-clear-btn"
            aria-label="Очистить OS Installed"
            title="Очистить"
            onClick={(e) => { e.stopPropagation(); save(''); }}
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        )}
      </span>
      {editing && (
        <OsMenuPanel anchorRect={anchorRect} onPick={save} onCancel={() => setEditing(false)} />
      )}
    </span>
  );
}
