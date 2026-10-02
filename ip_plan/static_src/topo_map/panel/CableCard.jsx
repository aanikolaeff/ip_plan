// Карточка кабеля (физический слой): концы порт-в-порт, к какой связи
// относится, соседи по агрегату, состояние, владелец, доказательство.

import { useMap, ACTIONS } from '../state/store.js';
import { pl, formatWhen } from '../format.js';
import { sourceLabel } from '../state/model.js';
import { PanelHead, NodeTile, Sec, ExtLink, cableStateLines, ownerText, StateLines, LineSample } from './common.jsx';
import { linkTitle } from './LinkCard.jsx';

export default function CableCard({ id }) {
  const { model, dispatch } = useMap();
  const c = model.cablesById.get(id);
  if (!c) return null;
  const link = model.linksById.get(c.link_id);
  const siblings = link ? link._cables.filter((x) => x.id !== c.id) : [];
  const sub = link && link._cables.length > 1
    ? `Член агрегата — один из ${pl(link._cables.length, 'cable')}`
    : 'Одиночный кабель';
  return (
    <div className="tm-card-panel">
      <PanelHead title={`Кабель #${c.id}`} sub={sub} />
      <div className="tm-ends">
        {[c.a, c.b].map((end, i) => {
          const node = model.nodesById.get(end.node);
          return (
            <div key={i} className="tm-end">
              <NodeTile node={node} />
              <span className="tm-end-text">
                <span className="tm-mono tm-end-name">{node?.name || '?'}</span>
                <span className="tm-muted"><span className="tm-mono" style={{ color: 'var(--text-main)' }}>{end.interface}</span> · {end.lag ? `член ${end.lag}` : 'без агрегата'}</span>
              </span>
            </div>
          );
        })}
      </div>
      <dl className="tm-dl">
        <dt>Чем доказан</dt>
        <dd className="tm-dd-flex" style={{ fontWeight: 600 }}><LineSample source={c.source} width={26} />{sourceLabel(model, c.source)}</dd>
        <dt>Логическая связь</dt>
        <dd className="tm-mono">{link ? linkTitle(model, link) : '—'}</dd>
        {siblings.length > 0 && (<>
          <dt>{siblings.length > 1 ? 'Другие кабели' : 'Второй кабель'}</dt>
          <dd className="tm-mono tm-dd-wrap">
            {siblings.map((x) => (
              <button type="button" key={x.id} className="tm-link-btn tm-mono" onClick={() => dispatch({ type: ACTIONS.SELECT, selection: { type: 'cable', id: x.id } })}>
                #{x.id} · {x.a.interface} — {x.b.interface}
              </button>
            ))}
          </dd>
        </>)}
        <dt>Записан</dt><dd>{c.created ? formatWhen(c.created) : '—'}</dd>
        {c.description && (<><dt>Описание</dt><dd>{c.description}</dd></>)}
      </dl>
      <div className="tm-pblock">
        <Sec>Состояние</Sec>
        <StateLines lines={cableStateLines(c, false)} />
        {c.conflict?.busy && <span className="tm-note">Занятый порт: <span className="tm-mono">{c.conflict.busy}</span></span>}
        <span className="tm-owner">{ownerText(c.owner, c.editors).replace('Заведена', 'Заведён').replace('правлена', 'правлен')}</span>
      </div>
      {(c.proof || []).length > 0 && (
        <div className="tm-pblock">
          <Sec>Чем доказано</Sec>
          <ol className="tm-proof">
            {c.proof.map((p, i) => <li key={i}><span className="tm-mono tm-faint">{i + 1}</span><span>{p}</span></li>)}
          </ol>
        </div>
      )}
      <div className="tm-actions" data-slot="cable-actions">
        {link && (
          <button
            type="button"
            className="tm-btn is-primary"
            onClick={() => dispatch({
              type: ACTIONS.LAYER_SET,
              layer: 'logical',
              selection: { type: 'link', id: link.id },
              focus: { type: 'link', id: link.id },
            })}
          >
            На логическом слое
          </button>
        )}
        <ExtLink href={c.netbox_url}>В NetBox</ExtLink>
      </div>
    </div>
  );
}
