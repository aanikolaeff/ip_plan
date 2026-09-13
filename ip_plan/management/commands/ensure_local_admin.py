"""Засев локальной учётной записи администратора при первом старте.

Вызывается из entrypoint.sh сразу после migrate. Идемпотентна и намеренно
осторожна: создаёт учётку ТОЛЬКО если в системе нет ни одной локальной
(LocalAccount). Повторные старты контейнера ничего не трогают — в
частности, НЕ сбрасывают пароль уже существующему admin, иначе каждая
пересборка образа возвращала бы дефолтный пароль.

Пароль по умолчанию заведомо слабый (admin/admin), поэтому учётка
создаётся с must_change_password=True: до смены пароля middleware
(ip_plan/middleware.py) не пускает никуда, кроме страницы смены.

Usage:
  python manage.py ensure_local_admin
  python manage.py ensure_local_admin --username ops --password 'S3cret'
  SEED_LOCAL_ADMIN=false python manage.py ensure_local_admin   # ничего не делает
"""
from decouple import config
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from ip_plan.models import LocalAccount


class Command(BaseCommand):
    help = "Создаёт локального администратора, если локальных учёток ещё нет"

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Имя учётки (по умолчанию LOCAL_ADMIN_USERNAME или admin)")
        parser.add_argument("--password", help="Пароль (по умолчанию LOCAL_ADMIN_PASSWORD или admin)")

    def handle(self, *args, **options):
        if not config("SEED_LOCAL_ADMIN", default=True, cast=bool):
            self.stdout.write("SEED_LOCAL_ADMIN=false — засев локального админа пропущен")
            return

        username = options.get("username") or config("LOCAL_ADMIN_USERNAME", default="admin")
        password = options.get("password") or config("LOCAL_ADMIN_PASSWORD", default="admin")

        if LocalAccount.objects.exists():
            self.stdout.write("Локальные учётки уже есть — ничего не делаю")
            return

        # Имя может быть занято зеркалом LDAP-пользователя: тогда локальную
        # с тем же именем заводить нельзя — ModelBackend стоит первым и
        # перекрыл бы вход этому человеку через FreeIPA.
        if User.objects.filter(username=username).exists():
            self.stderr.write(
                f"Пользователь '{username}' уже существует, но не помечен как локальный "
                f"(вероятно, это зеркало LDAP). Локальный админ не создан — задайте другое имя "
                f"через LOCAL_ADMIN_USERNAME."
            )
            return

        with transaction.atomic():
            user = User.objects.create_user(
                username=username, password=password,
                is_staff=True, is_superuser=True,
            )
            LocalAccount.objects.create(
                user=user, must_change_password=True, created_by="ensure_local_admin",
            )

        self.stdout.write(self.style.SUCCESS(
            f"Создан локальный администратор '{username}'. "
            f"Пароль потребуется сменить при первом входе."
        ))
