// Модель данных карты: индексы поверх ответа /map/data.json.
//
// Две отдельные сущности, как в NetBox и в синке:
//   кабель (cables[]) — физический факт «интерфейс ↔ интерфейс» со своим
//     владельцем (owner: ours | edited | manual), состоянием и доказательством;
//   связь (links[])   — логическое «устройство ↔ устройство» (агрегат/бонд —
//     одна связь), собранная из кабелей.
// Ручное создание/удаление кабеля (следующий шаг) меняет только cables[] и
// пересобирает затронутую связь — см. upsertCable/removeCable ниже: они уже
// работают с этой моделью, но из интерфейса пока не вызываются.

import { SOURCE_FALLBACK, worstState, kindOf, familyOf } from '../constants.js';
import { natCmp } from '../format.js';

function ownerOfCables(cables) {
  if (cables.some((c) => c.owner === 'manual')) return 'manual';
  if (cables.some((c) => c.owner === 'edited')) return 'edited';
  return 'ours';
}

function decorateLink(link, cablesById) {
  const cables = (link.cables || []).map((id) => cablesById.get(id)).filter(Boolean);
  const gaps = Array.isArray(link.lag_gaps) ? link.lag_gaps : [];
  let partial = null;
  if (link.state === 'partial' && link.members_expected > link.members_proven) {
    partial = {
      text: `${link.members_proven} из ${link.members_expected}`,
      hint: `Кабелем доказано ${link.members_proven} из ${link.members_expected} членов агрегата`,
    };
  }
  if (gaps.length) {
    const g = gaps[0];
    partial = {
      text: `${g.cabled} из ${g.members}`,
      hint: gaps.map((x) => `у ${x.lag} кабелем доказан ${x.cabled} из ${x.members}, без кабеля: ${(x.without_cable || []).join(', ') || '—'}`).join('; '),
    };
  }
  const editors = [...new Set(cables.flatMap((c) => c.editors || []))];
  return {
    ...link,
    lag_gaps: gaps,
    _cables: cables,
    _owner: ownerOfCables(cables),
    _editors: editors,
    _partial: partial,
    _self: link.a.node === link.b.node,
    _lag: !!link.is_lag || (link.members_expected || 1) > 1 || cables.length > 1,
  };
}

export function buildModel(raw) {
  const nodes = (raw.nodes || []).map((n) => ({
    ...n,
    kind: kindOf(n.kind),
    family: familyOf(n.family, n.kind),
    band: Math.max(0, Math.min(4, Number.isInteger(n.band) ? n.band : 4)),
  }));
  const nodesById = new Map(nodes.map((n) => [n.id, n]));

  const groups = (raw.groups || []).map((g) => ({
    ...g,
    kind: kindOf(g.kind),
    family: familyOf(g.family, g.kind),
    band: Math.max(0, Math.min(4, Number.isInteger(g.band) ? g.band : 4)),
    members: (g.members || []).filter((id) => nodesById.has(id)),
  })).filter((g) => g.members.length > 0);
  const groupsById = new Map(groups.map((g) => [g.id, g]));
  const groupOf = new Map();
  groups.forEach((g) => g.members.forEach((m) => groupOf.set(m, g.id)));
  groups.forEach((g) => g.members.sort((a, b) => natCmp(nodesById.get(a).name, nodesById.get(b).name)));

  const cables = (raw.cables || []).filter((c) => c && c.a && c.b);
  const cablesById = new Map(cables.map((c) => [c.id, c]));

  const links = (raw.links || [])
    .filter((l) => l && l.a && l.b && nodesById.has(l.a.node) && nodesById.has(l.b.node))
    .map((l) => decorateLink(l, cablesById));
  const linksById = new Map(links.map((l) => [l.id, l]));

  const linksByNode = new Map();
  links.forEach((l) => {
    [l.a.node, l.b.node].forEach((n, i) => {
      if (i === 1 && l._self) return;
      if (!linksByNode.has(n)) linksByNode.set(n, []);
      linksByNode.get(n).push(l.id);
    });
  });

  const cablesByNode = new Map();
  cables.forEach((c) => {
    [c.a.node, c.b.node].forEach((n, i) => {
      if (!n || (i === 1 && c.a.node === c.b.node)) return;
      if (!cablesByNode.has(n)) cablesByNode.set(n, []);
      cablesByNode.get(n).push(c.id);
    });
  });

  const sources = (Array.isArray(raw.sources) && raw.sources.length ? raw.sources : SOURCE_FALLBACK)
    .map((s) => ({ links: 0, cables: 0, ...s }));
  const sourcesByKey = new Map(sources.map((s) => [s.key, s]));

  return {
    raw,
    site: raw.site || { slug: '*', name: 'Все площадки' },
    collectedAt: raw.collected_at || null,
    partialRun: !!raw.partial_run,
    bands: Array.isArray(raw.bands) && raw.bands.length ? raw.bands : ['Граница', 'Защита', 'Ядро', 'Доступ', 'Серверы'],
    sources,
    sourcesByKey,
    nodes,
    nodesById,
    groups,
    groupsById,
    groupOf,
    links,
    linksById,
    linksByNode,
    cables,
    cablesById,
    cablesByNode,
    summary: raw.summary || {},
  };
}

export const sourceLabel = (model, key) => model.sourcesByKey.get(key)?.label || key || '—';

// Состояние кабеля как в NetBox-синке → худшее у набора кабелей.
export const cablesWorst = (cables) => worstState(cables.map((c) => c.state || 'confirmed'));

// ---- На вырост: ручные кабели (не вызывается из UI) ----------------------
// Новый/изменённый кабель кладётся в cables[], связь пересобирается сервером;
// здесь — только локальное отражение ответа API до перезапроса data.json.
export function upsertCable(model, cable) {
  const raw = { ...model.raw, cables: [...model.raw.cables.filter((c) => c.id !== cable.id), cable] };
  return buildModel(raw);
}
export function removeCable(model, cableId) {
  const raw = {
    ...model.raw,
    cables: model.raw.cables.filter((c) => c.id !== cableId),
    links: model.raw.links
      .map((l) => ({ ...l, cables: (l.cables || []).filter((id) => id !== cableId) }))
      .filter((l) => l.cables.length > 0),
  };
  return buildModel(raw);
}
