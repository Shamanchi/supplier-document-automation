from __future__ import annotations

from datetime import date

import pytest

from app import extract as extractor
from app.fsa.schema import TextSource, Verdict
from app.models import Document, DocumentStatus
from app.ocr_client import OCRResult, OCRUnavailable
from app.repository import RegistryRepository
from app.services.documents import (
    DocumentService,
    UnsupportedDocument,
    detect_media_type,
    read_text_layer,
    sanitize_text,
)
from app.services.reconcile import reconcile
from tests.conftest import (
    CERT_NUMBER,
    DEAD_NUMBER,
    SUCCESSOR_NUMBER,
    TODAY,
    UNKNOWN_CARD_NUMBER,
)


class FakeOCR:
    """Движок-заглушка: тяжёлый runtime в тестах не нужен."""

    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls = 0

    async def recognize(self, payload: bytes, filename: str, media_type: str):
        self.calls += 1
        if self.fail:
            raise OCRUnavailable("service_busy", status_code=429)
        return OCRResult(
            engine="paddle",
            schema_version="1.0",
            page_count=1,
            duration_ms=1000,
            text=self.text,
            pages=({"page_number": 1, "mean_confidence": 0.9},),
            source_sha256="",
        )

    async def close(self) -> None:
        return None


def pdf_without_text() -> bytes:
    """Минимальный валидный PDF без текстового слоя."""
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


class TestMediaDetection:
    def test_pdf_and_png_are_detected_by_content(self):
        assert detect_media_type(b"%PDF-1.7 ...") == "application/pdf"
        assert detect_media_type(b"\x89PNG\r\n\x1a\n rest") == "image/png"

    def test_extension_does_not_help_a_wrong_file(self):
        with pytest.raises(UnsupportedDocument):
            detect_media_type(b"just text pretending to be a pdf")


class TestSanitize:
    def test_control_bytes_are_removed(self):
        # Текстовый слой PDF регулярно содержит NUL, а Postgres его не примет.
        assert sanitize_text("а\x00б\x0cв\tг\n") == "аб\x0cв\tг\n".replace("\x0c", "")

    def test_tabs_and_newlines_survive(self):
        assert sanitize_text("a\tb\nc\r") == "a\tb\nc\r"


class TestTextLayer:
    def test_png_has_no_text_layer(self):
        assert read_text_layer(b"\x89PNG\r\n\x1a\n", "image/png") == ("", 0)

    def test_broken_pdf_does_not_raise(self):
        assert read_text_layer(b"%PDF-1.4 broken", "application/pdf") == ("", 0)


class TestIngest:
    async def test_text_layer_first_skips_the_engine(self, session, certificate_text):
        import fitz

        document = fitz.open()
        page = document.new_page()
        page.insert_text((40, 60), certificate_text[:200])
        payload = document.tobytes()
        document.close()

        engine = FakeOCR(text="не должно понадобиться")
        service = DocumentService(session, engine, store=False)
        outcome = await service.ingest(payload, filename="cert.pdf")

        assert outcome.document.text_source == TextSource.TEXT_LAYER.value
        assert engine.calls == 0

    async def test_scan_goes_to_the_engine(self, session, certificate_text):
        engine = FakeOCR(text=certificate_text)
        service = DocumentService(session, engine, store=False)
        outcome = await service.ingest(pdf_without_text(), filename="scan.pdf")

        assert engine.calls == 1
        assert outcome.document.status == DocumentStatus.recognized.value
        assert outcome.document.text_source == TextSource.OCR.value
        assert outcome.document.engine == "paddle"

    async def test_second_upload_of_the_same_file_does_not_call_ocr(
        self, session, certificate_text
    ):
        engine = FakeOCR(text=certificate_text)
        service = DocumentService(session, engine, store=False)
        payload = pdf_without_text()

        first = await service.ingest(payload, filename="scan.pdf")
        second = await service.ingest(payload, filename="scan-copy.pdf", supplier="другой")

        assert engine.calls == 1
        assert first.document.id == second.document.id
        assert second.reused is True
        assert second.created is False

    async def test_force_reruns_recognition(self, session, certificate_text):
        engine = FakeOCR(text=certificate_text)
        service = DocumentService(session, engine, store=False)
        payload = pdf_without_text()

        await service.ingest(payload, filename="scan.pdf")
        outcome = await service.ingest(payload, filename="scan.pdf", force=True)

        assert engine.calls == 2
        assert outcome.reused is False

    async def test_engine_failure_is_recorded_not_raised(self, session):
        service = DocumentService(session, FakeOCR(fail=True), store=False)
        outcome = await service.ingest(pdf_without_text(), filename="scan.pdf")

        assert outcome.document.status == DocumentStatus.failed.value
        assert "service_busy" in outcome.document.error

    async def test_missing_engine_is_reported(self, session):
        service = DocumentService(session, None, store=False)
        outcome = await service.ingest(pdf_without_text(), filename="scan.pdf")
        assert outcome.document.status == DocumentStatus.failed.value


