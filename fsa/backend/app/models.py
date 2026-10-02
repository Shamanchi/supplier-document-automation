"""Модель данных сервиса FSA.

Схема ложится на канонические модели `app/fsa/schema.py` один в один: таблица
`registry_certificate` повторяет `RegistryCertificate`, `document` —
`ExtractedDocument`, `document_check` — `Reconciliation`. Расхождение имён
между слоями специально отсутствует, иначе сравнивать извлечённое с эталоном
пришлось бы через переходник.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DocumentStatus(str, enum.Enum):
    """Жизненный цикл документа.

    `stored` → загружен в S3, но не распознан; `recognized` → текст получен;
    `failed` → движок не смог. Повторная загрузка того же файла не создаёт
    новую запись: идемпотентность держится на `sha256`.
    """

    stored = "stored"
    recognizing = "recognizing"
    recognized = "recognized"
    failed = "failed"


class RegistryCertificate(Base):
    """Карточка сертификата из набора открытых данных Росаккредитации."""

    __tablename__ = "registry_certificate"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Нормализованный номер — единственный надёжный ключ. Кириллические
    # омоглифы сведены в латиницу, иначе один сертификат распадается на
    # несколько записей.
    registration_number_key: Mapped[str] = mapped_column(String(128), nullable=False)
    registration_number: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="Неизвестен")
    status_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")

    registered_at: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    suspended_at: Mapped[date | None] = mapped_column(Date)
    suspension_until: Mapped[date | None] = mapped_column(Date)
    terminated_at: Mapped[date | None] = mapped_column(Date)
    resumed_at: Mapped[date | None] = mapped_column(Date)
    extended_at: Mapped[date | None] = mapped_column(Date)

    object_type: Mapped[str] = mapped_column(Text, default="")
    blank_number: Mapped[str] = mapped_column(Text, default="")
    annex_blank_number: Mapped[str] = mapped_column(Text, default="")
    technical_regulations: Mapped[str] = mapped_column(Text, default="")
    product_group: Mapped[str] = mapped_column(Text, default="")

    applicant_name: Mapped[str] = mapped_column(Text, default="")
    applicant_inn: Mapped[str] = mapped_column(Text, default="")
    manufacturer_name: Mapped[str] = mapped_column(Text, default="")
    manufacturer_inn: Mapped[str] = mapped_column(Text, default="")
    manufacturer_ogrn: Mapped[str] = mapped_column(Text, default="")

    product_full_name: Mapped[str] = mapped_column(Text, default="")
    product_designation: Mapped[str] = mapped_column(Text, default="")
    product_type: Mapped[str] = mapped_column(Text, default="")
    product_brand: Mapped[str] = mapped_column(Text, default="")
    product_model: Mapped[str] = mapped_column(Text, default="")
    product_article: Mapped[str] = mapped_column(Text, default="")
    okpd2_tnved: Mapped[str] = mapped_column(Text, default="")
    product_standards: Mapped[str] = mapped_column(Text, default="")

    source_slice: Mapped[str] = mapped_column(Text, default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("registration_number_key", name="uq_registry_certificate_key"),
        # Подбор преемника всегда идёт от изготовителя среди живых записей.
        Index("ix_registry_certificate_manufacturer", "manufacturer_inn"),
        Index("ix_registry_certificate_valid_until", "valid_until"),
        Index("ix_registry_certificate_status", "status"),
    )


class RegistryNumber(Base):
    """Все номера реестра, включая непожарные сегменты.

    Нужна ровно для одного различия: «номера нет в реестре» против «есть, но
    подробной карточки мы не держим». Схлопывание этих случаев в один статус
    теряет подсказку, что делать дальше.
    """

    __tablename__ = "registry_number"

    registration_number_key: Mapped[str] = mapped_column(String(128), primary_key=True)


class RegistryImportStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class RegistryImportKind(str, enum.Enum):
    """Что за файл загрузили.

    `index` — CSV с карточками, `numbers` — список номеров, `archive` — 7z
    из набора открытых данных, который надо сначала распаковать.
    """

    index = "index"
    numbers = "numbers"
    archive = "archive"


class RegistryImport(Base):
    """Журнал загрузок реестра — чтобы видеть, на какую дату актуальны данные."""

    __tablename__ = "registry_import"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default=RegistryImportKind.index.value)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RegistryImportStatus.queued.value
    )
    filename: Mapped[str] = mapped_column(Text, default="")
    storage_key: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    task_id: Mapped[str] = mapped_column(String(64), default="")
    certificates_loaded: Mapped[int] = mapped_column(Integer, default=0)
    numbers_loaded: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")


class Document(Base):
    """Загруженный документ и результат его распознавания.

    Идемпотентность: `sha256` уникален. Повторная загрузка того же файла
    возвращает существующую запись и не запускает OCR заново — это главное
    требование к пакетной обработке, где один и тот же сертификат лежит у
    нескольких поставщиков.
    """

    __tablename__ = "document"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    filename: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_path: Mapped[str] = mapped_column(Text, default="")
    supplier: Mapped[str] = mapped_column(Text, default="")
    media_type: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_key: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DocumentStatus.stored.value
    )
    doc_class: Mapped[str] = mapped_column(String(32), default="other")
    text_source: Mapped[str] = mapped_column(String(16), default="ocr")
    engine: Mapped[str] = mapped_column(String(32), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    mean_confidence: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    ocr_text: Mapped[str] = mapped_column(Text, default="")
    # Полный ExtractedDocument as-is: состав извлекаемых полей будет расти,
    # и добавлять колонку на каждое поле смысла нет.
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    registration_number_key: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    recognized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    checks: Mapped[list["DocumentCheck"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("sha256", name="uq_document_sha256"),
        Index("ix_document_status", "status"),
        Index("ix_document_number", "registration_number_key"),
        Index("ix_document_supplier", "supplier"),
    )


class DocumentCheck(Base):
    """Результат сверки документа с реестром на конкретную дату.

    Проверка привязана к дате, потому что вердикт со временем меняется сам:
    у 23% актуальных сертификатов срок истекает в пределах года. Старая
    проверка остаётся в истории, новая создаётся рядом.
    """

    __tablename__ = "document_check"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    certificate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("registry_certificate.id", ondelete="SET NULL")
    )
    successor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("registry_certificate.id", ondelete="SET NULL")
    )

    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    matched_by: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    registration_number_key: Mapped[str] = mapped_column(String(128), default="")
    checked_on: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date)
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, default=True)
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    successor_score: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped[Document] = relationship(back_populates="checks")

    __table_args__ = (
        UniqueConstraint("document_id", "checked_on", name="uq_document_check_day"),
        Index("ix_document_check_verdict", "verdict"),
    )


class DecisionAction(str, enum.Enum):
    """Что решил человек по документу.

    `confirm` — вердикт сверки верен, документ можно использовать как есть;
    `reject` — привязка неверна, к расчёту не прикладывать;
    `correct` — машина прочитала номер неправильно, человек указал верный.
    """

    confirm = "confirm"
    reject = "reject"
    correct = "correct"


class DocumentDecision(Base):
    """Решение человека по документу — отдельный слой поверх машинной сверки.

    Три правила заложены в схему намеренно:

    1. **Решения не редактируются и не удаляются.** Новое кладётся рядом,
       актуальным считается последнее. Иначе не ответить на вопрос «кто и
       когда подтвердил этот сертификат», а именно он возникает, когда КП с
       приложенным документом оспаривают.
    2. **Вердикт машины решение не переписывает.** Хранятся оба: `verdict`
       остаётся в `document_check`, здесь лежит то, что человек с ним сделал.
       Слияние в одно поле стёрло бы разницу между «реестр говорит valid» и
       «менеджер посмотрел и согласился».
    3. **Решение привязано к состоянию, на котором принято** — вердикту,
       номеру и времени распознавания. Если что-то из этого изменилось,
       подтверждение устарело и документ возвращается в очередь: подтверждали
       не то, что видно сейчас.
    """

    __tablename__ = "document_decision"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    check_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_check.id", ondelete="SET NULL")
    )

    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comment: Mapped[str] = mapped_column(Text, default="")

    # Состояние, на котором принято решение.
    verdict_at_decision: Mapped[str] = mapped_column(String(16), default="")
    number_at_decision: Mapped[str] = mapped_column(String(128), default="")
    recognized_at_decision: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Для `correct`: номер, который утвердил человек, и что по нему в реестре.
    corrected_number: Mapped[str] = mapped_column(Text, default="")
    corrected_number_key: Mapped[str] = mapped_column(String(128), default="")
    corrected_verdict: Mapped[str] = mapped_column(String(16), default="")
    corrected_valid_until: Mapped[date | None] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_document_decision_document", "document_id", "created_at"),
        Index("ix_document_decision_action", "action"),
    )


class RegistryProductKey(Base):
    """Обозначение изделия → карточка реестра.

    Второй вход в реестр, кроме номера сертификата: у позиции прайса номера
    нет, есть артикул и наименование. Ключи выводятся из полей карточки, а не
    хранятся в ней: структурные `Модель`/`Марка`/`Артикул` заполнены у 1.8%
    записей, и полагаться на них нельзя.

    Таблица производная — её можно пересобрать из `registry_certificate` в
    любой момент, поэтому уникальность на паре, а не на ключе: один сертификат
    покрывает серию изделий, и одно обозначение встречается у нескольких
    сертификатов разных лет.
    """

    __tablename__ = "registry_product_key"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    certificate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("registry_certificate.id", ondelete="CASCADE"), nullable=False
    )
    product_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_field: Mapped[str] = mapped_column(String(32), default="product_full_name")

    __table_args__ = (
        UniqueConstraint("certificate_id", "product_key", name="uq_registry_product_key"),
        Index("ix_registry_product_key", "product_key"),
    )


class ServiceClient(Base):
    """Потребитель API контура: соседний сервис, скрипт или интерфейс.

    Общий токен на всех — то, что было раньше, — нельзя ни отозвать по одному
    потребителю, ни ограничить по правам: любой, кто его получил, мог удалять
    документы и запускать импорт реестра. Ключ выдаётся на потребителя, в базе
    лежит только HMAC-хэш с pepper, а область прав хранится списком.
    """

    __tablename__ = "service_client"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # Префикс лежит открыто: по нему ключ находится в базе за одно сравнение,
    # без перебора хэшей всех клиентов.
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # Области через запятую: их единицы, и отдельная таблица здесь дала бы
    # join на каждый запрос ради списка из трёх слов.
    scopes: Mapped[str] = mapped_column(String(200), nullable=False, default="read")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
