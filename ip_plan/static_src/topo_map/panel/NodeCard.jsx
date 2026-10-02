// Карточка устройства: что это, где, как выбрана иконка и все его связи.

import { useMap, ACTIONS } from '../state/store.js';
import { HEALTH, STATE_RANK, KINDS } from '../constants.js';
import { endText, endShort, natCmp } from '../format.js';
import { sourceLabel } from '../state/model.js';
import { PanelHead, NodeTile, LineSample, Sec, ExtLink } from './common.jsx';
import { IconArrow } from '../components/Icons.jsx';
import { StateMark, OwnerMark } from '../edges/EdgeMarks.jsx';

const KIND_FROM = {
  'устройство': 'из поля устройства «Иконка на карте»',
  'модель': 'из модели',
  'роль': 'из роли',
};

export default function NodeCard({ id }) {
  const { model, dispatch } = useMap();
  const n = model.nodesById.get(id);
  if (!n) return null;
  const gid = model.groupOf.get(n.id);
  const group = gid ? model.groupsById.get(gid) : null;
  const links = (model.linksByNode.get(n.id) || []).map((lid) => model.linksById.get(lid));
  const rows = links.map((l) => {
    const mineA = l.a.node === n.id;
    const mine = mineA ? l.a : l.b;
    const peer = mineA ? l.b : l.a;
    const peerNode = model.nodesById.get(peer.node);
    return { l, mine, peer, peerName: l._self ? 'сам с собой' : (peerNode?.name || '?') };
  }).sort((a, b) => (STATE_RANK[b.l.state] || 0) - (STATE_RANK[a.l.state] || 0) || natCmp(a.peerName, b.peerName));

  return (
    <div className="tm-card-panel">
      <PanelHead title={n.name} sub={n.model} mono icon={<NodeTile node={n} size={44} icon={22} />} />
      <dl className="tm-dl">
        <dt>Роль</dt><dd>{n.role_title || n.role || '—'}</dd>
        <dt>Адрес</dt><dd className="tm-mono">{n.ip || '—'}</dd>
        <dt>Площадка</dt><dd>{n.site_name || n.site || '—'}</dd>
        {(group || n.cluster) && (<><dt>{group ? 'Группа' : 'Кластер'}</dt><dd>{group ? group.title : n.cluster}</dd></>)}
        <dt>Состояние</dt>
        <dd className="tm-dd-flex"><span className="tm-dot" style={{ background: HEALTH[n.health]?.color || 'var(--off)' }} />{n.health_text || HEALTH[n.health]?.label || '—'}</dd>
        <dt>Иконка</dt>
        <dd>{KINDS[n.kind]?.label} · <span className="tm-muted">{KIND_FROM[n.kind_from] || 'роль не задана — проставьте в NetBox'}</span></dd>
      </dl>
      <div className="tm-actions">
        <ExtLink href={n.netbox_url}>Открыть в NetBox</ExtLink>
        {group && (
          <button type="button" className="tm-btn" onClick={() => dispatch({ type: ACTIONS.GROUP_TOGGLE, id: group.id })}>
            Свернуть группу
          </button>
        )}
      </div>
      <div className="tm-sep" />
      <Sec count={rows.length} style={{ padding: '0 20px 6px' }}>Соединения</Sec>
      {rows.length === 0 && <span className="tm-note" style={{ padding: '0 20px' }}>Кабелей в NetBox нет.</span>}
      <div className="tm-conn-list">
        {rows.map(({ l, mine, peer, peerName }) => (
          <button
            type="button"
            key={l.id}
            className="tm-conn"
            onClick={() => dispatch({ type: ACTIONS.SELECT, selection: { type: 'link', id: l.id } })}
          >
            <span className="tm-conn-top">
              <span className="tm-mono tm-muted">{mine.lag ? `${mine.lag} (${endText({ ports: mine.ports })})` : endText(mine)}</span>
              <IconArrow style={{ color: 'var(--text-faint)', flexShrink: 0 }} />
              <span className="tm-mono tm-conn-peer">{peerName}</span>
              <StateMark state={l.state} />
            </span>
            <span className="tm-conn-sub">
              <LineSample source={l.source} width={24} lag={l._lag && !l._self} state={l.state} />
              {sourceLabel(model, l.source)}
              {l._lag && l._cables.length > 1 ? ` · агрегат из ${l._cables.length}` : ''}
              {' · порт '}<span className="tm-mono tm-muted">{endShort(peer)}</span>
              {l._partial && <span className="tm-tag is-partial tm-mono" title={l._partial.hint}>{l._partial.text}</span>}
              <OwnerMark owner={l._owner} />
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
