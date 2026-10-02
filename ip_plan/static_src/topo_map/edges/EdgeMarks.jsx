// Метки посередине ребра: состояние (! конфликт, ? без подтверждения),
// агрегат «×N» / неполный «k из N», владелец «вручную»/«правлен».

export function StateMark({ state }) {
  if (state === 'conflict') return <span className="tm-mark is-conflict" title="Конфликт: порт занят другим соседом">!</span>;
  if (state === 'unconfirmed') return <span className="tm-mark is-unconf" title="Не подтверждается последними прогонами">?</span>;
  return null;
}

export function OwnerMark({ owner }) {
  if (owner === 'manual') return <span className="tm-tag is-owner" title="Кабель заведён вручную">вручную</span>;
  if (owner === 'edited') return <span className="tm-tag is-owner" title="Кабель синка правлен вручную">правлен</span>;
  return null;
}
