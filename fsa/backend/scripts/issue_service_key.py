"""Выпуск и отзыв сервисных ключей контура сертификатов.

Запуск:
    python -m scripts.issue_service_key issue --name price-backend --scopes read
    python -m scripts.issue_service_key list
    python -m scripts.issue_service_key revoke --name price-backend

Ключ печатается один раз и в базе не хранится: там лежит только его хэш.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ServiceClient
from app.service_keys import ALL_SCOPES, ServiceKeyError, ServiceKeyService


async def issue(name: str, scopes: str, note: str) -> int:
    async with SessionLocal() as session:
        try:
            client, raw_key = await ServiceKeyService(session).issue(
                name=name, scopes=scopes, note=note
            )
        except ServiceKeyError as error:
            print(f"ошибка: {error}")
            return 1
        await session.commit()
        print(f"потребитель: {client.name}")
        print(f"области:     {client.scopes}")
        print(f"ключ:        {raw_key}")
        print("ключ показан один раз — сохраните его сейчас")
        return 0


async def revoke(name: str) -> int:
    async with SessionLocal() as session:
        client = await ServiceKeyService(session).revoke(name)
        if client is None:
            print(f"потребитель «{name}» не найден")
            return 1
        await session.commit()
        print(f"ключ потребителя «{client.name}» отозван")
        return 0


async def show() -> int:
    async with SessionLocal() as session:
        rows = list(await session.scalars(select(ServiceClient).order_by(ServiceClient.name)))
        if not rows:
            print("сервисных ключей нет")
            return 0
        for row in rows:
            state = "активен" if row.is_active else "отозван"
            used = row.last_used_at.isoformat() if row.last_used_at else "не использовался"
            print(f"{row.name:24} {row.scopes:20} {state:8} {used}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    issue_parser = sub.add_parser("issue", help="выпустить ключ")
    issue_parser.add_argument("--name", required=True, help="имя потребителя")
    issue_parser.add_argument(
        "--scopes",
        default="read",
        help=f"области через запятую: {', '.join(ALL_SCOPES)}",
    )
    issue_parser.add_argument("--note", default="", help="пояснение для будущего читателя")

    revoke_parser = sub.add_parser("revoke", help="отозвать ключ")
    revoke_parser.add_argument("--name", required=True)

    sub.add_parser("list", help="показать потребителей")

    args = parser.parse_args()
    if args.command == "issue":
        return asyncio.run(issue(args.name, args.scopes, args.note))
    if args.command == "revoke":
        return asyncio.run(revoke(args.name))
    return asyncio.run(show())


if __name__ == "__main__":
    raise SystemExit(main())
