"""Загрузка файлов реестра, добавленных руками.

Автоматической выкачки набора здесь нет намеренно: файлы кладут вручную, а
сервису нужен только приём. Поддерживаются три вида содержимого — CSV с
карточками, список номеров и 7z-архив из набора открытых данных, который
распаковывается на лету.
"""

from __future__ import annotations

import csv
import hashlib
import io
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TextIO

import psycopg2

from app.config import settings
from app.fsa.mapping import (
    DATE_FIELDS,
    INDEX_COLUMNS,
    LEGACY_COLUMN_ORDER,
    RegistrySchema,
    columns_for,
    detect_schema,
)
from app.fsa.normalize import normalize_number, parse_date
from app.fsa.schema import CertificateStatus
from app.models import RegistryImportKind

COLUMNS = (
    "id",
    "registration_number_key",
    "registration_number",
    "status",
    "status_raw",
    *[name for name in INDEX_COLUMNS if name not in {"registration_number", "status"}],
    "source_slice",
    "imported_at",
)

# Первая строка CSV карточек содержит эти заголовки; список номеров — просто
# по одному номеру на строку.
INDEX_MARKER = "Номер СС"


class RegistryFileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImportResult:
    certificates: int
    numbers: int


def sha256_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sync_dsn() -> str:
    return settings.database_url.replace("+asyncpg", "")


def detect_kind(filename: str, payload: bytes) -> RegistryImportKind:
    """Вид файла определяется по содержимому, расширение — только подсказка."""
    if payload[:6] == b"7z\xbc\xaf\x27\x1c":
        return RegistryImportKind.archive
    head = payload[:4096].decode("utf-8", errors="replace")
    if INDEX_MARKER in head or "reg_number" in head.splitlines()[0]:
        return RegistryImportKind.index
    first_line = head.splitlines()[0] if head.strip() else ""
    delimiter = detect_delimiter(first_line)
    first_row = next(csv.reader([first_line], delimiter=delimiter), []) if first_line else []
    if len(first_row) == len(LEGACY_COLUMN_ORDER) and normalize_number(first_row[3].strip()):
        return RegistryImportKind.index
    first = head.splitlines()[0].strip() if head.strip() else ""
    if first and normalize_number(first) == first and ";" not in first:
        return RegistryImportKind.numbers
    raise RegistryFileError(
        "не удалось определить содержимое: ожидается CSV карточек, "
        "список номеров или 7z-архив набора"
    )


