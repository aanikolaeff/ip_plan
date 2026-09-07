import ipaddress
import re

from django import forms

from .models import RegulatorDocument, RegulatorEntry

# Строго xxx.xxx.xxx.xxx/xx — отдельно от семантической проверки октетов/
# маски через ipaddress.IPv4Interface ниже (не пропускает IPv6, не пропускает
# адрес без маски, не пропускает произвольный текст).
ADDRESS_PATTERN = r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$"
ADDRESS_RE = re.compile(ADDRESS_PATTERN)

STATUS_CHOICES = [
    ("active", "Active"),
    ("reserved", "Reserved"),
    ("deprecated", "Deprecated"),
    ("dhcp", "DHCP"),
    ("slaac", "SLAAC"),
]

# Ключи custom_fields в NetBox (уточнены прямым запросом к API — отличаются
# от отображаемых имён из ТЗ). "Комментарии" в custom_fields не входит:
# это встроенное поле IP-адреса NetBox (ip.comments), а не custom field.
CUSTOM_FIELD_KEYS = ["cpe", "open_ports", "os_guess", "os_installed", "purpose"]


def _text_input(**extra_attrs):
    attrs = {"class": "field-control"}
    attrs.update(extra_attrs)
    return forms.TextInput(attrs=attrs)


def _readonly_text_input(**extra_attrs):
    # readonly, а не disabled: disabled-поля браузer не отправляет с формой —
    # значение, записанное сканированием/импортом, обнулилось бы при любом
    # сохранении формы. readonly не даёт редактировать руками, но отправляется.
    attrs = {"class": "field-control", "readonly": "readonly", "tabindex": "-1"}
    attrs.update(extra_attrs)
    return forms.TextInput(attrs=attrs)


class IPAddressForm(forms.Form):
    address = forms.CharField(
        label="Адрес (IP)",
        max_length=64,
        widget=_text_input(
            **{"class": "field-control field-control-ip"},
            placeholder="192.0.2.1/24",
            pattern=ADDRESS_PATTERN,
            title="Формат: xxx.xxx.xxx.xxx/xx, например 192.0.2.1/24",
        ),
    )
    status = forms.ChoiceField(
        label="Статус",
        choices=STATUS_CHOICES,
        widget=forms.Select(attrs={"class": "field-control"}),
    )
    dns_name = forms.CharField(label="DNS-имя", max_length=255, required=False, widget=_text_input())
    description = forms.CharField(
        label="Описание",
        max_length=255,
        required=False,
        widget=forms.Textarea(attrs={"class": "field-control", "rows": 2}),
    )
    # Хранит тэги как CSV-строку, синхронизируется с chip-инпутом на Alpine.js.
    tags = forms.CharField(required=False, widget=forms.HiddenInput())

    # CPE/Open Ports/OS Guess — заполняются только сканированием (кебаб-меню
    # "Сканировать порты"/импортом), руками не редактируются.
    cpe = forms.CharField(label="CPE", max_length=255, required=False, widget=_readonly_text_input())
    open_ports = forms.CharField(
        label="Open Ports", max_length=255, required=False, widget=_readonly_text_input()
    )
    os_guess = forms.CharField(
        label="OS Guess", max_length=255, required=False, widget=_readonly_text_input()
    )
    os_installed = forms.CharField(
        label="Операционная система", max_length=255, required=False,
        # list= — подсказки из справочника (см. base.html: datalist
        # #os-catalog-datalist), но это именно подсказки: значение вне
        # справочника всё равно принимается, поле остаётся обычным текстом.
        widget=_text_input(list="os-catalog-datalist"),
    )
    purpose = forms.CharField(label="Назначение", max_length=255, required=False, widget=_text_input())
    comments = forms.CharField(
        label="Комментарии",
        required=False,
        widget=forms.Textarea(attrs={"class": "field-control", "rows": 2}),
    )

    def clean_address(self):
        value = self.cleaned_data["address"].strip()
        # Сначала строгая форма xxx.xxx.xxx.xxx/xx (отсекает IPv6, адрес без
        # маски, произвольный текст) — затем семантика самих чисел через
        # IPv4Interface (октеты 0-255, маска 0-32).
        if not ADDRESS_RE.match(value):
            raise forms.ValidationError(
                "Введите адрес в формате xxx.xxx.xxx.xxx/xx, например 192.0.2.1/24."
            )
        try:
            ipaddress.IPv4Interface(value)
        except ValueError:
            raise forms.ValidationError(
                "Введите корректный адрес с маской, например 192.0.2.1/24."
            )
        return value

    def clean_tags(self):
        raw = self.cleaned_data.get("tags", "")
        names = []
        for chunk in raw.split(","):
            name = chunk.strip()
            if name and name not in names:
                names.append(name)
        return names


