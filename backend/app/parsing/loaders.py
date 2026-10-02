"""Loader registry: detect the physical file format by magic bytes/content
(not by extension) and turn each supported format into per-sheet grids so the
same header/mapping pipeline runs downstream.

Loaders: xlsx | xls | pdf_text | pdf_scan | docx | doc | txt.
pdf_scan is terminal at detection time and maps to needs_ocr; legacy doc is
converted into DOCX before it reaches the common grid pipeline.
"""
from __future__ import annotations

import csv
import io
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import zipfile
from xml.etree import ElementTree

from app.parsing.grid import MAX_COLS, MAX_ROWS, Cell, SheetGrid, load_grids

LOADER_XLSX = "xlsx"
LOADER_XLS = "xls"
LOADER_PDF_TEXT = "pdf_text"
LOADER_PDF_SCAN = "pdf_scan"
LOADER_DOCX = "docx"
LOADER_DOC = "doc"
LOADER_TXT = "txt"

GRID_LOADERS = {LOADER_XLSX, LOADER_XLS, LOADER_PDF_TEXT, LOADER_DOCX, LOADER_DOC, LOADER_TXT}

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Directory entry names inside an OLE compound file are stored UTF-16LE.
_OLE_WORD = "WordDocument".encode("utf-16-le")
_OLE_WORKBOOK = "Workbook".encode("utf-16-le")
_OLE_BOOK = "Book".encode("utf-16-le")

# A pdf page with fewer extractable characters than this is considered
# scanned; a file where every sampled page is scanned gets pdf_scan.
PDF_TEXT_CHARS_PER_PAGE = 50
PDF_PROBE_PAGES = 5
DOC_CONVERSION_TIMEOUT_SECONDS = 60
SPREADSHEET_CONVERSION_TIMEOUT_SECONDS = 120

# Archives are accepted only as office documents or as the one supported
# supplier XML export below.  These limits are deliberately shared by all ZIP
# paths, not just the two legacy wrappers: neither openpyxl nor python-docx
# should be handed an unbounded archive after the initial format probe.
MAX_ZIP_MEMBERS = 256
MAX_ZIP_MEMBER_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100
ZIP_READ_CHUNK_BYTES = 64 * 1024

OFFER_XML_SHEET_NAME = "offers"
OFFER_XML_HEADERS = [
    "Артикул",
    "Наименование",
    "Цена 1",
    "Цена 2",
    "Цена 3",
    "Цена 4",
    "Цена 5",
]


class UnknownFormatError(Exception):
    """Content does not match any supported loader."""


class PdfNoTablesError(Exception):
    """Textual PDF where pdfplumber found no tables on any page."""


class DocumentConversionError(Exception):
    """Legacy Word document could not be converted into a DOCX grid."""


def detect_loader(data: bytes, filename: str = "") -> str:
    if _is_scanned_urls_manifest(filename, data):
        raise UnknownFormatError("Служебный манифест SCANNED_URLS не является прайс-листом")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if data[:4] == b"PK\x03\x04":
        return _detect_zip(data)
    if data[:8] == _OLE_MAGIC:
        return _detect_ole(data, ext)
    if b"%PDF" in data[:1024]:
        return _probe_pdf(data)
    if _looks_like_text(data):
        return LOADER_TXT
    raise UnknownFormatError("Не удалось распознать формат файла по содержимому")


def _detect_zip(data: bytes) -> str:
    with _safe_zip_archive(data) as (zf, members):
        names = [member.filename for member in members]
        if any(name.startswith("xl/") for name in names):
            return LOADER_XLSX
        if any(name.startswith("word/") for name in names):
            return LOADER_DOCX
        if _find_wrapped_xls_member(zf, members) is not None:
            return LOADER_XLS
        if _find_offer_xml_member(zf, members) is not None:
            # The existing generic tabular pipeline handles the normalized
            # grid, so retain the established text-like loader identity
            # rather than add a special rule/UI branch for one vendor export.
            return LOADER_TXT

    # Extensions are intentionally not a fallback for arbitrary ZIP files.
    # They are commonly wrong for the two supported wrappers and would make a
    # manifest or a malformed archive reach an office parser.
    raise UnknownFormatError("Zip-контейнер без офисного содержимого")


def _detect_ole(data: bytes, ext: str) -> str:
    has_word = _OLE_WORD in data
    has_book = _OLE_WORKBOOK in data or _OLE_BOOK in data
    if has_word and not has_book:
        return LOADER_DOC
    if has_book and not has_word:
        return LOADER_XLS
    # Ambiguous or neither entry found in raw scan — fall back to extension.
    if ext == "doc":
        return LOADER_DOC
    return LOADER_XLS


