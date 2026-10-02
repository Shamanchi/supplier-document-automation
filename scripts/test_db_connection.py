import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

try:
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT version()")
    print("✅ Подключение к PostgreSQL успешно:")
    print(cur.fetchone()[0])
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    print("\n📋 Таблицы в БД:")
    for row in cur.fetchall():
        print(f"  - {row[0]}")
    conn.close()
except Exception as e:
    print(f"❌ Ошибка: {e}")
