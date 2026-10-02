// Итоговые позиции на полотне.
//
// «По ролям» — всё из computeRolesLayout. «Свободно» — сохранённые позиции
// пользователя (layout.json), а для того, чего в сохранённом нет (новые
// устройства, впервые развёрнутая группа) — позиция «По ролям» / сетка вокруг
// места группы. Физический слой берёт те же позиции, растягивает по ширине
// под карточки и раздвигает пересечения сверху вниз.

import {
  DEV_W, DEV_H, GROUP_HEAD, GROUP_PAD, PHYS_SCALE_X, CARD_W,
} from '../constants.js';
import { blockGrid, gridOffsets } from './roles.js';

export function effectivePositions(model, vis, roles, mode, saved) {
  if (mode !== 'free') return roles.pos;
  const pos = {};
  const inExpanded = (id) => {
    const g = model.groupOf.get(id);
    return g && vis.groupVisible(g) && vis.isExpanded(g);
  };
  vis.devices.forEach((n) => {
    if (inExpanded(n.id)) return;
    pos[n.id] = saved[n.id] || roles.pos[n.id] || [0, 0];
  });
  vis.collapsed.forEach((g) => { pos[g.id] = saved[g.id] || roles.pos[g.id] || [0, 0]; });
  vis.expanded.forEach((g) => {
    const members = vis.groupMembers.get(g.id);
    const anchor = saved[g.id];
    const grid = blockGrid(members.length);
    let k = 0;
    members.forEach((m) => {
      if (saved[m]) { pos[m] = saved[m]; return; }
      if (anchor) {
        const [ox, oy] = gridOffsets(k, grid.cols);
        k += 1;
        pos[m] = [anchor[0] + DEV_W / 2 - grid.w / 2 + ox, anchor[1] + oy];
        return;
      }
      pos[m] = roles.pos[m] || [0, 0];
    });
  });
  return pos;
}

// Рамка развёрнутой группы по фактическим позициям членов.
export function groupFrame(members, pos, sizeOf) {
  let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
  members.forEach((m) => {
    const p = pos[m];
    if (!p) return;
    const [w, h] = sizeOf(m);
    minX = Math.min(minX, p[0]); minY = Math.min(minY, p[1]);
    maxX = Math.max(maxX, p[0] + w); maxY = Math.max(maxY, p[1] + h);
  });
  if (!Number.isFinite(minX)) return null;
  return {
    x: minX - GROUP_PAD,
    y: minY - GROUP_HEAD,
    w: maxX - minX + GROUP_PAD * 2,
    h: maxY - minY + GROUP_HEAD + GROUP_PAD,
  };
}

const logicalSize = () => [DEV_W, DEV_H];
export { logicalSize };

// ---- Физический слой ---------------------------------------------------

const MARGIN = 28;
const ROW_MARGIN = 48;

// Ряды логического слоя (близкие по y) остаются рядами; высота ряда — по
// самой высокой карточке, внутри ряда карточки раздвигаются вправо и ряд
// центрируется на прежнем месте.
function rowLayout(units) {
  const sorted = [...units].sort((a, b) => a.y - b.y || a.x - b.x);
  const rows = [];
  sorted.forEach((u) => {
    const row = rows[rows.length - 1];
    if (!row || u.y - row.y0 > 40) rows.push({ y0: u.y, items: [u] });
    else row.items.push(u);
  });
  let cursor = rows.length ? rows[0].y0 : 0;
  rows.forEach((row) => {
    row.items.sort((a, b) => a.x - b.x);
    const c0 = (row.items[0].x + row.items[row.items.length - 1].x + row.items[row.items.length - 1].w) / 2;
    for (let i = 1; i < row.items.length; i += 1) {
      const p = row.items[i - 1];
      if (row.items[i].x < p.x + p.w + MARGIN) row.items[i].x = p.x + p.w + MARGIN;
    }
    const last = row.items[row.items.length - 1];
    const shift = (row.items[0].x + last.x + last.w) / 2 - c0;
    row.items.forEach((u) => { u.x -= shift; u.y = cursor; });
    cursor += Math.max(...row.items.map((u) => u.h)) + ROW_MARGIN;
  });
  return units;
}

/**
 * @param logicalPos  позиции логического слоя
 * @param cardSize    (id) => [w, h] карточки физического слоя
 */
export function physicalPositions(model, vis, logicalPos, cardSize) {
  const sx = PHYS_SCALE_X;
  const sy = 1.25;
  const units = [];
  const inExpanded = (id) => {
    const g = model.groupOf.get(id);
    return g && vis.groupVisible(g) && vis.isExpanded(g);
  };
  vis.devices.forEach((n) => {
    if (inExpanded(n.id)) return;
    const p = logicalPos[n.id] || [0, 0];
    const [w, h] = cardSize(n.id);
    units.push({ id: n.id, x: (p[0] + DEV_W / 2) * sx - w / 2, y: p[1] * sy, w, h });
  });
  vis.collapsed.forEach((g) => {
    const p = logicalPos[g.id] || [0, 0];
    const [w, h] = cardSize(g.id);
    units.push({ id: g.id, x: (p[0] + DEV_W / 2) * sx - w / 2, y: p[1] * sy, w, h });
  });
  // Развёрнутая группа — блок карточек: колонки по сетке логического слоя,
  // высота ряда — по самой высокой карточке в нём.
  const blocks = [];
  vis.expanded.forEach((g) => {
    const members = [...vis.groupMembers.get(g.id)];
    members.sort((a, b) => {
      const pa = logicalPos[a] || [0, 0]; const pb = logicalPos[b] || [0, 0];
      return pa[1] - pb[1] || pa[0] - pb[0];
    });
    const cols = Math.max(1, Math.min(members.length, Math.ceil(Math.sqrt(members.length * 1.2))));
    const rowsH = [];
    members.forEach((m, i) => {
      const r = Math.floor(i / cols);
      rowsH[r] = Math.max(rowsH[r] || 0, cardSize(m)[1]);
    });
    const offs = {};
    let oy = GROUP_HEAD;
    rowsH.forEach((rh, r) => {
      for (let c = 0; c < cols; c += 1) {
        const m = members[r * cols + c];
        if (m) offs[m] = [GROUP_PAD + c * (CARD_W + 24), oy];
      }
      oy += rh + 20;
    });
    const w = GROUP_PAD * 2 + cols * CARD_W + (cols - 1) * 24;
    const h = oy - 20 + GROUP_PAD;
    let minX = Infinity; let minY = Infinity; let maxX = -Infinity;
    members.forEach((m) => {
      const p = logicalPos[m] || [0, 0];
      minX = Math.min(minX, p[0]); maxX = Math.max(maxX, p[0] + DEV_W); minY = Math.min(minY, p[1]);
    });
    const cx = ((minX + maxX) / 2) * sx;
    const u = { id: `block:${g.id}`, x: cx - w / 2, y: (minY - GROUP_HEAD) * sy, w, h };
    units.push(u);
    blocks.push({ u, offs });
  });

  rowLayout(units);
  const pos = {};
  units.forEach((u) => { if (!u.id.startsWith('block:')) pos[u.id] = [u.x, u.y]; });
  blocks.forEach(({ u, offs }) => {
    Object.entries(offs).forEach(([m, [ox, oy]]) => { pos[m] = [u.x + ox, u.y + oy]; });
  });
  return pos;
}
