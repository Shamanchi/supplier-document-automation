# -*- coding: utf-8 -*-
"""Доувязка паспортов без привязок к каноническим товарам (document_links).

Скрипт находит product_documents (source=passport_extraction), у которых нет ни
одной связи document_links, и привязывает их к канонам по обозначениям изделий
тем же матчером, что использует API ``POST /api/v1/documents``
(backend/app/matching/product_codes.py).

Запуск из корня репозитория:
    python scripts/bind_documents_to_canonicals.py                        # dry run
    python scripts/bind_documents_to_canonicals.py --supplier "АО Источник"
    python scripts/bind_documents_to_canonicals.py --all --execute

--supplier ограничивает кандидатов канонами поставщика (по умолчанию АО Источник);
--all — кандидаты по всем канонам с ценовыми строками;
--doc-id — обработать только один документ;
--execute — записать document_links (иначе dry run).
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.matching.product_codes import match_canonical_titles  # noqa: E402
from db_config import DB_CONFIG  # noqa: E402

SOURCE = "passport_extraction"
CONFIRMED_BY = "automated_extraction"
DEFAULT_SUPPLIER = "АО Источник"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_args() -> dict:
    argv = sys.argv[1:]
    args = {
        "supplier": DEFAULT_SUPPLIER,
        "use_all": False,
        "doc_id": None,
        "execute": False,
    }
    if "--all" in argv:
        args["use_all"] = True
    if "--supplier" in argv:
        args["supplier"] = argv[argv.index("--supplier") + 1]
    if "--doc-id" in argv:
        args["doc_id"] = uuid.UUID(argv[argv.index("--doc-id") + 1])
    if "--execute" in argv:
        args["execute"] = True
    return args


def _resolve_supplier(cur, supplier_name: str) -> str:
    cur.execute(
        """
        SELECT s.id, s.name
        FROM suppliers s
        JOIN price_versions pv ON pv.supplier_id = s.id
        WHERE s.name ILIKE %s
        GROUP BY s.id, s.name
        ORDER BY count(pv.id) DESC
        LIMIT 1
        """,
        (f"%{supplier_name}%",),
    )
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"Поставщик не найден в БД: {supplier_name}")
    return str(row[0])


def main() -> None:
    args = _parse_args()
    print("РЕЖИМ ПРОВЕРКИ (dry run)\n" if not args["execute"] else "РЕАЛЬНАЯ ЗАПИСЬ\n")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        supplier_id = None
        if not args["use_all"]:
            supplier_id = _resolve_supplier(cur, args["supplier"])
            print(f"Поставщик для сужения кандидатов: {supplier_id}")

        # Каноны: только те, у которых есть ценовые строки (canonical_product_sources).
        if supplier_id is not None:
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
        else:
            cur.execute(
                """
                SELECT DISTINCT cp.id, cp.title
                FROM canonical_products cp
                JOIN canonical_product_sources cps ON cps.canonical_product_id = cp.id
                ORDER BY cp.title
                """
            )
        canonicals = [(str(row[0]), row[1] or "") for row in cur.fetchall()]
        print(f"Канонов-кандидатов: {len(canonicals)}")

        # Документы-сироты: паспорта без единой связи.
        sql = """
            SELECT pd.id, pd.title, pd.storage_key
            FROM product_documents pd
            WHERE pd.source = %s
              AND NOT EXISTS (
                  SELECT 1 FROM document_links dl WHERE dl.document_id = pd.id
              )
        """
        params: list = [SOURCE]
        if args["doc_id"] is not None:
            sql += " AND pd.id = %s"
            params.append(args["doc_id"])
        sql += " ORDER BY pd.title"
        cur.execute(sql, tuple(params))
        documents = [(str(row[0]), row[1] or "", row[2] or "") for row in cur.fetchall()]
        print(f"Паспортов без привязок: {len(documents)}")

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

        canonical_titles = [title for _, title in canonicals]
        by_title = {
            title: canonical_id for canonical_id, title in canonicals
        }
        to_insert: list[tuple[str, str, str, str]] = []  # doc, canon, storage, stage
        no_match: list[str] = []
        for doc_id, title, storage_key in documents:
            matches = match_canonical_titles(title, canonical_titles)
            inserted_for_doc = 0
            for canonical_title, stage in matches:
                canonical_id = by_title[canonical_title]
                key = (doc_id, canonical_id)
                if key not in existing_links:
                    existing_links.add(key)
                    to_insert.append((doc_id, canonical_id, storage_key, stage))
                    inserted_for_doc += 1
            if not matches:
                no_match.append(title)
            print(
                f"  {Path(storage_key).name[:48]:<48} "
                f"найдено={len(matches):<2} новых={inserted_for_doc}"
            )

        print(f"\nНовых document_links: {len(to_insert)}")
        print(f"Документов без кандидатов: {len(no_match)}")
        for title in no_match[:20]:
            print(f"  - {title[:110]}")

        if not args["execute"]:
            return

        now = _now()
        for doc_id, canonical_id, storage_key, stage in to_insert:
            cur.execute(
                """
                INSERT INTO document_links (
                    id, document_id, canonical_product_id, matched_by,
                    confidence, series_mask, evidence, confirmed_by,
                    confirmed_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    doc_id,
                    canonical_id,
                    "passport_designations" if stage == "designation" else "passport_codes",
                    1.0,
                    psycopg2.extras.Json(
                        {
                            "source": "bind_documents_to_canonicals",
                            "product_name": storage_key,
                            "matched_stage": stage,
                            "canonical_title": canonical_id,
                            "supplier": args["supplier"],
                        }
                    ),
                    CONFIRMED_BY,
                    now,
                    now,
                ),
            )
        conn.commit()
        print(f"\nЗаписано document_links: {len(to_insert)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
