// Фильтр «Роли»: чекбоксы по семействам и видам оборудования с числом
// устройств; скрытые виды пропадают с полотна вместе со своими связями.

import { useMemo } from 'react';
import { useMap, ACTIONS } from '../state/store.js';
import { FAMILY_ORDER, FAMILIES, KIND_ORDER, KINDS } from '../constants.js';
import { IconFilter, KindIcon } from './Icons.jsx';
import usePopover from './usePopover.js';

export default function RolesMenu() {
  const { model, state, dispatch } = useMap();
  const { open, setOpen, ref } = usePopover();
  const counts = useMemo(() => {
    const m = new Map();
    (model?.nodes || []).forEach((n) => m.set(n.kind, (m.get(n.kind) || 0) + 1));
    return m;
  }, [model]);
  const kinds = KIND_ORDER.filter((k) => counts.has(k));
  const hidden = new Set(state.hiddenKinds.filter((k) => counts.has(k)));
  const shown = kinds.length - hidden.size;
  const badge = hidden.size === 0 ? 'все' : `${shown} из ${kinds.length}`;

  const setHidden = (set) => dispatch({ type: ACTIONS.KINDS_SET, hidden: [...set] });
  const toggleKind = (k) => {
    const next = new Set(hidden);
    if (next.has(k)) next.delete(k); else next.add(k);
    setHidden(next);
  };
  const toggleFamily = (fam) => {
    const fk = kinds.filter((k) => KINDS[k].family === fam);
    const allOn = fk.every((k) => !hidden.has(k));
    const next = new Set(hidden);
    fk.forEach((k) => (allOn ? next.add(k) : next.delete(k)));
    setHidden(next);
  };

  return (
    <div className="tm-popwrap" ref={ref}>
      <button type="button" className={`tm-tbtn${hidden.size ? ' is-active' : ''}`} aria-haspopup="true" aria-expanded={open} disabled={!model} onClick={() => setOpen(!open)}>
        <IconFilter style={{ color: 'var(--text-muted)' }} />Роли<span className="tm-tbtn-badge">{badge}</span>
      </button>
      {open && (
        <div className="tm-pop tm-roles-pop" role="dialog" aria-label="Фильтр по ролям">
          {FAMILY_ORDER.map((fam) => {
            const fk = kinds.filter((k) => KINDS[k].family === fam);
            if (!fk.length) return null;
            const on = fk.filter((k) => !hidden.has(k)).length;
            const total = fk.reduce((s, k) => s + counts.get(k), 0);
            return (
              <div key={fam} className="tm-roles-fam">
                <label className="tm-check is-fam">
                  <input
                    type="checkbox"
                    checked={on === fk.length}
                    ref={(el) => { if (el) el.indeterminate = on > 0 && on < fk.length; }}
                    onChange={() => toggleFamily(fam)}
                  />
                  <span className={`tm-fam-swatch fam-${fam}`} />
                  <span className="tm-check-label">{FAMILIES[fam].label}</span>
                  <span className="tm-mono tm-faint">{total}</span>
                </label>
                {fk.map((k) => (
                  <label key={k} className="tm-check">
                    <input type="checkbox" checked={!hidden.has(k)} onChange={() => toggleKind(k)} />
                    <span className={`tm-mini-ico fam-${fam}`}><KindIcon kind={k} size={13} sw={1.8} /></span>
                    <span className="tm-check-label">{KINDS[k].label}</span>
                    <span className="tm-mono tm-faint">{counts.get(k)}</span>
                  </label>
                ))}
              </div>
            );
          })}
          <div className="tm-pop-foot">
            <button type="button" className="tm-link-btn" disabled={!hidden.size} onClick={() => setHidden(new Set())}>Показать все</button>
          </div>
        </div>
      )}
    </div>
  );
}
