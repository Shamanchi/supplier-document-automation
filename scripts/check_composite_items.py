import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
conn = psycopg2.connect(**DB)
cur = conn.cursor()

# Найти price_item_id для МПП(Н-Т)-50 и УСПАА-1 v5
cur.execute("""
    SELECT id, source_name FROM supplier_price_items
    WHERE source_name ILIKE '%МПП(Н-Т)-50-КД2%'
    OR source_name ILIKE '%УСПАА-1 v5%'
""")
items = cur.fetchall()

for pid, name in items:
    print(f"\nПрайс ID: {pid}")
    print(f"  {name[:80]}")
    cur.execute("""
        SELECT c.match_key FROM canonical_product_sources l
        JOIN canonical_products c ON c.id = l.canonical_product_id
        WHERE l.price_item_id = %s
    """, (str(pid),))
    links = cur.fetchall()
    if links:
        print(f"  Связан с: {links[0][0][:60]}")
    else:
        print("  ❌ Не связан")

conn.close()
