# Сид начальных значений FirewallObjectName — то, что реально применено на
# xfw5000-mdc прямо сейчас (см. netbox-sync/devices.json, blocklist_objects,
# и firewall_blocklists/xfw5000-mdc/latest.json — совпадает один в один,
# см. docs/plans/2026-09-04-regulator-blocklist-design.md).
from django.db import migrations


def seed(apps, schema_editor):
    FirewallObjectName = apps.get_model("ip_plan", "FirewallObjectName")
    FirewallObjectName.objects.get_or_create(kind="ip", defaults={"current_name": "BLOCK_BY_IP_2"})
    FirewallObjectName.objects.get_or_create(kind="fqdn", defaults={"current_name": "BBDNS_1"})


def unseed(apps, schema_editor):
    FirewallObjectName = apps.get_model("ip_plan", "FirewallObjectName")
    FirewallObjectName.objects.filter(kind__in=["ip", "fqdn"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ip_plan", "0002_firewallobjectname"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
