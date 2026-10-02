// Связка: все связи членов свёрнутой группы с одним внешним устройством.
import { useMap, ACTIONS } from '../state/store.js';
import { pl, endShort } from '../format.js';
import { sourceLabel } from '../state/model.js';
import { PanelHead, LineSample, Sec } from './common.jsx';
import { StateMark, OwnerMark } from '../edges/EdgeMarks.jsx';
import { IconArrow } from '../components/Icons.jsx';
import { linkTitle } from './LinkCard.jsx';

export default function BundleCard({ id, edge }) {
  const { model, dispatch } = useMap();
  const [s, t] = id.slice(2).split('|');
  const name = (x) => model.groupsById.get(x)?.title || model.nodesById.get(x)?.name || x;
  const links = edge?.links || [];
  const cables = links.reduce((acc, l) => acc + l._cables.length, 0);
  const groups = [s, t].filter((x) => model.groupsById.has(x));
  return (
    <div className="tm-card-panel">
      <PanelHead title={s === t ? `${name(s)} — петли` : `${name(s)} — ${name(t)}`} sub={`${pl(links.length, 'link')} · ${pl(cables, 'cable')}`} />
      <div className="tm-actions">
        {groups.map((g) => (
          <button type="button" key={g} className="tm-btn" onClick={() => dispatch({ type: ACTIONS.GROUP_TOGGLE, id: g })}>
            Развернуть {model.groupsById.get(g).title}
          </button>
        ))}
      </div>
      <Sec count={links.length} style={{ padding: '0 20px 6px' }}>Связи в связке</Sec>
      <div className="tm-conn-list">
        {links.map((l) => (
          <button type="button" key={l.id} className="tm-conn" onClick={() => dispatch({ type: ACTIONS.SELECT, selection: { type: 'link', id: l.id } })}>
            <span className="tm-conn-top">
              <span className="tm-mono tm-conn-peer">{linkTitle(model, l)}</span>
              <StateMark state={l.state} />
            </span>
            <span className="tm-conn-sub">
              <LineSample source={l.source} width={24} lag={l._lag && !l._self} state={l.state} />
              {sourceLabel(model, l.source)}
              <span className="tm-mono tm-muted"> · {endShort(l.a)}</span>
              <IconArrow size={11} style={{ color: 'var(--text-faint)' }} />
              <span className="tm-mono tm-muted">{endShort(l.b)}</span>
              {l._partial && <span className="tm-tag is-partial tm-mono" title={l._partial.hint}>{l._partial.text}</span>}
              <OwnerMark owner={l._owner} />
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
