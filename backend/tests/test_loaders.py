"""Loader detection by magic bytes/content — synthetic samples plus real
corpus files when the corpus is present."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from app.api.versions import _load_grids_for_preview
from app.api.parse_rules import KNOWN_LOADERS
from app.parsing import loaders
from app.parsing.grid import Cell, SheetGrid, _normalize_ooxml_shared_strings_path
from app.parsing.loaders import (
    GRID_LOADERS,
    LOADER_DOC,
    LOADER_DOCX,
    LOADER_PDF_SCAN,
    LOADER_PDF_TEXT,
    LOADER_TXT,
    LOADER_XLS,
    LOADER_XLSX,
    UnknownFormatError,
    detect_loader,
    load_grids_for,
)
from app.parsing.pipeline import parse_workbook

CORPUS = Path("/home/nstepankov/Projects/supplier-document-automation/Nikolay_Stepankov/external_price_corpus")
requires_corpus = pytest.mark.skipif(
    not CORPUS.is_dir(), reason="external price corpus is not available on this machine"
)


def _make_xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Наименование", "Цена"])
    ws.append(["Муфта", 10])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buf.getvalue()


def _offer_export_xml() -> bytes:
    return b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Tree>
  <categories>
    <category><id>1</id><title>Only a dictionary, not a product</title></category>
  </categories>
  <offers>
    <offer>
      <name>Product one</name><code>A-1</code>
      <price1>10,50</price1><price2>9,50</price2><price3>8,50</price3>
      <price4>7,50</price4><price5>6,50</price5>
    </offer>
    <offer>
      <name>Product two</name><code>B-2</code>
      <price1>20</price1><price2>19</price2><price3>18</price3>
      <price4>17</price4><price5/>
    </offer>
  </offers>
</Tree>"""


def test_detect_synthetic_formats():
    assert detect_loader(_make_xlsx(), "renamed.bin") == LOADER_XLSX
    assert detect_loader("Наименование;Цена\nМуфта;10\n".encode("cp1251"), "x.csv") == LOADER_TXT
    with pytest.raises(UnknownFormatError):
        detect_loader(b"\x00\x01\x02\x03garbage", "x.zzz")


def test_txt_grid_parsing():
    data = "Артикул;Наименование;Ед. изм.;Цена\nA-1;Муфта;шт;100,50\n".encode("utf-8")
    assert detect_loader(data, "price.csv") == LOADER_TXT
    grids = load_grids_for(LOADER_TXT, data, "price.csv")
    assert grids[0].rows[0][0].value == "Артикул"
    assert grids[0].rows[1][3].value == "100,50"


def test_txt_grid_accepts_mixed_newline_sequences():
    data = "Наименование;Цена\rМуфта;10\r\nКлапан;20\n".encode()

    grids = load_grids_for(LOADER_TXT, data, "price.csv")

    assert [[cell.value for cell in row] for row in grids[0].rows] == [
        ["Наименование", "Цена"],
        ["Муфта", "10"],
        ["Клапан", "20"],
    ]


def test_xlsx_grid_accepts_case_broken_shared_strings_part():
    data = _make_xlsx()
    source = zipfile.ZipFile(io.BytesIO(data))
    files = {
        ("xl/SharedStrings.xml" if name == "xl/sharedStrings.xml" else name): source.read(name)
        for name in source.namelist()
    }
    source.close()
    broken_case = _make_zip(files)

    grids = load_grids_for(LOADER_XLSX, broken_case, "price.xlsx")

    assert [[cell.value for cell in row] for row in grids[0].rows] == [
        ["Наименование", "Цена"],
        ["Муфта", 10],
    ]


def test_ooxml_normalizer_adds_canonical_shared_strings_alias():
    broken_case = _make_zip({"xl/SharedStrings.xml": b"shared-strings"})

    normalized = _normalize_ooxml_shared_strings_path(broken_case)

    with zipfile.ZipFile(io.BytesIO(normalized)) as archive:
        assert archive.read("xl/SharedStrings.xml") == b"shared-strings"
        assert archive.read("xl/sharedStrings.xml") == b"shared-strings"


