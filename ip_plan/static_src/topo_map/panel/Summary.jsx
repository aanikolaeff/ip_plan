// Сводка (ничего не выбрано): счётчики, чем доказаны связи (фильтр по
// источникам), требует внимания, без связей, отброшено как сомнительное.

import { useState } from 'react';
import { useMap, ACTIONS } from '../state/store.js';
import { pl, plural, W, formatDate } from '../format.js';
import { LineSample, NodeTile, Sec } from './common.jsx';
import { IconWarn } from '../components/Icons.jsx';

const REJECTED_LIMIT = 10;

function cableTitle(model, c) {
  const a = model.nodesById.get(c.a.node)?.name || '?';
  const b = model.nodesById.get(c.b.node)?.name || '?';
  return `${a} ${c.a.interface || ''} ↔ ${b} ${c.b.interface || ''}`;
}

function Attention() {
  const { model, dispatch } = useMap();
  const s = model.summary;
  const conflicts = s.conflicts || [];
  const unconf = (s.unconfirmed || []).filter((c) => !conflicts.some((x) => x.id === c.id));
  const partial = s.partial_lags || [];
  const total = conflicts.length + unconf.length + partial.length;
  const goLink = (id) => id && dispatch({ type: ACTIONS.FOCUS, target: { type: 'link', id } });
  return (
    <div className="tm-pblock">
      <Sec count={total}>Требует внимания</Sec>
      {total === 0 && <span className="tm-note">Конфликтов, неподтверждённых кабелей и неполных агрегатов нет.</span>}
      {conflicts.map((c) => (
        <button type="button" key={`c${c.id}`} className="tm-alert is-danger" onClick={() => goLink(c.link_id)}>
          <span className="tm-alert-head"><span className="tm-mark is-conflict">!</span><span className="tm-mono">{cableTitle(model, c)}</span></span>
          <span className="tm-alert-text">
            Конфликт: порт занят другим соседом — {c.conflict?.observed_peer || '?'}{c.conflict?.observed_source ? ` (${c.conflict.observed_source})` : ''}
          </span>
        </button>
      ))}
      {unconf.map((c) => (
        <button type="button" key={`u${c.id}`} className="tm-alert" onClick={() => goLink(c.link_id)}>
          <span className="tm-alert-head"><span className="tm-mark is-unconf">?</span><span className="tm-mono">{cableTitle(model, c)}</span></span>
          <span className="tm-alert-text">
            Не подтверждается {c.unconfirmed?.runs || 0} {plural(c.unconfirmed?.runs || 0, W.run)} с {formatDate(c.unconfirmed?.since)}
          </span>
        </button>
      ))}
      {partial.map((p) => (
        <button type="button" key={`p${p.link}-${p.node}-${p.lag}`} className="tm-alert is-warn" onClick={() => goLink(p.link)}>
          <span className="tm-alert-head"><span className="tm-tag is-partial tm-mono">{p.proven} из {p.members}</span><span className="tm-mono">{p.device} {p.lag}</span></span>
          <span className="tm-alert-text">
            Агрегат собран не полностью{p.without_cable?.length ? `: без кабеля ${p.without_cable.join(', ')}` : ''}
          </span>
        </button>
      ))}
    </div>
  );
}

