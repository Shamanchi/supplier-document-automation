"""Приём документов: идемпотентная загрузка, распознавание, сверка.

Порядок намеренно такой: сначала пробуем текстовый слой, и только при его
отсутствии зовём OCR. В корпусе 118 из 120 паспортов читаются без движка, а
сканы сосредоточены в сертификатах — гонять через OCR всё подряд означает
работу впустую на порядок большем объёме.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import extract as extractor
from app import storage
from app.fsa.schema import TextSource, to_jsonable
from app.models import Document, DocumentCheck, DocumentStatus
from app.ocr_client import OCRClient, OCRUnavailable
from app.repository import RegistryRepository
from app.services.reconcile import reconcile

PDF_SIGNATURE = b"%PDF-"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Ниже этого порога плотности текста на странице считаем, что слоя нет и
# нужен OCR. Порог тот же, по которому корпус классифицировался в анализе.
MIN_TEXT_CHARS_PER_PAGE = 50

# Текстовый слой PDF регулярно содержит NUL и прочие управляющие байты;
# Postgres их в text не принимает, а для разбора они бесполезны.
_CONTROL_CHARS = {code: None for code in range(32) if code not in (9, 10, 13)}
_CONTROL_CHARS[0x7F] = None


def sanitize_text(text: str) -> str:
    return (text or "").translate(_CONTROL_CHARS)


class UnsupportedDocument(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    document: Document
    created: bool
    recognized: bool
    reused: bool
    check: DocumentCheck | None = None


def detect_media_type(payload: bytes) -> str:
    """Тип определяется по содержимому, а не по расширению или заголовку."""
    if payload.startswith(PDF_SIGNATURE):
        return "application/pdf"
    if payload.startswith(PNG_SIGNATURE):
        return "image/png"
    raise UnsupportedDocument("поддерживаются только PDF и PNG")


def sha256_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class BrokenDocument(ValueError):
    """PDF не открывается или не содержит ни одной страницы."""


def probe_pdf(payload: bytes) -> int:
    """Число страниц PDF; исключение, если файл структурно непригоден.

    Проверять до вызова движка стоит потому, что на таком файле он ответит
    `bad_request`, и в базе останется код ошибки вместо внятной причины.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return 0
    try:
        with fitz.open(stream=payload, filetype="pdf") as document:
            pages = document.page_count
            encrypted = document.is_encrypted
    except Exception as error:  # noqa: BLE001 — важен факт, а не тип сбоя
        raise BrokenDocument("PDF не открывается") from error
    if encrypted:
        raise BrokenDocument("PDF зашифрован")
    if pages == 0:
        raise BrokenDocument("в PDF нет ни одной страницы")
    return pages


def read_text_layer(payload: bytes, media_type: str) -> tuple[str, int]:
    """Вернуть текстовый слой PDF и число страниц; для PNG — пусто."""
    if media_type != "application/pdf":
        return "", 0
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return "", 0
    try:
        with fitz.open(stream=payload, filetype="pdf") as document:
            pages = [page.get_text() for page in document]
    except Exception:  # noqa: BLE001 — битый PDF не должен ронять приём
        return "", 0
    text = sanitize_text("\n".join(pages))
    if not pages:
        return "", 0
    if len(text.strip()) < MIN_TEXT_CHARS_PER_PAGE * len(pages):
        return "", len(pages)
    return text, len(pages)


