import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

# 1. Какие уникальные названия в позициях прайса?
cur.execute("""
    SELECT DISTINCT source_name
    FROM supplier_price_items
    LIMIT 20
""")
print("📋 Примеры названий в прайсе:")
for row in cur.fetchall():
    print(f"  {row[0][:80]}")

# 2. Есть ли пересечение по ключевым словам?
# Найдём канонические товары, которые содержат "МППВК", "МУПТВ", "ГГПТ" и т.д.
print("\n📋 Канонические товары с кодами:")
cur.execute("""
    SELECT title, match_key FROM canonical_products
    WHERE title ~* 'МПП|МУПТВ|ГГПТ|ГОА|УСПАА'
    ORDER BY title
""")
for row in cur.fetchall():
    print(f"  {row[0]}")

# 3. Попробуем найти совпадения по нормализованным именам
# Нормализуем: убрать всё после скобок, убрать пробелы, верхний регистр
print("\n📋 Попытка fuzzy-поиска:")
cur.execute("SELECT title FROM canonical_products ORDER BY title")
canonicals = [r[0] for r in cur.fetchall()]

cur.execute("SELECT source_name FROM supplier_price_items ORDER BY source_name")
price_items = [r[0] for r in cur.fetchall()]

# Простой matching: если ключевое слово из канона есть в прайсе
matches = []
for c in canonicals[:20]:
    # Извлекаем "код" из канонического названия
    import re
    code_match = re.search(r'[МГУ][ПГО][ПТ][ТВКА]-?\d+', c)
    if code_match:
        code = code_match.group(0)
        for p in price_items:
            if code in p:
                matches.append((c, p))
                break

print(f"\nНайдено {len(matches)} совпадений (по коду):")
for c, p in matches[:5]:
    print(f"  Канон:  {c[:50]}")
    print(f"  Прайс:  {p[:50]}")
    print()

conn.close()
