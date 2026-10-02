import csv
import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
conn = psycopg2.connect(**DB)
cur = conn.cursor()

csv_rows = []
with open("all_passports_results.csv", "r", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f, delimiter=";"):
        if row.get("Поставщик", "").strip() == "АО ИСТОЧНИК" and row.get("Статус", "").strip() == "OK":
            csv_rows.append(row)

print("❌ Товары БЕЗ связи:")
for row in csv_rows:
    title = row.get("Наименование изделия", "").strip()
    if not title:
        continue
    cur.execute("SELECT 1 FROM canonical_products c JOIN canonical_product_sources l ON c.id = l.canonical_product_id WHERE c.match_key = %s AND l.supplier_id = 'ae683cb3-2ccc-4f72-94a9-72184c2b2bdf'", (title.upper(),))
    if not cur.fetchone():
        print(f"  • {title}")

conn.close()
