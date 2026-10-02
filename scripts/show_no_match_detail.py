"""
Анализ 14 несвязанных товаров АО ИСТОЧНИК.
Показывает: канонический товар + поиск похожих позиций в прайсе.
"""
import csv
import re
import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

SUPPLIER_ISTOCHNIK = "ae683cb3-2ccc-4f72-94a9-72184c2b2bdf"
CSV_PATH = "all_passports_results.csv"

def _fix(t: str) -> str:
    return t.replace("\u2010", "-").replace("\u2013", "-").replace("\u2014", "-")

def extract_codes(title: str) -> list:
    title = _fix(title).upper()
    codes = []
    parts = [p.strip() for p in title.split(";")]
    for part in parts:
        part_ns = part.replace(" ", "")
        m = re.search(r'(МППВК|МПП|МУПТВ|ГГПТ|УСПАА)[\(]?([А-ЯA-Z0-9\-]{1,10})?[\)]?[-]?(\d+[\.,]?\d*)[\(]?([А-ЯA-Z])?[\)]?', part_ns)
        if m:
            code = m.group(1)
            mod1 = (m.group(2) or "").replace("-", "")
            num = m.group(3).replace(",", ".")
            mod2 = m.group(4) or ""
            if num.endswith(".0"): num = num[:-2]
            elif "." in num: num = num.replace(".", "")
            codes.append(f"{code}{mod1}{num}{mod2}")
            continue
        m = re.search(r'(ГОА)-(II|I|2|1)[-]?(\d+[\.,]?\d*)', part_ns)
        if m:
            mod = m.group(2).replace("II", "2").replace("I", "1")
            num = m.group(3).replace(",", ".").replace(".", "")
            codes.append(f"ГОА{mod}{num}")
    return codes

def norm(t: str) -> str:
    return _fix(t).upper().replace(" ", "").replace("(", "").replace(")", "").replace("-", "").replace(",", ".")

# 1. Читаем CSV — товары АО ИСТОЧНИК со статусом OK
csv_rows = []
with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f, delimiter=";"):
        if row.get("Поставщик", "").strip() == "АО ИСТОЧНИК" and row.get("Статус", "").strip() == "OK":
            csv_rows.append(row)

# 2. Подключаемся к БД
conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

# Канонические товары
canonical_map = {}
for row in csv_rows:
    title = row.get("Наименование изделия", "").strip()
    if not title: continue
    cur.execute("SELECT id FROM canonical_products WHERE match_key = %s", (title.upper(),))
    r = cur.fetchone()
    if r:
        canonical_map[title] = r[0]

# Позиции прайса
cur.execute("""
    SELECT id, source_name, normalized_name
    FROM supplier_price_items
    WHERE price_version_id IN (SELECT id FROM price_versions WHERE supplier_id = %s)
""", (SUPPLIER_ISTOCHNIK,))
price_items = cur.fetchall()

price_index = {}
for item_id, source_name, norm_name in price_items:
    key = norm(norm_name or source_name)
    price_index.setdefault(key, []).append((item_id, source_name))

# 3. Находим no_match
no_match = []
for title, canonical_id in canonical_map.items():
    codes = extract_codes(title)
    found = False
    for code in codes:
        for norm_key in price_index:
            if code in norm_key:
                found = True
                break
        if found: break
    if not found:
        # Ищем похожие позиции в прайсе (fuzzy)
        similar = []
        title_norm = norm(title)
        for norm_key, items in price_index.items():
            # Проверяем общие подстроки длиной >= 4
            for i in range(len(title_norm) - 3):
                substr = title_norm[i:i+4]
                if len(substr) >= 4 and substr in norm_key:
                    similar.extend(items)
                    break
            if len(similar) >= 3:
                break
        no_match.append((title, codes, similar[:5]))

print(f"❌ Несвязанных товаров: {len(no_match)}\n")
print("="*80)
for i, (title, codes, similar) in enumerate(no_match, 1):
    print(f"\n{i}. {title}")
    print(f"   Коды: {codes}")
    if similar:
        print(f"   Похожие позиции в прайсе:")
        for _, sname in similar:
            print(f"      • {sname[:70]}")
    else:
        print(f"   Похожих позиций НЕ НАЙДЕНО")
    print("-"*80)

conn.close()
