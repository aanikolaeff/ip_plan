// Полоса роли («Граница», «Защита», …) — фон раскладки «По ролям».
import { memo } from 'react';

function BandNode({ data }) {
  return (
    <div className={`tm-band${data.first ? ' is-first' : ''}`}>
      <span className="tm-band-label">{data.label}</span>
    </div>
  );
}

export default memo(BandNode);
