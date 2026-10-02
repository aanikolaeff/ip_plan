// «Найти устройство»: подсказки по имени и IP (до 8), Enter/выбор —
// раскрыть группу при необходимости, выбрать и центрировать узел.

import { useMemo, useState } from 'react';
import { useMap, ACTIONS } from '../state/store.js';
import { IconSearch } from './Icons.jsx';
import { NodeTile } from '../panel/common.jsx';

const LIMIT = 8;

export default function SearchBox() {
  const { model, state, dispatch } = useMap();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const q = state.query.trim().toLowerCase();

  const hits = useMemo(() => {
    if (!model || !q) return [];
    const scored = [];
    model.nodes.forEach((n) => {
      const name = n.name.toLowerCase();
      const ip = (n.ip || '').toLowerCase();
      let score = -1;
      if (name.startsWith(q)) score = 0;
      else if (ip.startsWith(q)) score = 1;
      else if (name.includes(q)) score = 2;
      if (score >= 0) scored.push([score, n]);
    });
    scored.sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name, 'ru', { numeric: true }));
    return scored.slice(0, LIMIT).map((x) => x[1]);
  }, [model, q]);

  const pick = (n) => {
    if (!n) return;
    if (state.hiddenKinds.includes(n.kind)) {
      dispatch({ type: ACTIONS.KINDS_SET, hidden: state.hiddenKinds.filter((k) => k !== n.kind) });
    }
    dispatch({ type: ACTIONS.FOCUS, target: { type: 'node', id: n.id } });
    dispatch({ type: ACTIONS.QUERY_SET, query: n.name });
    setOpen(false);
  };

  const onKey = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setActive((a) => Math.min(hits.length - 1, a + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
    else if (e.key === 'Enter') { e.preventDefault(); pick(hits[active] || hits[0]); }
    else if (e.key === 'Escape') {
      e.stopPropagation();
      if (open && hits.length) setOpen(false);
      else dispatch({ type: ACTIONS.QUERY_SET, query: '' });
    }
  };

  const groupName = (n) => {
    const g = model.groupOf.get(n.id);
    return g ? model.groupsById.get(g)?.title : null;
  };

  return (
    <div className="tm-search">
      <span className="tm-search-ico"><IconSearch /></span>
      <input
        type="search"
        aria-label="Найти устройство на карте"
        placeholder="Найти устройство"
        value={state.query}
        disabled={!model}
        role="combobox"
        aria-expanded={open && hits.length > 0}
        aria-controls="tm-search-list"
        aria-autocomplete="list"
        onChange={(e) => { dispatch({ type: ACTIONS.QUERY_SET, query: e.target.value }); setOpen(true); setActive(0); }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={onKey}
      />
      {open && q && (
        <div className="tm-pop tm-search-pop" id="tm-search-list" role="listbox">
          {hits.length === 0 && <div className="tm-pop-empty">Ничего не найдено</div>}
          {hits.map((n, i) => (
            <button
              type="button"
              role="option"
              aria-selected={i === active}
              key={n.id}
              className={`tm-hit${i === active ? ' is-active' : ''}`}
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActive(i)}
              onClick={() => pick(n)}
            >
              <NodeTile node={n} size={24} icon={14} />
              <span className="tm-hit-text">
                <span className="tm-hit-name">{n.name}</span>
                <span className="tm-hit-sub">
                  {n.ip && <span className="tm-mono">{n.ip}</span>}
                  {groupName(n) && <span> · в группе {groupName(n)}</span>}
                  {n.site_name && model.site.slug === '*' && <span> · {n.site_name}</span>}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