class TestCheck:
    async def test_valid_certificate_is_attachable(
        self, registry, certificate_text
    ):
        service = DocumentService(registry, FakeOCR(text=certificate_text), store=False)
        outcome = await service.ingest(pdf_without_text(), filename="cert.pdf")
        check = await service.check(
            outcome.document, RegistryRepository(registry), on=TODAY
        )

        assert check is not None
        assert check.verdict == Verdict.VALID.value
        assert check.valid_until == date(2026, 11, 14)
        assert check.needs_manual_review is False

    async def test_dead_certificate_gets_a_successor_hint(self, registry):
        text = f"СЕРТИФИКАТ СООТВЕТСТВИЯ № {DEAD_NUMBER}"
        service = DocumentService(registry, FakeOCR(text=text), store=False)
        outcome = await service.ingest(pdf_without_text(), filename="dead.pdf")
        check = await service.check(
            outcome.document, RegistryRepository(registry), on=TODAY
        )

        assert check.verdict == Verdict.TERMINATED.value
        assert check.successor_id is not None
        assert check.needs_manual_review is True

    async def test_recheck_on_the_same_day_updates_not_duplicates(
        self, registry, certificate_text
    ):
        service = DocumentService(registry, FakeOCR(text=certificate_text), store=False)
        outcome = await service.ingest(pdf_without_text(), filename="cert.pdf")
        first = await service.check(
            outcome.document, RegistryRepository(registry), on=TODAY
        )
        second = await service.check(
            outcome.document, RegistryRepository(registry), on=TODAY
        )
        assert first.id == second.id

    async def test_unrecognized_document_is_not_checked(self, registry):
        document = Document(sha256="x" * 64, filename="a.pdf", status="stored", extracted={})
        registry.add(document)
        await registry.flush()
        service = DocumentService(registry, None, store=False)
        assert await service.check(document, RegistryRepository(registry), on=TODAY) is None


class TestReconcileAgainstDatabase:
    async def _reconcile(self, session, text: str):
        document = extractor.extract(
            text, doc_id="sha256:test", text_source=TextSource.TEXT_LAYER
        )
        return await reconcile(document, RegistryRepository(session), on=TODAY)

    async def test_known_number_without_card_is_unknown(self, registry):
        outcome = await self._reconcile(
            registry, f"СЕРТИФИКАТ СООТВЕТСТВИЯ № {UNKNOWN_CARD_NUMBER}"
        )
        assert outcome.verdict is Verdict.UNKNOWN

    async def test_absent_number_is_not_found(self, registry):
        outcome = await self._reconcile(
            registry, "СЕРТИФИКАТ СООТВЕТСТВИЯ № ЕАЭС RU С-RU.ЗЗ99.В.09999/25"
        )
        assert outcome.verdict is Verdict.NOT_FOUND

    async def test_passport_is_checked_by_referenced_certificate(self, registry):
        outcome = await self._reconcile(
            registry, f"ПАСПОРТ и руководство по эксплуатации. Сертификат {CERT_NUMBER}"
        )
        assert outcome.verdict is Verdict.VALID
        assert any("из ссылки в паспорте" in r for r in outcome.review_reasons)


class TestRepository:
    async def test_search_finds_by_manufacturer_and_number(self, registry):
        repository = RegistryRepository(registry)
        assert await repository.search("Генераторы")
        assert await repository.search("ПБ97")

    async def test_stats_counts_alive_by_date(self, registry):
        stats = await RegistryRepository(registry).stats(TODAY)
        assert stats.certificates == 3
        # Живых двое: третий прекращён и просрочен.
        assert stats.alive == 2

    async def test_successor_search_needs_same_manufacturer(self, registry):
        repository = RegistryRepository(registry)
        dead = await repository.get(DEAD_NUMBER)
        found = await repository.find_successors(dead, TODAY)
        assert [c.certificate.registration_number for c in found] == [SUCCESSOR_NUMBER]


class TestBrokenDocuments:
    def test_zero_page_pdf_is_rejected_before_the_engine(self):
        """У структурно битого PDF ноль страниц; движок ответил бы 400."""
        from app.services.documents import BrokenDocument, probe_pdf

        with pytest.raises(BrokenDocument):
            probe_pdf(b"%PDF-1.4\ntrailer<<>>\n%%EOF\n")

    async def test_broken_pdf_gets_a_readable_reason(self, session):
        engine = FakeOCR(text="не должно понадобиться")
        service = DocumentService(session, engine, store=False)
        outcome = await service.ingest(
            b"%PDF-1.4\ntrailer<<>>\n%%EOF\n", filename="broken.pdf"
        )

        assert outcome.document.status == DocumentStatus.failed.value
        # Причина человеческая, а не код движка.
        assert "PDF" in outcome.document.error
        assert engine.calls == 0

    async def test_store_only_does_not_recognize(self, session):
        service = DocumentService(session, FakeOCR(text="x"), store=False)
        outcome = await service.store_only(pdf_without_text(), filename="scan.pdf")

        assert outcome.created is True
        assert outcome.document.status == DocumentStatus.stored.value
        assert outcome.document.ocr_text == ""

    async def test_store_only_is_idempotent(self, session):
        service = DocumentService(session, None, store=False)
        payload = pdf_without_text()
        first = await service.store_only(payload, filename="a.pdf")
        second = await service.store_only(payload, filename="b.pdf")

        assert first.document.id == second.document.id
        assert second.reused is True
