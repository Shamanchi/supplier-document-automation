#!/usr/bin/env python3
"""Замер связки «позиция прайса → сертификат» на боевых данных.

Работает по файлам зеркала и корпуса, без базы: замер разовый, поднимать под
него Postgres незачем. Считает две стороны одной связки:

* **сторона реестра** — сколько карточек дают хотя бы один ключ обозначения,
  сколько ключей приходится на карточку и насколько ключи различают карточки;
* **сторона прайсов** — сколько позиций каталога находят по этим ключам хотя бы
  один сертификат, у скольких кандидат ровно один и есть ли среди кандидатов
  действующий.

    python3 scripts/measure_product_keys.py --out ../../ocr/reports/product_keys
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.fsa.normalize import normalize_designation, parse_date  # noqa: E402
from app.fsa.product_keys import designations_of, lookup_keys  # noqa: E402

MIRROR = Path(__file__).resolve().parents[3] / "fsa_registry_mirror"
CATALOG = (
    Path(__file__).resolve().parents[3]
    / "external_certificates_passports_corpus"
    / "normalized_catalog.csv"
)

ALIVE_STATUSES = {"Действует", "Возобновлен", "Продлен"}


def load_registry(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def build_index(rows: list[dict[str, str]], on: date):
    """Ключ обозначения → список карточек; плюс статистика по стороне реестра."""
    index: dict[str, list[int]] = defaultdict(list)
    per_card: Counter[int] = Counter()
    alive: set[int] = set()

    for position, row in enumerate(rows):
        keys = designations_of(
            row.get("Обозначение продукции", ""),
            row.get("Модель продукции", ""),
            row.get("Марка продукции", ""),
            row.get("Артикул продукции", ""),
            row.get("Полное наименование продукции", ""),
        )
        per_card[position] = len(keys)
        for key in keys:
            index[key].append(position)

        status = (row.get("Статус") or "").strip()
        until = parse_date((row.get("Срок действия") or "").strip())
        if status in ALIVE_STATUSES and until is not None and until >= on:
            alive.add(position)

    return index, per_card, alive


def registry_side(rows, index, per_card) -> dict[str, object]:
    with_keys = sum(1 for value in per_card.values() if value)
    keys_total = sum(per_card.values())
    sizes = Counter(len(cards) for cards in index.values())
    unique_keys = sizes.get(1, 0)
    return {
        "certificates": len(rows),
        "certificates_with_keys": with_keys,
        "certificates_without_keys": len(rows) - with_keys,
        "coverage": round(with_keys / len(rows), 3) if rows else 0.0,
        "keys_total": keys_total,
        "keys_unique": len(index),
        "keys_per_certificate_mean": round(keys_total / len(rows), 2) if rows else 0.0,
        "keys_pointing_to_one_certificate": unique_keys,
        "keys_pointing_to_one_certificate_share": (
            round(unique_keys / len(index), 3) if index else 0.0
        ),
        "top_ambiguous_keys": [
            {"key": key, "certificates": len(cards)}
            for key, cards in sorted(
                index.items(), key=lambda item: len(item[1]), reverse=True
            )[:10]
        ],
    }


def price_side(index, alive, catalog_path: Path, limit: int) -> dict[str, object]:
    with catalog_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if limit:
        rows = rows[:limit]

    stats = Counter()
    candidates_hist = Counter()
    by_category: dict[str, Counter] = defaultdict(Counter)
    examples: list[dict[str, object]] = []

    for row in rows:
        stats["positions"] += 1
        category = (row.get("category") or "прочее").strip() or "прочее"
        by_category[category]["positions"] += 1
        query = " ".join(
            value
            for value in (
                row.get("article", ""),
                row.get("model", ""),
                row.get("normalized_product_name", "")
                or row.get("raw_product_name", ""),
            )
            if value
        ).strip()
        if not query:
            stats["empty"] += 1
            continue

        keys = lookup_keys(query)
        if not keys:
            stats["no_keys"] += 1
            continue
        stats["with_keys"] += 1

        found: set[int] = set()
        matched_key = ""
        for key in keys:
            cards = index.get(key)
            if cards:
                matched_key = matched_key or key
                found.update(cards)

        if not found:
            stats["no_certificate"] += 1
            continue

        stats["with_certificate"] += 1
        by_category[category]["hits"] += 1
        candidates_hist[min(len(found), 10)] += 1
        if len(found) == 1:
            stats["single_candidate"] += 1
        if found & alive:
            stats["has_alive_candidate"] += 1
        if len(examples) < 15:
            examples.append(
                {
                    "query": query[:90],
                    "matched_key": matched_key,
                    "candidates": len(found),
                    "alive": bool(found & alive),
                }
            )

    positions = stats["positions"] or 1
    return {
        "positions": stats["positions"],
        "positions_without_query": stats["empty"],
        "positions_with_keys": stats["with_keys"],
        "positions_without_keys": stats["no_keys"],
        "positions_with_certificate": stats["with_certificate"],
        "positions_without_certificate": stats["no_certificate"],
        "hit_rate": round(stats["with_certificate"] / positions, 3),
        "single_candidate": stats["single_candidate"],
        "single_candidate_share_of_hits": (
            round(stats["single_candidate"] / stats["with_certificate"], 3)
            if stats["with_certificate"]
            else 0.0
        ),
        "has_alive_candidate": stats["has_alive_candidate"],
        "alive_share_of_hits": (
            round(stats["has_alive_candidate"] / stats["with_certificate"], 3)
            if stats["with_certificate"]
            else 0.0
        ),
        "candidates_histogram": dict(sorted(candidates_hist.items())),
        "by_category": [
            {
                "category": name,
                "positions": counter["positions"],
                "hits": counter["hits"],
                "hit_rate": round(counter["hits"] / counter["positions"], 3)
                if counter["positions"]
                else 0.0,
            }
            for name, counter in sorted(
                by_category.items(), key=lambda item: item[1]["positions"], reverse=True
            )[:15]
        ],
        "examples": examples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", type=Path, default=MIRROR)
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--on", default=date.today().isoformat())
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()

    on = date.fromisoformat(arguments.on)
    rows = load_registry(arguments.mirror / "registry_index.csv")
    print(f"карточек реестра: {len(rows)}", flush=True)

    index, per_card, alive = build_index(rows, on)
    registry = registry_side(rows, index, per_card)
    print(json.dumps(registry, ensure_ascii=False, indent=2), flush=True)

    prices = price_side(index, alive, arguments.catalog, arguments.limit)
    print(json.dumps(prices, ensure_ascii=False, indent=2), flush=True)

    numbers_path = arguments.mirror / "registry_all_numbers.txt"
    numbers = sum(1 for line in numbers_path.open(encoding="utf-8") if line.strip())

    report = {
        "checked_on": on.isoformat(),
        "registry_numbers_total": numbers,
        "registry": registry,
        "prices": prices,
        "alive_certificates": len(alive),
    }
    if arguments.out:
        arguments.out.mkdir(parents=True, exist_ok=True)
        (arguments.out / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
