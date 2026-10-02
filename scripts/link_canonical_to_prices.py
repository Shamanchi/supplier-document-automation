# -*- coding: utf-8 -*-
"""Связка канонических товаров (из паспортов) с позициями прайса.

Скрипт обобщён (wave-2, 2026-09-03): поставщик и CSV задаются аргументами,
жёсткая привязка к «АО ИСТОЧНИК» убрана. Демо-поставщик — Supplier A.

Сопоставление по обозначениям изделий: у канона (паспорта) из названия
извлекаются коды размерного ряда («ОП-25», «МПП-2,5»), у позиции прайса — её
обозначение; позиция привязывается к канону, если хотя бы один код совпал.
Один паспорт закрывает весь ряд (например, ОП-25..100), поэтому на один канон
обычно приходится много позиций прайса.

Запуск из корня репозитория:
    python scripts/link_canonical_to_prices.py                      # dry run
    python scripts/link_canonical_to_prices.py --execute            # запись
    python scripts/link_canonical_to_prices.py --csv all_passports_results.csv --supplier "Supplier F (Supplier A прайс)" --execute
"""

import csv
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_config import DB_CONFIG

PROJECT_ORIGINAL = "10000000-0000-4000-8000-000000000001"
CSV_PATH = "all_passports_results.csv"
SUPPLIER = "Supplier A"

# Семейства обозначений: порядок важен — длинные альтернативы раньше коротких.
_FAMILIES = r"МППВК|МПП|МУПТВ|ГГПТ|УСПАА|ОПУ|ОСП|ОВП|ОП|ОУ"
_CODE_RE = re.compile(
    r"(?<![А-ЯЁA-Z0-9])(?:" + _FAMILIES + r")[\s\-–—]?\(?[Н]?\)?[\s\-–—]?(\d{1,3}(?:[.,]\d)?)"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)

# Аксессуары и ЗИП: паспорт относится к самому изделию, а не к его
# принадлежностям (чехол, ЗПУ, кронштейн и т.п.).
ACCESSORY = (
    "ЧЕХОЛ", "КРОНШТЕЙН", "ЗПУ", "РАСТРУБ", "ПОДСТАВКА", "ПОДСТ.",
    "КРЕСТ", "ТЕЛЕЖК", "ВЫКИДНАЯ", "ПЛАКАТ", "СТЕНД", "ПЕНОГЕНЕРАТОР",
    "ТРАНСП", "ШЛАНГ", "СУМКА", "ЯЩИК", "КОМПЛЕКТ", "ЗАРЯД", "ПЕРЕЗАРЯД",
    "ЗАПРАВКА", "БАЛЛОН", "ЗАГЛУШК", "СИГНАЛИЗАТОР", "УКУПОРК",
)


def extract_codes(text: str) -> set[str]:
    """Обозначения вида (семейство, число) из строки: 'ОП-25(з)' -> {'ОП25'}."""
    if not text:
        return set()
    codes: set[str] = set()
    for match in _CODE_RE.finditer(text.upper()):
        prefix = match.group(0)
        family = re.search(_FAMILIES, prefix).group(0)
        raw = match.group(1)
        number = raw.replace(",", ".")
        if number.endswith(".0"):
            number = number[:-2]
        codes.add(f"{family}{number}")
    return codes


def _cli_args() -> dict:
    args = {"csv": CSV_PATH, "supplier": SUPPLIER, "execute": False}
    argv = sys.argv[1:]
    if "--csv" in argv:
        args["csv"] = argv[argv.index("--csv") + 1]
    if "--supplier" in argv:
        args["supplier"] = argv[argv.index("--supplier") + 1]
    if "--execute" in argv:
        args["execute"] = True
    return args


def _resolve_supplier(cur, supplier_name: str) -> str:
    """Поставщик, у которого в БД реально есть позиции прайса."""
    cur.execute(
        """
        SELECT s.id, s.name
        FROM suppliers s
        JOIN price_versions pv ON pv.supplier_id = s.id
        WHERE s.name ILIKE %s
        GROUP BY s.id, s.name
        ORDER BY count(pv.id) DESC
        LIMIT 1
        """,
        (f"%{supplier_name}%",),
    )
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"Поставщик не найден или без прайсов: {supplier_name}")
    return str(row[0])


