import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
conn = psycopg2.connect(**DB)
cur = conn.cursor()

# Проверяем МПП(Н-Т)-50 и УСПАА-1
cur.execute("SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%МПП(Н-Т)-50-КД2%'")
print("МПП(Н-Т)-50-КД2:")
for r in cur.fetchall():
    print(f"  {r[0]}")

cur.execute("SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%УСПАА-1%v5%'")
print("\nУСПАА-1 v5:")
for r in cur.fetchall():
    print(f"  {r[0]}")

conn.close()
