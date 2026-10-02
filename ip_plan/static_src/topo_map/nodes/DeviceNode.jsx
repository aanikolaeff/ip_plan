// Плитка устройства (логический слой): иконка вида на фоне семейства, точка
// состояния, имя, адрес. Подробность зависит от масштаба: издалека — только
// плитка, вблизи — ещё модель и число связей.

import { memo } from 'react';
import { Handle, Position, useStore } from '@xyflow/react';
import { KindIcon } from '../components/Icons.jsx';
import { useMap } from '../state/store.js';
import { HEALTH } from '../constants.js';
import { pl } from '../format.js';

export const zoomBucket = (s) => {
  const z = s.transform[2];
  if (z < 0.3) return 0;
  if (z > 1.3) return 2;
  return 1;
};

function DeviceNode({ data }) {
  const { model } = useMap();
  const zb = useStore(zoomBucket);
  const n = model.nodesById.get(data.nodeId);
  if (!n) return null;
  const nLinks = (model.linksByNode.get(n.id) || []).length;
  const cls = [
    'tm-dev',
    data.dim && 'is-dim',
    data.selected && 'is-selected',
    data.match && 'is-match',
    `zoom-${zb}`,
  ].filter(Boolean).join(' ');
  return (
    <div className={cls} title={`${n.name}${n.ip ? ` · ${n.ip}` : ''}\n${n.role_title || n.role || ''}`}>
      <Handle type="source" position={Position.Top} className="tm-handle-hidden" isConnectable={false} />
      {data.otherSite && <span className="tm-site-pill" title={`Площадка ${data.otherSite}`}>{data.otherSite}</span>}
      <div className={`tm-tile fam-${n.family}${n.kind === 'unknown' ? ' is-unknown' : ''}`}>
        <KindIcon kind={n.kind} size={22} sw={1.6} />
        <span className="tm-health" style={{ background: HEALTH[n.health]?.color || 'var(--off)' }} title={n.health_text || HEALTH[n.health]?.label} />
      </div>
      <div className="tm-dev-text">
        <span className="tm-dev-name">{n.name}</span>
        <span className="tm-dev-sub tm-mono">{n.ip || n.model || '—'}</span>
        {zb === 2 && <span className="tm-dev-sub">{n.ip ? `${n.model || ''} · ` : ''}{pl(nLinks, 'link')}</span>}
      </div>
    </div>
  );
}

export default memo(DeviceNode);
