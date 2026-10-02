// Карточка связи: концы, кабели-члены, неполный агрегат, состояние и
// владелец, «Чем доказано» по шагам, ссылки на кабели в NetBox.
// Блок .tm-actions — место под будущие действия (удалить ручной кабель).

import { useMap, ACTIONS } from '../state/store.js';
import { endText, pl, plural, W, formatWhen } from '../format.js';
import { sourceLabel } from '../state/model.js';
import { PanelHead, NodeTile, LineSample, Sec, ExtLink, cableStateLines, ownerText, StateLines } from './common.jsx';
import { StateMark, OwnerMark } from '../edges/EdgeMarks.jsx';

export function linkTitle(model, l) {
  const a = model.nodesById.get(l.a.node)?.name || '?';
  const b = model.nodesById.get(l.b.node)?.name || '?';
  return l._self ? `${a} — сам с собой` : `${a} — ${b}`;
}

function mergedProof(cables) {
  const seen = new Set();
  const out = [];
  cables.forEach((c) => (c.proof || []).forEach((p) => {
    if (!seen.has(p)) { seen.add(p); out.push(p); }
  }));
  return out;
}

function linkStateLines(l) {
  const cables = l._cables;
  if (!cables.length) return [{ tone: 'muted', text: 'Кабели связи не найдены' }];
  if (cables.length === 1) return cableStateLines(cables[0], true);
  const bad = cables.filter((c) => c.conflict || c.unconfirmed);
  if (!bad.length) {
    return cables.every((c) => c.confirmed_last_run)
      ? [{ tone: 'ok', text: 'Подтверждена в последнем прогоне' }]
      : [{ tone: 'muted', text: 'Последний прогон проверил не все кабели связи' }];
  }
  return bad.flatMap((c) => cableStateLines(c, false).map((x) => ({ ...x, text: `#${c.id}: ${x.text}` })));
}

function linkOwnerLine(l) {
  const owners = new Set(l._cables.map((c) => c.owner || 'ours'));
  if (owners.size <= 1) return ownerText(l._owner, l._editors);
  return l._cables.map((c) => `#${c.id}: ${ownerText(c.owner, c.editors).toLowerCase()}`).join('; ');
}

export default function LinkCard({ id }) {
  const { model, dispatch } = useMap();
  const l = model.linksById.get(id);
  if (!l) return null;
  const an = model.nodesById.get(l.a.node);
  const bn = model.nodesById.get(l.b.node);
  const nCab = l._cables.length;
  const sub = l._lag && nCab > 1 ? `Агрегат из ${pl(nCab, 'cable')}` : (l._lag ? 'Агрегат · кабелем доказан один член' : 'Один кабель');
  const proof = mergedProof(l._cables);
  const created = l._cables.map((c) => c.created).filter(Boolean).sort()[0];
  const selectCable = (cid) => dispatch({ type: ACTIONS.SELECT, selection: { type: 'cable', id: cid } });

  return (
    <div className="tm-card-panel">
      <PanelHead title={linkTitle(model, l)} sub={sub} />
      <div className="tm-ends">
        {[[an, l.a], [bn, l.b]].map(([node, end], i) => (
          <div key={i} className="tm-end">
            <NodeTile node={node} />
            <span className="tm-end-text">
              <span className="tm-mono tm-end-name">{node?.name || '?'}</span>
              <span className="tm-mono tm-muted">{endText(end)}</span>
            </span>
          </div>
        ))}
      </div>

      {(l.lag_gaps.length > 0 || l.state === 'partial') && (
        <div className="tm-pblock">
          <Sec>Агрегат собран не полностью</Sec>
          {l.lag_gaps.map((g) => (
            <div key={`${g.node}-${g.lag}`} className="tm-warnbox">
              <span className="tm-mono tm-warnbox-title">{model.nodesById.get(g.node)?.name || '?'} {g.lag} · {g.cabled} из {g.members}</span>
              <span>
                Кабелем доказан{g.cabled === 1 ? '' : 'о'} {g.cabled} из {g.members} {plural(g.members, ['члена', 'членов', 'членов'])}.
                {g.without_cable?.length ? ` Без кабеля: ${g.without_cable.join(', ')} — куда идёт, неизвестно (часто ко второму коммутатору пары).` : ''}
              </span>
            </div>
          ))}
          {l.state === 'partial' && !l.lag_gaps.length && (
            <div className="tm-warnbox">
              <span>Кабелем доказано {l.members_proven} из {l.members_expected} членов агрегата.</span>
            </div>
          )}
        </div>
      )}

      <div className="tm-pblock">
        <Sec>Состояние</Sec>
        <StateLines lines={linkStateLines(l)} />
        <span className="tm-owner">{linkOwnerLine(l)}</span>
      </div>

      {nCab > 0 && (
        <div className="tm-pblock">
          <Sec count={nCab}>{nCab > 1 ? 'Кабели агрегата' : 'Кабель'}</Sec>
          <div className="tm-cable-list">
            {l._cables.map((c) => (
              <button type="button" key={c.id} className="tm-cable-row" onClick={() => selectCable(c.id)} title="Открыть карточку кабеля">
                <span className="tm-mono tm-faint">#{c.id}</span>
                <span className="tm-mono tm-cable-ports">{c.a.node === l.a.node ? `${c.a.interface} ↔ ${c.b.interface}` : `${c.b.interface} ↔ ${c.a.interface}`}</span>
                <span className="tm-cable-meta">
                  <LineSample source={c.source} width={20} state={c.state} />
                  <StateMark state={c.state} />
                  <OwnerMark owner={c.owner} />
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="tm-pblock">
        <Sec>Чем доказано</Sec>
        <span className="tm-proof-src">
          <LineSample source={l.source} width={30} />
          {sourceLabel(model, l.source)}
          {Object.keys(l.sources || {}).length > 1 && (
            <span className="tm-muted"> · {Object.entries(l.sources).map(([k, v]) => `${sourceLabel(model, k)} ${v}`).join(', ')}</span>
          )}
        </span>
        {proof.length === 0 && <span className="tm-note">{model.sourcesByKey.get(l.source)?.hint || 'Шаги доказательства не сохранены.'}</span>}
        <ol className="tm-proof">
          {proof.map((p, i) => <li key={i}><span className="tm-mono tm-faint">{i + 1}</span><span>{p}</span></li>)}
        </ol>
      </div>

      <dl className="tm-dl">
        <dt>В NetBox</dt>
        <dd className="tm-mono tm-dd-wrap">
          {l._cables.map((c, i) => (
            <span key={c.id}>{i > 0 && ', '}<a href={c.netbox_url} target="_blank" rel="noopener noreferrer">#{c.id}</a></span>
          ))}
        </dd>
        <dt>Записан</dt><dd>{created ? formatWhen(created) : '—'}</dd>
      </dl>
      {/* Место под действия со связью: «Удалить ручной кабель» и т.п. появятся здесь. */}
      <div className="tm-actions" data-slot="link-actions">
        {nCab === 1 && <ExtLink href={l._cables[0].netbox_url}>Кабель в NetBox</ExtLink>}
      </div>
    </div>
  );
}
