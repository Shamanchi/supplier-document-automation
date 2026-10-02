#!/usr/bin/env python3
"""Прогон подбора по размеченному голдсету.

Голдсет (`request_price_matching/goldset/`) — единственный размеченный набор в
проекте: 76 строк заявок, к каждой руками отмечены допустимые позиции прайса
(`acceptable_targets`), запрещённые (`forbidden_targets`) и случаи, где
правильный исход — уточнение у менеджера, а не автовыбор.

Прогон идёт на снимке каталога из того же голдсета, а не на боевой базе: так
цифра воспроизводима и не зависит от того, какие прайсы залиты на стенде.
Внутри поднимается временная SQLite-база, в неё грузятся позиции, строится
канон и индекс эмбеддингов, и дальше работает ровно тот же каскад, что в бою.

    python3 scripts/eval_matching.py --out ../../../docs/eval/matching
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.matching.canonical import CanonicalService  # noqa: E402
from app.matching.embedder import build_embedder, set_embedder  # noqa: E402
from app.matching.cascade import AUTO_ACCEPT, NEEDS_REVIEW, MatchService  # noqa: E402
from app.matching.embeddings import EmbeddingIndexService  # noqa: E402
from app.matching.text import normalize_name  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    CanonicalProductSource,
    PriceFile,
    PriceVersion,
    Project,
    Supplier,
    SupplierPriceItem,
)

GOLDSET = (
    Path(__file__).resolve().parents[4] / "request_price_matching" / "goldset"
)
PROJECT_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")


def position_id(supplier: str, sheet: str, row: str) -> str:
    return f"{supplier}|{sheet}|{row}"


async def load_catalog(session, path: Path, semantic=None) -> dict[str, list[uuid.UUID]]:
    """Снимок каталога голдсета → позиции прайсов во временной базе.

    Ключ — нормализованное наименование, а не «поставщик|лист|строка»: в
    снимке на одном листе лежат несколько таблиц бок о бок, и номер строки
    повторяется у разных товаров (1 002 таких ключа из 1 606). Голдсет
    ссылается на позицию и по имени тоже, и оно однозначно.
    """
    project = Project(id=PROJECT_ID, name="Голдсет", slug="goldset")
    session.add(project)
    await session.flush()

    suppliers: dict[str, PriceVersion] = {}
    items: dict[str, list[uuid.UUID]] = {}
    seen: set[tuple[str, str, str]] = set()

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            supplier_name = (row.get("supplier") or "").strip()
            version = suppliers.get(supplier_name)
            if version is None:
                supplier = Supplier(code=uuid.uuid4().hex[:8], name=supplier_name)
                session.add(supplier)
                await session.flush()
                price_file = PriceFile(
                    project_id=project.id,
                    supplier_id=supplier.id,
                    original_filename=f"{supplier_name}.xlsx",
                    storage_key=f"{supplier_name}.xlsx",
                    sha256=uuid.uuid4().hex,
                    size_bytes=1,
                    mime="application/vnd.ms-excel",
                )
                session.add(price_file)
                await session.flush()
                version = PriceVersion(
                    supplier_id=supplier.id,
                    price_file_id=price_file.id,
                    version_no=1,
                    status="active",
                )
                session.add(version)
                await session.flush()
                suppliers[supplier_name] = version

            # Имя листа берётся как есть: в снимке встречаются листы с
            # ведущими и хвостовыми пробелами («ПРАЙС дил.роз  »), и голдсет
            # ссылается именно на такой идентификатор.
            sheet = row.get("sheet") or ""
            name = (row.get("name") or "").strip()
            fingerprint = (supplier_name, sheet, name)
            if fingerprint in seen:
                # Одна позиция занимает несколько строк — по одной на ценовой
                # уровень. В базу она попадает один раз.
                continue
            seen.add(fingerprint)
            item = SupplierPriceItem(
                price_version_id=version.id,
                source_name=name,
                normalized_name=normalize_name(name),
                source_sheet=sheet,
                source_row=int(row.get("row") or 0),
                unit_code=(row.get("unit") or "").strip() or None,
                category_path=(row.get("category_context") or "").strip() or None,
            )
            session.add(item)
            await session.flush()
            items.setdefault(normalize_name(name), []).append(item.id)

    for version in suppliers.values():
        await EmbeddingIndexService(session).index_version(version.id)
        if semantic is not None:
            # Второй набор векторов той же версии: ключ различает модели, и
            # смысловая ступень ищет по своему индексу.
            await EmbeddingIndexService(session).index_version(
                version.id, embedder=semantic
            )
    await session.flush()
    return items


async def canonical_of(session, item_ids: list[uuid.UUID]) -> set[str]:
    """Канонические товары, в которые вошли эти позиции прайса."""
    if not item_ids:
        return set()
    rows = await session.execute(
        select(CanonicalProductSource.canonical_product_id).where(
            CanonicalProductSource.price_item_id.in_(item_ids)
        )
    )
    return {str(row[0]) for row in rows}


async def run(
    goldset: Path, limit: int, embedder: str = "", semantic: str = ""
) -> dict[str, object]:
    cases = [json.loads(line) for line in (goldset / "goldset_cases.jsonl").open(encoding="utf-8")]
    if limit:
        cases = cases[:limit]
    if embedder:
        # Прогон с конкретным эмбеддером: так сравниваются модели на одном и
        # том же голдсете, а не на разных прогонах с разной конфигурацией.
        set_embedder(build_embedder(embedder))
    semantic_embedder = build_embedder(semantic) if semantic else None

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        started = time.monotonic()
        items = await load_catalog(session, goldset / "catalog_snapshot.csv", semantic_embedder)
        built = await CanonicalService(session).rebuild()
        # Канон пересобран после загрузки — индекс должен смотреть на те же
        # позиции, поэтому эмбеддинги считаются до него и не зависят от канона.
        setup_seconds = time.monotonic() - started

        matcher = MatchService(session, semantic=semantic_embedder)
        results: list[dict[str, object]] = []
        stages = Counter()
        decisions = Counter()

        for case in cases:
            acceptable_ids = [
                item_id
                for target in case.get("acceptable_targets", [])
                for item_id in items.get(normalize_name(target.get("name", "")), [])
            ]
            forbidden_ids = [
                item_id
                for target in case.get("forbidden_targets", [])
                for item_id in items.get(normalize_name(target.get("name", "")), [])
            ]
            acceptable = await canonical_of(session, acceptable_ids)
            forbidden = await canonical_of(session, forbidden_ids)

            scope = case.get("supplier_scope") or "all"
            outcome = await matcher.match(
                case["query_name"], supplier="" if scope == "all" else scope
            )
            decisions[outcome.decision] += 1
            stages[outcome.stage] += 1

            top = [candidate.canonical_id for candidate in outcome.candidates]
            top1 = top[0] if top else ""
            results.append(
                {
                    "case_id": case["case_id"],
                    "query": case["query_name"],
                    "ambiguous": not acceptable,
                    # Разметка различает два разных «нет ответа»: товара нет в
                    # каталоге (ждём not_found) и запрос без модели, по
                    # которому выбрать нельзя (ждём уточнения у менеджера).
                    "expects_clarification": not acceptable
                    and case.get("status") == "verified_manual_ambiguous",
                    "decision": outcome.decision,
                    "stage": outcome.stage,
                    "score": outcome.candidates[0].score if outcome.candidates else 0.0,
                    "top1_title": outcome.candidates[0].title if outcome.candidates else "",
                    "top1_hit": bool(top1) and top1 in acceptable,
                    "top3_hit": bool(acceptable & set(top)),
                    "forbidden_top1": bool(top1) and top1 in forbidden,
                }
            )

    await engine.dispose()

    answerable = [row for row in results if not row["ambiguous"]]
    clarification = [row for row in results if row["expects_clarification"]]
    absent = [
        row for row in results if row["ambiguous"] and not row["expects_clarification"]
    ]
    auto = [row for row in answerable if row["decision"] == "auto"]

    from app.matching.embeddings import NORMALIZER_VERSION
    from app.matching.embedder import active_embedder

    summary = {
        "embedder": f"{active_embedder().name}:{active_embedder().revision}/{NORMALIZER_VERSION}",
        "semantic": semantic_embedder.name if semantic_embedder else "",
        "cases": len(results),
        "catalog_positions": sum(len(value) for value in items.values()),
        "canonicals": built.canonicals,
        "setup_seconds": round(setup_seconds, 1),
        "thresholds": {"auto": AUTO_ACCEPT, "review": NEEDS_REVIEW},
        "answerable": {
            "cases": len(answerable),
            "top1_hit": sum(1 for row in answerable if row["top1_hit"]),
            "top3_hit": sum(1 for row in answerable if row["top3_hit"]),
            "top1_accuracy": round(
                sum(1 for row in answerable if row["top1_hit"]) / len(answerable), 3
            )
            if answerable
            else 0.0,
            "top3_recall": round(
                sum(1 for row in answerable if row["top3_hit"]) / len(answerable), 3
            )
            if answerable
            else 0.0,
            "not_found": sum(1 for row in answerable if row["decision"] == "not_found"),
            "forbidden_top1": sum(1 for row in answerable if row["forbidden_top1"]),
        },
        "auto_decisions": {
            "cases": len(auto),
            "correct": sum(1 for row in auto if row["top1_hit"]),
            "precision": round(sum(1 for row in auto if row["top1_hit"]) / len(auto), 3)
            if auto
            else 0.0,
        },
        "needs_clarification": {
            # Запрос без модели: правильный исход — уточнение, а не «нет такого».
            "cases": len(clarification),
            "clarify_or_review": sum(
                1 for row in clarification if row["decision"] in {"clarify", "review"}
            ),
            "not_found": sum(1 for row in clarification if row["decision"] == "not_found"),
            "auto": sum(1 for row in clarification if row["decision"] == "auto"),
        },
        "absent_from_catalog": {
            # Товара в прайсах нет: правильный исход — «не найдено», и любой
            # автовыбор здесь опаснее пустого ответа.
            "cases": len(absent),
            "not_found": sum(1 for row in absent if row["decision"] == "not_found"),
            "auto": sum(1 for row in absent if row["decision"] == "auto"),
            "review_or_clarify": sum(
                1 for row in absent if row["decision"] in {"review", "clarify"}
            ),
        },
        "decisions": dict(decisions.most_common()),
        "stages": dict(stages.most_common()),
    }
    return {"summary": summary, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goldset", type=Path, default=GOLDSET)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--embedder",
        default="",
        help="trigram | mpnet; пусто — как настроено в конфигурации",
    )
    parser.add_argument(
        "--semantic",
        default="",
        help="дополнительная смысловая ступень: mpnet",
    )
    arguments = parser.parse_args()

    report = asyncio.run(
        run(arguments.goldset, arguments.limit, arguments.embedder, arguments.semantic)
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))

    if arguments.out:
        arguments.out.mkdir(parents=True, exist_ok=True)
        (arguments.out / "summary.json").write_text(
            json.dumps(report["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (arguments.out / "cases.json").write_text(
            json.dumps(report["results"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
