"""CLI for pruning stale parsed categories and rebuilding global mappings."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.category_service import refresh_category_directory
from app.db import make_task_engine


async def _run(created_by: str, *, dry_run: bool) -> None:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            result = await refresh_category_directory(
                session, created_by=created_by, commit=not dry_run
            )
            if dry_run:
                await session.rollback()
                result["dry_run"] = True
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--created-by", default="bootstrap:refresh-script")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate and validate the migration, then roll the transaction back",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.created_by, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
