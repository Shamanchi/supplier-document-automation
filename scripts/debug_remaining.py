import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
conn = psycopg2.connect(**DB)
cur = conn.cursor()

titles = [
    "ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐1,0",
    "МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-Т)-50-КД2-ГЭ-У2",
    "ПОЖАРОТУШЕНИЯ УСП АА-1 Модификация v5",
    "ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐7,0",
    "МОДУЛЬ УСТАНОВОК ПОЖАРОТУШЕНИЯ ТОНКОРАСПЫЛЕННОЙ ВОДОЙ АВТОМАТИЧЕСКИХ МУПТВ-18,5-ГЗ-ВД (tºC = +5;-30;-50_п)",
]

for title in titles:
    print(f"\n{title[:60]}")
    cur.execute("SELECT id FROM canonical_products WHERE match_key = %s", (title.upper(),))
    r = cur.fetchone()
    if not r:
        print("  ❌ Нет в canonical_products")
        continue
    canon_id = r[0]
    print(f"  canonical_id: {canon_id}")
    
    cur.execute("SELECT price_item_id FROM canonical_product_sources WHERE canonical_product_id = %s AND supplier_id = 'ae683cb3-2ccc-4f72-94a9-72184c2b2bdf'", (str(canon_id),))
    links = cur.fetchall()
    if links:
        print(f"  ✅ Уже связан: {len(links)} связей")
    else:
        print("  ❌ Нет связей")
        # Ищем похожие в прайсе
        if "ГГПТ" in title:
            cur.execute("SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%ГГПТ-1,0%' OR source_name ILIKE '%ГГПТ-7,0%'")
        elif "МПП(Н-Т)-50" in title:
            cur.execute("SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%МПП(Н-Т)-50%'")
        elif "УСП" in title:
            cur.execute("SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%УСПАА-1%v5%'")
        elif "МУПТВ-18,5" in title:
            cur.execute("SELECT source_name FROM supplier_price_items WHERE source_name ILIKE '%МУПТВ-18,5%'")
        rows = cur.fetchall()
        print(f"  Похожие в прайсе ({len(rows)}):")
        for r in rows[:2]:
            print(f"    • {r[0][:60]}")

conn.close()
