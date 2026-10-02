"""Определение формата файла по содержимому (docs/api/uploads §Проверки Backend).

Расширению и заголовку `Content-Type` доверять нельзя: и то и другое задаёт
клиент. Формат определяется по сигнатуре, а расширение служит лишь вторым
мнением — расхождение означает отказ, а не молчаливое исправление.

Модуль намеренно ничего не знает про HTTP: он возвращает формат либо бросает
`FileRejected` с причиной, а сопоставление причин со статусами — дело сервиса.

Содержимое файла здесь не исполняется и не разбирается офисным движком: XLSX и
DOCX вскрываются как обычный ZIP, поэтому формулы таблицы остаются данными
(uploads QA: «spreadsheet с формулами не исполняется сервером»).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO

PDF_MEDIA_TYPE = "application/pdf"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"

#: Маркер внутри OOXML-контейнера, по которому различаются форматы.
_OOXML_MARKERS = (
    ("xl/workbook.xml", "xlsx", XLSX_MEDIA_TYPE),
    ("word/document.xml", "docx", DOCX_MEDIA_TYPE),
)


class RejectionReason(StrEnum):
    """Причины отказа. Значения совпадают с кодами ошибок контракта uploads."""

    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_SIGNATURE_MISMATCH = "FILE_SIGNATURE_MISMATCH"
    MALICIOUS_FILE_DETECTED = "MALICIOUS_FILE_DETECTED"


class FileRejected(Exception):
    def __init__(self, reason: RejectionReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ZipLimits:
    """Границы разбора OOXML-контейнера."""

    max_entries: int
    max_uncompressed_bytes: int


@dataclass(frozen=True)
class DetectedFormat:
    extension: str
    media_type: str


def detect(data: bytes, *, file_name: str, limits: ZipLimits) -> DetectedFormat:
    """Определяет формат по содержимому и сверяет его с расширением имени."""
    detected = _detect_by_content(data, limits=limits)
    _assert_extension_matches(file_name, detected)
    return detected


def safe_file_name(file_name: str) -> str:
    """Имя файла для метаданных: без путей, разделителей и управляющих символов.

    Storage key из имени не строится вовсе (контракт, пункт 5), но имя попадает
    в ответы API и в отчёты, поэтому «../../etc/passwd» не должен доезжать даже
    как строка.
    """
    tail = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(char for char in tail if char.isprintable()).strip()
    # Ведущие точки убираются вместе со скрытыми именами вида «.env»: имя
    # документа, начинающееся с точки, — почти наверняка попытка что-то скрыть.
    cleaned = cleaned.lstrip(". ")
    return cleaned or "document"


def _detect_by_content(data: bytes, *, limits: ZipLimits) -> DetectedFormat:
    if data.startswith(_PDF_MAGIC):
        return DetectedFormat("pdf", PDF_MEDIA_TYPE)
    if data.startswith(_ZIP_MAGIC):
        return _detect_ooxml(data, limits=limits)
    raise FileRejected(
        RejectionReason.UNSUPPORTED_FILE_TYPE,
        "Поддерживаются только PDF, XLSX и DOCX",
    )


def _detect_ooxml(data: bytes, *, limits: ZipLimits) -> DetectedFormat:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            _assert_not_zip_bomb(archive, limits=limits)
    except zipfile.BadZipFile as broken:
        # Обрезанный или повреждённый файл: сигнатура ZIP есть, содержимого нет.
        raise FileRejected(
            RejectionReason.FILE_SIGNATURE_MISMATCH,
            "Файл повреждён или обрезан: контейнер не читается",
        ) from broken

    if "[Content_Types].xml" not in names:
        raise FileRejected(
            RejectionReason.UNSUPPORTED_FILE_TYPE,
            "ZIP-архив не является документом Office",
        )
    for marker, extension, media_type in _OOXML_MARKERS:
        if marker in names:
            return DetectedFormat(extension, media_type)
    raise FileRejected(
        RejectionReason.UNSUPPORTED_FILE_TYPE,
        "Поддерживаются только PDF, XLSX и DOCX",
    )


def _assert_not_zip_bomb(archive: zipfile.ZipFile, *, limits: ZipLimits) -> None:
    """Проверяет объявленные размеры, ничего не распаковывая.

    Заголовки ZIP могут врать, но zip bomb выдаёт себя именно ими: распаковка
    ради проверки означала бы согласиться на ту самую атаку.
    """
    entries = archive.infolist()
    if len(entries) > limits.max_entries:
        raise FileRejected(
            RejectionReason.MALICIOUS_FILE_DETECTED,
            f"В контейнере больше {limits.max_entries} элементов",
        )
    total = sum(entry.file_size for entry in entries)
    if total > limits.max_uncompressed_bytes:
        raise FileRejected(
            RejectionReason.MALICIOUS_FILE_DETECTED,
            "Распакованный объём контейнера превышает допустимый",
        )


def _assert_extension_matches(file_name: str, detected: DetectedFormat) -> None:
    """Расхождение расширения и содержимого — отказ, а не тихое исправление.

    Файл без расширения принимается: имя — метаданные, а формат уже определён по
    содержимому. Отвергается именно ложное расширение (uploads QA).
    """
    _, dot, extension = safe_file_name(file_name).rpartition(".")
    if not dot:
        return
    extension = extension.casefold()
    known = {ext for _, ext, _ in _OOXML_MARKERS} | {"pdf"}
    if extension in known and extension != detected.extension:
        raise FileRejected(
            RejectionReason.FILE_SIGNATURE_MISMATCH,
            f"Расширение .{extension} не соответствует содержимому"
            f" ({detected.extension})",
        )
