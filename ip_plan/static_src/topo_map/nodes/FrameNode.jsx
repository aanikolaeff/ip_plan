// Рамка развёрнутой группы: подложка под членами, заголовок и «Свернуть».
// Рамку можно тащить — вместе с ней едут члены (см. MapCanvas.onNodesChange).

import { memo } from 'react';
import { useMap, ACTIONS } from '../state/store.js';
import { KindIcon, IconCollapse } from '../components/Icons.jsx';
import { pl } from '../format.js';

function FrameNode({ data }) {
  const { model, dispatch } = useMap();
  const g = model.groupsById.get(data.groupId);
  if (!g) return null;
  return (
    <div className={`tm-frame${data.dim ? ' is-dim' : ''}${data.match ? ' is-match' : ''}`}>
      <div className="tm-frame-head">
        <span className={`tm-frame-ico fam-${g.family}`}><KindIcon kind={g.kind} size={13} sw={1.8} /></span>
        <span className="tm-frame-title">{g.title}</span>
        <span className="tm-frame-sub">{pl(data.count, 'host')}{g.how === 'upstream' ? ' за одним коммутатором' : ''}</span>
        <button
          type="button"
          className="tm-frame-btn nodrag"
          onClick={(e) => { e.stopPropagation(); dispatch({ type: ACTIONS.GROUP_TOGGLE, id: g.id }); }}
          title="Свернуть группу в одну плитку"
        >
          <IconCollapse />Свернуть
        </button>
      </div>
    </div>
  );
}

export default memo(FrameNode);
