import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM canonical_product_sources")
print(f"Связей канон-товар -> прайс: {cur.fetchone()[0]}")

cur.execute("""
    SELECT cp.title, spi.source_name
    FROM canonical_product_sources cps
    JOIN canonical_products cp ON cp.id = cps.canonical_product_id
    JOIN supplier_price_items spi ON spi.id = cps.price_item_id
    LIMIT 5
""")
print("\nПримеры связей:")
for row in cur.fetchall():
    print(f"  Канон: {row[0][:40]}")
    print(f"  Прайс: {row[1][:40]}")
    print()

conn.close()