def test_zip_wrapped_xls_is_unwrapped_before_grid_loading(monkeypatch):
    embedded_xls = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1nested-xls"
    data = _make_zip({"price_21.02.2022.xls": embedded_xls})
    loaded: dict[str, object] = {}

    def fake_load_grids(payload: bytes, filename: str) -> list[SheetGrid]:
        loaded["payload"] = payload
        loaded["filename"] = filename
        return [SheetGrid(name="price", rows=[[Cell(value="ok")]])]

    monkeypatch.setattr(loaders, "load_grids", fake_load_grids)

    assert detect_loader(data, "renamed-upload.xls") == LOADER_XLS
    grids = load_grids_for(LOADER_XLS, data, "renamed-upload.xls")

    assert loaded == {"payload": embedded_xls, "filename": "f.xls"}
    assert grids[0].rows[0][0].value == "ok"


def test_corrupt_biff8_falls_back_to_isolated_libreoffice_conversion(monkeypatch):
    calls: list[str] = []

    def fake_load_grids(payload: bytes, filename: str) -> list[SheetGrid]:
        calls.append(filename)
        if filename == "f.xls":
            raise IndexError("corrupt OLE allocation table")
        return [SheetGrid(name="recovered", rows=[[Cell(value="ok")]])]

    def fake_run(command, **_kwargs):
        output_dir = Path(command[command.index("--outdir") + 1])
        (output_dir / "source.xlsx").write_bytes(_make_xlsx())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(loaders, "load_grids", fake_load_grids)
    monkeypatch.setattr(loaders.shutil, "which", lambda _name: "/usr/bin/soffice")
    monkeypatch.setattr(loaders.subprocess, "run", fake_run)

    grids = load_grids_for(LOADER_XLS, b"broken-biff8", "price.xls")

    assert calls == ["f.xls", "f.xlsx"]
    assert grids[0].rows[0][0].value == "ok"


def test_zip_offer_xml_is_normalized_to_one_product_grid():
    data = _make_zip({"price.xml": _offer_export_xml()})

    assert detect_loader(data, "059_Проф.ру.xls") == LOADER_TXT
    grids = load_grids_for(LOADER_TXT, data, "059_Проф.ру.xls")

    assert len(grids) == 1
    assert grids[0].name == "offers"
    assert [cell.value for cell in grids[0].rows[0]] == [
        "Артикул",
        "Наименование",
        "Цена 1",
        "Цена 2",
        "Цена 3",
        "Цена 4",
        "Цена 5",
    ]
    assert [[cell.value for cell in row] for row in grids[0].rows[1:]] == [
        ["A-1", "Product one", "10,50", "9,50", "8,50", "7,50", "6,50"],
        ["B-2", "Product two", "20", "19", "18", "17", None],
    ]


def test_zip_safety_limits_reject_path_traversal_and_compression_bombs():
    wrapped_xls = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1nested-xls"
    traversal = _make_zip({"../price.xls": wrapped_xls})
    bomb = _make_zip({"price.xls": b"0" * (256 * 1024)})

    with pytest.raises(UnknownFormatError, match="небезопасный путь"):
        detect_loader(traversal, "price.xls")
    with pytest.raises(UnknownFormatError, match="высокая степень сжатия"):
        detect_loader(bomb, "price.xls")


def test_scanned_urls_manifest_is_not_treated_as_a_price():
    data = b"https://example.test/one.xls\nhttps://example.test/two.xlsx\nhttps://example.test/three.pdf\n"

    with pytest.raises(UnknownFormatError, match="SCANNED_URLS"):
        detect_loader(data, "SCANNED_URLS_FULL.txt")
    with pytest.raises(UnknownFormatError, match="SCANNED_URLS"):
        detect_loader(
            b"url,company,domain,has_files\nhttps://example.test,Acme,example.test,False\n",
            "renamed.csv",
        )


def test_legacy_doc_is_available_for_parse_rules():
    assert LOADER_DOC in KNOWN_LOADERS


