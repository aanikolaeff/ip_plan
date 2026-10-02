// Свёрнутая группа хостов: одна плитка «BC01 · 10 хостов» со сводкой
// состояний членов. Клик (или ▸) — развернуть.

import { memo } from 'react';
import { Handle, Position, useStore } from '@xyflow/react';
import { KindIcon, IconChevronRight } from '../components/Icons.jsx';
import { useMap, ACTIONS } from '../state/store.js';
import { pl } from '../format.js';
import { zoomBucket } from './DeviceNode.jsx';

export function groupStats(model, g) {
  const health = { ok: 0, stale: 0, down: 0 };
  g.members.forEach((m) => {
    const h = model.nodesById.get(m)?.health;
    if (h in health) health[h] += 1;
  });
  let conflict = 0; let unconfirmed = 0; let partial = 0;
  const seen = new Set();
  g.members.forEach((m) => (model.linksByNode.get(m) || []).forEach((lid) => {
    if (seen.has(lid)) return;
    seen.add(lid);
    const l = model.linksById.get(lid);
    if (l.state === 'conflict') conflict += 1;
    else if (l.state === 'unconfirmed') unconfirmed += 1;
    if (l._partial) partial += 1;
  }));
  return { health, conflict, unconfirmed, partial };
}

function GroupNode({ data }) {
  const { model, dispatch } = useMap();
  const zb = useStore(zoomBucket);
  const g = model.groupsById.get(data.groupId);
  if (!g) return null;
  const st = groupStats(model, g);
  const cls = ['tm-dev', 'tm-group', data.dim && 'is-dim', data.match && 'is-match', `zoom-${zb}`].filter(Boolean).join(' ');
  return (
    <div className={cls} title={`${g.title}: ${pl(data.count, 'host')} — нажмите, чтобы развернуть`}>
      <Handle type="source" position={Position.Top} className="tm-handle-hidden" isConnectable={false} />
      <div className={`tm-tile tm-tile-stack fam-${g.family}`}>
        <KindIcon kind={g.kind} size={22} sw={1.6} />
        <span className="tm-count">{data.count}</span>
        {st.conflict > 0 && <span className="tm-tile-mark is-conflict" title={`Конфликтов: ${st.conflict}`}>!</span>}
        {!st.conflict && st.unconfirmed > 0 && <span className="tm-tile-mark is-unconf" title={`Без подтверждения: ${st.unconfirmed}`}>?</span>}
      </div>
      <button
        type="button"
        className="tm-expand nodrag"
        aria-label={`Развернуть группу ${g.title}`}
        title="Развернуть"
        onClick={(e) => { e.stopPropagation(); dispatch({ type: ACTIONS.GROUP_TOGGLE, id: g.id }); }}
      >
        <IconChevronRight />
      </button>
      <div className="tm-dev-text">
        <span className="tm-dev-name">{g.title} · {pl(data.count, 'host')}</span>
        <span className="tm-dev-sub tm-group-health">
          {st.health.ok > 0 && <span><i style={{ background: 'var(--ok)' }} />{st.health.ok}</span>}
          {st.health.stale > 0 && <span><i style={{ background: 'var(--warn)' }} />{st.health.stale}</span>}
          {st.health.down > 0 && <span><i style={{ background: 'var(--off)' }} />{st.health.down}</span>}
        </span>
      </div>
    </div>
  );
}

export default memo(GroupNode);