def extract_archive(payload: bytes, directory: str) -> list[Path]:
    """Распаковать 7z и вернуть пути, не загружая многогигабайтный набор в RAM."""
    archive = Path(directory) / "data.7z"
    archive.write_bytes(payload)
    try:
        subprocess.run(
            ["7z", "x", "-y", f"-o{directory}", str(archive)],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise RegistryFileError("на хосте нет 7z для распаковки архива") from error
    except subprocess.CalledProcessError as error:
        raise RegistryFileError("архив не распаковался") from error
    return sorted(Path(directory).rglob("*.csv"))


# Разделитель в выгрузках Росаккредитации не один: часть срезов приходит с
# точкой с запятой, часть — с вертикальной чертой. Определять его по заголовку
# дешевле, чем требовать от оператора знать формат конкретного файла, и
# надёжнее, чем принять один вариант и молча загрузить ноль карточек.
def detect_delimiter(header: str) -> str:
    return max(";|,\t", key=header.count)


def _certificate_rows_from_text(text: TextIO, source: str) -> tuple[IO[str], int]:
    csv.field_size_limit(2**31 - 1)
    now = datetime.now(timezone.utc).isoformat()
    # COPY-буфер уходит на диск после 8 МБ: один исторический CSV может весить
    # сотни мегабайт, поэтому держать его преобразованную копию в RAM нельзя.
    buffer = tempfile.SpooledTemporaryFile(
        max_size=8 * 1024 * 1024, mode="w+", encoding="utf-8", newline=""
    )
    writer = csv.writer(buffer, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
    seen: set[str] = set()
    total = 0

    first_line = text.readline()
    delimiter = detect_delimiter(first_line)
    schema = detect_schema(first_line)
    text.seek(0)
    if schema is RegistrySchema.LEGACY_HEADLESS:
        reader = csv.DictReader(text, fieldnames=LEGACY_COLUMN_ORDER, delimiter=delimiter)
    else:
        reader = csv.DictReader(text, delimiter=delimiter)
    field_map = columns_for(schema)
    for row in reader:
        number = (row.get(field_map["registration_number"]) or "").strip()
        if not number:
            continue
        key = (row.get("Ключ номера") or normalize_number(number)).strip()
        # Дубликаты внутри одной пачки COPY не отсекает, в отличие от UPSERT.
        if key in seen:
            continue
        seen.add(key)

        status_raw = (row.get(field_map["status"]) or "").strip()
        values = [
            str(uuid.uuid4()),
            key,
            number,
            CertificateStatus.parse(status_raw).value,
            status_raw,
        ]
        for name in COLUMNS[5:-2]:
            source_column = field_map.get(name)
            raw = (row.get(source_column) or "").strip() if source_column else ""
            if name == "okpd2_tnved" and schema is not RegistrySchema.MODERN:
                raw = " ".join(
                    part.strip()
                    for part in (row.get("product_okpd2") or "", row.get("product_tn_ved") or "")
                    if part.strip()
                )
            if name in DATE_FIELDS:
                parsed = parse_date(raw)
                values.append(parsed.isoformat() if parsed else "\\N")
            else:
                values.append(raw.replace("\t", " ").replace("\n", " "))
        values.append((row.get("Источник среза") or source).strip())
        values.append(now)
        writer.writerow(values)
        total += 1

    buffer.seek(0)
    return buffer, total


def _certificate_rows(payload: bytes, source: str) -> tuple[IO[str], int]:
    return _certificate_rows_from_text(
        io.StringIO(payload.decode("utf-8-sig", errors="replace")), source
    )


def load_certificates(connection, payload: bytes, source: str) -> int:
    buffer, total = _certificate_rows(payload, source)
    return _copy_certificates(connection, buffer, total)


def load_certificates_path(connection, path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as text:
        buffer, total = _certificate_rows_from_text(text, path.name)
    return _copy_certificates(connection, buffer, total)


def _copy_certificates(connection, buffer: IO[str], total: int) -> int:
    if not total:
        buffer.close()
        return 0
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS tmp_cert")
            cursor.execute(
                "CREATE TEMP TABLE tmp_cert (LIKE registry_certificate) ON COMMIT DROP"
            )
            cursor.copy_expert(
                "COPY tmp_cert (" + ", ".join(COLUMNS) + ") FROM STDIN WITH "
                "(FORMAT csv, DELIMITER E'\t', NULL '\\N')",
                buffer,
            )
            assignments = ", ".join(
                f"{name} = EXCLUDED.{name}"
                for name in COLUMNS
                if name not in {"id", "registration_number_key"}
            )
            cursor.execute(
                f"INSERT INTO registry_certificate ({', '.join(COLUMNS)}) "
                f"SELECT {', '.join(COLUMNS)} FROM tmp_cert "
                f"ON CONFLICT (registration_number_key) DO UPDATE SET {assignments}"
            )
    finally:
        buffer.close()
    return total


def load_numbers(connection, payload: bytes) -> int:
    buffer = io.StringIO()
    total = 0
    for line in payload.decode("utf-8", errors="replace").splitlines():
        key = line.strip()
        if key:
            buffer.write(key + "\n")
            total += 1
    if not total:
        return 0
    buffer.seek(0)
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS tmp_num")
        cursor.execute(
            "CREATE TEMP TABLE tmp_num (registration_number_key text) ON COMMIT DROP"
        )
        cursor.copy_expert("COPY tmp_num FROM STDIN", buffer)
        cursor.execute(
            "INSERT INTO registry_number (registration_number_key) "
            "SELECT DISTINCT registration_number_key FROM tmp_num "
            "ON CONFLICT DO NOTHING"
        )
    return total


def import_payload(
    payload: bytes, *, filename: str, kind: RegistryImportKind
) -> ImportResult:
    """Разобрать файл и залить его в базу. Возвращает счётчики."""
    certificates = 0
    numbers = 0
    connection = psycopg2.connect(sync_dsn())
    try:
        if kind is RegistryImportKind.archive:
            with tempfile.TemporaryDirectory() as directory:
                members = extract_archive(payload, directory)
                if not members:
                    raise RegistryFileError("в архиве нет CSV")
                for path in members:
                    certificates += load_certificates_path(connection, path)
                    connection.commit()
        elif kind is RegistryImportKind.index:
            certificates = load_certificates(connection, payload, filename)
        else:
            numbers = load_numbers(connection, payload)
        connection.commit()
    finally:
        connection.close()
    return ImportResult(certificates=certificates, numbers=numbers)