function Loose() {
  const { model, dispatch } = useMap();
  const loose = model.summary.loose || [];
  if (!loose.length) return null;
  return (
    <div className="tm-pblock">
      <Sec count={loose.length}>Без связей</Sec>
      {loose.map((u) => {
        const node = model.nodesById.get(u.node);
        return (
          <button
            type="button"
            key={u.node || u.name}
            className="tm-loose"
            onClick={() => node && dispatch({ type: ACTIONS.FOCUS, target: { type: 'node', id: node.id } })}
          >
            <NodeTile node={node || { kind: u.kind || 'unknown', family: 'oth' }} size={30} icon={16} />
            <span className="tm-loose-text">
              <span className="tm-mono tm-loose-name">{u.name}</span>
              <span className="tm-muted">{u.reason}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function Rejected() {
  const { model } = useMap();
  const [all, setAll] = useState(false);
  const list = model.summary.rejected || [];
  if (!list.length) return null;
  const shown = all ? list : list.slice(0, REJECTED_LIMIT);
  return (
    <div className="tm-pblock">
      <Sec count={list.length}>Отброшено как сомнительное</Sec>
      <span className="tm-note">Связь могла быть, но доказательство неоднозначное — кабель не заведён.</span>
      {shown.map((r, i) => (
        <div key={`${r.where}-${i}`} className="tm-rej">
          <span className="tm-mono tm-rej-where">{r.where}{r.count > 1 ? <span className="tm-faint"> · {pl(r.count, 'item')}</span> : null}</span>
          <span className="tm-muted">{r.reason}</span>
        </div>
      ))}
      {list.length > REJECTED_LIMIT && (
        <button type="button" className="tm-link-btn" onClick={() => setAll(!all)}>
          {all ? 'Свернуть' : `ещё ${list.length - REJECTED_LIMIT}`}
        </button>
      )}
    </div>
  );
}

function Sources({ physical }) {
  const { model, state, dispatch } = useMap();
  const hidden = new Set(state.hiddenSources);
  return (
    <div className="tm-sources">
      {model.sources.map((s) => {
        const off = hidden.has(s.key);
        const n = physical ? s.cables : s.links;
        return (
          <button
            type="button"
            key={s.key}
            className={`tm-src${off ? ' is-off' : ''}${!n ? ' is-empty' : ''}`}
            aria-pressed={!off}
            title={off ? 'Показать на карте' : 'Скрыть на карте'}
            onClick={() => dispatch({ type: ACTIONS.SOURCE_TOGGLE, key: s.key })}
          >
            <LineSample source={s.key} />
            <span className="tm-src-text">
              <span className="tm-src-label">{s.label}</span>
              <span className="tm-src-hint">{s.hint}</span>
            </span>
            <span className="tm-mono tm-src-n">{n || 0}</span>
          </button>
        );
      })}
    </div>
  );
}

function Counters({ physical }) {
  const { model } = useMap();
  const s = model.summary;
  const devices = s.devices ?? model.nodes.length;
  const links = s.links ?? model.links.length;
  const cables = s.cables ?? model.cables.length;
  const inLag = s.cables_in_lag ?? 0;
  const items = physical
    ? [[cables, plural(cables, W.cable)], [inLag, 'в агрегатах'], [Math.max(0, cables - inLag), plural(Math.max(0, cables - inLag), ['одиночный', 'одиночных', 'одиночных'])]]
    : [[devices, plural(devices, W.device)], [links, plural(links, W.link)], [cables, plural(cables, W.cable)]];
  const extra = [];
  if (s.manual) extra.push(`${s.manual} вручную`);
  if (s.edited) extra.push(`${s.edited} ${plural(s.edited, ['правлен', 'правлены', 'правлены'])} руками`);
  return (
    <>
      <div className="tm-stats">
        {items.map(([n, label]) => (
          <div key={label} className="tm-stat"><span className="tm-stat-n">{n}</span><span className="tm-stat-l">{label}</span></div>
        ))}
      </div>
      {extra.length > 0 && <span className="tm-note" style={{ padding: '0 20px 10px' }}>Кабели: {extra.join(', ')}.</span>}
    </>
  );
}

export default function Summary({ physical }) {
  const { model } = useMap();
  return (
    <div className="tm-summary">
      {model.partialRun && (
        <div className="tm-banner">
          <IconWarn />
          <span>Последний прогон топологии был частичным — часть связей могла не обновиться.</span>
        </div>
      )}
      <div className="tm-pblock is-top">
        <Sec>{physical ? 'Физический слой' : 'Логический слой'}</Sec>
        <span className="tm-note">
          {physical
            ? 'Каждый кабель отдельно, от порта к порту. Члены агрегата сгруппированы в карточке устройства.'
            : 'Одна линия — одна связь. Агрегат или бонд — двойная линия, кабели внутри — на физическом слое.'}
        </span>
      </div>
      <Counters physical={physical} />
      <div className="tm-pblock">
        <Sec>{physical ? 'Чем доказан кабель' : 'Чем доказаны связи'}</Sec>
        <span className="tm-note">Нажмите, чтобы скрыть или показать</span>
      </div>
      <Sources physical={physical} />
      <div className="tm-sep" />
      <Attention />
      <div className="tm-sep" />
      <Loose />
      {(model.summary.loose || []).length > 0 && <div className="tm-sep" />}
      <Rejected />
    </div>
  );
}
