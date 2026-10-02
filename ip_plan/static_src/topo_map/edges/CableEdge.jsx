// Кабель физического слоя: порт → порт.
import { memo } from 'react';
import { EdgeLabelRenderer } from '@xyflow/react';
import { sourceStyle } from '../constants.js';
import { useMap, ACTIONS } from '../state/store.js';
import { cableGeometry } from './geometry.js';
import { StateMark, OwnerMark } from './EdgeMarks.jsx';

function CableEdge({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }) {
  const { dispatch } = useMap();
  const g = cableGeometry(sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, data.bulge);
  const st = sourceStyle(data.source);
  const unconf = data.state === 'unconfirmed';
  let dash = st.dash;
  if (unconf && !dash) dash = '4 4';
  const cls = ['tm-edge', 'tm-cable', data.lit && 'is-lit', data.dim && 'is-dim',
    data.state === 'conflict' && 'is-conflict', unconf && 'is-unconf', st.manual && 'is-manual'].filter(Boolean).join(' ');
  const showLabel = data.state === 'conflict' || unconf || data.owner !== 'ours';
  return (
    <>
      <g className={cls}>
        <path d={g.path} className="tm-edge-line" strokeWidth={data.lit ? 2.3 : 1.5} strokeDasharray={dash || undefined} fill="none" strokeLinecap="round" />
        <path d={g.path} className="react-flow__edge-interaction" stroke="transparent" strokeWidth={12} fill="none" />
      </g>
      {showLabel && (
        <EdgeLabelRenderer>
          <div
            className={`tm-elabel nodrag nopan${data.dim ? ' is-dim' : ''}`}
            style={{ transform: `translate(-50%, -50%) translate(${g.mid[0]}px, ${g.mid[1]}px)` }}
            onClick={(e) => { e.stopPropagation(); dispatch({ type: ACTIONS.SELECT, selection: { type: 'cable', id: data.cableId } }); }}
          >
            <StateMark state={data.state} />
            <OwnerMark owner={data.owner} />
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export default memo(CableEdge);
