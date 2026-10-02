// Общие кусочки правой панели: шапка с «Закрыть», образец линии источника,
// плитка устройства, строки состояния кабеля/связи.

import { KindIcon, IconClose, IconExternal } from '../components/Icons.jsx';
import { sourceStyle } from '../constants.js';
import { formatDate, plural, W } from '../format.js';
import { useMap, ACTIONS } from '../state/store.js';

export function PanelHead({ title, sub, icon, mono }) {
  const { dispatch } = useMap();
  return (
    <div className="tm-ph">
      {icon}
      <span className="tm-ph-text">
        <span className={`tm-ph-title${mono ? ' tm-mono' : ''}`}>{title}</span>
        {sub && <span className="tm-ph-sub">{sub}</span>}
      </span>
      <button type="button" className="tm-icon-btn" aria-label="Закрыть" title="Закрыть (Esc)" onClick={() => dispatch({ type: ACTIONS.SELECT_CLEAR })}>
        <IconClose />
      </button>
    </div>
  );
}

export function LineSample({ source, width = 36, lag = false, color = 'var(--text-main)', state }) {
  const st = sourceStyle(source);
  const h = 10;
  const d = `M2 ${h / 2}h${width - 4}`;
  let stroke = st.manual ? 'var(--tm-edge-manual)' : color;
  let dash = st.dash;
  if (state === 'conflict') stroke = 'var(--danger)';
  if (state === 'unconfirmed') { stroke = 'var(--off)'; if (!dash) dash = '4 4'; }
  return (
    <svg width={width} height={h} viewBox={`0 0 ${width} ${h}`} aria-hidden="true" className="tm-line-sample">
      {lag ? (
        <>
          <path d={d} fill="none" stroke={stroke} strokeWidth={5.5} strokeLinecap="round" />
          <path d={d} fill="none" stroke="var(--tm-lag-inner)" strokeWidth={2} strokeLinecap="round" />
        </>
      ) : (
        <path d={d} fill="none" stroke={stroke} strokeWidth={st.w} strokeDasharray={dash || undefined} strokeLinecap="round" />
      )}
    </svg>
  );
}

export function Tile({ kind, family, size = 34, icon = 18, unknown }) {
  return (
    <span className={`tm-ptile fam-${family}${unknown ? ' is-unknown' : ''}`} style={{ width: size, height: size }}>
      <KindIcon kind={kind} size={icon} sw={1.6} />
    </span>
  );
}

export function NodeTile({ node, size, icon }) {
  if (!node) return <Tile kind="unknown" family="oth" size={size} icon={icon} />;
  return <Tile kind={node.kind} family={node.family} size={size} icon={icon} unknown={node.kind === 'unknown'} />;
}

export function Sec({ children, count, style }) {
  return (
    <span className="tm-sec" style={style}>
      {children}{count !== undefined ? ` · ${count}` : ''}
    </span>
  );
}

export function ExtLink({ href, children }) {
  if (!href) return null;
  return (
    <a className="tm-btn" href={href} target="_blank" rel="noopener noreferrer">
      {children}<IconExternal style={{ color: 'var(--text-faint)' }} />
    </a>
  );
}

// Состояние одного кабеля словами (п.2 контракта). fem — для связи.
export function cableStateLines(c, fem = true) {
  const out = [];
  if (c.conflict) {
    const peer = c.conflict.observed_peer || 'неизвестный сосед';
    const src = c.conflict.observed_source ? ` (${c.conflict.observed_source})` : '';
    out.push({ tone: 'danger', text: `Конфликт: прогон видит порт занятым другим соседом — ${peer}${src}` });
  }
  if (c.unconfirmed) {
    const runs = c.unconfirmed.runs || 0;
    out.push({ tone: 'warn', text: `Не подтверждается ${runs} ${plural(runs, W.run)} с ${formatDate(c.unconfirmed.since)}` });
  } else if (c.confirmed_last_run) {
    out.push({ tone: 'ok', text: fem ? 'Подтверждена в последнем прогоне' : 'Подтверждён в последнем прогоне' });
  } else if (!c.conflict) {
    out.push({ tone: 'muted', text: 'Последний прогон эту связь не проверял' });
  }
  return out;
}

export function ownerText(owner, editors) {
  if (owner === 'manual') return 'Заведена вручную';
  if (owner === 'edited') return editors && editors.length ? `Заведена синком, правлена: ${editors.join(', ')}` : 'Заведена синком, правлена вручную';
  return 'Заведена синком';
}

export function StateLines({ lines }) {
  return (
    <div className="tm-state-lines">
      {lines.map((l, i) => (
        <span key={i} className={`tm-state-line tone-${l.tone}`}>
          <i />{l.text}
        </span>
      ))}
    </div>
  );
}
