"""
Импорт паспортов из all_passports_results.csv в price_loader_app.

Создаёт:
  - CanonicalProduct для каждого уникального наименования
  - ProductDocument (kind="passport") для каждого файла
  - DocumentLink — связь между ними

Запуск:
    python scripts/import_passports_to_db.py

Требует:
    pip install psycopg2-binary
    Docker-контейнеры price_loader_app запущены (PostgreSQL опубликован наружу; параметры подключения - в scripts/db_config.py)
"""

import sys
import csv
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

# ─── Конфигурация ────────────────────────────────────────────────────────────

CSV_PATH = "all_passports_results.csv"

# Путь к папке с паспортами (для формирования storage_key)
PASSPORTS_BASE = Path("OCR_prices/Прайсы/прайсы")

# ─── Утилиты ─────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_title(title: str) -> str:
    """Нормализованное наименование (как в системе коллеги)."""
    return title.strip().upper()


def _match_key(title: str) -> str:
    """Уникальный ключ для CanonicalProduct."""
    # Используем нормализованный title, но ограничиваем 256 символами
    key = _norm_title(title)
    return key[:256]


# ─── Основная логика ─────────────────────────────────────────────────────────


def import_passports(dry_run: bool = False) -> dict:
    """Читает CSV и загружает данные в PostgreSQL."""

    # 1. Читаем CSV
    csv_path = Path(CSV_PATH)
    if not csv_path.exists():
        print(f"❌ Файл {CSV_PATH} не найден. Запустите сначала:")
        print("   python scripts/extract_all_passports_master.py")
        return {"status": "error", "reason": "csv_not_found"}

    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row.get("Статус", "").strip().upper() == "OK":
                rows.append(row)

    print(f"📄 Прочитано строк из CSV: {len(rows)} (только OK)")

    # 2. Группируем по уникальным наименованиям
    #    Одно наименование может иметь несколько паспортов (разные модификации)
    products: dict[str, dict] = {}  # match_key -> {title, passports: []}

    for row in rows:
        title = row.get("Наименование изделия", "").strip()
        supplier = row.get("Поставщик", "").strip()
        filename = row.get("Файл", "").strip()
        pages = row.get("Страниц", "0").strip()

        if not title or not filename:
            continue

        key = _match_key(title)
        if key not in products:
            products[key] = {
                "title": title,
                "normalized_title": _norm_title(title),
                "match_key": key,
                "passports": [],
            }
        products[key]["passports"].append(
            {
                "supplier": supplier,
                "filename": filename,
                "pages": int(pages) if pages.isdigit() else 0,
            }
        )

    print(f"🔧 Уникальных наименований: {len(products)}")
    print(f"📦 Всего паспортов: {sum(len(p['passports']) for p in products.values())}")

    if dry_run:
        print("\n🏃 DRY RUN — данные не записываются")
        for key, prod in list(products.items())[:3]:
            print(f"\n  {key}")
            print(f"    Наименование: {prod['title']}")
            print(f"    Паспортов: {len(prod['passports'])}")
            for pp in prod['passports'][:2]:
                print(f"      - {pp['filename']} ({pp['supplier']})")
        return {"status": "dry_run", "products": len(products)}

    # 3. Подключаемся к БД и вставляем
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    now = _now()

    inserted_products = 0
    inserted_documents = 0
    inserted_links = 0
    skipped_duplicates = 0

    for key, prod in products.items():
        # ── 3a. CanonicalProduct ────────────────────────────────────────────
        cur.execute(
            "SELECT id FROM canonical_products WHERE match_key = %s",
            (key,),
        )
        existing = cur.fetchone()

        if existing:
            canonical_id = existing[0]
            skipped_duplicates += 1
        else:
            canonical_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO canonical_products (
                    id, match_key, title, normalized_title, normalized_article,
                    category_id, merge_status, sources_count, embedding,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, NULL, NULL, 'auto', 0, NULL, %s, %s)
                """,
                (
                    str(canonical_id),
                    key,
                    prod["title"],
                    prod["normalized_title"],
                    now,
                    now,
                ),
            )
            inserted_products += 1

        # ── 3b. ProductDocument для каждого паспорта ────────────────────────
        for pp in prod["passports"]:
            doc_id = uuid.uuid4()

            # Формируем storage_key — путь к файлу относительно проекта
            # Или абсолютный путь (система коллеги может хранить как удобно)
            storage_key = str(
                PASSPORTS_BASE / pp["supplier"] / "Паспорта" / pp["filename"]
            )

            cur.execute(
                """
                INSERT INTO product_documents (
                    id, kind, title, registration_number, registration_number_key,
                    valid_until, storage_key, sha256, source, external_id, created_at
                ) VALUES (%s, %s, %s, NULL, NULL, NULL, %s, NULL, %s, NULL, %s)
                """,
                (
                    str(doc_id),
                    "passport",
                    prod["title"],
                    storage_key,
                    "passport_extraction",
                    now,
                ),
            )
            inserted_documents += 1

            # ── 3c. DocumentLink ────────────────────────────────────────────
            evidence = {
                "source": "pdf_extraction",
                "extracted_by": "automated extraction",
                "pages": pp["pages"],
                "filename": pp["filename"],
                "supplier": pp["supplier"],
            }

            link_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO document_links (
                    id, document_id, canonical_product_id, matched_by,
                    confidence, series_mask, evidence, confirmed_by, confirmed_at, created_at
                ) VALUES (%s, %s, %s, %s, NULL, NULL, %s, %s, %s, %s)
                """,
                (
                    str(link_id),
                    str(doc_id),
                    str(canonical_id),
                    "passport_extraction",
                    psycopg2.extras.Json(evidence),
                    "automated_extraction",
                    now,
                    now,
                ),
            )
            inserted_links += 1

    conn.commit()
    cur.close()
    conn.close()

    result = {
        "status": "success",
        "products_total": len(products),
        "products_inserted": inserted_products,
        "products_skipped": skipped_duplicates,
        "documents_inserted": inserted_documents,
        "links_inserted": inserted_links,
    }

    print("\n" + "=" * 60)
    print("✅ ИМПОРТ ЗАВЕРШЁН")
    print("=" * 60)
    print(f"  Канонических товаров создано:   {inserted_products}")
    print(f"  Уже существовало (пропущено):   {skipped_duplicates}")
    print(f"  Паспортов-документов создано:   {inserted_documents}")
    print(f"  Связей товар-документ создано:  {inserted_links}")
    print("=" * 60)

    return result


# ─── Точка входа ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # По умолчанию — dry run для безопасности
    dry = "--execute" not in sys.argv

    if dry:
        print("🏃 РЕЖИМ ПРОВЕРКИ (dry run)")
        print("   Данные НЕ будут записаны в БД.")
        print("   Чтобы выполнить импорт, добавьте флаг --execute\n")
    else:
        print("⚠️  РЕАЛЬНЫЙ ИМПОРТ в БД price_loader_app\n")

    import_passports(dry_run=dry)
