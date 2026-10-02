"""
Исследование несопоставленных товаров.
Проверяем: есть ли они в прайсе под другим названием?
"""

import psycopg2
import re

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

# Товары без совпадения (из предыдущего запуска)
no_match_cases = [
    ("ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐0,25; ГГПТ‐0,5", "ГГПТ 0.25 0.5"),
    ("ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐1,0", "ГГПТ 1"),
    ("ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐3,0", "ГГПТ 3"),
    ("ГЕНЕРАТОР ОГНЕТУШАЩЕГО АЭРОЗОЛЯ ГОА‐II‐1,10‐020‐010", "ГОА"),
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н)-0,65-И-ГЭ-У2", "МППН 0.65"),
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-Взр)-2,7-И-ГЭ-У2", "МППНВзр 2.7"),
    ("МУПТВ-13,5-ГЗ-ВД", "МУПТВ 13.5"),
    ("МУПТВ-18,5-ГЗ-ВД", "МУПТВ 18.5"),
    ("МУПТВ(Взр)-13,5-ГЗ-ВД", "МУПТВВзр 13.5"),
    ("УСП АА-1 Модификация v5", "УСПАА"),
]

conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

cur.execute("""
    SELECT source_name FROM supplier_price_items
    WHERE price_version_id IN (
        SELECT id FROM price_versions 
        WHERE supplier_id = 'ae683cb3-2ccc-4f72-94a9-72184c2b2bdf'
    )
""")
price_names = [r[0] for r in cur.fetchall()]

print("📋 Поиск несопоставленных товаров в прайсе:\n")

for canonical, search_terms in no_match_cases:
    print(f"🔍 Канон: {canonical[:60]}")
    found = []
    terms = search_terms.lower().split()
    for p in price_names:
        p_lower = p.lower()
        if all(t in p_lower for t in terms):
            found.append(p)
    if found:
        print(f"   ✅ Найдено в прайсе ({len(found)}):")
        for f in found[:3]:
            print(f"      {f[:70]}")
    else:
        print(f"   ❌ Не найдено в прайсе")
        # Поищем по частям
        for t in terms:
            partial = [p for p in price_names if t in p.lower()]
            if partial:
                print(f"      По '{t}': {partial[0][:60]}")
                break
    print()

conn.close()
