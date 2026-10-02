"""Соответствие колонок набора открытых данных каноническим полям.

Набор публикуется в трёх схемах сразу, и это не редкий край: русские
заголовки, английские заголовки и помесячные файлы вообще без строки
заголовка с фиксированным порядком из 51 колонки. Разделитель тоже плавает
между `;`, `|` и `,`, а имена колонок в части срезов дополнены пробелами до
фиксированной ширины.
"""

from __future__ import annotations

from enum import Enum

CANONICAL_FIELDS = (
    "registration_number",
    "status",
    "registered_at",
    "valid_until",
    "suspended_at",
    "suspension_until",
    "terminated_at",
    "resumed_at",
    "extended_at",
    "object_type",
    "blank_number",
    "annex_blank_number",
    "technical_regulations",
    "product_group",
    "applicant_name",
    "applicant_inn",
    "manufacturer_name",
    "manufacturer_inn",
    "manufacturer_ogrn",
    "product_full_name",
    "product_designation",
    "product_type",
    "product_brand",
    "product_model",
    "product_article",
    "okpd2_tnved",
    "product_standards",
)

DATE_FIELDS = frozenset(
    {
        "registered_at",
        "valid_until",
        "suspended_at",
        "suspension_until",
        "terminated_at",
        "resumed_at",
        "extended_at",
    }
)


class RegistrySchema(str, Enum):
    MODERN = "modern"
    LEGACY_HEADER = "legacy_header"
    LEGACY_HEADLESS = "legacy_headless"


# Русские заголовки современных срезов.
MODERN_COLUMNS = {
    "registration_number": "Номер СС",
    "status": "Статус",
    "registered_at": "Дата рег",
    "valid_until": "Срок действия",
    "suspended_at": "Дата приостановки",
    "suspension_until": "Срок приостановки",
    "terminated_at": "Дата прекращения",
    "resumed_at": "Дата возобновления",
    "extended_at": "Дата продления",
    "object_type": "Тип объекта серт",
    "blank_number": "Номер бланка",
    "annex_blank_number": "Номер бланка приложений",
    "technical_regulations": "Тех регламенты",
    "product_group": "Группа продукции",
    "applicant_name": "Заявитель",
    "applicant_inn": "ИНН заявителя",
    "manufacturer_name": "Изготовитель",
    "manufacturer_inn": "ИНН изготовителя",
    "manufacturer_ogrn": "ОГРН изготовителя",
    "product_full_name": "Полное наименование продукции",
    "product_designation": "Обозначение продукции",
    "product_type": "Тип продукции",
    "product_brand": "Марка продукции",
    "product_model": "Модель продукции",
    "product_article": "Артикул продукции",
    "okpd2_tnved": "Коды ОКПД2/ТНВЭД",
    "product_standards": "Стандарты продукции",
}

# Английские заголовки исторических срезов. Полей меньше: дат приостановки и
# прекращения там нет вовсе, и подставлять вместо них что-то другое нельзя —
# пусто честнее.
LEGACY_COLUMNS = {
    "registration_number": "reg_number",
    "status": "cert_status",
    "registered_at": "date_begining",
    "valid_until": "date_finish",
    "object_type": "product_object_type_cert",
    "technical_regulations": "product_tech_reg",
    "product_group": "product_group",
    "applicant_name": "applicant_name",
    "applicant_inn": "applicant_inn",
    "manufacturer_name": "manufacturer_name",
    "manufacturer_inn": "manufacturer_inn",
    "manufacturer_ogrn": "manufacturer_ogrn",
    "product_full_name": "product_name",
    "product_designation": "product_info",
    "product_type": "product_type",
    "okpd2_tnved": "product_tn_ved",
    "product_standards": "product_national_standart",
}

# Порядок колонок безголовых помесячных файлов 2019–2023 — тот же, что у
# файлов `rss_od_*` с английским заголовком.
LEGACY_COLUMN_ORDER = (
    "id_cert", "cert_status", "cert_type", "reg_number", "date_begining",
    "date_finish", "product_scheme", "product_object_type_cert", "product_type",
    "product_okpd2", "product_tn_ved", "product_tech_reg", "product_group",
    "product_name", "product_info", "applicant_type", "person_applicant_type",
    "applicant_ogrn", "applicant_inn", "applicant_phone", "applicant_fax",
    "applicant_email", "applicant_website", "applicant_name",
    "applicant_director_name", "applicant_address", "applicant_address_actual",
    "manufacturer_type", "manufacturer_ogrn", "manufacturer_inn",
    "manufacturer_phone", "manufacturer_fax", "manufacturer_email",
    "manufacturer_website", "manufacturer_name", "manufacturer_director_name",
    "manufacturer_country", "manufacturer_address",
    "manufacturer_address_actual", "manufacturer_address_filial",
    "organ_to_certification_name", "organ_to_certification_reg_number",
    "organ_to_certification_head_name", "basis_for_certificate",
    "old_basis_for_certificate", "fio_expert", "fio_signatory",
    "product_national_standart", "production_analysis_for_act",
    "production_analysis_for_act_number", "production_analysis_for_act_date",
)

# Колонки индекса, который собирает build_fsa_registry_index.py.
INDEX_COLUMNS = dict(MODERN_COLUMNS)

FIRE_SAFETY_MARKER = "пожарной безопасности"


def detect_schema(header_line: str) -> RegistrySchema:
    if "Номер СС" in header_line:
        return RegistrySchema.MODERN
    if "reg_number" in header_line:
        return RegistrySchema.LEGACY_HEADER
    return RegistrySchema.LEGACY_HEADLESS


def detect_delimiter(header_line: str) -> str:
    """Разделитель определяется отдельно от схемы: они варьируются независимо."""
    return max((";", "|", ","), key=header_line.count)


def columns_for(schema: RegistrySchema) -> dict[str, str]:
    if schema is RegistrySchema.MODERN:
        return MODERN_COLUMNS
    return LEGACY_COLUMNS
