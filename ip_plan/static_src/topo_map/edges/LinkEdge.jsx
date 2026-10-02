// Ребро логического слоя: одна связь (или связка связей свёрнутой группы).
// Штрих — по источнику доказательства; агрегат — двойная линия; конфликт —
// красная линия и «!»; без подтверждения — приглушённая пунктирная и «?».

import { memo } from 'react';
import { EdgeLabelRenderer, useInternalNode } from '@xyflow/react';
import { sourceStyle } from '../constants.js';
import { endShort, plural, W } from '../format.js';
import { useMap, ACTIONS } from '../state/store.js';
import { linkGeometry } from './geometry.js';
import { StateMark, OwnerMark } from './EdgeMarks.jsx';

function LinkEdge({ id, source, target, data }) {
  const { dispatch } = useMap();
  const s = useInternalNode(source);
  const t = useInternalNode(target);
  if (!s || !t) return null;
  const g = linkGeometry(s, t, data.parallel, data.self, data.offS, data.offT);
  const st = sourceStyle(data.source);
  const unconf = data.state === 'unconfirmed';
  const cls = [
    'tm-edge',
    data.lit && 'is-lit',
    data.dim && 'is-dim',
    data.state === 'conflict' && 'is-conflict',
    unconf && 'is-unconf',
    st.manual && 'is-manual',
  ].filter(Boolean).join(' ');

  let dash = st.dash;
  if (unconf && !dash) dash = '4 4';
  const w = data.kind === 'bundle' ? Math.min(3.4, st.w + 0.4 + data.count * 0.15) : st.w;

  const pick = (e) => {
    e.stopPropagation();
    dispatch(data.kind === 'bundle'
      ? { type: ACTIONS.SELECT, selection: { type: 'bundle', id } }
      : { type: ACTIONS.SELECT, selection: { type: 'link', id: data.linkIds[0] } });
  };

  // подпись: у подсвеченной связи — концы, иначе ×N / k из N / связка
  const parts = [];
  if (data.lit && data.ends) {
    const a = endShort(data.ends.a);
    const b = endShort(data.ends.b);
    parts.push(<span key="ends" className="tm-tag is-ends tm-mono">{g.aFirst ? `${a} · ${b}` : `${b} · ${a}`}</span>);
  }
  if (data.kind === 'bundle') {
    parts.push(<span key="n" className={`tm-tag${data.lit ? ' is-lit' : ''}`}>{data.self ? `петля ×${data.count}` : `×${data.count} ${plural(data.count, W.link)}`}</span>);
  } else if (data.partial) {
    parts.push(<span key="p" className="tm-tag is-partial tm-mono" title={data.partial.hint}>{data.partial.text}</span>);
  } else if (data.lag && data.members > 1 && !data.lit) {
    parts.push(<span key="x" className="tm-tag tm-mono">×{data.members}</span>);
  }
  if (data.kind === 'bundle' && data.partial) parts.push(<span key="bp" className="tm-tag is-partial" title={data.partial.hint}>неполный</span>);
  const mark = <StateMark key="s" state={data.state} />;
  const owner = <OwnerMark key="o" owner={data.owner} />;

  return (
    <>
      <g className={cls}>
        {data.lag ? (
          <>
            <path d={g.path} className="tm-edge-line" strokeWidth={data.lit ? 6.2 : 5.5} fill="none" strokeLinecap="round" />
            <path d={g.path} className="tm-edge-inner" strokeWidth={2} fill="none" strokeLinecap="round" />
          </>
        ) : (
          <path d={g.path} className="tm-edge-line" strokeWidth={data.lit ? w + 0.7 : w} strokeDasharray={dash || undefined} fill="none" strokeLinecap="round" />
        )}
        <path d={g.path} className="react-flow__edge-interaction" stroke="transparent" strokeWidth={16} fill="none" />
      </g>
      {(parts.length > 0 || data.state === 'conflict' || unconf || data.owner !== 'ours') && (
        <EdgeLabelRenderer>
          <div
            className={`tm-elabel nodrag nopan${data.dim ? ' is-dim' : ''}`}
            style={{ transform: `translate(-50%, -50%) translate(${g.mid[0]}px, ${g.mid[1]}px)` }}
            onClick={pick}
          >
            {mark}
            {parts}
            {owner}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export default memo(LinkEdge);
