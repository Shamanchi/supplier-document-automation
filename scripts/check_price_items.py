import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

# 1. Сколько поставщиков?
cur.execute("SELECT COUNT(*) FROM suppliers")
print(f"Поставщиков: {cur.fetchone()[0]}")

# 2. Сколько версий прайсов?
cur.execute("SELECT COUNT(*) FROM price_versions")
print(f"Версий прайсов: {cur.fetchone()[0]}")

# 3. Сколько позиций?
cur.execute("SELECT COUNT(*) FROM supplier_price_items")
print(f"Позиций прайса: {cur.fetchone()[0]}")

# 4. Посмотрим структуру supplier_price_items
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'supplier_price_items'
    ORDER BY ordinal_position
""")
print("\n📋 Структура supplier_price_items:")
for col in cur.fetchall():
    print(f"  {col[0]}: {col[1]}")

# 5. Примеры позиций (если есть)
cur.execute("""
    SELECT id, source_name, source_article, description, price_version_id, supplier_category_id
    FROM supplier_price_items
    LIMIT 10
""")
rows = cur.fetchall()
if rows:
    print("\n📋 Примеры позиций прайса:")
    for row in rows:
        print(f"  Название: {row[1]}")
        print(f"  Артикул: {row[2]}")
        print(f"  Описание: {row[3]}")
        print(f"  version_id: {row[4]}")
        print(f"  category_id: {row[5]}")
        print()
else:
    print("\n⚠️ Позиций прайса пока нет — нужно загрузить прайс через price_loader_app")

# 6. Канонические товары (наши)
cur.execute("SELECT COUNT(*) FROM canonical_products")
print(f"Канонических товаров: {cur.fetchone()[0]}")

cur.execute("SELECT title, match_key FROM canonical_products LIMIT 5")
print("\n📋 Примеры канонических товаров:")
for row in cur.fetchall():
    print(f"  {row[0]}")

conn.close()
