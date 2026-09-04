from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import path

from . import views

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
    path("acl/", views.acl_lookup, name="acl_lookup"),
    path("acl/<str:device>/object-group/<str:name>/", views.acl_object_group_detail, name="acl_object_group_detail"),
    path("acl/<str:device>/<str:acl_name>/", views.acl_detail, name="acl_detail"),
    path("regulator/", views.regulator_list, name="regulator_list"),
    path("regulator/document/add/", views.regulator_document_add, name="regulator_document_add"),
    path("regulator/document/<int:document_id>/", views.regulator_document_detail, name="regulator_document_detail"),
    path(
        "regulator/document/<int:document_id>/entry/quick-add/",
        views.regulator_entry_quick_add, name="regulator_entry_quick_add",
    ),
    path("regulator/entry/<int:entry_id>/revoke/", views.regulator_entry_revoke, name="regulator_entry_revoke"),
    path("regulator/entry/<int:entry_id>/reactivate/", views.regulator_entry_reactivate, name="regulator_entry_reactivate"),
    path("regulator/entry/<int:entry_id>/delete/", views.regulator_entry_delete, name="regulator_entry_delete"),
    path("ip/add/", views.ip_add, name="ip_add"),
    path("ip/<int:ip_id>/edit/", views.ip_edit_modal, name="ip_edit_modal"),
    path("ip/<int:ip_id>/save/", views.ip_save, name="ip_save"),
    path("ip/<int:ip_id>/patch/", views.ip_patch, name="ip_patch"),
    path("ip/<int:ip_id>/delete/", views.ip_delete, name="ip_delete"),
    path("ip/<int:ip_id>/check/", views.ip_check_availability, name="ip_check_availability"),
    path("ip/<int:ip_id>/scan-ports/", views.ip_scan_ports, name="ip_scan_ports"),
    path("ip/<int:ip_id>/resolve-dns/", views.ip_resolve_dns, name="ip_resolve_dns"),
]
