// Карточка устройства физического слоя: шапка и список портов с кабелями.
// У каждого порта два разъёма (слева/справа) — кабель цепляется к тому, что
// смотрит на соседа; второй невидим. Члены агрегата — под его заголовком.

import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';
import { KindIcon, IconChevronRight } from '../components/Icons.jsx';
import { useMap, ACTIONS } from '../state/store.js';
import { HEALTH } from '../constants.js';
import { pl } from '../format.js';

function PortRow({ row, side, lit }) {
  const peer = row.missing
    ? 'нет кабеля'
    : row.cables.map((c) => `${c.peerName} ${c.peerPort}`).join(', ');
  const cls = ['tm-port', row.inLag && 'in-lag', row.missing && 'is-missing', lit && 'is-lit'].filter(Boolean).join(' ');
  return (
    <div className={cls} title={row.missing ? `${row.name}: член ${row.lag}, кабель не найден` : `${row.name} → ${peer}`}>
      {row.inLag && <span className="tm-port-bar" />}
      <span className="tm-port-name tm-mono">{row.name}</span>
      <span className="tm-port-peer">{peer}</span>
      <Handle
        type="source"
        id={`${row.key}:L`}
        position={Position.Left}
        isConnectable={false}
        className={`tm-socket${side === 'L' ? ' is-used' : ''}${!side && !row.missing ? ' is-idle' : ''}`}
      />
      <Handle
        type="source"
        id={`${row.key}:R`}
        position={Position.Right}
        isConnectable={false}
        className={`tm-socket${side === 'R' ? ' is-used' : ''}${row.missing ? ' is-missing' : ''}${!side && !row.missing ? ' is-idle-r' : ''}`}
      />
    </div>
  );
}

function CardNode({ data }) {
  const { model } = useMap();
  const n = model.nodesById.get(data.nodeId);
  if (!n) return null;
  const litPorts = new Set(data.litPorts);
  const cls = ['tm-card', data.dim && 'is-dim', (data.selected || data.lit) && 'is-lit', data.selected && 'is-selected', data.match && 'is-match']
    .filter(Boolean).join(' ');
  return (
    <div className={cls}>
      <div className="tm-card-head">
        <span className={`tm-card-ico fam-${n.family}`}><KindIcon kind={n.kind} size={14} sw={1.75} /></span>
        <span className="tm-card-name">{n.name}</span>
        {data.otherSite && <span className="tm-site-pill is-inline">{data.otherSite}</span>}
        <span className="tm-card-health" style={{ background: HEALTH[n.health]?.color || 'var(--off)' }} title={n.health_text} />
      </div>
      <div className="tm-card-body">
        {data.rows.length === 0 && <div className="tm-port is-empty">Кабелей нет</div>}
        {data.rows.map((r) => (r.type === 'lag' ? (
          <div key={r.key} className="tm-lag-row">
            <span className="tm-mono">{r.name}</span>
            <span className="tm-lag-note">{r.note}</span>
            {r.partial && <span className="tm-tag is-partial" title="Не у всех членов агрегата есть кабель">{r.partial}</span>}
          </div>
        ) : (
          <PortRow key={r.key} row={r} side={data.sides[r.key]} lit={litPorts.has(r.key)} />
        )))}
      </div>
    </div>
  );
}

export const MemoCardNode = memo(CardNode);

function PhysGroupNode({ data }) {
  const { model, dispatch } = useMap();
  const g = model.groupsById.get(data.groupId);
  if (!g) return null;
  const cls = ['tm-card', 'tm-card-group', data.dim && 'is-dim', data.lit && 'is-lit', data.match && 'is-match'].filter(Boolean).join(' ');
  return (
    <div className={cls}>
      <div className="tm-card-head">
        <span className={`tm-card-ico fam-${g.family}`}><KindIcon kind={g.kind} size={14} sw={1.75} /></span>
        <span className="tm-card-name">{g.title}</span>
        <span className="tm-card-count">{data.count}</span>
        <Handle type="source" id="g:L" position={Position.Left} isConnectable={false} className="tm-socket is-used" />
        <Handle type="source" id="g:R" position={Position.Right} isConnectable={false} className="tm-socket is-used" />
      </div>
      <div className="tm-card-group-body">
        <span>{pl(data.count, 'host')} · порты скрыты</span>
        <button
          type="button"
          className="tm-link-btn nodrag"
          onClick={(e) => { e.stopPropagation(); dispatch({ type: ACTIONS.GROUP_TOGGLE, id: g.id }); }}
        >
          Развернуть<IconChevronRight size={12} />
        </button>
      </div>
    </div>
  );
}

export const MemoPhysGroupNode = memo(PhysGroupNode);
