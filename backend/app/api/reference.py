"""Read-only справочники: валюты, словарь характеристик, профили LLM.

Три ручки собраны в один префикс, потому что у них общая природа: данные не
принадлежат ни импорту, ни версии прайса, только читаются и нужны интерфейсу
для выпадающих списков. Раньше они лежали в трёх разных местах, а профили LLM
существовали в двух одинаковых копиях — под `/rule-proposal-options` для
редактора правил и под `/price-imports/options` для мастера импорта.

Аутентификацию держит `ApiAuthMiddleware`: префикс не входит ни в публичные
пути, ни в сервисный, поэтому нужен браузерный JWT, а запись здесь невозможна
в принципе.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.llm_providers import provider_options
from app.models import AttributeDefinition, Currency
from app.schemas import AttributeDefinitionOut, CurrencyOut, RuleModelOptionsOut

router = APIRouter(prefix="/reference", tags=["reference"])


@router.get("/currencies", response_model=list[CurrencyOut])
async def list_currencies(
    session: AsyncSession = Depends(get_session),
) -> list[CurrencyOut]:
    """List the ISO 4217 currency справочник, default currency first."""

    rows = (
        await session.execute(
            select(Currency).order_by(Currency.is_default.desc(), Currency.code)
        )
    ).scalars().all()
    return [
        CurrencyOut(
            id=row.id,
            code=row.code,
            name=row.name,
            symbol=row.symbol,
            is_default=row.is_default,
        )
        for row in rows
    ]


@router.get("/attribute-definitions", response_model=list[AttributeDefinitionOut])
async def list_attribute_definitions(
    query: str | None = Query(default=None, max_length=160),
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> list[AttributeDefinitionOut]:
    """Словарь характеристик наполняется разбором; правила ссылаются на `code`."""

    stmt = select(AttributeDefinition)
    if query and query.strip():
        pattern = f"%{query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                AttributeDefinition.code.ilike(pattern),
                AttributeDefinition.display_name.ilike(pattern),
            )
        )
    definitions = (
        await session.execute(
            stmt.order_by(AttributeDefinition.display_name, AttributeDefinition.code).limit(limit)
        )
    ).scalars().all()
    return [AttributeDefinitionOut.model_validate(definition) for definition in definitions]


@router.get("/llm-profiles", response_model=RuleModelOptionsOut)
async def list_llm_profiles() -> RuleModelOptionsOut:
    """Отдать выбираемые профили моделей без ключей и адресов провайдеров."""

    return RuleModelOptionsOut(
        providers=provider_options(),
        defaults={
            "provider": settings.llm_provider,
            "temperature": 0,
            "top_p": 1,
            "max_output_tokens": settings.openai_rule_max_output_tokens,
            "timeout_seconds": settings.openai_rule_timeout_seconds,
            "thinking": "disabled",
            "reasoning_effort": "high",
        },
    )
