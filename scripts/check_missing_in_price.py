import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
conn = psycopg2.connect(**DB)
cur = conn.cursor()

# Ищем конкретные позиции
queries = [
    ("ГГПТ-0,25 или ГГПТ-0,5", "SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%ГГПТ%0,25%' OR source_name ILIKE '%ГГПТ%0.25%' OR source_name ILIKE '%ГГПТ%0,5%' OR source_name ILIKE '%ГГПТ%0.5%'"),
    ("ГОА", "SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%ГОА%' LIMIT 5"),
    ("МПП(Н)-0,65", "SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%МПП%0,65%' OR source_name ILIKE '%МПП%0.65%'"),
    ("МПП(Н)-2,7", "SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%МПП%2,7%' OR source_name ILIKE '%МПП%2.7%'"),
    ("МУПТВ-13,5", "SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%МУПТВ%13,5%' OR source_name ILIKE '%МУПТВ%13.5%'"),
    ("МУПТВ-18,5", "SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%МУПТВ%18,5%' OR source_name ILIKE '%МУПТВ%18.5%'"),
    ("УСПАА-1", "SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%УСПАА%1%' OR source_name ILIKE '%УСП АА%'"),
    ("МППВК-100(Взр)", "SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%МППВК%100%'"),
]

for name, sql in queries:
    cur.execute(sql)
    rows = cur.fetchall()
    status = "✅ НАЙДЕНО" if rows else "❌ НЕТ"
    print(f"\n{name}: {status}")
    for r in rows[:3]:
        print(f"  • {r[0][:80]}")

conn.close()
