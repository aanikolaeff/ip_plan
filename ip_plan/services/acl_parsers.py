"""Реестр парсеров списков доступа: interfaces_parser из devices.json
(он же поле «parser» в config_backups/<dev>/meta.json) → модуль с
parse_snapshot(files, device_name).

Модули импортируются лениво: страница ACL не должна падать целиком, если
один вендорский парсер ещё не дописан или сломан, — такое устройство
получает пустой ParsedDevice с предупреждением в warnings."""

import importlib
import logging

from .acl_model import ParsedDevice

logger = logging.getLogger(__name__)

# parser → (модуль, какие файлы снимка ему нужны). Имена файлов — ключи
# meta.json["files"] / policy_commands в devices.json; «config» — latest.cfg.
PARSERS = {
    "cisco_ios":    ("ip_plan.services.cisco_acl",       ("config",)),
    "xfw":          ("ip_plan.services.vipnet_firewall", ("firewall_rules", "firewall_objects", "iplir_config")),
    "huawei_vrp":   ("ip_plan.services.huawei_acl",      ("config",)),
    "eltex_status": ("ip_plan.services.eltex_acl",       ("config",)),
    "eltex_mes24":  ("ip_plan.services.eltex_acl",       ("config",)),   # MES24xx: ISS-CLI, синтаксис ACL Cisco-подобный
    "maipu":        ("ip_plan.services.maipu_acl",       ("config",)),
}

# Старые снимки без meta.json — это ASR, снятые до появления файла.
DEFAULT_PARSER = "cisco_ios"


def needed_files(parser: str) -> tuple:
    spec = PARSERS.get(parser)
    return spec[1] if spec else ()


def parse(parser: str, files: dict, device_name: str) -> ParsedDevice:
    """files — {имя снимка: текст}. Неизвестный парсер или ошибка импорта →
    пустое устройство с warning, а не исключение."""
    spec = PARSERS.get(parser)
    if spec is None:
        dev = ParsedDevice(device_name=device_name, parser=parser)
        dev.warnings.append(f"Парсер «{parser}» не реализован для списков доступа")
        return dev
    module_name, _ = spec
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        logger.error("Парсер %s не импортируется: %s", module_name, exc)
        dev = ParsedDevice(device_name=device_name, parser=parser)
        dev.warnings.append(f"Парсер «{parser}» недоступен: {exc}")
        return dev
    result = module.parse_snapshot(files, device_name)
    # Ключ реестра важнее того, что проставил модуль: eltex_acl общий для
    # eltex_status и eltex_mes24, а в сводке должен быть парсер устройства.
    result.parser = parser
    return result
