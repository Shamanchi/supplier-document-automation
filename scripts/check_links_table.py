import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

# Структура canonical_product_sources
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'canonical_product_sources'
    ORDER BY ordinal_position
""")
print("📋 Структура canonical_product_sources:")
for col in cur.fetchall():
    print(f"  {col[0]}: {col[1]}")

# Сколько связей уже есть?
cur.execute("SELECT COUNT(*) FROM canonical_product_sources")
print(f"\nТекущих связей: {cur.fetchone()[0]}")

# Примеры supplier_price_items с кодами МПП, МУПТВ, ГГПТ
cur.execute("""
    SELECT id, source_name
    FROM supplier_price_items
    WHERE source_name ~* 'МПП\(Н\)-\d|МУПТВ-\d|ГГПТ-\d'
    LIMIT 10
""")
print("\n📋 Позиции с кодами МПП/МУПТВ/ГГПТ:")
for row in cur.fetchall():
    print(f"  {row[1][:70]}")

conn.close()
