"""
Ручная связка 14 несвязанных товаров АО ИСТОЧНИК.
Использует поиск по подстроке source_name (без жёсткой нормализации).
"""
import csv
import re
import uuid
import sys
import psycopg2
from datetime import datetime, timezone

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
SUPPLIER = "ae683cb3-2ccc-4f72-94a9-72184c2b2bdf"
PROJECT = "10000000-0000-4000-8000-000000000001"
CSV_PATH = "all_passports_results.csv"

# 14 несвязанных товаров: (каноническое_название, [поисковые_запросы_для_прайса])
# Поисковые запросы — подстроки, которые должны быть в source_name
MANUAL_MATCHES = [
    ("ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐0,25; ГГПТ‐0,5", ["ГГПТ-0,25", "ГГПТ-0,5"]),
    ("ГЕНЕРАТОР ОГНЕТУШАЩЕГО АЭРОЗОЛЯ ГОА‐II‐1,10‐020‐010", ["ГОА-II-1,10"]),
    ("ГЕНЕРАТОР ОГНЕТУШАЩЕГО АЭРОЗОЛЯ ГОА‐II‐0,35‐020‐020", ["ГОА-II-0,35"]),
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н)-0,65-И-ГЭ-У2", ["МПП(Н)-0,65-И-ГЭ-У2"]),
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-Взр)-2,7-И-ГЭ-У2", ["МПП(Н-Взр)-2,7"]),
    ("МУПТВ-13,5-ГЗ-ВД (tºC = +5;-30;-50_п) МУПТВ(С)-13,5-ГЗ-ВД (tºC = -30;-50) МУПТВ(С)-13,5-ГЗ-ВД (tºC = +5;-30;-50_п)", ["МУПТВ-13,5"]),
    ("ТОНКОРАСПЫЛЕННОЙ ВОДОЙ АВТОМАТИЧЕСКИХ МУПТВ-18,5-ГЗ-ВД (tºC = -30;-50) МУПТВ-18,5-ГЗ-ВД (tºC = +5;-30;-50_п)", ["МУПТВ-18,5"]),
    ("ТОНКОРАСПЫЛЕННОЙ ВОДОЙ АВТОМАТИЧЕСКИХ МУПТВ(Взр)-13,5-ГЗ-ВД (tºC = -30;-50) МУПТВ(Взр)-13,5-ГЗ-ВД (tºC = +5;-30;-50_п)", ["МУПТВ(Взр)-13,5"]),
    ("ПОЖАРОТУШЕНИЯ УСП АА-1 Модификация v5", ["УСПАА-1 v5"]),
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-РП)-0,65-И-ГЭ-У2", ["МПП(Н-РП)-0,65"]),
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-Взр)-0,65-И-ГЭ-У2", ["МПП(Н-Взр)-0,65"]),
    ("МОДУЛЬ УСТАНОВОК ПОЖАРОТУШЕНИЯ ТОНКОРАСПЫЛЕННОЙ ВОДОЙ АВТОМАТИЧЕСКИХ МУПТВ-18,5-ГЗ-ВД (tºC = +5;-30;-50_п)", ["МУПТВ-18,5"]),
    ("МОДУЛЬ ПОЖАРОТУШЕНИЯ ПЕНОЙ ВЫСОКОЙ КРАТНОСТИ МППВК-100(Взр)-ГЗ", ["МППВК(Взр)-100"]),
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н)-2,7-И-ГЭ-У2", ["МПП(Н)-2,7"]),
]

def now():
    return datetime.now(timezone.utc)

conn = psycopg2.connect(**DB)
cur = conn.cursor()

dry_run = "--execute" not in sys.argv
print("🏃 DRY RUN" if dry_run else "⚠️  РЕАЛЬНАЯ ЗАПИСЬ")
print()

inserted = 0
not_found = []

for canon_title, search_patterns in MANUAL_MATCHES:
    # Ищем canonical_id
    cur.execute("SELECT id FROM canonical_products WHERE match_key = %s", (canon_title.upper(),))
    r = cur.fetchone()
    if not r:
        print(f"❌ Канон не найден: {canon_title[:50]}")
        not_found.append((canon_title, "no_canonical"))
        continue
    canon_id = r[0]

    # Ищем позицию прайса
    found_items = []
    for pattern in search_patterns:
        cur.execute("""
            SELECT id, source_name FROM supplier_price_items
            WHERE price_version_id IN (SELECT id FROM price_versions WHERE supplier_id = %s)
            AND source_name ILIKE %s
        """, (SUPPLIER, f'%{pattern}%'))
        found_items.extend(cur.fetchall())
        if found_items:
            break

    if not found_items:
        print(f"❌ Прайс не найден: {canon_title[:50]}")
        not_found.append((canon_title, "no_price"))
        continue

    # Берём первое совпадение
    price_id, price_name = found_items[0]
    print(f"✅ {canon_title[:50]} → {price_name[:50]}")

    if not dry_run:
        link_id = uuid.uuid4()
        cur.execute("""
            INSERT INTO canonical_product_sources
                (id, canonical_product_id, price_item_id, supplier_id, project_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (str(link_id), str(canon_id), str(price_id), SUPPLIER, PROJECT, now()))
        inserted += 1

if not dry_run:
    conn.commit()
    print(f"\n✅ Создано связей: {inserted}")
else:
    print(f"\n🏃 DRY RUN — связи не созданы. Для записи запусти с --execute")

if not_found:
    print(f"\n❌ Не найдено: {len(not_found)}")
    for t, reason in not_found:
        print(f"  [{reason}] {t[:60]}")

conn.close()
