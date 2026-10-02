import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

# Проверим canonical_products
cur.execute("SELECT COUNT(*), MAX(created_at) FROM canonical_products")
row = cur.fetchone()
print(f"Канонических товаров: {row[0]}, последний: {row[1]}")

# Проверим product_documents
cur.execute("SELECT COUNT(*), MAX(created_at) FROM product_documents")
row = cur.fetchone()
print(f"Документов: {row[0]}, последний: {row[1]}")

# Посмотрим структуру таблиц
cur.execute("""
    SELECT column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_name = 'canonical_products'
    ORDER BY ordinal_position
""")
print("\nСтруктура canonical_products:")
for col in cur.fetchall():
    print(f"  {col[0]}: {col[1]} {'NULL' if col[2]=='YES' else 'NOT NULL'}")

cur.execute("""
    SELECT column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_name = 'product_documents'
    ORDER BY ordinal_position
""")
print("\nСтруктура product_documents:")
for col in cur.fetchall():
    print(f"  {col[0]}: {col[1]} {'NULL' if col[2]=='YES' else 'NOT NULL'}")

conn.close()
