"""
Связка 5 оставшихся товаров АО ИСТОЧНИК.
"""
import uuid
import psycopg2
from datetime import datetime, timezone

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

DB = DB_CONFIG
SUPPLIER = "ae683cb3-2ccc-4f72-94a9-72184c2b2bdf"
PROJECT = "10000000-0000-4000-8000-000000000001"

# (канон_title, поиск_в_прайсе)
PAIRS = [
    ("ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐1,0", "ГГПТ-1,0 ("),
    ("ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐7,0", "ГГПТ-7,0 ("),
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-Т)-50-КД2-ГЭ-У2", "МПП(Н-Т)-50-КД2"),
    ("ПОЖАРОТУШЕНИЯ УСП АА-1 Модификация v5", "УСПАА-1 v5"),
    ("МОДУЛЬ УСТАНОВОК ПОЖАРОТУШЕНИЯ ТОНКОРАСПЫЛЕННОЙ ВОДОЙ АВТОМАТИЧЕСКИХ МУПТВ-18,5-ГЗ-ВД (tºC = +5;-30;-50_п)", "МУПТВ-18,5-ГЗ-ВД"),
]

conn = psycopg2.connect(**DB)
cur = conn.cursor()

for canon_title, price_search in PAIRS:
    # Найти canonical_id
    cur.execute("SELECT id FROM canonical_products WHERE match_key = %s", (canon_title.upper(),))
    r = cur.fetchone()
    if not r:
        print(f"❌ Нет канона: {canon_title[:50]}")
        continue
    canon_id = r[0]
    
    # Найти price_item
    cur.execute("""
        SELECT id FROM supplier_price_items
        WHERE price_version_id IN (SELECT id FROM price_versions WHERE supplier_id = %s)
        AND source_name ILIKE %s
        LIMIT 1
    """, (SUPPLIER, f'%{price_search}%'))
    r2 = cur.fetchone()
    if not r2:
        print(f"❌ Нет прайса: {canon_title[:50]}")
        continue
    price_id = r2[0]
    
    # Удалить старую связь если есть (чтобы избежать конфликта)
    cur.execute("""
        DELETE FROM canonical_product_sources
        WHERE canonical_product_id = %s AND supplier_id = %s
    """, (str(canon_id), SUPPLIER))
    
    # Создать связь
    link_id = uuid.uuid4()
    cur.execute("""
        INSERT INTO canonical_product_sources
            (id, canonical_product_id, price_item_id, supplier_id, project_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (str(link_id), str(canon_id), str(price_id), SUPPLIER, PROJECT, datetime.now(timezone.utc)))
    print(f"✅ {canon_title[:50]} → {price_search}")

conn.commit()
conn.close()
print("\nГотово!")
