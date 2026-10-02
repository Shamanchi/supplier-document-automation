import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
conn = psycopg2.connect(**DB)
cur = conn.cursor()

# Сколько канонических товаров АО ИСТОЧНИК имеет связь?
cur.execute("""
    SELECT COUNT(DISTINCT c.id)
    FROM canonical_products c
    JOIN canonical_product_sources l ON c.id = l.canonical_product_id
    WHERE l.supplier_id = 'ae683cb3-2ccc-4f72-94a9-72184c2b2bdf'
""")
linked = cur.fetchone()[0]

# Сколько всего канонических товаров АО ИСТОЧНИК?
cur.execute("""
    SELECT COUNT(*) FROM canonical_products
    WHERE match_key IN (
        SELECT DISTINCT match_key FROM canonical_products
        WHERE id IN (SELECT canonical_product_id FROM canonical_product_sources WHERE supplier_id = 'ae683cb3-2ccc-4f72-94a9-72184c2b2bdf')
    )
""")
# Проще: посчитаем через CSV
import csv
csv_rows = []
with open("all_passports_results.csv", "r", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f, delimiter=";"):
        if row.get("Поставщик", "").strip() == "АО ИСТОЧНИК" and row.get("Статус", "").strip() == "OK":
            csv_rows.append(row)

total_canonical = 0
linked_canonical = 0
for row in csv_rows:
    title = row.get("Наименование изделия", "").strip()
    if not title:
        continue
    total_canonical += 1
    cur.execute("SELECT 1 FROM canonical_products c JOIN canonical_product_sources l ON c.id = l.canonical_product_id WHERE c.match_key = %s AND l.supplier_id = 'ae683cb3-2ccc-4f72-94a9-72184c2b2bdf'", (title.upper(),))
    if cur.fetchone():
        linked_canonical += 1

print(f"Всего товаров АО ИСТОЧНИК: {total_canonical}")
print(f"Связано с прайсом: {linked_canonical}")
print(f"Покрытие: {linked_canonical}/{total_canonical} ({linked_canonical*100//total_canonical}%)")

conn.close()
