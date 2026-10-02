"""Распознавание паспортов и сертификатов PDF по текстовому слою.

Спецификация (issue «Разпознавание паспортов и сертификатов PDF с помощью
OCR», высокий приоритет):

- система читает типовые PDF-документы с текстовым слоем — паспорта и
  сертификаты;
- у паспорта наименование изделия берётся с первой и второй страниц;
- сертификат указан на предпоследней и последней страницах паспорта —
  распознаётся его номер и срок действия;
- файл сертификата распознаётся как сертификат с регистрационным номером;
- скан без текстового слоя не «выдумывает» реквизиты: помечается как
  требующий OCR.

Тесты построены на сгенерированных PDF-фикстурах с настоящим текстовым
слоем (backend/tests/fixtures/documents), а не на коде реализации.
"""

from __future__ import annotations

from pathlib import Path

from app import document_recognition as recognition
from app.document_recognition import DOC_CERTIFICATE, DOC_OTHER, DOC_PASSPORT

FIXTURES = Path(__file__).parent / "fixtures" / "documents"

PASSPORT_2P = FIXTURES / "passport_2p.pdf"
PASSPORT_4P = FIXTURES / "passport_4p.pdf"
PASSPORT_NAME_PAGE2 = FIXTURES / "passport_name_page2.pdf"
CERTIFICATE_1P = FIXTURES / "certificate_1p.pdf"
SCAN_NO_TEXT = FIXTURES / "scan_notext.pdf"


def _recognize(path: Path) -> recognition.RecognizeResult:
    return recognition.recognize_pdf(path, file=path.name)


class TestPassportNameFromFirstTwoPages:
    def test_name_on_first_page_before_passport_marker(self):
        """Наименование — строки перед «Паспорт»/«РЭ» на первой странице."""
        result = _recognize(PASSPORT_2P)
        assert result.doc_class == DOC_PASSPORT
        assert result.page_count == 2
        assert result.product_name == "ОГНЕТУШИТЕЛИ ПОРОШКОВЫЕ ЗАКАЧНЫЕ МИГ"

    def test_name_keeps_designation_from_first_page(self):
        """Титул типа «ГЕНЕРАТОРЫ … / ГГПТ-7,0 / Паспорт» сохраняет ряд."""
        result = _recognize(PASSPORT_4P)
        assert result.product_name is not None
        assert "ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ" in result.product_name
        assert "ГГПТ-7,0" in result.product_name

    def test_name_is_read_from_second_page_when_first_is_cover(self):
        """Система читает первую И вторую страницы: название может быть на 2-й."""
        result = _recognize(PASSPORT_NAME_PAGE2)
        assert result.doc_class == DOC_PASSPORT
        assert result.page_count == 2
        assert result.product_name == "ОГНЕТУШИТЕЛЬ ПОРОШКОВЫЙ ПЕРЕДВИЖНОЙ ЗАКАЧНОЙ"

    def test_junk_from_cover_does_not_leak_into_name(self):
        """Реквизиты изготовителя/адреса не попадают в наименование."""
        result = _recognize(PASSPORT_NAME_PAGE2)
        assert result.product_name
        upper = result.product_name.upper()
        assert "ПОЖТЕХНИКА" not in upper
        assert "БЕЛАРУСЬ" not in upper
        assert "ВИТЕБСК" not in upper

    def test_code_line_of_passport_is_not_the_name(self):
        """Служебная строка «ГГПТ-7,0 ПС» не становится наименованием."""
        result = _recognize(PASSPORT_4P)
        assert result.product_name
        assert "ПС" not in result.product_name.split()[-1]


class TestCertificateMentionOnLastPages:
    def test_passport_2p_certificate_on_last_page(self):
        """Двухстраничный паспорт: сертификат ищется на последней странице."""
        result = _recognize(PASSPORT_2P)
        assert result.certificates
        mention = result.certificates[0]
        assert mention.page == 2
        assert "ЕАЭС RU C-RU.ЧС13.В.00348/21" == mention.number
        assert mention.valid_until == "09.11.2026"

    def test_passport_4p_certificate_on_last_page_not_earlier(self):
        """Предпоследняя/последняя страница: номер не ищется по всему PDF."""
        result = _recognize(PASSPORT_4P)
        assert result.certificates
        mention = result.certificates[0]
        assert mention.page == 4
        assert "ЕАЭС RU С-RU.ПБ97.В.00627/25" == mention.number
        assert mention.valid_until == "10.03.2029"

    def test_no_certificates_invented_when_last_pages_clean(self):
        """Нет упоминаний — пустой список, а не фиктивный номер."""
        # Одностраничный сертификат ниже; для паспортов проверяем через
        # строковый вызов: страницы без слова «сертификат» не дают номеров.
        mentions = recognition.find_certificate_mentions(
            (
                "1 НАЗНАЧЕНИЕ\nОгнетушители предназначены для тушения пожаров.",
                "7 МЕРЫ БЕЗОПАСНОСТИ\nСоблюдать осторожность.",
            )
        )
        assert mentions == []