def _probe_pdf(data: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = pdf.pages[:PDF_PROBE_PAGES]
        if not pages:
            return LOADER_PDF_SCAN
        for page in pages:
            text = page.extract_text() or ""
            if len(text.strip()) >= PDF_TEXT_CHARS_PER_PAGE:
                return LOADER_PDF_TEXT
    return LOADER_PDF_SCAN


def _looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    for enc in ("utf-8", "cp1251"):
        try:
            sample.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _is_scanned_urls_manifest(filename: str, data: bytes) -> bool:
    """Exclude corpus crawl manifests even if they are uploaded as text/CSV.

    They contain URLs to *find* price lists, not prices themselves.  Checking
    a small content signature also covers a renamed copy while avoiding a
    broad file-extension rule.
    """
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if basename.startswith("scanned_urls"):
        return True

    sample = _decode_text(data[:8192]).lstrip("\ufeff").casefold()
    if sample.startswith("url,company,domain,"):
        return True
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    return len(lines) >= 3 and all(line.startswith(("http://", "https://")) for line in lines[:3])


def _zip_member_name_is_safe(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def _validate_zip_members(members: list[zipfile.ZipInfo]) -> None:
    if len(members) > MAX_ZIP_MEMBERS:
        raise UnknownFormatError(f"Zip-контейнер содержит более {MAX_ZIP_MEMBERS} файлов")

    total_size = 0
    for member in members:
        if not _zip_member_name_is_safe(member.filename):
            raise UnknownFormatError("Zip-контейнер содержит небезопасный путь")
        if member.flag_bits & 0x1:
            raise UnknownFormatError("Зашифрованные zip-контейнеры не поддерживаются")
        if member.is_dir():
            continue
        if member.file_size > MAX_ZIP_MEMBER_UNCOMPRESSED_BYTES:
            raise UnknownFormatError("Файл внутри zip-контейнера превышает допустимый размер")
        total_size += member.file_size
        if total_size > MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES:
            raise UnknownFormatError("Суммарный размер zip-контейнера превышает допустимый")
        compressed_size = member.compress_size
        if member.file_size and (
            compressed_size == 0
            or member.file_size > compressed_size * MAX_ZIP_COMPRESSION_RATIO
        ):
            raise UnknownFormatError("Подозрительно высокая степень сжатия zip-контейнера")


@contextmanager
def _safe_zip_archive(data: bytes):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UnknownFormatError("Повреждённый zip-контейнер") from exc
    try:
        members = zf.infolist()
        _validate_zip_members(members)
        yield zf, members
    finally:
        zf.close()


def _read_zip_member(zf: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    """Read one already-validated member with an actual decompression cap."""
    chunks: list[bytes] = []
    total_size = 0
    try:
        with zf.open(member) as stream:
            while True:
                chunk = stream.read(ZIP_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_ZIP_MEMBER_UNCOMPRESSED_BYTES:
                    raise UnknownFormatError("Файл внутри zip-контейнера превышает допустимый размер")
                chunks.append(chunk)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise UnknownFormatError("Не удалось безопасно прочитать файл из zip-контейнера") from exc
    return b"".join(chunks)


def _find_wrapped_xls_member(
    zf: zipfile.ZipFile, members: list[zipfile.ZipInfo]
) -> zipfile.ZipInfo | None:
    matches: list[zipfile.ZipInfo] = []
    for member in members:
        if member.is_dir() or not member.filename.casefold().endswith(".xls"):
            continue
        if _read_zip_member(zf, member).startswith(_OLE_MAGIC):
            matches.append(member)
    if len(matches) > 1:
        raise UnknownFormatError("Zip-контейнер содержит несколько вложенных XLS-файлов")
    return matches[0] if matches else None


def _reject_unsafe_xml(data: bytes) -> None:
    # External entities and DTDs are not needed in a supplier export and can
    # otherwise turn an XML parse into entity expansion or external I/O.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise UnknownFormatError("XML с DTD или сущностями не поддерживается")


def _xml_local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].casefold()


def _looks_like_offer_xml(data: bytes) -> bool:
    if b"<offers" not in data or b"<offer" not in data:
        return False
    try:
        _reject_unsafe_xml(data)
        saw_offers = False
        saw_offer = False
        for event, element in ElementTree.iterparse(io.BytesIO(data), events=("start", "end")):
            if event == "start":
                tag = _xml_local_name(element.tag)
                saw_offers = saw_offers or tag == "offers"
                saw_offer = saw_offer or tag == "offer"
            elif _xml_local_name(element.tag) != "offers":
                element.clear()
        return saw_offers and saw_offer
    except (ElementTree.ParseError, UnknownFormatError):
        return False


def _find_offer_xml_member(
    zf: zipfile.ZipFile, members: list[zipfile.ZipInfo]
) -> zipfile.ZipInfo | None:
    matches: list[zipfile.ZipInfo] = []
    for member in members:
        if member.is_dir() or not member.filename.casefold().endswith(".xml"):
            continue
        if _looks_like_offer_xml(_read_zip_member(zf, member)):
            matches.append(member)
    if len(matches) > 1:
        raise UnknownFormatError("Zip-контейнер содержит несколько XML-выгрузок товаров")
    return matches[0] if matches else None


# --- grid loading ------------------------------------------------------------

def load_grids_for(loader: str, data: bytes, filename: str) -> list[SheetGrid]:
    if loader == LOADER_XLSX:
        _validate_zip_archive(data)
        return load_grids(data, filename if filename.lower().endswith((".xlsx", ".xlsm")) else "f.xlsx")
    if loader == LOADER_XLS:
        if data[:4] == b"PK\x03\x04":
            data = _extract_wrapped_xls(data)
        return _load_xls_grids(data)
    if loader == LOADER_PDF_TEXT:
        return _load_pdf_grids(data)
    if loader == LOADER_DOCX:
        _validate_zip_archive(data)
        return _load_docx_grids(data)
    if loader == LOADER_DOC:
        return _load_doc_grids(data)
    if loader == LOADER_TXT:
        if data[:4] == b"PK\x03\x04":
            return _load_offer_xml_from_zip(data)
        if _looks_like_offer_xml(data):
            return _load_offer_xml_grids(data)
        return _load_txt_grids(data)
    raise UnknownFormatError(f"Загрузчик {loader} не поддерживает табличный разбор")


def _rows_to_cells(raw_rows: list[list[object | None]]) -> list[list[Cell]]:
    rows: list[list[Cell]] = []
    for raw in raw_rows[:MAX_ROWS]:
        cells: list[Cell] = []
        for v in raw[:MAX_COLS]:
            if isinstance(v, str):
                v = v.strip() or None
            cells.append(Cell(value=v))
        rows.append(cells)
    return rows


def _validate_zip_archive(data: bytes) -> None:
    with _safe_zip_archive(data):
        pass


def _extract_wrapped_xls(data: bytes) -> bytes:
    with _safe_zip_archive(data) as (zf, members):
        member = _find_wrapped_xls_member(zf, members)
        if member is None:
            raise UnknownFormatError("В zip-контейнере не найден вложенный XLS-файл")
        return _read_zip_member(zf, member)


def _load_offer_xml_from_zip(data: bytes) -> list[SheetGrid]:
    with _safe_zip_archive(data) as (zf, members):
        member = _find_offer_xml_member(zf, members)
        if member is None:
            raise UnknownFormatError("В zip-контейнере не найдена XML-выгрузка товаров")
        return _load_offer_xml_grids(_read_zip_member(zf, member))


def _offer_xml_value(offer: ElementTree.Element, tag_name: str) -> str | None:
    for child in offer:
        if _xml_local_name(child.tag) != tag_name:
            continue
        value = (child.text or "").strip()
        return value or None
    return None


def _load_offer_xml_grids(data: bytes) -> list[SheetGrid]:
    """Normalize the documented ``Tree/offers/offer`` export to one grid.

    Only product identity and the five published price tiers are exposed.  In
    particular, category/brand dictionaries and crawl manifests cannot become
    accidental product rows.
    """
    _reject_unsafe_xml(data)
    raw_rows: list[list[object | None]] = [list(OFFER_XML_HEADERS)]
    offers_depth = 0
    saw_offers = False
    try:
        for event, element in ElementTree.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = _xml_local_name(element.tag)
            if event == "start":
                if tag == "offers":
                    offers_depth += 1
                    saw_offers = True
                continue

            if tag == "offer" and offers_depth:
                raw_rows.append(
                    [
                        _offer_xml_value(element, "code"),
                        _offer_xml_value(element, "name"),
                        _offer_xml_value(element, "price1"),
                        _offer_xml_value(element, "price2"),
                        _offer_xml_value(element, "price3"),
                        _offer_xml_value(element, "price4"),
                        _offer_xml_value(element, "price5"),
                    ]
                )
                element.clear()
                if len(raw_rows) >= MAX_ROWS:
                    break
            elif tag == "offers":
                offers_depth -= 1
                element.clear()
            elif not offers_depth:
                element.clear()
    except ElementTree.ParseError as exc:
        raise UnknownFormatError("Повреждённая XML-выгрузка товаров") from exc

    if not saw_offers:
        raise UnknownFormatError("XML не содержит раздел offers с товарами")
    return [SheetGrid(name=OFFER_XML_SHEET_NAME, rows=_rows_to_cells(raw_rows))]


def _load_pdf_grids(data: bytes) -> list[SheetGrid]:
    """All tables from all pages concatenated into one grid: multi-page price
    lists repeat their header per page, and the pipeline already skips
    repeated header rows within a sheet."""
    import pdfplumber

    raw_rows: list[list[object | None]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    raw_rows.append(list(row))
    if not raw_rows:
        raise PdfNoTablesError("в PDF не найдено таблиц")
    return [SheetGrid(name="pdf", rows=_rows_to_cells(raw_rows))]


def _load_docx_grids(data: bytes) -> list[SheetGrid]:
    import docx

    document = docx.Document(io.BytesIO(data))
    rows: list[list[Cell]] = []
    for table in document.tables:
        for row in table.rows:
            # python-docx expands a horizontally merged ``w:tc`` into one
            # proxy per visual grid slot.  Those proxies contain the same text
            # but are not independent source columns.  Preserve the same
            # merged-fill invariant as XLS/XLSX so header detection does not
            # fabricate five ``Наименование`` columns from one merged cell.
            seen_cells: set[int] = set()
            parsed_row: list[Cell] = []
            for cell in row.cells[:MAX_COLS]:
                tc_identity = id(cell._tc)
                is_merged_fill = tc_identity in seen_cells
                seen_cells.add(tc_identity)
                value = cell.text.strip() if isinstance(cell.text, str) else cell.text
                parsed_row.append(
                    Cell(
                        value=value or None,
                        is_merged_fill=is_merged_fill,
                    )
                )
            rows.append(parsed_row)
            if len(rows) >= MAX_ROWS:
                break
        if len(rows) >= MAX_ROWS:
            break
    return [SheetGrid(name="docx", rows=rows)]


def _load_doc_grids(data: bytes) -> list[SheetGrid]:
    """Convert legacy Word in an isolated temporary profile, then reuse DOCX parsing."""
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if converter is None:
        raise DocumentConversionError("Для разбора .doc требуется LibreOffice Writer")

    with tempfile.TemporaryDirectory(prefix="price-loader-doc-") as directory:
        workspace = Path(directory)
        source = workspace / "source.doc"
        output_dir = workspace / "output"
        profile_dir = workspace / "profile"
        source.write_bytes(data)
        output_dir.mkdir()
        profile_dir.mkdir()

        try:
            completed = subprocess.run(
                [
                    converter,
                    "--headless",
                    "--nodefault",
                    "--nofirststartwizard",
                    "--norestore",
                    f"-env:UserInstallation={profile_dir.as_uri()}",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    str(output_dir),
                    str(source),
                ],
                check=False,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DOC_CONVERSION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentConversionError("Превышено время конвертации .doc") from exc

        converted = output_dir / "source.docx"
        if completed.returncode != 0 or not converted.is_file():
            raise DocumentConversionError("Не удалось конвертировать .doc в .docx")
        return _load_docx_grids(converted.read_bytes())


def _load_xls_grids(data: bytes) -> list[SheetGrid]:
    """Read BIFF8 directly, recovering structurally damaged exports via Calc.

    Some supplier files remain readable by Excel/LibreOffice even though their
    OLE allocation tables are inconsistent and xlrd rejects them.  The normal
    path stays in-process; conversion is attempted only for the two corruption
    exception families emitted by xlrd.
    """
    import xlrd

    try:
        return load_grids(data, "f.xls")
    except (IndexError, xlrd.compdoc.CompDocError) as source_error:
        converter = shutil.which("soffice") or shutil.which("libreoffice")
        if converter is None:
            raise source_error

        with tempfile.TemporaryDirectory(prefix="price-loader-xls-") as directory:
            workspace = Path(directory)
            source = workspace / "source.xls"
            output_dir = workspace / "output"
            profile_dir = workspace / "profile"
            source.write_bytes(data)
            output_dir.mkdir()
            profile_dir.mkdir()
            try:
                completed = subprocess.run(
                    [
                        converter,
                        "--headless",
                        "--nodefault",
                        "--nofirststartwizard",
                        "--norestore",
                        f"-env:UserInstallation={profile_dir.as_uri()}",
                        "--convert-to",
                        "xlsx",
                        "--outdir",
                        str(output_dir),
                        str(source),
                    ],
                    check=False,
                    cwd=workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=SPREADSHEET_CONVERSION_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                raise source_error

            converted = output_dir / "source.xlsx"
            if completed.returncode != 0 or not converted.is_file():
                raise source_error
            return load_grids(converted.read_bytes(), "f.xlsx")


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _load_txt_grids(data: bytes) -> list[SheetGrid]:
    text = _decode_text(data)
    sample = text[:4096]
    delimiter = None
    for cand in ("\t", ";", ","):
        if cand in sample:
            delimiter = cand
            break
    raw_rows: list[list[object | None]] = []
    if delimiter:
        # ``newline=''`` is required by the csv module for files containing
        # mixed CR/LF sequences, a common artefact of HTML/CSV exports.
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter):
            raw_rows.append(list(row))
    else:
        for line in text.splitlines():
            raw_rows.append([line])
    return [SheetGrid(name="txt", rows=_rows_to_cells(raw_rows))]
