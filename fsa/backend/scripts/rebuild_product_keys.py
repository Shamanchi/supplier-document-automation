"""Rebuild the derived registry product-key index from the command line."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.db import SessionLocal, engine
from app.services.lookup import LookupService


async def rebuild(batch: int) -> dict[str, int]:
    try:
        async with SessionLocal() as session:
            result = await LookupService(session).rebuild_product_keys(batch=batch)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=2000)
    args = parser.parse_args()
    if args.batch < 1:
        parser.error("--batch must be positive")
    print(json.dumps(asyncio.run(rebuild(args.batch)), ensure_ascii=False))


if __name__ == "__main__":
    main()
