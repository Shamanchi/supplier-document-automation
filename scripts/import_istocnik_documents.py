# -*- coding: utf-8 -*-
"""Импорт и привязка распознанных паспортов АО ИСТОЧНИК (JSON -> БД).

Читает JSON из recognize_supplier_documents.py (--output), берёт файлы из
папки Паспорта (doc_class=passport, есть product_name) и:
  1) создаёт product_documents (kind=passport, source=passport_extraction);
  2) привязывает document_links к канонам позиций прайса поставщика
     (пересечение кодов изделий из названия паспорта и названий канонов).

Запуск из корня репозитория:
    python scripts/import_istocnik_documents.py <path-to-json>            # dry run
    python scripts/import_istocnik_documents.py <path-to-json> --execute  # запись
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG  # noqa: E402
from link_canonical_to_prices import extract_codes  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPLIER_ILIKE = "%АО ИСТОЧНИК%"
FALLBACK_SUPPLIER_ILIKE = "%ИСТОЧНИК%"
CONFIRMED_BY = "automated_extraction"
SOURCE = "passport_extraction"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(rel_path: str) -> str | None:
    path = REPO_ROOT / rel_path
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_supplier(cur) -> str:
    cur.execute(
        """
        SELECT s.id, s.name, count(pv.id) AS versions
        FROM suppliers s
        LEFT JOIN price_versions pv ON pv.supplier_id = s.id
        GROUP BY s.id, s.name
        ORDER BY versions DESC
        """
    )
    rows = [(str(r[0]), r[1] or "") for r in cur.fetchall()]
    preferred = [row for row in rows if "ао источник" in row[1].casefold()]
    if preferred:
        return preferred[0][0]
    fallback = [row for row in rows if "источник" in row[1].casefold()]
    if fallback:
        return fallback[0][0]
    raise SystemExit("Поставщик не найден в БД (АО ИСТОЧНИК)")


def main(json_path: str, execute: bool) -> None:
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    entries = [
        entry
        for entry in payload.get("files", [])
        if entry.get("folder_kind") == "Паспорта"
        and entry.get("doc_class") == "passport"
        and (entry.get("product_name") or "").strip()
    ]
    print(f"Паспортов к импорту: {len(entries)}")
    for entry in entries:
        print(f"  - {Path(entry['file']).name[:70]} | {entry['product_name'][:80]}")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        supplier_id = _resolve_supplier(cur)
        print(f"\nПоставщик в БД: {supplier_id}")

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
        canonicals = [(str(row[0]), row[1] or "") for row in cur.fetchall()]
        print(f"Канонов позиций поставщика: {len(canonicals)}")

        cur.execute(
            "SELECT storage_key, id FROM product_documents WHERE source = %s",
            (SOURCE,),
        )
        existing_docs = {row[0]: str(row[1]) for row in cur.fetchall()}
        cur.execute(
            """
            SELECT dl.document_id, dl.canonical_product_id
            FROM document_links dl
            JOIN product_documents pd ON pd.id = dl.document_id
            WHERE pd.source = %s
            """,
            (SOURCE,),
        )
        existing_links = {(str(a), str(b)) for a, b in cur.fetchall()}
        print(f"Уже есть документов (source={SOURCE}): {len(existing_docs)}; ссылок: {len(existing_links)}")

        to_insert_docs: list[dict] = []
        to_insert_links: list[tuple[str, str, str]] = []  # (doc_id, canonical_id, storage)
        docs_with_links = 0
        for entry in entries:
            storage = str(entry.get("path") or "").strip()
            if not storage:
                continue
            doc_id = existing_docs.get(storage)
            if doc_id is None:
                doc_id = str(uuid.uuid4())
                existing_docs[storage] = doc_id
                to_insert_docs.append(
                    {
                        "id": doc_id,
                        "kind": "passport",
                        "title": entry["product_name"].strip(),
                        "storage_key": storage,
                        "sha256": _sha256(storage),
                        "pages": int(entry.get("page_count") or 0),
                    }
                )
            codes = extract_codes(entry["product_name"])
            matched = [
                canonical_id
                for canonical_id, title in canonicals
                if extract_codes(title) & codes
            ]
            for canonical_id in matched:
                key = (doc_id, canonical_id)
                if key not in existing_links:
                    existing_links.add(key)
                    to_insert_links.append((doc_id, canonical_id, storage))
            if matched:
                docs_with_links += 1
            print(
                f"  паспорт {Path(entry['file']).name[:45]:<45} "
                f"коды={len(codes):<2} канонов={len(matched)}"
            )

        print(f"\nНовых документов: {len(to_insert_docs)}")
        print(f"Новых document_links: {len(to_insert_links)}")
        print(f"Паспортов с привязками: {docs_with_links}/{len(entries)}")

        if not execute:
            print("\nDRY RUN — запись не выполнена. Для записи добавьте --execute.")
            return

        now = _now()
        for doc in to_insert_docs:
            cur.execute(
                """
                INSERT INTO product_documents (
                    id, kind, title, registration_number, registration_number_key,
                    valid_until, storage_key, sha256, source, external_id, created_at
                ) VALUES (%s, %s, %s, NULL, NULL, NULL, %s, %s, %s, NULL, %s)
                """,
                (
                    doc["id"],
                    doc["kind"],
                    doc["title"],
                    doc["storage_key"],
                    doc["sha256"],
                    SOURCE,
                    now,
                ),
            )
        for doc_id, canonical_id, storage in to_insert_links:
            entry = next(
                (e for e in entries if e.get("path") == storage), {}
            )
            evidence = {
                "source": "pdf_extraction",
                "extracted_by": CONFIRMED_BY,
                "filename": storage,
                "product_name": (entry.get("product_name") or "").strip(),
                "pages": int(entry.get("page_count") or 0),
                "supplier": "АО ИСТОЧНИК",
            }
            cur.execute(
                """
                INSERT INTO document_links (
                    id, document_id, canonical_product_id, matched_by,
                    confidence, series_mask, evidence, confirmed_by, confirmed_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    doc_id,
                    canonical_id,
                    "passport_extraction",
                    1.0,
                    psycopg2.extras.Json(evidence),
                    CONFIRMED_BY,
                    now,
                    now,
                ),
            )
        conn.commit()
        print(f"\nЗаписано: документов={len(to_insert_docs)}, ссылок={len(to_insert_links)}")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    execute = "--execute" in sys.argv
    main(json_path=sys.argv[1], execute=execute)