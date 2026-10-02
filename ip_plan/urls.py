from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import path

from . import views_map, views, views_obs, views_settings

app_name = "ip_plan"

urlpatterns = [
    # login_not_required — единственное исключение из LoginRequiredMiddleware
    # (см. config/settings.py); без него вход на страницу логина сам
    # требовал бы предварительного логина.
    path(
        "login/",
        login_not_required(auth_views.LoginView.as_view(template_name="ip_plan/login.html")),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/", views.password_change, name="password_change"),
    path("", views.ip_list, name="ip_list"),
    path("ip/list.json", views.ip_list_json, name="ip_list_json"),
    path("csrf/", views.csrf_refresh, name="csrf_refresh"),
    path("help/", views.help_modal, name="help_modal"),
    path("sync-log/summary.json", views.sync_log_summary, name="sync_log_summary"),
    path("sync-log/modal/", views.sync_log_modal, name="sync_log_modal"),
    path("os-catalog/", views.os_catalog_manage, name="os_catalog_manage"),
    path("os-catalog/<str:category>/<str:type_>/entries/", views.os_catalog_entries, name="os_catalog_entries"),
    path(
        "os-catalog/<str:category>/<str:type_>/entries/add/",
        views.os_catalog_entry_form, name="os_catalog_entry_add_form",
    ),
    path(
        "os-catalog/<str:category>/<str:type_>/entries/add/save/",
        views.os_catalog_entry_save, name="os_catalog_entry_add_save",
    ),
    path(
        "os-catalog/<str:category>/<str:type_>/entries/<str:entry_id>/edit/",
        views.os_catalog_entry_form, name="os_catalog_entry_edit_form",
    ),
    path(
        "os-catalog/<str:category>/<str:type_>/entries/<str:entry_id>/save/",
        views.os_catalog_entry_save, name="os_catalog_entry_edit_save",
    ),
    path(
        "os-catalog/<str:category>/<str:type_>/entries/<str:entry_id>/delete/",
        views.os_catalog_entry_delete, name="os_catalog_entry_delete",
    ),
    path("export/", views.export_excel, name="export_excel"),

    # --- Раздел "Настройки" (ip_plan/views_settings.py) ---
    path("settings/", views_settings.settings_page, name="settings"),
    path("settings/tab/<str:tab>/", views_settings.settings_page, name="settings_tab"),
    path("settings/export/", views_settings.export_confirm, name="settings_export_confirm"),
    path("settings/export/download/", views_settings.export_download, name="settings_export_download"),
    path("settings/import/", views_settings.import_form, name="settings_import_form"),
    path("settings/import/preview/", views_settings.import_preview, name="settings_import_preview"),
    path("settings/import/apply/", views_settings.import_apply, name="settings_import_apply"),
    path("settings/test/<str:kind>/<path:key>/start/", views_settings.test_start, name="settings_test_start"),
    path("settings/test/<str:kind>/<path:key>/status/", views_settings.test_status, name="settings_test_status"),
    path("settings/test/<str:kind>/<path:key>/terminal/", views_settings.test_terminal, name="settings_test_terminal"),
    path("settings/test/<str:kind>/<path:key>/terminal/body/", views_settings.test_terminal_body, name="settings_test_terminal_body"),
    path("settings/devices/flags/", views_settings.device_flags, name="settings_device_flags"),
    path("settings/devices/add/", views_settings.device_form, name="settings_device_add"),
    path("settings/devices/add/save/", views_settings.device_save, name="settings_device_add_save"),
    path("settings/devices/<str:name>/edit/", views_settings.device_form, name="settings_device_edit"),
    path("settings/devices/<str:name>/save/", views_settings.device_save, name="settings_device_save"),
    path("settings/devices/<str:name>/delete/", views_settings.device_delete, name="settings_device_delete"),
    path("settings/models/add/", views_settings.device_type_form, name="settings_type_add"),
    path("settings/models/add/save/", views_settings.device_type_save, name="settings_type_add_save"),
    path("settings/models/<str:name>/edit/", views_settings.device_type_form, name="settings_type_edit"),
    path("settings/models/<str:name>/save/", views_settings.device_type_save, name="settings_type_save"),
    path("settings/models/<str:name>/delete/", views_settings.device_type_delete, name="settings_type_delete"),
    path("settings/sources/<str:kind>/add/", views_settings.source_form, name="settings_source_add"),
    path("settings/sources/<str:kind>/add/save/", views_settings.source_save, name="settings_source_add_save"),
    path("settings/sources/<str:kind>/<int:index>/edit/", views_settings.source_form, name="settings_source_edit"),
    path("settings/sources/<str:kind>/<int:index>/save/", views_settings.source_save, name="settings_source_save"),
    path("settings/sources/<str:kind>/<int:index>/delete/", views_settings.source_delete, name="settings_source_delete"),
    path("settings/scan/save/", views_settings.scan_save, name="settings_scan_save"),
    path("settings/access/save/", views_settings.ssh_access_save, name="settings_ssh_access_save"),
    path("settings/access/delete/", views_settings.ssh_access_delete, name="settings_ssh_access_delete"),
    path("settings/access/test/", views_settings.ssh_access_test, name="settings_ssh_access_test"),
    path("settings/environment/<str:env>/edit/", views_settings.environment_form, name="settings_env_edit"),
    path("settings/environment/<str:env>/save/", views_settings.environment_save, name="settings_env_save"),
    path("settings/ldap/save/", views_settings.ldap_save, name="settings_ldap_save"),
    path("settings/ldap/test/", views_settings.ldap_test, name="settings_ldap_test"),
    path("settings/ldap/status/", views_settings.ldap_status, name="settings_ldap_status"),
    path("settings/sync/state/", views_settings.sync_state, name="settings_sync_state"),
    path("settings/sync/run/", views_settings.sync_run, name="settings_sync_run"),
    path("settings/users/add/", views_settings.user_form, name="settings_user_add"),
    path("settings/users/add/save/", views_settings.user_save, name="settings_user_add_save"),
    path("settings/users/<int:user_id>/edit/", views_settings.user_form, name="settings_user_edit"),
    path("settings/users/<int:user_id>/save/", views_settings.user_save, name="settings_user_save"),
    path("settings/users/<int:user_id>/delete/", views_settings.user_delete, name="settings_user_delete"),
    path("site/select/", views.set_site, name="set_site"),
    path("acl/", views.acl_lookup, name="acl_lookup"),
    path("acl/where/", views.acl_where, name="acl_where"),
    path("acl/<str:device>/object-group/<str:name>/panel/", views.acl_group_panel, name="acl_group_panel"),
    path("acl/<str:device>/object-group/<str:name>/", views.acl_object_group_detail, name="acl_object_group_detail"),
    path("acl/<str:device>/history/", views.acl_history, name="acl_history"),
    path("acl/<str:device>/<str:acl_name>/rule/<str:rule_id>/<str:side>/panel/", views.acl_rule_nodes_panel, name="acl_rule_nodes_panel"),
    path("acl/<str:device>/<str:acl_name>/", views.acl_detail, name="acl_detail"),
    path("regulator/", views.regulator_list, name="regulator_list"),
    path("regulator/entries.json", views.regulator_entries_json, name="regulator_entries_json"),
    path("regulator/commands/", views.regulator_commands, name="regulator_commands"),
    path("map/", views_map.map_view, name="map_view"),
    path("obs/", views_obs.obs_search, name="obs_search"),
    path("map/data.json", views_map.map_data, name="map_data"),
    path("map/layout.json", views_map.map_layout, name="map_layout"),
    path("regulator/document/<int:document_id>/entries/", views.regulator_document_entries, name="regulator_document_entries"),
    path("regulator/document/add/", views.regulator_document_add, name="regulator_document_add"),
    path("regulator/document/<int:document_id>/", views.regulator_document_detail, name="regulator_document_detail"),
    path("regulator/document/<int:document_id>/edit/", views.regulator_document_edit, name="regulator_document_edit"),
    path("regulator/document/<int:document_id>/delete/", views.regulator_document_delete, name="regulator_document_delete"),
    path(
        "regulator/document/<int:document_id>/entry/quick-add/",
        views.regulator_entry_quick_add, name="regulator_entry_quick_add",
    ),
    # Раньше был ещё entry/csv-upload/ (одна колонка, один тип на весь
    # файл) — убран, общая загрузка в "Разборе документов" ниже
    # (ioc/parse/) уже покрывает CSV/.txt с автоопределением типа.
    path(
        "regulator/document/<int:document_id>/ioc/parse/",
        views.regulator_document_ioc_parse, name="regulator_document_ioc_parse",
    ),
    path(
        "regulator/document/<int:document_id>/ioc/save/",
        views.regulator_document_ioc_save, name="regulator_document_ioc_save",
    ),
    # Снятие/восстановление/удаление записи — только JSON (React-таблица,
    # static_src/regulator_table, и на вкладке «Записи», и на детальной
    # странице письма). HTML-версии с модалкой снятия и OOB-рендером всей
    # таблицы сервером убраны — той таблицы, для которой они существовали,
    # больше нет.
    path(
        "regulator/entry/<int:entry_id>/revoke.json",
        views.regulator_entry_revoke_api, name="regulator_entry_revoke_api",
    ),
    path(
        "regulator/entry/<int:entry_id>/reactivate.json",
        views.regulator_entry_reactivate_api, name="regulator_entry_reactivate_api",
    ),
    path(
        "regulator/entry/<int:entry_id>/delete.json",
        views.regulator_entry_delete_api, name="regulator_entry_delete_api",
    ),
    # Раньше был ещё GET regulator/firewall-commands/ (модалка) — команды
    # переехали на отдельную вкладку страницы (regulator_lookup.html),
    # рендерится вместе с остальной страницей при обычном GET /regulator/,
    # отдельный URL для GET больше не нужен (см. regulator_list /
    # _regulator_firewall_context).
    path(
        "regulator/firewall-commands/apply/",
        views.regulator_firewall_commands_apply, name="regulator_firewall_commands_apply",
    ),
    path("ip/add/", views.ip_add, name="ip_add"),
    path("ip/<int:ip_id>/edit/", views.ip_edit_modal, name="ip_edit_modal"),
    path("ip/<int:ip_id>/save/", views.ip_save, name="ip_save"),
    path("ip/<int:ip_id>/patch/", views.ip_patch, name="ip_patch"),
    path("ip/<int:ip_id>/delete/", views.ip_delete, name="ip_delete"),
    path("ip/<int:ip_id>/check/", views.ip_check_availability, name="ip_check_availability"),
    path("ip/<int:ip_id>/scan-ports/", views.ip_scan_ports, name="ip_scan_ports"),
    path("ip/<int:ip_id>/resolve-dns/", views.ip_resolve_dns, name="ip_resolve_dns"),
    path("ip/<int:ip_id>/console/", views.ip_console, name="ip_console"),
    path("ip/<int:ip_id>/connect/", views.ip_connect, name="ip_connect"),
]