@requires_corpus
def test_detect_real_corpus_files():
    xlsx = CORPUS / "xlsx" / "103_ССД_противопожарное_оборудование.xlsx"
    xls = CORPUS / "xls" / "001_Consol.xls"
    doc = CORPUS / "doc" / "001_OT-ISC_Народ.doc"
    docx = CORPUS / "docx" / "001_Centrfire_Челябинск.docx"

    assert detect_loader(xlsx.read_bytes(), xlsx.name) == LOADER_XLSX
    assert detect_loader(xls.read_bytes(), xls.name) == LOADER_XLS
    assert detect_loader(doc.read_bytes(), doc.name) == LOADER_DOC
    assert detect_loader(docx.read_bytes(), docx.name) == LOADER_DOCX


@requires_corpus
def test_detect_pdf_text_and_scan():
    text_pdf = CORPUS / "pdf" / "001_01161_Ростов.pdf"
    scan_pdf = CORPUS / "pdf" / "285_ScannedDoc_79.pdf"

    assert detect_loader(text_pdf.read_bytes(), text_pdf.name) == LOADER_PDF_TEXT
    if scan_pdf.exists():
        assert detect_loader(scan_pdf.read_bytes(), scan_pdf.name) == LOADER_PDF_SCAN

    grids = load_grids_for(LOADER_PDF_TEXT, text_pdf.read_bytes(), text_pdf.name)
    assert grids and grids[0].rows


@requires_corpus
def test_loads_legacy_doc_for_parsing_and_mapping_preview():
    doc = CORPUS / "doc" / "002_PKF_Техсервис.doc"
    data = doc.read_bytes()

    grids = load_grids_for(LOADER_DOC, data, doc.name)

    assert grids
    assert any(grid.rows for grid in grids)
    assert _load_grids_for_preview(data, doc.name)
    assert LOADER_DOC in GRID_LOADERS


@requires_corpus
def test_xls_ole_not_confused_with_doc():
    # Detection is content-based: a mislabeled .xls that is really a zip
    # container is legitimately classified as xlsx (e.g. 005_grateks_01-36_v2).
    seen = set()
    for path in sorted((CORPUS / "xls").glob("*.xls"))[:5]:
        loader = detect_loader(path.read_bytes(), "renamed.bin")
        assert loader in (LOADER_XLS, LOADER_XLSX), path.name
        seen.add(loader)
    assert LOADER_XLS in seen


@requires_corpus
def test_loads_recoverable_zip_wrapped_corpus_prices():
    bolid = CORPUS / "xls" / "041_НВП_Болид.xls"
    prof = CORPUS / "xls" / "059_Проф.ру.xls"

    bolid_data = bolid.read_bytes()
    prof_data = prof.read_bytes()
    assert detect_loader(bolid_data, bolid.name) == LOADER_XLS
    assert detect_loader(prof_data, prof.name) == LOADER_TXT

    bolid_grids = load_grids_for(LOADER_XLS, bolid_data, bolid.name)
    prof_grids = load_grids_for(LOADER_TXT, prof_data, prof.name)
    bolid_result = parse_workbook(bolid_data, bolid.name)
    prof_result = parse_workbook(prof_data, prof.name)

    assert bolid_grids and any(grid.rows for grid in bolid_grids)
    assert not bolid_result.needs_mapping
    # Two priced continuation rows leave the nominal name cell empty but have
    # a concise product designation in the description column.  The parser
    # preserves them with an auditable NAME_FROM_DESCRIPTION warning instead
    # of dropping six valid tier prices.
    assert len(bolid_result.items) == 2873
    assert sum(len(item.values) for item in bolid_result.items) == 8619
    assert sum(issue.code == "NAME_FROM_DESCRIPTION" for issue in bolid_result.issues) == 2
    assert len(prof_grids) == 1
    assert len(prof_grids[0].rows) == 2277  # header + 2,276 offers in source export
    assert [cell.value for cell in prof_grids[0].rows[1][:3]] == [
        "003504",
        "Головка торцевая 14 мм, цинк. (YG-001)",
        "31,63",
    ]
    assert not prof_result.needs_mapping
    assert len(prof_result.items) == 2276
    assert sum(len(item.values) for item in prof_result.items) == 11044
