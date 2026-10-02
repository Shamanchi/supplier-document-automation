# -*- coding: utf-8 -*-
"""Пакетное распознавание паспортов и сертификатов PDF по текстовому слою.

Читает PDF из папок поставщиков и достаёт:
  - класс документа (паспорт / сертификат);
  - наименование изделия с первой-второй страниц паспорта;
  - сведения о сертификате с предпоследней-последней страниц паспорта
    (у файла сертификата — регистрационный номер по всему тексту).

Результат сохраняется в JSON (UTF-8). Опция --db дополнительно показывает
(read-only, dry run), к каким каноническим товарам позиций прайса паспорта
привязались бы по обозначениям изделий — это шаг «подтянуть документы к
заявкам», запись в БД НЕ производится.

Запуск из корня репозитория:
    python scripts/recognize_supplier_documents.py
    python scripts/recognize_supplier_documents.py "OCR_prices/Прайсы/прайсы/Supplier A"
    python scripts/recognize_supplier_documents.py "путь/к/паспорту.pdf"
    python scripts/recognize_supplier_documents.py "OCR_prices/Прайсы/прайсы/Supplier E" --db
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import document_recognition as recognition  # noqa: E402

PASSPORT_DIR = "Паспорта"
CERTIFICATE_DIR = "Сертификаты"


def _paths_to_scan(target: Path) -> list[tuple[Path, str, str]]:
    """(файл, supplier, folder_kind) для сканирования."""
    result: list[tuple[Path, str, str]] = []

    if target.is_file():
        supplier = target.parent.parent.name if target.parent.name in (
            PASSPORT_DIR, CERTIFICATE_DIR
        ) else target.parent.name
        folder_kind = target.parent.name if target.parent.name in (
            PASSPORT_DIR, CERTIFICATE_DIR
        ) else ""
        return [(target, supplier, folder_kind)]

    corpus = target
    if (corpus / PASSPORT_DIR).is_dir() or (corpus / CERTIFICATE_DIR).is_dir():
        # Папка одного поставщика.
        suppliers = [corpus]
    else:
        # Корпус: OCR_prices/Прайсы/прайсы/*/Паспорта, */Сертификаты.
        suppliers = sorted(
            child for child in corpus.iterdir()
            if child.is_dir()
            and ((child / PASSPORT_DIR).is_dir() or (child / CERTIFICATE_DIR).is_dir())
        )
        if not suppliers:
            suppliers = [corpus]

    for supplier_dir in suppliers:
        supplier = supplier_dir.name
        for kind in (PASSPORT_DIR, CERTIFICATE_DIR):
            folder = supplier_dir / kind
            if not folder.is_dir():
                continue
            for pdf in sorted(folder.glob("*.pdf")):
                result.append((pdf, supplier, kind))
        # PDF прямо в папке поставщика (без подпапок).
        if not any(pdf.suffix.lower() == ".pdf" for pdf in supplier_dir.iterdir()):
            continue
        for pdf in sorted(supplier_dir.glob("*.pdf")):
            result.append((pdf, supplier, ""))
    return result


def _print_line(line: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print(line)


def _db_candidates(
    recognized: list[recognition.RecognizeResult],
    supplier_name: str,
) -> dict:
    """Dry-run: кандидаты привязки паспортов к канонам позиций прайса."""
    import psycopg2

    from db_config import DB_CONFIG
    from link_canonical_to_prices import extract_codes

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.id FROM suppliers s
            JOIN price_versions pv ON pv.supplier_id = s.id
            WHERE s.name ILIKE %s
            GROUP BY s.id
            ORDER BY count(pv.id) DESC
            LIMIT 1
            """,
            (f"%{supplier_name}%",),
        )
        row = cur.fetchone()
        if row is None:
            return {"status": "supplier_not_found", "detail": supplier_name}
        supplier_id = str(row[0])
        cur.execute(
            """
            SELECT DISTINCT cp.id, cp.title
            FROM canonical_products cp
            JOIN canonical_product_sources cps ON cps.canonical_product_id = cp.id
            WHERE cps.supplier_id = %s
            ORDER BY cp.title
            """,
            (supplier_id,),
        )
        canonicals = [(str(r[0]), r[1] or "") for r in cur.fetchall()]
    finally:
        conn.close()

    passports_ready = 0
    matched_canonical_ids: set[str] = set()
    by_file: list[dict] = []
    for result in recognized:
        if result.doc_class != recognition.DOC_PASSPORT or not result.product_name:
            continue
        codes = extract_codes(result.product_name)
        if not codes:
            continue
        passports_ready += 1
        matched = [
            canonical_id
            for canonical_id, title in canonicals
            if extract_codes(title) & codes
        ]
        matched_canonical_ids.update(matched)
        by_file.append(
            {
                "file": result.file,
                "product_name": result.product_name,
                "codes": sorted(codes),
                "matched_canonicals": len(matched),
            }
        )
    return {
        "status": "ok",
        "supplier_id": supplier_id,
        "passports_with_codes": passports_ready,
        "matched_canonical_products": len(matched_canonical_ids),
        "files": by_file,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Распознавание паспортов и сертификатов PDF (текстовый слой)"
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=str(REPO_ROOT / "OCR_prices" / "Прайсы" / "прайсы"),
        help="PDF, папка поставщика или корпус (по умолчанию весь OCR_prices)",
    )
    parser.add_argument(
        "--output",
        default="",
        help="куда писать JSON-результат (по умолчанию document_recognition.json рядом с корнем)",
    )
    parser.add_argument(
        "--db",
        action="store_true",
        help="показать dry-run привязки распознанных паспортов к канонам прайса (read-only)",
    )
    parser.add_argument(
        "--supplier",
        default="",
        help="имя поставщика для --db, если папка не называется как поставщик в БД",
    )
    args = parser.parse_args(argv)

    target = Path(args.target)
    if not target.exists():
        _print_line(f"Нет такого пути: {target}")
        return 2

    started = time.monotonic()
    scan = _paths_to_scan(target)
    _print_line(f"Найдено PDF: {len(scan)} (корень: {target})")

    results: list[dict] = []
    recognized_objects: list[recognition.RecognizeResult] = []
    for pdf, supplier, folder_kind in scan:
        try:
            result = recognition.recognize_pdf(
                pdf,
                file=pdf.name,
                folder_kind=folder_kind,
                supplier=supplier,
            )
        except Exception as error:  # noqa: BLE001
            result = recognition.RecognizeResult(
                file=pdf.name,
                supplier=supplier,
                folder_kind=folder_kind,
                error=f"распознавание упало: {error}",
            )
        recognized_objects.append(result)
        entry = result.to_dict()
        entry["path"] = str(pdf)
        results.append(entry)
        if result.error:
            if result.error.startswith("no_text_layer"):
                mark = "NO-TEXT"
            else:
                mark = "ERR"
        elif result.doc_class == recognition.DOC_PASSPORT and result.product_name:
            mark = "OK"
        else:
            mark = "--"
        certs = ",".join(c.number for c in result.certificates[:2] if c.number)
        _print_line(
            f"[{mark}] {supplier}/{folder_kind}/{result.file} "
            f"({result.page_count} стр.) -> {result.product_name or ''} | {certs}"
        )

    summary = {
        "files": len(results),
        "text_layer": sum(1 for r in results if r.get("has_text_layer")),
        "no_text_layer": sum(1 for r in results if r.get("error", "").startswith("no_text_layer")),
        "errors": sum(1 for r in results if r.get("error") and not r.get("error", "").startswith("no_text_layer")),
        "passports": sum(1 for r in recognized_objects if r.doc_class == recognition.DOC_PASSPORT),
        "certificates": sum(1 for r in recognized_objects if r.doc_class == recognition.DOC_CERTIFICATE),
        "passport_names_found": sum(
            1
            for r in recognized_objects
            if r.doc_class == recognition.DOC_PASSPORT and r.product_name
        ),
        "passport_certificate_mentions": sum(
            len(r.certificates)
            for r in recognized_objects
            if r.doc_class == recognition.DOC_PASSPORT
        ),
        "certificate_numbers_in_files": sum(
            1
            for r in recognized_objects
            if r.doc_class == recognition.DOC_CERTIFICATE and r.registration_number
        ),
    }

    if args.db:
        supplier = args.supplier or (
            target.name if target.is_dir() else target.parent.name
        )
        if not supplier:
            supplier = "Supplier A"
        try:
            db_summary = _db_candidates(recognized_objects, supplier)
            summary["db_dry_run"] = db_summary
            if db_summary.get("status") == "ok":
                _print_line(
                    f"\nDB dry-run ({supplier}): паспортов с кодами = "
                    f"{db_summary['passports_with_codes']}, канонов-кандидатов = "
                    f"{db_summary['matched_canonical_products']} (запись не производилась)"
                )
            else:
                _print_line(f"\nDB dry-run: поставщик не найден ({db_summary.get('detail')})")
        except Exception as error:  # noqa: BLE001
            summary["db_dry_run"] = {"status": "error", "detail": str(error)}
            _print_line(f"\nDB dry-run недоступен: {error}")

    if args.output:
        output = Path(args.output)
    elif target.is_dir():
        output = target / "document_recognition.json"
    else:
        # Одиночный PDF: JSON кладём рядом с файлом.
        output = target.parent / f"{target.stem}.json"
    payload = {
        "schema": "document-recognition/1",
        "scanned_root": str(target),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": round(time.monotonic() - started, 2),
        "files": results,
        "summary": summary,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_line(
        f"\nИтого: файлов {summary['files']}, текстовый слой {summary['text_layer']}, "
        f"паспортов {summary['passports']} (названия у {summary['passport_names_found']}), "
        f"сертификатов-файлов {summary['certificates']} "
        f"(номера у {summary['certificate_numbers_in_files']}), ошибок {summary['errors']}"
    )
    _print_line(f"JSON: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())