# --- Реестр блокировок регулятора (models.py: RegulatorDocument/RegulatorEntry) ---

FQDN_RE = re.compile(r"^(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(\.[a-zA-Z0-9-]{1,63})+$")
# MD5 (32 hex) / SHA1 (40 hex) / SHA256 (64 hex) — все три реально
# встречаются в письмах регулятора и ложатся в один kind="hash" ("IOC"),
# см. ip_plan/services/ioc_parser.py (HASH_RE — тот же набор длин) и
# docs/plans/2026-09-07-ioc-parser-fstec-corpus-audit.md. Раньше здесь
# была только 64-символьная SHA256_RE — даже верно распознанный парсером
# MD5/SHA1 отваливался тут при сохранении.
HASH_VALUE_RE = re.compile(r"^[a-fA-F0-9]{64}$|^[a-fA-F0-9]{40}$|^[a-fA-F0-9]{32}$")


class RegulatorDocumentForm(forms.ModelForm):
    class Meta:
        model = RegulatorDocument
        fields = ["reference_no", "document_date", "note"]
        labels = {
            "reference_no": "Номер письма",
            "document_date": "Дата письма",
            "note": "Примечание",
        }
        widgets = {
            "reference_no": _text_input(placeholder="Например, 12-34/5678"),
            "document_date": forms.DateInput(attrs={"class": "field-control", "type": "date"}),
            "note": forms.Textarea(attrs={"class": "field-control", "rows": 2}),
        }


class RegulatorEntryForm(forms.ModelForm):
    class Meta:
        model = RegulatorEntry
        fields = ["document", "kind", "value"]
        labels = {"document": "Письмо", "kind": "Тип", "value": "Значение"}
        widgets = {
            "document": forms.Select(attrs={"class": "field-control"}),
            "kind": forms.Select(attrs={"class": "field-control"}),
            "value": _text_input(placeholder="IP / example.com / https://example.com/path / MD5/SHA1/SHA256-хэш"),
        }

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        value = (cleaned.get("value") or "").strip()
        cleaned["value"] = value
        if not kind or not value:
            return cleaned
        if kind == "ip":
            # Только адрес, без маски — это конкретный блокируемый хост, не
            # подсеть (в отличие от IPAddressForm выше, где маска обязательна
            # для полей IP Plan).
            try:
                ipaddress.ip_address(value)
            except ValueError:
                self.add_error("value", "Введите корректный IP-адрес (без маски), например 1.2.3.4")
        elif kind == "fqdn":
            if not FQDN_RE.match(value):
                self.add_error("value", "Введите корректный FQDN, например example.com")
        elif kind == "url":
            if "://" not in value:
                self.add_error("value", "URL должен содержать схему, например https://example.com/path")
        elif kind == "hash":
            if not HASH_VALUE_RE.match(value):
                self.add_error("value", "Введите MD5 (32), SHA1 (40) или SHA256 (64) hex-символа")
        return cleaned


# RegulatorEntryRevokeForm (была тут) убрана вместе с HTML-модалкой снятия
# (regulator_entry_revoke_modal.html) — таблица записей письма теперь React
# (см. regulator_document_detail.html, static_src/regulator_table), «Снять»
# там собирает причину через window.prompt() и шлёт JSON прямо в
# regulator_entry_revoke_api, форма-обёртка вокруг одного необязательного
# поля больше не нужна.
