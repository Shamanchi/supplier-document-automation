import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
conn = psycopg2.connect(**DB)
cur = conn.cursor()

# Найти, с какими канонами связаны ГГПТ-1,0, ГГПТ-7,0 и т.д.
searches = ["ГГПТ-1,0 (%", "ГГПТ-7,0 (%", "МПП(Н-Т)-50-КД2%", "УСПАА-1 v5%", "МУПТВ-18,5-ГЗ-ВД%"]

for s in searches:
    cur.execute("""
        SELECT c.match_key, p.source_name
        FROM canonical_product_sources l
        JOIN canonical_products c ON c.id = l.canonical_product_id
        JOIN supplier_price_items p ON p.id = l.price_item_id
        WHERE p.source_name ILIKE %s AND l.supplier_id = 'ae683cb3-2ccc-4f72-94a9-72184c2b2bdf'
    """, (s,))
    rows = cur.fetchall()
    print(f"\n{s}:")
    for match_key, price_name in rows:
        print(f"  Канон: {match_key[:50]}")
        print(f"  Прайс: {price_name[:50]}")

conn.close()