class DocumentService:
    def __init__(
        self,
        session: AsyncSession,
        ocr_client: OCRClient | None,
        *,
        store: bool = True,
    ) -> None:
        self._session = session
        self._ocr = ocr_client
        self._store = store

    async def get_by_sha256(self, digest: str) -> Document | None:
        return await self._session.scalar(
            select(Document).where(Document.sha256 == digest)
        )

    async def ingest(
        self,
        payload: bytes,
        *,
        filename: str,
        source_path: str = "",
        supplier: str = "",
        force: bool = False,
    ) -> IngestOutcome:
        """Загрузить документ и распознать его, если он ещё не распознан.

        Идемпотентность держится на sha256: один и тот же файл лежит у
        нескольких поставщиков, и повторно платить за OCR незачем. `force`
        нужен только для перепроверки после смены движка.
        """
        media_type = detect_media_type(payload)
        digest = sha256_of(payload)

        document = await self.get_by_sha256(digest)
        created = False
        if document is None:
            storage_key = ""
            if self._store:
                storage_key = storage.put_document(
                    payload, digest, filename, media_type
                )
            document = Document(
                sha256=digest,
                filename=filename,
                source_path=source_path,
                supplier=supplier,
                media_type=media_type,
                size_bytes=len(payload),
                storage_key=storage_key,
                status=DocumentStatus.stored.value,
            )
            self._session.add(document)
            await self._session.flush()
            created = True

        already_recognized = document.status == DocumentStatus.recognized.value
        if already_recognized and not force:
            return IngestOutcome(
                document=document, created=created, recognized=False, reused=True
            )

        await self._recognize(document, payload, media_type)
        return IngestOutcome(
            document=document, created=created, recognized=True, reused=False
        )

    async def store_only(
        self,
        payload: bytes,
        *,
        filename: str,
        source_path: str = "",
        supplier: str = "",
    ) -> IngestOutcome:
        """Положить файл в S3 и завести запись, не распознавая.

        Используется фоновым режимом: распознавание уносится в Celery, а
        клиент получает идентификатор документа сразу.
        """
        media_type = detect_media_type(payload)
        digest = sha256_of(payload)

        document = await self.get_by_sha256(digest)
        if document is not None:
            return IngestOutcome(
                document=document, created=False, recognized=False, reused=True
            )

        storage_key = ""
        if self._store:
            storage_key = storage.put_document(payload, digest, filename, media_type)
        document = Document(
            sha256=digest,
            filename=filename,
            source_path=source_path,
            supplier=supplier,
            media_type=media_type,
            size_bytes=len(payload),
            storage_key=storage_key,
            status=DocumentStatus.stored.value,
        )
        self._session.add(document)
        await self._session.flush()
        return IngestOutcome(
            document=document, created=True, recognized=False, reused=False
        )

    async def _recognize(
        self, document: Document, payload: bytes, media_type: str
    ) -> None:
        if media_type == "application/pdf":
            try:
                probe_pdf(payload)
            except BrokenDocument as error:
                document.status = DocumentStatus.failed.value
                document.error = str(error)
                return

        text, page_count = read_text_layer(payload, media_type)
        source = TextSource.TEXT_LAYER
        engine = ""
        mean_confidence: float | None = None
        duration_ms = 0

        if not text:
            if self._ocr is None:
                document.status = DocumentStatus.failed.value
                document.error = "текстового слоя нет, а OCR-движок не настроен"
                return
            try:
                result = await self._ocr.recognize(
                    payload, document.filename or "document", media_type
                )
            except OCRUnavailable as error:
                document.status = DocumentStatus.failed.value
                document.error = str(error)
                return
            text = sanitize_text(result.text)
            page_count = result.page_count or page_count
            engine = result.engine
            mean_confidence = result.mean_confidence
            duration_ms = result.duration_ms
            source = TextSource.OCR

        extracted = extractor.extract(
            text,
            doc_id=f"sha256:{document.sha256}",
            text_source=source,
            engine=engine,
            page_count=page_count,
            mean_confidence=mean_confidence,
            source_path=document.source_path,
        )

        document.status = DocumentStatus.recognized.value
        document.doc_class = extracted.doc_class.value
        document.text_source = source.value
        document.engine = engine
        document.page_count = page_count
        document.mean_confidence = mean_confidence
        document.duration_ms = duration_ms
        document.ocr_text = text
        document.extracted = to_jsonable(extracted)
        document.registration_number_key = (
            str(extracted.registration_number.value or "")
        )
        document.error = ""
        document.recognized_at = datetime.now(timezone.utc)

    async def check(
        self,
        document: Document,
        repository: RegistryRepository,
        *,
        on: date,
    ) -> DocumentCheck | None:
        """Сверить распознанный документ с реестром и сохранить результат."""
        if document.status != DocumentStatus.recognized.value:
            return None

        extracted = extractor.from_payload(document.extracted)
        outcome = await reconcile(extracted, repository, on=on)

        existing = await self._session.scalar(
            select(DocumentCheck).where(
                DocumentCheck.document_id == document.id,
                DocumentCheck.checked_on == on,
            )
        )
        certificate_row = (
            await repository.get_row(outcome.certificate.registration_number)
            if outcome.certificate
            else None
        )
        best = outcome.successors[0] if outcome.successors else None
        successor_row = (
            await repository.get_row(best.certificate.registration_number)
            if best
            else None
        )

        check = existing or DocumentCheck(document_id=document.id, checked_on=on)
        check.verdict = outcome.verdict.value
        check.matched_by = outcome.matched_by.value
        check.registration_number_key = (
            certificate_row.registration_number_key if certificate_row else
            str(extracted.registration_number.value or "")
        )
        check.certificate_id = certificate_row.id if certificate_row else None
        check.successor_id = successor_row.id if successor_row else None
        check.successor_score = best.score if best else None
        check.valid_until = outcome.valid_until
        check.needs_manual_review = outcome.needs_manual_review
        check.review_reasons = list(outcome.review_reasons)
        if existing is None:
            self._session.add(check)
        await self._session.flush()
        return check
