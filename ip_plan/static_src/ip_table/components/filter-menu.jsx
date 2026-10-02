import { useEffect, useMemo, useState } from 'react';
import { IconNetwork, IconRole, IconSearch, IconStatus, IconTag } from './icons.jsx';
import { Popover } from './popover.jsx';
import { StatusDot } from './cells.jsx';

const FIELD_ICONS = { prefix: IconNetwork, status: IconStatus, role: IconRole, tags: IconTag };

const FIELD_ROW_H = 34;
const FIELD_SEARCH_H = 44;

/** Двухуровневое меню «Фильтр»: слева поля (с бейджем числа выбранных),
 * справа значения активного поля с поиском и счётчиками. Выбор
 * применяется сразу — onChange(key, values[]). */
export function FilterMenu({ anchorRef, open, onClose, fields, selected, onChange }) {
  const [active, setActive] = useState(fields[0]?.key);
  const [fieldQuery, setFieldQuery] = useState('');
  const [valueQuery, setValueQuery] = useState('');

  useEffect(() => {
    if (open) { setFieldQuery(''); setValueQuery(''); }
  }, [open]);

  const visibleFields = useMemo(() => {
    const q = fieldQuery.trim().toLowerCase();
    return q ? fields.filter((f) => f.label.toLowerCase().includes(q)) : fields;
  }, [fields, fieldQuery]);

  const activeIdx = visibleFields.findIndex((f) => f.key === active);
  const cur = visibleFields[activeIdx] || visibleFields[0];
  const curSel = (cur && selected[cur.key]) || [];

  const values = useMemo(() => {
    if (!cur) return [];
    const q = valueQuery.trim().toLowerCase();
    if (!q) return cur.values;
    return cur.values.filter((v) => [v.value, v.label, v.note].some((s) => s && String(s).toLowerCase().includes(q)));
  }, [cur, valueQuery]);

  function toggle(value) {
    const set = new Set(curSel);
    if (set.has(value)) set.delete(value); else set.add(value);
    onChange(cur.key, [...set]);
  }

  return (
    <Popover anchorRef={anchorRef} open={open} onClose={onClose} className="ipt-pop-filter" role="dialog" ariaLabel="Фильтр">
      <div className="ipt-fm-fields" role="menu" aria-label="Фильтр по полю">
        <label className="ipt-fm-search">
          <IconSearch size={14} />
          <input
            type="text"
            aria-label="Найти поле"
            placeholder="Фильтровать по…"
            value={fieldQuery}
            onChange={(e) => setFieldQuery(e.target.value)}
          />
        </label>
        <div className="ipt-fm-list">
          {visibleFields.map((f) => {
            const n = (selected[f.key] || []).length;
            const Icon = FIELD_ICONS[f.key] || IconTag;
            const isActive = cur && f.key === cur.key;
            return (
              <button
                key={f.key}
                type="button"
                role="menuitem"
                aria-haspopup="true"
                aria-expanded={isActive}
                className={`ipt-fm-field${isActive ? ' is-active' : ''}`}
                onClick={() => { setActive(f.key); setValueQuery(''); }}
                onMouseEnter={() => { if (!isActive) { setActive(f.key); setValueQuery(''); } }}
              >
                <span className="ipt-fm-ficon"><Icon size={15} /></span>
                <span className="ipt-fm-flabel">{f.label}</span>
                {n > 0 && <span className="ipt-badge-n ipt-mono">{n}</span>}
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" className="ipt-fm-chev" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
              </button>
            );
          })}
          {!visibleFields.length && <div className="ipt-fm-empty">Нет такого поля</div>}
        </div>
      </div>

      {cur && (
        <div
          className="ipt-fm-values"
          role="menu"
          aria-label={cur.label}
          style={{ marginTop: FIELD_SEARCH_H + Math.max(activeIdx, 0) * FIELD_ROW_H }}
        >
          <label className="ipt-fm-search">
            <IconSearch size={14} />
            <input
              type="text"
              aria-label="Найти значение"
              placeholder={cur.placeholder || 'Найти'}
              value={valueQuery}
              onChange={(e) => setValueQuery(e.target.value)}
            />
          </label>
          <div className="ipt-fm-vlist">
            {values.map((v) => (
              <label key={v.value} className="ipt-fm-value">
                <input type="checkbox" checked={curSel.includes(v.value)} onChange={() => toggle(v.value)} />
                {v.dot && <StatusDot value={v.dot} className="ipt-dot-plain" />}
                <span className="ipt-fm-vtext">
                  <span className={cur.mono ? 'ipt-mono ipt-fm-vmono' : ''}>{v.label}</span>
                  {v.note && <span className="ipt-fm-vnote">{v.note}</span>}
                </span>
                <span className="ipt-fm-vcount ipt-mono">{v.count}</span>
              </label>
            ))}
            {!values.length && <div className="ipt-fm-empty">Ничего не найдено</div>}
          </div>
          <div className="ipt-fm-foot">
            <span>{curSel.length ? `Выбрано: ${curSel.length} · применяется сразу` : 'Ничего не выбрано'}</span>
            <button type="button" className="ipt-btn-ghost ipt-btn-sm" onClick={() => onChange(cur.key, [])} disabled={!curSel.length}>Очистить</button>
          </div>
        </div>
      )}
    </Popover>
  );
}
