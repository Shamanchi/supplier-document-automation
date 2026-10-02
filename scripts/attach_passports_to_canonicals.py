# -*- coding: utf-8 -*-
"""Привязка паспортов к каноническим товарам позиций прайса (document_links).

Демо wave-2 (Supplier A). После пересборки канонического слоя каждая позиция
прайса принадлежит ровно одному канону (UNIQUE price_item_id в
canonical_product_sources). Паспорт закрывает целый размерный ряд изделий
(например, ОП-25..100), поэтому он привязывается документом сразу ко всем
канонам позиций, чьё обозначение входит в ряд паспорта:

    product_documents(kind=passport) <- document_links -> canonical_products

Откуда берутся «паспортные» записи: product_documents со source
'passport_extraction' (созданы import_passports_to_db.py) и их каноны —
реестр паспортов с человекочитаемыми названиями рядов.

Запуск из корня репозитория:
    python scripts/attach_passports_to_canonicals.py          # dry run
    python scripts/attach_passports_to_canonicals.py --execute
"""

import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG
from link_canonical_to_prices import _CODE_RE, _FAMILIES, extract_codes

SUPPLIER = "Supplier A"

CONFIRMED_BY = "demo_pipeline_wave2"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _passport_family_codes(title: str) -> set[str]:
    """Коды из названия паспортного канона (название ряда)."""
    return extract_codes(title)


def _canonical_codes(title: str) -> set[str]:
    """Коды из названия канона позиции прайса."""
    return extract_codes(title)


def _resolve_supplier(cur) -> str:
    cur.execute(
        """
        SELECT s.id FROM suppliers s
        JOIN price_versions pv ON pv.supplier_id = s.id
        WHERE s.name ILIKE %s
        GROUP BY s.id
        ORDER BY count(pv.id) DESC
        LIMIT 1
        """,
        (f"%{SUPPLIER}%",),
    )
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"Поставщик не найден: {SUPPLIER}")
    return str(row[0])


def main(dry_run: bool = True) -> dict:
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    supplier_id = _resolve_supplier(cur)
    print(f"Поставщик в БД: {supplier_id}")

    # 1. Паспорта: документ + название ряда (канон паспорта)
    cur.execute(
        """
        SELECT pd.id, pd.storage_key, cp.title
        FROM product_documents pd
        JOIN document_links dl ON dl.document_id = pd.id
        JOIN canonical_products cp ON cp.id = dl.canonical_product_id
        WHERE pd.source = 'passport_extraction'
        ORDER BY pd.storage_key
        """
    )
    passports = [(str(r[0]), r[1] or "", r[2] or "") for r in cur.fetchall()]
    print(f"Паспортов в реестре: {len(passports)}")
    for _, storage, title in passports:
        print(f"  - {storage.split('/')[-1]} | {title[:60]}")

    # 2. Каноны позиций прайса поставщика (у которых есть источники-позиции)
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
    print(f"Канонов позиций прайса: {len(canonicals)}")

    # 3. Индекс существующих связей
    cur.execute(
        "SELECT document_id, canonical_product_id FROM document_links"
    )
    existing = {(str(r[0]), str(r[1])) for r in cur.fetchall()}
    print(f"Уже есть document_links: {len(existing)}")

    # 4. Сопоставление по кодам
    matched_passport_ids: set = set()
    links: list[tuple[str, str, str]] = []  # (doc_id, canonical_id, storage)
    passport_code_map = []
    for doc_id, storage, title in passports:
        codes = _passport_family_codes(title)
        if not codes:
            continue
        passport_code_map.append((doc_id, storage, codes))

    for doc_id, storage, p_codes in passport_code_map:
        attached = 0
        for canonical_id, c_title in canonicals:
            c_codes = _canonical_codes(c_title)
            if c_codes & p_codes:
                key = (doc_id, canonical_id)
                if key not in existing:
                    links.append((doc_id, canonical_id, storage))
                    existing.add(key)
                    attached += 1
        if attached:
            matched_passport_ids.add(doc_id)
        print(f"  паспорт {storage.split('/')[-1][:50]}: совпало канонов = {attached}")

    print(f"\nИтого новых document_links: {len(links)}")
    print(f"Паспортов с привязками: {len(matched_passport_ids)}/{len(passports)}")

    if dry_run:
        conn.close()
        return {"status": "dry_run", "new_links": len(links)}

    now = _now()
    inserted = 0
    for doc_id, canonical_id, storage in links:
        evidence = {
            "source": "pdf_extraction",
            "extracted_by": "demo_wave2_passport_series",
            "filename": storage,
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
        inserted += 1

    conn.commit()
    conn.close()
    print(f"Записано document_links: {inserted}")
    return {"status": "success", "inserted": inserted}


if __name__ == "__main__":
    dry = "--execute" not in sys.argv
    print("РЕЖИМ ПРОВЕРКИ (dry run)\n" if dry else "РЕАЛЬНАЯ ЗАПИСЬ\n")
    main(dry_run=dry)
