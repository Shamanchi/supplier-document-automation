"""Канонические модели связки «документ → реестр → позиция прайса».

Схема намеренно одна на три стороны:

* `RegistryCertificate` — запись реестра Росаккредитации, приведённая из CSV
  открытых данных (в наборе колонки называются по-русски и в трёх разных
  схемах, см. `mapping.py`);
* `ExtractedDocument` — то, что удалось вычитать из документа текстовым слоем
  или OCR, с уверенностью по каждому полю;
* `Reconciliation` — результат сверки первого со вторым, он же то, что
  потребляет price_loader_app.

Общие имена полей у извлечения и у реестра — не косметика: только так их можно
сравнивать поштучно и показывать менеджеру расхождения.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Generic, TypeVar

SCHEMA_VERSION = "1.0"

T = TypeVar("T")


class CertificateStatus(str, Enum):
    """Статус документа в реестре.

    Значения повторяют реестр дословно, чтобы не терять информацию при
    импорте; интерпретация — в `is_alive`.
    """

    ACTIVE = "Действует"
    ARCHIVED = "Архивный"
    TERMINATED = "Прекращен"
    SUSPENDED = "Приостановлен"
    RESUMED = "Возобновлен"
    EXTENDED = "Продлен"
    INVALID = "Недействителен"
    UNKNOWN = "Неизвестен"

    @classmethod
    def parse(cls, raw: str) -> "CertificateStatus":
        cleaned = (raw or "").strip()
        for member in cls:
            if member.value.casefold() == cleaned.casefold():
                return member
        return cls.UNKNOWN

    @property
    def is_alive(self) -> bool:
        """Статус сам по себе не означает актуальности — нужен ещё срок.

        Проверять только статус нельзя: записи реестра хранят состояние на
        момент последнего изменения, поэтому у давно не менявшихся документов
        статус остаётся `Действует` спустя годы после истечения срока.
        """
        return self in {self.ACTIVE, self.RESUMED, self.EXTENDED}


class DocumentClass(str, Enum):
    CERTIFICATE = "certificate"
    DECLARATION = "declaration"
    REGISTRY_EXTRACT = "registry_extract"
    PASSPORT = "passport"
    OTHER = "other"


class TextSource(str, Enum):
    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    HYBRID = "hybrid"


class Verdict(str, Enum):
    """Итог сверки — то, на что смотрит расчёт."""

    VALID = "valid"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"

    @property
    def attachable(self) -> bool:
        """Можно ли прикладывать документ к коммерческому предложению."""
        return self is Verdict.VALID


class MatchedBy(str, Enum):
    REGISTRATION_NUMBER = "registration_number"
    BLANK_NUMBER = "blank_number"
    MANUFACTURER_AND_PRODUCT = "manufacturer_and_product"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Field(Generic[T]):
    """Значение с происхождением.

    `confidence` заполняется только для OCR-ветки; для текстового слоя и для
    записей реестра он равен 1.0. `page` и `bbox` позволяют показать
    менеджеру, откуда взято значение.
    """

    value: T | None
    confidence: float = 1.0
    page: int | None = None
    bbox: tuple[int, int, int, int] | None = None

    @property
    def is_present(self) -> bool:
        return self.value is not None and self.value != "" and self.value != []


@dataclass(frozen=True, slots=True)
class RegistryCertificate:
    """Запись реестра, приведённая к каноническим именам."""

    registration_number: str
    registration_number_key: str
    status: CertificateStatus
    status_raw: str = ""

    registered_at: date | None = None
    valid_until: date | None = None
    suspended_at: date | None = None
    suspension_until: date | None = None
    terminated_at: date | None = None
    resumed_at: date | None = None
    extended_at: date | None = None

    object_type: str = ""
    blank_number: str = ""
    annex_blank_number: str = ""
    technical_regulations: str = ""
    product_group: str = ""

    applicant_name: str = ""
    applicant_inn: str = ""
    manufacturer_name: str = ""
    manufacturer_inn: str = ""
    manufacturer_ogrn: str = ""

    product_full_name: str = ""
    product_designation: str = ""
    product_type: str = ""
    product_brand: str = ""
    product_model: str = ""
    product_article: str = ""
    okpd2_tnved: str = ""
    product_standards: str = ""

    source_slice: str = ""

    def is_alive_on(self, on: date) -> bool:
        """Актуальность = статус И срок. Ни одного из двух по отдельности мало."""
        if not self.status.is_alive:
            return False
        if self.valid_until is None:
            return False
        return self.valid_until >= on

    @property
    def product_keys(self) -> list[str]:
        """Поля, из которых имеет смысл добывать обозначения изделий.

        Порядок — по фактической заполненности в наборе: наименование есть
        почти всегда, структурные `Модель`/`Марка`/`Артикул` — у единиц
        процентов записей.
        """
        return [
            value
            for value in (
                self.product_full_name,
                self.product_designation,
                self.product_model,
                self.product_brand,
                self.product_article,
            )
            if value
        ]


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Результат разбора документа: текстовый слой или OCR."""

    doc_id: str
    doc_class: DocumentClass
    text_source: TextSource
    source_path: str = ""
    engine: str = ""
    page_count: int = 0
    mean_confidence: float | None = None

    # Сертификат / декларация.
    registration_number: Field[str] = field(default_factory=lambda: Field(None))
    blank_number: Field[str] = field(default_factory=lambda: Field(None))
    valid_from: Field[date] = field(default_factory=lambda: Field(None))
    valid_until: Field[date] = field(default_factory=lambda: Field(None))
    products_raw: Field[str] = field(default_factory=lambda: Field(None))
    product_designations: Field[list[str]] = field(
        default_factory=lambda: Field(None)
    )
    technical_regulation: Field[str] = field(default_factory=lambda: Field(None))
    tu_reference: Field[str] = field(default_factory=lambda: Field(None))
    annex_reference: Field[str] = field(default_factory=lambda: Field(None))
    manufacturer_name: Field[str] = field(default_factory=lambda: Field(None))

    # Паспорт.
    product_designation: Field[str] = field(default_factory=lambda: Field(None))
    product_name: Field[str] = field(default_factory=lambda: Field(None))
    mass_kg: Field[float] = field(default_factory=lambda: Field(None))
    dimensions_mm: Field[str] = field(default_factory=lambda: Field(None))
    service_life: Field[str] = field(default_factory=lambda: Field(None))
    warranty_period: Field[str] = field(default_factory=lambda: Field(None))
    certificate_numbers: Field[list[str]] = field(
        default_factory=lambda: Field(None)
    )

    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SuccessorCandidate:
    """Вероятная замена мёртвого сертификата.

    Явной связи «старый → новый» в реестре нет, поэтому кандидат всегда
    остаётся подсказкой менеджеру и никогда не подставляется автоматически.
    """

    certificate: RegistryCertificate
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Итог сверки документа с реестром."""

    verdict: Verdict
    matched_by: MatchedBy
    checked_on: date
    schema_version: str = SCHEMA_VERSION
    document: ExtractedDocument | None = None
    certificate: RegistryCertificate | None = None
    successors: tuple[SuccessorCandidate, ...] = ()
    needs_manual_review: bool = True
    review_reasons: tuple[str, ...] = ()

    @property
    def valid_until(self) -> date | None:
        return self.certificate.valid_until if self.certificate else None


@dataclass(frozen=True, slots=True)
class PositionDocumentLink:
    """То, что забирает price_loader_app для колонки «ссылка на документацию».

    `position_ref` — идентификатор строки расчёта на стороне price_loader_app;
    контур OCR его не интерпретирует и возвращает как есть.
    """

    position_ref: str
    doc_id: str
    document_url: str
    verdict: Verdict
    valid_until: date | None = None
    registration_number: str = ""
    needs_manual_review: bool = True
    review_reasons: tuple[str, ...] = ()
    successor_registration_number: str = ""
    successor_valid_until: date | None = None


def to_jsonable(value: Any) -> Any:
    """Рекурсивно привести dataclass/enum/date к JSON-совместимому виду."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    return value
