"""Словарь подписей колонок из БД поверх встроенного словаря ролей.

Зачем: подпись, разобранную один раз (человеком или моделью с подтверждением),
не нужно разбирать снова на следующем прайсе. Встроенный `_ALIASES` в
`app.parsing.headers` — база, БД — накопленное поверх неё.

Приоритет от частного к общему: запись под конкретный файл перекрывает запись
поставщика, та перекрывает глобальную, а глобальная — встроенный словарь. Так
у одного поставщика «расч.» может значить оптовую цену, у другого — расчётную
величину, и общий словарь от этого не ломается.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ColumnAlias, ColumnAliasScope
from app.parsing.headers import ColumnSuggestion, suggest_role, use_alias_overlay

_WS = re.compile(r"\s+")

# Порядок разрешения: чем частнее область, тем выше приоритет.
_SCOPE_PRIORITY = {
    ColumnAliasScope.price_file.value: 3,
    ColumnAliasScope.supplier.value: 2,
    ColumnAliasScope.global_.value: 1,
}


def normalize_alias(caption: object) -> str:
    """Ключ словаря: нижний регистр, схлопнутые пробелы.

    Двухъярусная подпись приходит склейкой «Цена для дилеров / С учетом НДС 22%»
    и нормализуется целиком: у яруса по отдельности другой смысл.
    """
    return _WS.sub(" ", str(caption or "")).strip().casefold()


@dataclass(frozen=True)
class AliasResolution:
    role: str
    price_kind: str | None
    vat_mode: str | None
    scope: str
    source: str


class AliasDictionary:
    """Снимок словаря под один разбор: одна выборка из БД, дальше только память."""

    def __init__(self, entries: list[ColumnAlias]) -> None:
        self._by_alias: dict[str, list[ColumnAlias]] = {}
        for entry in entries:
            self._by_alias.setdefault(entry.alias, []).append(entry)

    @classmethod
    async def load(
        cls,
        session: AsyncSession,
        *,
        supplier_id: uuid.UUID | None = None,
        price_file_id: uuid.UUID | None = None,
    ) -> "AliasDictionary":
        """Только записи, применимые к этому разбору: глобальные + свои."""
        conditions = [ColumnAlias.scope == ColumnAliasScope.global_.value]
        if supplier_id is not None:
            conditions.append(
                (ColumnAlias.scope == ColumnAliasScope.supplier.value)
                & (ColumnAlias.supplier_id == supplier_id)
            )
        if price_file_id is not None:
            conditions.append(
                (ColumnAlias.scope == ColumnAliasScope.price_file.value)
                & (ColumnAlias.price_file_id == price_file_id)
            )
        clause = conditions[0]
        for extra in conditions[1:]:
            clause = clause | extra
        rows = (await session.execute(select(ColumnAlias).where(clause))).scalars().all()
        return cls(list(rows))

    def lookup(self, caption: object) -> AliasResolution | None:
        entries = self._by_alias.get(normalize_alias(caption))
        if not entries:
            return None
        best = max(entries, key=lambda e: _SCOPE_PRIORITY.get(e.scope, 0))
        return AliasResolution(
            role=best.role,
            price_kind=best.price_kind,
            vat_mode=best.vat_mode,
            scope=best.scope,
            source=best.source,
        )

    def resolve(self, caption: object) -> ColumnSuggestion | None:
        """Шов для `headers.suggest_role`: None = записи нет, решай сам.

        Возвращать сюда `suggest_role` нельзя — оверлей вызывается изнутри него.
        """
        resolved = self.lookup(caption)
        if resolved is None:
            return None
        suggestion = ColumnSuggestion(
            index=-1, header="" if caption is None else str(caption)
        )
        suggestion.role = resolved.role
        # Запись словаря — это подтверждённое решение, а не догадка по подстроке.
        suggestion.confidence = 0.95
        if resolved.role == "price":
            suggestion.price_descriptor = {
                "kind": resolved.price_kind or "other",
                "vat_mode": resolved.vat_mode or "unknown",
            }
        return suggestion

    def suggest(self, caption: object) -> ColumnSuggestion:
        """Роль колонки с учётом словаря — для прямых вызовов вне разбора."""
        return self.resolve(caption) or suggest_role(caption)

    def active(self):
        """Словарь действует на весь разбор: `with dictionary.active(): ...`."""
        return use_alias_overlay(self)


async def remember(
    session: AsyncSession,
    caption: object,
    *,
    role: str,
    price_kind: str | None = None,
    vat_mode: str | None = None,
    scope: str = ColumnAliasScope.global_.value,
    supplier_id: uuid.UUID | None = None,
    price_file_id: uuid.UUID | None = None,
    source: str = "human",
    note: str | None = None,
) -> ColumnAlias:
    """Записать разобранную подпись. Повтор той же области обновляет запись."""
    alias = normalize_alias(caption)
    if not alias:
        raise ValueError("пустая подпись не может стать записью словаря")
    if scope == ColumnAliasScope.supplier.value and supplier_id is None:
        raise ValueError("scope=supplier требует supplier_id")
    if scope == ColumnAliasScope.price_file.value and price_file_id is None:
        raise ValueError("scope=price_file требует price_file_id")

    existing = (
        await session.execute(
            select(ColumnAlias).where(
                ColumnAlias.alias == alias,
                ColumnAlias.scope == scope,
                ColumnAlias.supplier_id.is_(supplier_id)
                if supplier_id is None
                else ColumnAlias.supplier_id == supplier_id,
                ColumnAlias.price_file_id.is_(price_file_id)
                if price_file_id is None
                else ColumnAlias.price_file_id == price_file_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.role = role
        existing.price_kind = price_kind
        existing.vat_mode = vat_mode
        existing.source = source
        existing.note = note
        return existing

    entry = ColumnAlias(
        alias=alias,
        role=role,
        price_kind=price_kind,
        vat_mode=vat_mode,
        scope=scope,
        supplier_id=supplier_id,
        price_file_id=price_file_id,
        source=source,
        note=note,
    )
    session.add(entry)
    await session.flush()
    return entry
