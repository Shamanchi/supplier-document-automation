import psycopg2
import re

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

# Канонические товары без совпадений
cur.execute("SELECT title, match_key FROM canonical_products ORDER BY title")
canonicals = cur.fetchall()

# Все позиции АО Источник
cur.execute("""
    SELECT source_name FROM supplier_price_items
    WHERE price_version_id IN (
        SELECT id FROM price_versions WHERE supplier_id = 'ae683cb3-2ccc-4f72-94a9-72184c2b2bdf'
    )
""")
price_names = [r[0] for r in cur.fetchall()]

def extract_code(title):
    title = title.upper().replace(" ", "")
    match = re.search(r'(МППВК|МПП|МУПТВ|ГГПТ|ГОА|УСПАА)[\(]?([А-ЯВЗР-Т]{1,3})?[\)]?-?(\d+[\.,]?\d*)', title)
    if match:
        code = match.group(1)
        mod = match.group(2) or ""
        num = match.group(3).replace(",", ".")
        if num.endswith(".0"):
            num = num[:-2]
        return f"{code}{mod}{num}"
    return None

def norm(t):
    return t.upper().replace(" ", "").replace("(", "").replace(")", "").replace("-", "").replace(",", ".")

no_match = []
for title, match_key in canonicals:
    code = extract_code(title)
    if not code:
        no_match.append((title, "no_code"))
        continue
    found = any(code in norm(p) for p in price_names)
    if not found:
        no_match.append((title, code))

print(f"❌ Без совпадений: {len(no_match)}")
print("\n📋 Примеры (первые 20):")
for title, code in no_match[:20]:
    print(f"  Код: {code}")
    print(f"    {title[:70]}")
    print()

conn.close()
