"""Маршрутизатор баз: netobs (хранилище наблюдений netbox-sync) — только для
чтения сырым SQL из ip_plan/services/netobs_search.py (KTD11).

Схемой netobs владеет netbox-sync (netobs/migrations), моделей Django у неё
нет: миграции ip_plan идут только в default, запись — только в default.
db_for_write возвращает "default" явно, а не None: при None Django взял бы
базу из instance._state.db, и объект, прочитанный через .using("netobs"),
попытался бы сохраниться обратно туда."""


class NetobsRouter:
    def db_for_read(self, model, **hints):
        return None

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == "default"