def link_filtered(dry_run: bool = True, csv_path: str = "", supplier: str = "") -> dict:
    # 1. Читаем CSV (только строки нужного поставщика со статусом OK)
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = [
            row
            for row in csv.DictReader(f, delimiter=";")
            if row.get("Поставщик", "").strip() == supplier and row.get("Статус", "").strip() == "OK"
        ]
    print(f"Паспортов в CSV ({supplier}): {len(rows)}")

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    supplier_id = _resolve_supplier(cur, supplier)
    print(f"Поставщик в БД: {supplier_id}")

    # 2. Канонические товары по названиям из CSV
    titles = [row.get("Наименование изделия", "").strip() for row in rows if row.get("Наименование изделия")]
    canonical_map: dict[str, str] = {}
    for title in titles:
        cur.execute("SELECT id FROM canonical_products WHERE match_key = %s", (title.upper(),))
        row = cur.fetchone()
        if row:
            canonical_map[title] = str(row[0])
    print(f"Канонов найдено: {len(canonical_map)}/{len(set(titles))}")

    # 3. Позиции прайса поставщика
    cur.execute(
        """
        SELECT i.id, i.source_name, i.normalized_name
        FROM supplier_price_items i
        WHERE i.price_version_id IN (SELECT id FROM price_versions WHERE supplier_id = %s)
        """,
        (supplier_id,),
    )
    price_items = cur.fetchall()
    print(f"Позиций прайса: {len(price_items)}")

    item_codes: dict[str, list] = {}
    for item_id, source_name, normalized_name in price_items:
        name = normalized_name or source_name or ""
        if any(a in name.upper() for a in ACCESSORY):
            continue
        for code in extract_codes(name):
            item_codes.setdefault(code, []).append((str(item_id), source_name))

    # 4. Сопоставление по кодам
    matches: list[tuple[str, str, str, str, str]] = []
    no_match: list[tuple[str, str]] = []
    for title, canonical_id in canonical_map.items():
        codes = extract_codes(title)
        if not codes:
            no_match.append((title, "no_code"))
            continue
        found: set = set()
        for code in codes:
            for item_id, source_name in item_codes.get(code, []):
                found.add((item_id, source_name))
        if found:
            first = sorted(found)[0]
            matches.append((canonical_id, first[0], title, first[1], ", ".join(sorted(codes)[:5])))
        else:
            no_match.append((title, ", ".join(sorted(codes))))

    print(f"\nСовпадений (канон -> первая позиция): {len(matches)}")
    print(f"Канонов без совпадения: {len(no_match)}")
    print("\nПримеры:")
    for _, _, title, price_name, codes in matches[:6]:
        print(f"  [{codes}]")
        print(f"    Канон:  {title[:80]}")
        print(f"    Прайс:  {price_name[:80]}")

    if no_match:
        print("\nБез совпадения:")
        for title, codes in no_match:
            print(f"  [{codes}] {title[:80]}")

    if dry_run:
        conn.close()
        return {"status": "dry_run", "canonicals": len(canonical_map), "matched_canonicals": len(matches)}

    # 5. Запись: сначала чистим старые связи этих канонов, затем вставляем
    canonical_ids = list(canonical_map.values())
    cur.execute(
        """
        DELETE FROM canonical_product_sources
        WHERE supplier_id = %s AND canonical_product_id = ANY(%s::uuid[])
        """,
        (supplier_id, canonical_ids),
    )
    print(f"\nУдалено старых связей: {cur.rowcount}")

    inserted = 0
    for canonical_id, _, title, _, _ in matches:
        cur.execute(
            """
            SELECT price_item_id FROM canonical_product_sources
            WHERE canonical_product_id = %s AND supplier_id = %s
            """,
            (canonical_id, supplier_id),
        )
        linked = {r[0] for r in cur.fetchall()}
        codes = extract_codes(title)
        for code in codes:
            for item_id, _ in item_codes.get(code, []):
                if item_id in linked:
                    continue
                cur.execute("SELECT 1 FROM canonical_product_sources WHERE price_item_id = %s", (item_id,))
                if cur.fetchone():
                    continue
                cur.execute(
                    """
                    INSERT INTO canonical_product_sources
                        (id, canonical_product_id, price_item_id, supplier_id, project_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (str(uuid.uuid4()), str(canonical_id), item_id, supplier_id, PROJECT_ORIGINAL, _now()),
                )
                inserted += 1
                linked.add(item_id)

    conn.commit()
    conn.close()
    print(f"Создано связей канон->позиция: {inserted}")
    return {"status": "success", "inserted": inserted}


if __name__ == "__main__":
    cli = _cli_args()
    dry = not cli["execute"]
    print("РЕЖИМ ПРОВЕРКИ (dry run), данные не пишутся\n" if dry else "РЕАЛЬНАЯ ЗАПИСЬ\n")
    link_filtered(dry_run=dry, csv_path=cli["csv"], supplier=cli["supplier"])
