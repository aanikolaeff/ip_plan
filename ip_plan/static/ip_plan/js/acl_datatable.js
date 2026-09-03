// Инициализация DataTables для acl_detail.html / acl_object_group_detail.html
// (таблица всех правил ACL / всех адресов object-group). Минимальная,
// самостоятельная — НЕ переиспользует сложную #ip-table инициализацию из
// app.js (fixed columns, kebab-меню, inline-редактирование): здесь просто
// таблица для чтения с пагинацией и built-in global-search.
//
// language{} — по умолчанию DataTables рисует "entries per page"/"Search:"/
// "Showing 1 to X of Y entries" по-английски, что выбивалось на фоне
// остального русского интерфейса; визуальное совпадение с остальным сайтом
// (pill-shaped input/select вместо сырого bootstrap form-control) — в
// style.css (.dt-container.dt-bootstrap5 ...).
function initAclDetailTable(tableSelector, loadingOverlayId) {
  document.addEventListener('DOMContentLoaded', function () {
    new DataTable(tableSelector, {
      pageLength: 25,
      lengthMenu: [10, 25, 50, 100, 500, 1000],
      language: {
        search: 'Поиск:',
        lengthMenu: 'Показывать _MENU_ записей',
        info: 'Показаны _START_–_END_ из _TOTAL_ записей',
        infoEmpty: 'Нет записей',
        infoFiltered: '(отфильтровано из _MAX_)',
        zeroRecords: 'Ничего не найдено',
        emptyTable: 'Нет данных для отображения',
        paginate: {
          first: 'Первая',
          previous: 'Пред.',
          next: 'След.',
          last: 'Последняя',
        },
      },
    });
    document.getElementById(loadingOverlayId)?.remove();
  });
  // Страховка на случай, если что-то помешает инициализации выше отработать.
  setTimeout(function () {
    document.getElementById(loadingOverlayId)?.remove();
  }, 8000);
}
