"""Idempotently build the initial global category directory from source leaves."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.category_service import bootstrap_global_category_directory
from app.db import SessionLocal, engine


async def _run(created_by: str) -> None:
    try:
        async with SessionLocal() as session:
            result = await bootstrap_global_category_directory(
                session, created_by=created_by
            )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--created-by", default="bootstrap:script")
    args = parser.parse_args()
    asyncio.run(_run(args.created_by))


if __name__ == "__main__":
    main()
