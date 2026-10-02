// Легенда карты: семейства, линии по источнику (включая «MAC-таблица + ARP»
// и ручные), состояние связи, состояние устройства, иконки.

import { useMap, ACTIONS } from '../state/store.js';
import { FAMILY_ORDER, FAMILIES, KIND_ORDER, KINDS, SOURCE_FALLBACK } from '../constants.js';
import { KindIcon, IconClose } from './Icons.jsx';
import { LineSample } from '../panel/common.jsx';

function Row({ sample, label, hint }) {
  return (
    <div className="tm-lg-row">
      <span className="tm-lg-sample">{sample}</span>
      <span className="tm-lg-text"><b>{label}</b>{hint && <span>{hint}</span>}</span>
    </div>
  );
}

const Mini = ({ ring, dot, dim, pill }) => (
  <span className={`tm-lg-tile fam-net${ring ? ` ring-${ring}` : ''}`} style={{ opacity: dim ? 0.3 : 1 }}>
    <KindIcon kind="core" size={13} sw={1.8} />
    {dot && <i style={{ background: dot }} />}
    {pill && <em>{pill}</em>}
  </span>
);

export default function Legend() {
  const { model, dispatch } = useMap();
  const sources = model?.sources?.length ? model.sources : SOURCE_FALLBACK;
  return (
    <div className="tm-legend" role="dialog" aria-label="Легенда карты">
      <div className="tm-legend-head">
        <span className="tm-sec">Легенда</span>
        <button type="button" className="tm-icon-btn" aria-label="Закрыть легенду" onClick={() => dispatch({ type: ACTIONS.LEGEND_TOGGLE, open: false })}>
          <IconClose size={13} />
        </button>
      </div>
      <div className="tm-legend-body">
        <span className="tm-sec">Связь — чем доказана</span>
        {sources.map((s) => (
          <Row key={s.key} sample={<LineSample source={s.key} width={44} color="var(--tm-edge)" />} label={s.label} hint={s.hint} />
        ))}
        <Row sample={<LineSample source="lldp" width={44} lag color="var(--tm-edge)" />} label="Агрегат или бонд" hint="двойная линия, «×2» — число кабелей" />
        <Row sample={<LineSample source="lldp" width={44} color="var(--accent)" />} label="Выбранная" hint="связь выбранного устройства" />

        <span className="tm-sec">Состояние связи</span>
        <Row sample={<LineSample source="lldp" width={44} color="var(--tm-edge)" />} label="Подтверждена" hint="последний прогон видит этот кабель" />
        <Row sample={<span className="tm-lg-combo"><LineSample source="lldp" width={30} state="unconfirmed" /><span className="tm-mark is-unconf">?</span></span>} label="Без подтверждения" hint="прогоны подряд её не видят" />
        <Row sample={<span className="tm-lg-combo"><LineSample source="lldp" width={30} state="conflict" /><span className="tm-mark is-conflict">!</span></span>} label="Конфликт" hint="порт занят другим соседом" />
        <Row sample={<span className="tm-tag is-partial tm-mono">1 из 2</span>} label="Агрегат неполный" hint="не у всех членов есть кабель" />
        <Row sample={<span className="tm-tag is-owner">вручную</span>} label="Заведена вручную" hint="кабель не от синка" />
        <Row sample={<span className="tm-tag is-owner">правлен</span>} label="Правлена вручную" hint="кабель синка изменён руками" />

        <span className="tm-sec">Устройство</span>
        <div className="tm-lg-grid">
          <span><Mini dot="var(--ok)" />Работает</span>
          <span><Mini dot="var(--warn)" />Данные устарели</span>
          <span><Mini dot="var(--off)" />Не отвечает</span>
          <span><Mini dot="var(--ok)" ring="sel" />Выбрано</span>
          <span><Mini dot="var(--ok)" ring="hit" />Найдено поиском</span>
          <span><Mini dot="var(--ok)" dim />Не связано с выбранным</span>
          <span><Mini dot="var(--ok)" pill="MDC" />Другая площадка</span>
          <span><span className="tm-lg-tile tm-tile-stack fam-cmp"><KindIcon kind="hypervisor" size={13} sw={1.8} /></span>Группа хостов</span>
        </div>
        <span className="tm-note">Цвет плитки — семейство, а не состояние.</span>

        <span className="tm-sec">Семейства и иконки</span>
        <div className="tm-lg-fams">
          {FAMILY_ORDER.map((f) => <span key={f}><i className={`tm-fam-swatch fam-${f}`} />{FAMILIES[f].label}</span>)}
        </div>
        <div className="tm-lg-icons">
          {KIND_ORDER.map((k) => (
            <span key={k} title={KINDS[k].label}>
              <span className={`tm-lg-tile fam-${KINDS[k].family}${k === 'unknown' ? ' is-unknown' : ''}`}><KindIcon kind={k} size={13} sw={1.8} /></span>
              {KINDS[k].label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
