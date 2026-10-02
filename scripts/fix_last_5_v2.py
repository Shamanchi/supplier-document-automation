"""
Связка 5 оставшихся товаров (дубли и составные позиции).
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

PAIRS = [
    # ГГПТ-1,0 дубль → та же позиция (удалим старую связь дубля если есть)
    ("ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐1,0", "ГГПТ-1,0 ("),
    # ГГПТ-7,0 дубль
    ("ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐7,0", "ГГПТ-7,0 ("),
    # МПП(Н-Т)-50 → составная позиция с МПП(Н)-24
    ("МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-Т)-50-КД2-ГЭ-У2", "МПП(Н-Т)-50-КД2"),
    # УСПАА-1 v5 → свободная позиция
    ("ПОЖАРОТУШЕНИЯ УСП АА-1 Модификация v5", "УСПАА-1 v5 (двухканальный"),
    # МУПТВ-18,5 → уже связан с другим canonical, проверим
    ("МОДУЛЬ УСТАНОВОК ПОЖАРОТУШЕНИЯ ТОНКОРАСПЫЛЕННОЙ ВОДОЙ АВТОМАТИЧЕСКИХ МУПТВ-18,5-ГЗ-ВД (tºC = +5;-30;-50_п)", "МУПТВ-18,5-ГЗ-ВД (tºC = -50)"),
]

conn = psycopg2.connect(**DB)
cur = conn.cursor()

for canon_title, price_search in PAIRS:
    cur.execute("SELECT id FROM canonical_products WHERE match_key = %s", (canon_title.upper(),))
    r = cur.fetchone()
    if not r:
        print(f"❌ Нет канона: {canon_title[:50]}")
        continue
    canon_id = r[0]
    
    # Найти price_item (незанятую)
    cur.execute("""
        SELECT p.id, p.source_name FROM supplier_price_items p
        WHERE p.price_version_id IN (SELECT id FROM price_versions WHERE supplier_id = %s)
        AND p.source_name ILIKE %s
        AND NOT EXISTS (
            SELECT 1 FROM canonical_product_sources l
            WHERE l.price_item_id = p.id
        )
        LIMIT 1
    """, (SUPPLIER, f'%{price_search}%'))
    r2 = cur.fetchone()
    if not r2:
        # Если не нашли свободную, берём любую
        cur.execute("""
            SELECT id, source_name FROM supplier_price_items
            WHERE price_version_id IN (SELECT id FROM price_versions WHERE supplier_id = %s)
            AND source_name ILIKE %s
            LIMIT 1
        """, (SUPPLIER, f'%{price_search}%'))
        r2 = cur.fetchone()
    if not r2:
        print(f"❌ Нет прайса: {canon_title[:50]}")
        continue
    price_id, price_name = r2
    
    # Удалить старую связь этого канона если есть
    cur.execute("DELETE FROM canonical_product_sources WHERE canonical_product_id = %s AND supplier_id = %s", (str(canon_id), SUPPLIER))
    
    # Создать связь
    link_id = uuid.uuid4()
    cur.execute("""
        INSERT INTO canonical_product_sources (id, canonical_product_id, price_item_id, supplier_id, project_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (str(link_id), str(canon_id), str(price_id), SUPPLIER, PROJECT, datetime.now(timezone.utc)))
    print(f"✅ {canon_title[:50]} → {price_name[:50]}")

conn.commit()
conn.close()
print("\nГотово!")
