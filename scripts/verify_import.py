import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM canonical_products")
print(f"Канонических товаров: {cur.fetchone()[0]}")

cur.execute("SELECT COUNT(*) FROM product_documents WHERE kind = 'passport'")
print(f"Паспортов-документов: {cur.fetchone()[0]}")

cur.execute("SELECT COUNT(*) FROM document_links")
print(f"Связей товар-документ: {cur.fetchone()[0]}")

# Покажем пример записи
cur.execute("""
    SELECT cp.title, cp.match_key, pd.storage_key
    FROM canonical_products cp
    JOIN document_links dl ON dl.canonical_product_id = cp.id
    JOIN product_documents pd ON pd.id = dl.document_id
    LIMIT 3
""")
print("\n📋 Примеры записей:")
for row in cur.fetchall():
    print(f"  Товар: {row[0][:50]}...")
    print(f"  Файл: {row[2]}")
    print()

conn.close()
