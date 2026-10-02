// Правая панель «Подробности»: режим по текущему выбору.
import { useMap } from '../state/store.js';
import Summary from './Summary.jsx';
import NodeCard from './NodeCard.jsx';
import LinkCard from './LinkCard.jsx';
import BundleCard from './BundleCard.jsx';
import CableCard from './CableCard.jsx';

export default function RightPanel({ logicalEdges }) {
  const { state, model } = useMap();
  const sel = state.selection;
  let body;
  if (!model) body = null;
  else if (sel?.type === 'node' && model.nodesById.has(sel.id)) body = <NodeCard key={sel.id} id={sel.id} />;
  else if (sel?.type === 'link' && model.linksById.has(sel.id)) body = <LinkCard key={sel.id} id={sel.id} />;
  else if (sel?.type === 'cable' && model.cablesById.has(sel.id)) body = <CableCard key={sel.id} id={sel.id} />;
  else if (sel?.type === 'bundle') {
    const edge = (logicalEdges || []).find((e) => e.id === sel.id);
    body = edge ? <BundleCard key={sel.id} id={sel.id} edge={edge} /> : <Summary physical={state.layer === 'physical'} />;
  } else body = <Summary physical={state.layer === 'physical'} />;
  return (
    <aside className="tm-panel" aria-label="Подробности">
      {body}
    </aside>
  );
}
