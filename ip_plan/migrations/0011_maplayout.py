from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ip_plan", "0010_connectiontest"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MapLayout",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("site", models.CharField(max_length=100, verbose_name="Площадка")),
                ("layer", models.CharField(default="l2", max_length=20, verbose_name="Слой")),
                ("positions", models.JSONField(blank=True, default=dict, verbose_name="Позиции")),
                ("expanded", models.JSONField(blank=True, default=list, verbose_name="Развёрнутые группы")),
                ("locked", models.BooleanField(default=False, verbose_name="Закреплено")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Изменено")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="map_layouts", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "Раскладка карты", "verbose_name_plural": "Раскладки карты"},
        ),
        migrations.AddConstraint(
            model_name="maplayout",
            constraint=models.UniqueConstraint(fields=("user", "site", "layer"), name="uniq_map_layout"),
        ),
    ]
