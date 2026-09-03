"""Справочник "Операционная система" — JSON-файл, не модель БД.

Лежит в admin/ — bind mount на хосте (см. docker-compose.yml). Дерево:
категория (windows/linux_western/linux_russian) -> тип (desktop/server) ->
список записей {id, name, versions, certification}. Категории и типы —
фиксированный набор узлов навигации (см. CATEGORIES/TYPES), не редактируются
пользователем; записи внутри каждого узла — полноценный CRUD через веб-форму
(см. views.py: os_catalog_manage/_entries/_entry_form/_entry_save/_entry_delete).
"""
import json
import uuid
from pathlib import Path

from django.conf import settings
from django.utils.safestring import mark_safe

CATALOG_PATH = Path(settings.BASE_DIR) / "admin" / "os_catalog.json"

CATEGORIES = ["linux_russian", "linux_western", "windows"]
CATEGORY_LABELS = {
    "linux_russian": "Linux (российский)",
    "linux_western": "Linux (западный)",
    "windows": "Windows",
}
TYPES = ["desktop", "server"]
TYPE_LABELS = {"desktop": "Десктоп", "server": "Сервер"}


def _empty_tree():
    return {cat: {t: [] for t in TYPES} for cat in CATEGORIES}


def load_tree():
    """Полное дерево {категория: {тип: [записи]}}. Недостающие/битые узлы
    дополняются пустыми списками — UI всегда видит все 6 фиксированных узлов."""
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    categories = ((raw or {}).get("os_directory") or {}).get("categories") or {}
    tree = _empty_tree()
    for cat in CATEGORIES:
        for t in TYPES:
            entries = (categories.get(cat) or {}).get(t) or []
            if isinstance(entries, list):
                tree[cat][t] = [e for e in entries if isinstance(e, dict) and e.get("name")]
    return tree


def save_tree(tree):
    """Атомарная запись (tmp-файл + rename)."""
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"os_directory": {"categories": tree}}
    tmp_path = CATALOG_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(CATALOG_PATH)


def get_entry(category, type_, entry_id):
    tree = load_tree()
    for entry in tree.get(category, {}).get(type_, []):
        if entry.get("id") == entry_id:
            return entry
    return None


def upsert_entry(category, type_, entry_id, name, versions, certification):
    """entry_id=None -> создать новую запись (новый id), иначе обновить
    существующую (или создать с этим id, если её почему-то не нашли)."""
    if category not in CATEGORIES or type_ not in TYPES:
        raise ValueError(f"Неизвестная категория/тип: {category}/{type_}")
    tree = load_tree()
    bucket = tree[category][type_]
    data = {
        "id": entry_id or uuid.uuid4().hex[:12],
        "name": name.strip(),
        "versions": [v.strip() for v in versions if v.strip()],
        "certification": (certification or "").strip(),
    }
    if entry_id:
        for i, e in enumerate(bucket):
            if e.get("id") == entry_id:
                bucket[i] = data
                break
        else:
            bucket.append(data)
    else:
        bucket.append(data)
    save_tree(tree)
    return data


def delete_entry(category, type_, entry_id):
    tree = load_tree()
    bucket = tree.get(category, {}).get(type_, [])
    tree[category][type_] = [e for e in bucket if e.get("id") != entry_id]
    save_tree(tree)


def grouped_entries():
    """{ярлык категории: [{"name": ..., "versions": [...]}, ...]} — для
    <select> с <optgroup> в таблице IP и каскадного выбора версии (см.
    app.js: startTomSelectEdit). desktop+server одной категории объединяются
    в одну группу; версии одноимённой записи из обоих узлов — суммируются."""
    tree = load_tree()
    grouped = {}
    for cat in CATEGORIES:
        by_name = {}
        for t in TYPES:
            for entry in tree[cat][t]:
                versions = by_name.setdefault(entry["name"], set())
                versions.update(entry.get("versions") or [])
        if by_name:
            grouped[CATEGORY_LABELS[cat]] = [
                {"name": name, "versions": sorted(versions)}
                for name, versions in sorted(by_name.items())
            ]
    return grouped


def flat_names(grouped):
    """Все имена без группировки — для datalist полной формы IP."""
    names = set()
    for entries in grouped.values():
        names.update(e["name"] for e in entries)
    return sorted(names)


def catalog_json(value):
    """JSON для встраивания в <script>...</script>. Экранируем "</", чтобы
    значение, содержащее буквально "</script>", не разорвало тег."""
    raw = json.dumps(value, ensure_ascii=False)
    return mark_safe(raw.replace("</", "<\\/"))
