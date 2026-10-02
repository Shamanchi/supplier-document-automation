import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

# Поставщики
cur.execute("SELECT id, name FROM suppliers")
print("📋 Поставщики:")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

# Проекты
cur.execute("SELECT id, name FROM projects")
print("\n📋 Проекты:")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

# Версии прайсов
cur.execute("""
    SELECT pv.id, pv.version_no, s.name, pv.status
    FROM price_versions pv
    JOIN suppliers s ON s.id = pv.supplier_id
""")
print("\n📋 Версии прайсов:")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]} | {row[2]} | {row[3]}")

conn.close()
