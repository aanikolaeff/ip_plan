"""Принудительная смена пароля для локальных учёток.

Нужна из-за засева admin/admin (см. management/commands/ensure_local_admin.py):
без принуждения дефолтный пароль остаётся в проде навсегда. Пока у
LocalAccount стоит must_change_password, пользователь не попадает никуда,
кроме страницы смены пароля.

Хук — process_view, а не __call__: request.resolver_match заполняется
Django уже ПОСЛЕ прохода всех middleware "на вход", поэтому в __call__ имя
маршрута ещё недоступно (там был бы None и разрешающий список не работал
бы). В MIDDLEWARE ставится после AuthenticationMiddleware (нужен
request.user) и после LoginRequiredMiddleware (анонимов та уже развернула
на страницу входа).
"""
from django.shortcuts import redirect
from django.urls import reverse

# Куда пускаем даже с невыполненным требованием: сама страница смены
# пароля (иначе редирект на неё же — цикл), вход/выход (человек должен
# иметь возможность просто уйти) и служебный csrf-эндпоинт фронтенда.
# worker_health — JSON баннера оболочки: его запрашивает fetch на любой
# странице, в том числе на странице смены пароля; редирект на HTML он не
# разобрал бы. Секретов в ответе нет (только состояние сборщика).
_ALLOWED_URL_NAMES = {"password_change", "login", "logout", "csrf_refresh", "worker_health"}


class ForcePasswordChangeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None

        match = request.resolver_match
        if match and match.url_name in _ALLOWED_URL_NAMES:
            return None

        # LDAP-пользователей это не касается: их пароль живёт в FreeIPA,
        # LocalAccount у них нет вообще. Обратный OneToOne при отсутствии
        # записи бросает RelatedObjectDoesNotExist — он наследуется в том
        # числе от AttributeError, поэтому getattr с дефолтом корректно
        # отдаёт None, а не падает.
        account = getattr(user, "local_account", None)
        if account is not None and account.must_change_password:
            return redirect(reverse("ip_plan:password_change"))
        return None