class TestCertificateFile:
    def test_certificate_document_class_and_number(self):
        result = _recognize(CERTIFICATE_1P)
        assert result.doc_class == DOC_CERTIFICATE
        assert result.registration_number == "ЕАЭС RU С-RU.ПБ01.В.00272/21"
        assert result.product_name is None

    def test_certificate_validity_period_from_blank(self):
        """«СРОК ДЕЙСТВИЯ С … ПО …» на бланке сертификата распознаётся."""
        mentions = recognition.find_certificate_mentions(
            (
                "ЕВРАЗИЙСКИЙ ЭКОНОМИЧЕСКИЙ СОЮЗ\nСЕРТИФИКАТ СООТВЕТСТВИЯ\n"
                "№ ЕАЭС RU С-RU.ПБ01.В.00272/21\n"
                "СРОК ДЕЙСТВИЯ С 15.11.2021 ПО 14.11.2026 ВКЛЮЧИТЕЛЬНО",
            ),
            whole_document=True,
        )
        assert mentions
        assert mentions[0].number == "ЕАЭС RU С-RU.ПБ01.В.00272/21"
        assert mentions[0].valid_until == "14.11.2026"


class TestNoTextLayer:
    def test_scan_is_marked_for_ocr_without_fabricated_fields(self):
        result = _recognize(SCAN_NO_TEXT)
        assert result.has_text_layer is False
        assert result.product_name is None
        assert result.certificates == ()
        assert result.error and "no_text_layer" in result.error


class TestClassification:
    def test_certificate_marker_wins_when_it_opens_document(self):
        text = (
            "ЕВРАЗИЙСКИЙ ЭКОНОМИЧЕСКИЙ СОЮЗ\n"
            "СЕРТИФИКАТ СООТВЕТСТВИЯ\n№ ЕАЭС RU С-RU.ПБ01.В.00272/21"
        )
        assert recognition.classify(text) == DOC_CERTIFICATE

    def test_passport_marker_wins_when_title_is_on_first_page(self):
        """«ПАСПОРТ» на титуле — паспорт, даже если ниже упомянут сертификат."""
        text = (
            "ОГНЕТУШИТЕЛИ ПОРОШКОВЫЕ ЗАКАЧНЫЕ МИГ\n"
            "ПАСПОРТ\n"
            "Качество изделия подтверждено сертификатом соответствия\n"
            "№ ЕАЭС RU С-RU.ЧС13.В.00348/21"
        )
        assert recognition.classify(text) == DOC_PASSPORT

    def test_passport_title_beats_inline_certificate_mention_on_cover(self):
        """Обложка «Сертификат соответствия: № … / ПАСПОРТ» — всё равно паспорт."""
        text = (
            "ОП-1(з) АВСЕ-01, ОП-2(з) ВСЕ-02\n"
            "Сертификат соответствия: № C-RU. ПБ04.В.01851 Внимание!\n"
            "КОРПУС ОГНЕТУШИТЕЛЯ НАХОДИТСЯ ПОД ДАВЛЕНИЕМ\n"
            "ПАСПОРТ\n"
            "(Руководство по эксплуатации)"
        )
        assert recognition.classify(text) == DOC_PASSPORT

    def test_unmarked_document_is_other(self):
        assert recognition.classify("Счёт № 42 от 01.01.2026") == DOC_OTHER


class TestNameEdgeCases:
    def test_name_requires_real_text(self):
        assert recognition.find_product_name(()) is None
        assert recognition.find_product_name(("",)) is None

    def test_name_not_found_on_empty_cover(self):
        """Титул без названия и без маркера не даёт ложного имени."""
        name = recognition.find_product_name(
            ("ООО «Пример»\nwww.example.org\n",)
        )
        assert name is None