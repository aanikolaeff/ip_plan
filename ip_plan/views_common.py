"""Общие ответы разделов «Настройки» и «Задания»: модалка ошибки, тост,
тост с переходом, запись в журнал настроек и постановка задания сборщика.

Отдельный модуль, а не views_settings: views_jobs берёт оттуда staff_required,
а «Запуск и расписание» в «Настройках» ставит задания так же, как «Задания», —
без общего места модули импортировали бы друг друга.
"""
import json

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse

from .models import SettingsAudit
from .services import sync_jobs


def error_modal(request, message, status=400):
    return render(request, "ip_plan/_modal_error.html", {"error": message}, status=status)


def toast(message, kind="info", status=204):
    response = HttpResponse(status=status)
    response["HX-Trigger"] = json.dumps({"show-toast": {"message": message, "kind": kind}})
    return response


def toast_redirect(url, message, kind="success"):
    """Закрыть модалку, показать тост и перейти по url (HX-Redirect)."""
    response = toast(message, kind)
    response["HX-Redirect"] = url
    return response


def audit(request, scope, target, action, details=""):
    SettingsAudit.objects.create(
        username=request.user.username, scope=scope,
        target=target, action=action, details=details,
    )


def start_job(request, kind, steps, schedule=None):
    """Поставить задание и ответить переходом на него. Повторный запуск того
    же вида — уведомление и переход на уже стоящее задание (KTD14)."""
    job, created = sync_jobs.enqueue(kind, steps, user=request.user.username, schedule=schedule)
    url = reverse("ip_plan:job_detail", args=[job.pk])
    label = sync_jobs.kind_label(kind)
    if not created:
        return toast_redirect(url, f"Задание этого вида уже выполняется: «{label}» (#{job.pk})", "info")
    audit(request, "sync", f"задание #{job.pk}", "create",
          f"запуск «{label}»: " + ", ".join(sync_jobs.step_label(s) for s in steps))
    return toast_redirect(url, f"Задание «{label}» поставлено в очередь (#{job.pk})")
