"""Measure catalog-to-certificate coverage through a deployed lookup API."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from collections import Counter
from pathlib import Path
import httpx

from app.fsa.product_keys import lookup_keys


async def main_async(arguments: argparse.Namespace) -> dict[str, object]:
    with arguments.catalog.open(encoding="utf-8-sig", newline="") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if (row.get("category") or "").strip() == arguments.category
        ]

    semaphore = asyncio.Semaphore(arguments.concurrency)
    stats = Counter()

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {arguments.token}"}, timeout=30
    ) as client:
        async def check(row: dict[str, str]) -> None:
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
            stats["positions"] += 1
            if not lookup_keys(query):
                stats["without_keys"] += 1
                return
            stats["with_keys"] += 1
            async with semaphore:
                response = await client.get(
                    f"{arguments.base_url.rstrip('/')}/v1/lookup/by-product",
                    params={"code": query, "limit": 1},
                )
                response.raise_for_status()
            if response.json().get("total", 0):
                stats["hits"] += 1

        await asyncio.gather(*(check(row) for row in rows))

    positions = stats["positions"]
    return {
        "category": arguments.category,
        "positions": positions,
        "positions_with_keys": stats["with_keys"],
        "positions_without_keys": stats["without_keys"],
        "hits": stats["hits"],
        "hit_rate": round(stats["hits"] / positions, 4) if positions else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", default=os.environ.get("FSA_API_TOKEN", ""))
    parser.add_argument("--concurrency", type=int, default=20)
    arguments = parser.parse_args()
    if not arguments.token:
        parser.error("--token or FSA_API_TOKEN is required")
    print(json.dumps(asyncio.run(main_async(arguments)), ensure_ascii=False))


if __name__ == "__main__":
    main()
