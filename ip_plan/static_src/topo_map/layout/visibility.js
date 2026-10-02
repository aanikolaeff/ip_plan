// Что видно на полотне: фильтр «Роли» (скрытые виды) и свёрнутые группы.
//
// unitOf(node) — единица раскладки «По ролям»: группа (свёрнутая плитка или
//   развёрнутый блок) либо само устройство;
// repOf(node)  — что рисуется на месте устройства: свёрнутая группа или само
//   устройство (у развёрнутой группы члены видны по отдельности).

export function computeVisibility(model, expandedSet, hiddenKinds) {
  const nodeVisible = (id) => {
    const n = model.nodesById.get(id);
    return !!n && !hiddenKinds.has(n.kind);
  };
  const groupMembers = new Map();
  model.groups.forEach((g) => groupMembers.set(g.id, g.members.filter(nodeVisible)));
  const groupVisible = (gid) => (groupMembers.get(gid) || []).length > 0;
  const isExpanded = (gid) => expandedSet.has(gid);

  const unitOf = (id) => {
    if (!nodeVisible(id)) return null;
    const g = model.groupOf.get(id);
    if (g && groupVisible(g)) return g;
    return id;
  };
  const repOf = (id) => {
    if (!nodeVisible(id)) return null;
    const g = model.groupOf.get(id);
    if (g && groupVisible(g) && !isExpanded(g)) return g;
    return id;
  };

  const devices = [];       // устройства, видимые отдельной плиткой
  const collapsed = [];     // свёрнутые группы
  const expanded = [];      // развёрнутые группы
  model.groups.forEach((g) => {
    if (!groupVisible(g.id)) return;
    if (isExpanded(g.id)) expanded.push(g);
    else collapsed.push(g);
  });
  model.nodes.forEach((n) => {
    if (!nodeVisible(n.id)) return;
    const g = model.groupOf.get(n.id);
    if (g && groupVisible(g) && !isExpanded(g)) return;
    devices.push(n);
  });

  return { nodeVisible, groupVisible, groupMembers, isExpanded, unitOf, repOf, devices, collapsed, expanded };
}
