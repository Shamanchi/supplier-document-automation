# -*- coding: utf-8 -*-
"""Автопривязка product_documents к canonical_products по обозначениям изделий.

Менеджер загружает паспорт/сертификат (PDF), сервис распознаёт наименование и
сразу создаёт подтверждённые ``document_links`` к каноническим товарам, чьи
обозначения совпадают с обозначениями из документа.  Это тот же механизм,
которым паспорта привязывались скриптами
(``scripts/attach_passports_to_canonicals.py``,
``scripts/import_istocnik_documents.py``), только теперь он вызывается и из API
загрузки документов (``POST /api/v1/documents``).

Связь считается подтверждённой автоматически: паспорт покрывает размерный ряд
изделия, а не одну строку прайса, поэтому один документ привязывается сразу ко
всем подходящим канонам.  ``evidence`` фиксирует, по какому наименованию и на
каком уровне (точное обозначение / семейство+число) сработало сопоставление.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.product_codes import match_canonical_titles
from app.models import (
    CanonicalProduct,
    CanonicalProductSource,
    DocumentLink,
    ProductDocument,
)


@dataclass(frozen=True, slots=True)
class MatchedCanonical:
    """Один подходящий канон: id, название и уровень сопоставления."""

    canonical_id: uuid.UUID
    title: str
    stage: str


@dataclass(slots=True)
class BindResult:
    """Итог привязки документа: кандидаты и сколько связей создано."""

    document_id: uuid.UUID | None = None
    matched: list[MatchedCanonical] = field(default_factory=list)
    created: int = 0
    existing: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": str(self.document_id) if self.document_id else None,
            "created_links": self.created,
            "existing_links": self.existing,
            "candidates": [
                {
                    "canonical_id": str(item.canonical_id),
                    "title": item.title,
                    "stage": item.stage,
                }
                for item in self.matched
            ],
        }


def _canonical_matched_by(stage: str) -> str:
    return "passport_designations" if stage == "designation" else "passport_codes"


async def find_canonical_matches(
    session: AsyncSession,
    *,
    product_name: str,
    supplier_id: uuid.UUID | None = None,
) -> list[MatchedCanonical]:
    """Каноны (id, название), подходящие документу по обозначениям.

    В выборку попадают только каноны, у которых есть хотя бы одна строка прайса
    (canonical_product_sources): паспортные записи-каноны из реестра сюда не
    попадают.  При ``supplier_id`` кандидаты дополнительно ограничиваются
    канонами этого поставщика.
    """
    statement = (
        select(CanonicalProduct)
        .join(CanonicalProductSource)
        .distinct()
    )
    if supplier_id is not None:
        statement = statement.where(
            CanonicalProductSource.supplier_id == supplier_id
        )
    canonicals = (await session.execute(statement)).scalars().all()
    by_title = {canonical.title or "": canonical for canonical in canonicals}
    matched: list[MatchedCanonical] = []
    for title, stage in match_canonical_titles(product_name, by_title):
        canonical = by_title.get(title)
        if canonical is not None:
            matched.append(
                MatchedCanonical(
                    canonical_id=canonical.id,
                    title=canonical.title or "",
                    stage=stage,
                )
            )
    return matched


async def auto_bind_document(
    session: AsyncSession,
    *,
    document: ProductDocument,
    product_name: str,
    supplier_id: uuid.UUID | None = None,
    confirmed_by: str = "auto_binding",
) -> BindResult:
    """Создать недостающие document_links для документа.

    Повторный вызов идемпотентен: уже существующие пары (document, canonical)
    пропускаются.  Коммит выполняет вызывающий код.
    """
    result = BindResult(document_id=document.id)
    if not product_name:
        return result

    candidates = await find_canonical_matches(
        session, product_name=product_name, supplier_id=supplier_id
    )
    if not candidates:
        return result

    existing_ids = set(
        (
            await session.execute(
                select(DocumentLink.canonical_product_id).where(
                    DocumentLink.document_id == document.id
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    for candidate in candidates:
        result.matched.append(candidate)
        if candidate.canonical_id in existing_ids:
            result.existing += 1
            continue
        session.add(
            DocumentLink(
                document_id=document.id,
                canonical_product_id=candidate.canonical_id,
                matched_by=_canonical_matched_by(candidate.stage),
                confidence=1.0,
                evidence={
                    "source": "document_upload",
                    "product_name": product_name,
                    "matched_stage": candidate.stage,
                    "canonical_title": candidate.title,
                },
                confirmed_by=confirmed_by,
                confirmed_at=now,
            )
        )
        result.created += 1
    return result
