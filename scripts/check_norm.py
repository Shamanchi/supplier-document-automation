import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
conn = psycopg2.connect(**DB)
cur = conn.cursor()

# Покажем source_name и normalized_name для конкретных позиций
ids = [
    ("ГГПТ-0,25", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%ГГПТ%0,25%' OR source_name ILIKE '%ГГПТ%0.25%'"),
    ("ГГПТ-0,5", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%ГГПТ%0,5%' OR source_name ILIKE '%ГГПТ%0.5%'"),
    ("ГОА-II-1,10", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%ГОА%1,10%' OR source_name ILIKE '%ГОА%1.10%' OR source_name ILIKE '%ГОА%110%'"),
    ("ГОА-II-0,35", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%ГОА%0,35%' OR source_name ILIKE '%ГОА%0.35%'"),
    ("МПП(Н)-0,65", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%МПП(Н)-0,65%' OR source_name ILIKE '%МПП(Н-Взр)-0,65%'"),
    ("МПП(Н)-2,7", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%МПП(Н)-2,7%' OR source_name ILIKE '%МПП(Н-Взр)-2,7%'"),
    ("МУПТВ-13,5", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%МУПТВ%13,5%'"),
    ("МУПТВ-18,5", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%МУПТВ%18,5%'"),
    ("УСПАА-1", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%УСПАА-1 v5%' OR source_name ILIKE '%УСПАА%1%v5%'"),
    ("МППВК-100(Взр)", "SELECT source_name, normalized_name FROM supplier_price_items WHERE source_name ILIKE '%МППВК(Взр)-100%' OR source_name ILIKE '%МППВК%100%(Взр)%'"),
]

for name, sql in ids:
    cur.execute(sql)
    rows = cur.fetchall()
    print(f"\n{name}:")
    for src, norm in rows[:2]:
        print(f"  source:     {src[:70]}")
        print(f"  normalized: {norm[:70] if norm else '[NULL]'}")

conn.close()
