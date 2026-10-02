import asyncio
from sqlalchemy import text
from app.db import make_task_engine

async def main():
    engine, factory = make_task_engine()
    async with factory() as s:
        r = await s.execute(text("""
            SELECT v.amount, v.currency, v.vat_mode, v.min_quantity, v.source_label,
                   v.price_kind, v.source_column_index, v.currency_source
            FROM supplier_price_values v JOIN supplier_price_items i ON i.id=v.item_id
            JOIN supplier_price_versions pv ON pv.id=i.price_version_id
            WHERE pv.id::text LIKE 'd70bf29c%' AND i.source_name LIKE 'МППВК-17%'
            ORDER BY v.source_column_index
        """))
        for row in r:
            print(tuple(row))
        print('--- aggregate d70bf29c ---')
        r2 = await s.execute(text("""
            SELECT v.vat_mode, v.currency, v.currency_source, count(*)
            FROM supplier_price_values v
            JOIN supplier_price_items i ON i.id=v.item_id
            JOIN supplier_price_versions pv ON pv.id=i.price_version_id
            WHERE pv.id::text LIKE 'd70bf29c%' GROUP BY 1,2,3
        """))
        for row in r2:
            print(tuple(row))
    await engine.dispose()

asyncio.run(main())
