"""Маршруты модуля `admin/catalogs` и read-only словаря.

Три роутера с разным доступом:
  * `/admin/catalogs` — версионируемое редактирование, роль `admin`
    (contract: «`sysadmin` не получает это право автоматически»);
  * `/admin/synonyms` — решение по предложениям синонимов, роль `admin`;
  * `/dictionary` — проекция опубликованной версии, любой аутентифицированный.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from app.core.concurrency import format_etag, require_idempotency_key, require_if_match
from app.core.idempotency import IdempotencyStore, idempotent, request_hash
from app.modules.catalogs.models import (
    CanonicalParameter,
    CatalogSuggestion,
    CatalogVersion,
    ConversionRule,
    EquipmentType,
    MaterialNode,
    ParameterAlias,
    ParameterTemplate,
    Unit,
)
from app.modules.catalogs.schemas import (
    AliasCreate,
    AliasResponse,
    AliasUpdate,
    ConversionCreate,
    ConversionResponse,
    ConversionTest,
    ConversionTestResult,
    ConversionUpdate,
    DictionaryResponse,
    EquipmentTypeCreate,
    EquipmentTypeResponse,
    EquipmentTypeUpdate,
    MaterialCreate,
    MaterialMove,
    MaterialResponse,
    MaterialUpdate,
    ParameterCreate,
    ParameterResponse,
    ParameterUpdate,
    SuggestionCreate,
    SuggestionDecision,
    SuggestionResponse,
    TemplateCreate,
    TemplateResponse,
    TemplateUpdate,
    UnitCreate,
    UnitResponse,
    UnitUpdate,
    ValidationIssue,
    ValidationReport,
    VersionCreate,
    VersionResponse,
)
from app.modules.catalogs.service import CatalogsService
from app.modules.identity.dependencies import (
    CurrentUser,
    SessionDep,
    require_roles,
)
from app.modules.identity.models import User

#: Каталогом управляет роль admin — так записано в контракте модуля.
CatalogAdmin = Annotated[User, Depends(require_roles("admin"))]

catalogs_router = APIRouter(prefix="/admin/catalogs", tags=["admin/catalogs"])
synonyms_router = APIRouter(prefix="/admin/synonyms", tags=["admin/catalogs"])
dictionary_router = APIRouter(prefix="/dictionary", tags=["admin/catalogs"])

IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]
IfMatch = Annotated[int, Depends(require_if_match)]


def get_catalogs_service(session: SessionDep) -> CatalogsService:
    return CatalogsService(session)


ServiceDep = Annotated[CatalogsService, Depends(get_catalogs_service)]


# --- сборка ответов ---------------------------------------------------------


def _validation_report(version: CatalogVersion) -> ValidationReport | None:
    if not version.validation:
        return None
    stored = version.validation
    return ValidationReport(
        version_id=version.id,
        status=version.status,
        errors=[ValidationIssue(**issue) for issue in stored.get("errors", [])],
        warnings=[ValidationIssue(**issue) for issue in stored.get("warnings", [])],
        regression_summary=stored.get("regression_summary"),
    )


async def _version_body(
    service: CatalogsService, version: CatalogVersion
) -> VersionResponse:
    return VersionResponse(
        id=version.id,
        code=version.code,
        status=version.status,
        is_default=version.is_default,
        parent_id=version.parent_id,
        version=version.version,
        validation=_validation_report(version),
        created_at=version.created_at,
        published_at=version.published_at,
        retired_at=version.retired_at,
        counts=await service.counts(version.id),
    )


def _unit_body(unit: Unit) -> UnitResponse:
    return UnitResponse(
        id=unit.id,
        code=unit.code,
        symbol=unit.symbol,
        dimension=unit.dimension,
        active=unit.active,
    )


def _alias_body(alias: ParameterAlias) -> AliasResponse:
    return AliasResponse(
        id=alias.id,
        alias_text=alias.alias_text,
        normalized_text=alias.normalized_text,
        scope=alias.scope,
        priority=alias.priority,
        active=alias.active,
    )


def _parameter_body(
    parameter: CanonicalParameter,
    unit_codes: dict[UUID, str],
    aliases: dict[UUID, list[ParameterAlias]],
) -> ParameterResponse:
    return ParameterResponse(
        id=parameter.id,
        code=parameter.code,
        display_name=parameter.display_name,
        value_kind=parameter.value_kind,
        dimension=parameter.dimension,
        canonical_unit_code=unit_codes.get(parameter.canonical_unit_id),
        active=parameter.active,
        aliases=[_alias_body(alias) for alias in aliases.get(parameter.id, [])],
    )


async def _unit_codes(service: CatalogsService, version_id: UUID) -> dict[UUID, str]:
    """Карта «идентификатор единицы → код» — одна на запрос, а не на параметр."""
    return {unit.id: unit.code for unit in await service.list_units(version_id)}


def _conversion_body(rule: ConversionRule) -> ConversionResponse:
    return ConversionResponse(
        id=rule.id,
        from_unit_code=rule.from_unit_code,
        to_unit_code=rule.to_unit_code,
        dimension=rule.dimension,
        formula_type=rule.formula_type,
        scale=rule.scale,
        offset=rule.offset_value,
    )


def _material_body(node: MaterialNode) -> MaterialResponse:
    return MaterialResponse(
        id=node.id,
        code=node.code,
        name=node.name,
        path=node.path,
        parent_id=node.parent_id,
        active=node.active,
    )


def _type_body(equipment_type: EquipmentType) -> EquipmentTypeResponse:
    return EquipmentTypeResponse(
        id=equipment_type.id,
        code=equipment_type.code,
        name=equipment_type.name,
        parent_id=equipment_type.parent_id,
        classifier_labels=equipment_type.classifier_labels,
        active=equipment_type.active,
    )


def _template_body(
    template: ParameterTemplate, parameter_code: str
) -> TemplateResponse:
    return TemplateResponse(
        id=template.id,
        equipment_type_id=template.equipment_type_id,
        canonical_parameter_id=template.canonical_parameter_id,
        parameter_code=parameter_code,
        required=template.required,
        operator=template.operator,
        tolerance=template.tolerance,
        priority=template.priority,
    )


def _suggestion_body(
    suggestion: CatalogSuggestion, draft: CatalogVersion | None = None
) -> SuggestionResponse:
    return SuggestionResponse(
        id=suggestion.id,
        canonical=suggestion.canonical_text,
        synonym=suggestion.synonym_text,
        status=suggestion.status.lower(),
        canonical_parameter_id=suggestion.canonical_parameter_id,
        author_id=suggestion.author_id,
        created_at=suggestion.created_at,
        decided_at=suggestion.decided_at,
        draft_version_id=draft.id if draft else None,
        draft_version_code=draft.code if draft else None,
    )


def _with_etag(body: dict, version: int, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=body, headers={"ETag": format_etag(version)}
    )


# --- версии каталога --------------------------------------------------------


@catalogs_router.get("/versions", response_model=list[VersionResponse])
async def list_versions(
    service: ServiceDep, user: CatalogAdmin
) -> list[VersionResponse]:
    """Список версий, свежие первыми."""
    return [
        await _version_body(service, version)
        for version in await service.list_versions()
    ]


@catalogs_router.post(
    "/versions", status_code=status.HTTP_201_CREATED, response_model=VersionResponse
)
async def create_version(
    payload: VersionCreate, service: ServiceDep, user: CatalogAdmin
) -> VersionResponse:
    """Создать draft. По умолчанию содержимое копируется из published-версии."""
    version = await service.create_version(payload, user.id)
    return await _version_body(service, version)


@catalogs_router.get("/versions/{version_id}", response_model=None)
async def get_version(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> JSONResponse:
    """Метаданные версии и результат последней валидации."""
    version = await service.get_version(version_id)
    body = await _version_body(service, version)
    return _with_etag(body.model_dump(mode="json", by_alias=True), version.version)


@catalogs_router.post("/versions/{version_id}/validate", response_model=None)
async def validate_version(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> JSONResponse:
    """Проверить конфликты и регрессии; статус становится VALID или INVALID."""
    await service.validate_version(version_id)
    version = await service.get_version(version_id)
    body = await _version_body(service, version)
    return _with_etag(body.model_dump(mode="json", by_alias=True), version.version)


@catalogs_router.post(
    "/versions/{version_id}/publish",
    status_code=status.HTTP_200_OK,
    response_model=None,
)
async def publish_version(
    version_id: UUID,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CatalogAdmin,
    idempotency_key: IdempotencyKey,
    if_match: IfMatch,
) -> JSONResponse:
    """Атомарно опубликовать версию. Требует ключ идемпотентности и ETag."""
    store = IdempotencyStore(session)
    digest = request_hash("POST", request.url.path, {"version": if_match})

    async def operation() -> dict:
        version = await service.publish_version(version_id, if_match)
        body = await _version_body(service, version)
        return body.model_dump(mode="json", by_alias=True)

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_200_OK, operation
    )


@catalogs_router.post("/versions/{version_id}/retire", response_model=VersionResponse)
async def retire_version(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin, if_match: IfMatch
) -> VersionResponse:
    """Вывести версию из выбора для новых прогонов."""
    version = await service.retire_version(version_id, if_match)
    return await _version_body(service, version)


# --- единицы ----------------------------------------------------------------


@catalogs_router.get("/versions/{version_id}/units", response_model=list[UnitResponse])
async def list_units(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> list[UnitResponse]:
    return [_unit_body(unit) for unit in await service.list_units(version_id)]


@catalogs_router.post(
    "/versions/{version_id}/units",
    status_code=status.HTTP_201_CREATED,
    response_model=UnitResponse,
)
async def create_unit(
    version_id: UUID, payload: UnitCreate, service: ServiceDep, user: CatalogAdmin
) -> UnitResponse:
    return _unit_body(await service.create_unit(version_id, payload))


@catalogs_router.patch(
    "/versions/{version_id}/units/{unit_id}", response_model=UnitResponse
)
async def update_unit(
    version_id: UUID,
    unit_id: UUID,
    payload: UnitUpdate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> UnitResponse:
    return _unit_body(await service.update_unit(version_id, unit_id, payload))


# --- канонические параметры и синонимы --------------------------------------


@catalogs_router.get(
    "/versions/{version_id}/parameters", response_model=list[ParameterResponse]
)
async def list_parameters(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> list[ParameterResponse]:
    unit_codes = await _unit_codes(service, version_id)
    aliases = await service.aliases_by_parameter(version_id)
    return [
        _parameter_body(parameter, unit_codes, aliases)
        for parameter in await service.list_parameters(version_id)
    ]


@catalogs_router.post(
    "/versions/{version_id}/parameters",
    status_code=status.HTTP_201_CREATED,
    response_model=ParameterResponse,
)
async def create_parameter(
    version_id: UUID, payload: ParameterCreate, service: ServiceDep, user: CatalogAdmin
) -> ParameterResponse:
    parameter = await service.create_parameter(version_id, payload)
    return _parameter_body(
        parameter,
        await _unit_codes(service, version_id),
        await service.aliases_by_parameter(version_id),
    )


@catalogs_router.patch(
    "/versions/{version_id}/parameters/{parameter_id}", response_model=ParameterResponse
)
async def update_parameter(
    version_id: UUID,
    parameter_id: UUID,
    payload: ParameterUpdate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> ParameterResponse:
    parameter = await service.update_parameter(version_id, parameter_id, payload)
    return _parameter_body(
        parameter,
        await _unit_codes(service, version_id),
        await service.aliases_by_parameter(version_id),
    )


@catalogs_router.delete(
    "/versions/{version_id}/parameters/{parameter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_parameter(
    version_id: UUID, parameter_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> None:
    await service.delete_parameter(version_id, parameter_id)


@catalogs_router.get(
    "/versions/{version_id}/aliases", response_model=list[AliasResponse]
)
async def list_aliases(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> list[AliasResponse]:
    return [_alias_body(alias) for alias in await service.list_aliases(version_id)]


@catalogs_router.post(
    "/versions/{version_id}/aliases",
    status_code=status.HTTP_201_CREATED,
    response_model=AliasResponse,
)
async def create_alias(
    version_id: UUID, payload: AliasCreate, service: ServiceDep, user: CatalogAdmin
) -> AliasResponse:
    """Синоним параметра. Нормализованную форму вычисляет сервер."""
    return _alias_body(await service.create_alias(version_id, payload))


@catalogs_router.patch(
    "/versions/{version_id}/aliases/{alias_id}", response_model=AliasResponse
)
async def update_alias(
    version_id: UUID,
    alias_id: UUID,
    payload: AliasUpdate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> AliasResponse:
    return _alias_body(await service.update_alias(version_id, alias_id, payload))


@catalogs_router.delete(
    "/versions/{version_id}/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_alias(
    version_id: UUID, alias_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> None:
    await service.delete_alias(version_id, alias_id)


# --- конверсии --------------------------------------------------------------


@catalogs_router.get(
    "/versions/{version_id}/conversions", response_model=list[ConversionResponse]
)
async def list_conversions(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> list[ConversionResponse]:
    return [
        _conversion_body(rule) for rule in await service.list_conversions(version_id)
    ]


@catalogs_router.post(
    "/versions/{version_id}/conversions",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversionResponse,
)
async def create_conversion(
    version_id: UUID, payload: ConversionCreate, service: ServiceDep, user: CatalogAdmin
) -> ConversionResponse:
    """Линейное правило `value × scale + offset`. Размерности обязаны совпадать."""
    return _conversion_body(await service.create_conversion(version_id, payload))


@catalogs_router.patch(
    "/versions/{version_id}/conversions/{conversion_id}",
    response_model=ConversionResponse,
)
async def update_conversion(
    version_id: UUID,
    conversion_id: UUID,
    payload: ConversionUpdate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> ConversionResponse:
    rule = await service.update_conversion(version_id, conversion_id, payload)
    return _conversion_body(rule)


@catalogs_router.post(
    "/versions/{version_id}/conversions/test", response_model=ConversionTestResult
)
async def test_conversion(
    version_id: UUID, payload: ConversionTest, service: ServiceDep, user: CatalogAdmin
) -> ConversionTestResult:
    """Пробный расчёт по существующему правилу. Ничего не сохраняет."""
    result, explanation = await service.test_conversion(
        version_id, payload.from_unit_code, payload.to_unit_code, payload.value
    )
    return ConversionTestResult(
        from_unit_code=payload.from_unit_code,
        to_unit_code=payload.to_unit_code,
        value=payload.value,
        result=result,
        explanation=explanation,
    )


# --- материалы --------------------------------------------------------------


@catalogs_router.get(
    "/versions/{version_id}/materials", response_model=list[MaterialResponse]
)
async def list_materials(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> list[MaterialResponse]:
    return [_material_body(node) for node in await service.list_materials(version_id)]


@catalogs_router.post(
    "/versions/{version_id}/materials",
    status_code=status.HTTP_201_CREATED,
    response_model=MaterialResponse,
)
async def create_material(
    version_id: UUID, payload: MaterialCreate, service: ServiceDep, user: CatalogAdmin
) -> MaterialResponse:
    return _material_body(await service.create_material(version_id, payload))


@catalogs_router.patch(
    "/versions/{version_id}/materials/{node_id}", response_model=MaterialResponse
)
async def update_material(
    version_id: UUID,
    node_id: UUID,
    payload: MaterialUpdate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> MaterialResponse:
    return _material_body(await service.update_material(version_id, node_id, payload))


@catalogs_router.post(
    "/versions/{version_id}/materials/{node_id}/move", response_model=MaterialResponse
)
async def move_material(
    version_id: UUID,
    node_id: UUID,
    payload: MaterialMove,
    service: ServiceDep,
    user: CatalogAdmin,
) -> MaterialResponse:
    """Перенос узла вместе с поддеревом. Циклы отвергаются."""
    node = await service.move_material(version_id, node_id, payload.parent_id)
    return _material_body(node)


@catalogs_router.delete(
    "/versions/{version_id}/materials/{node_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_material(
    version_id: UUID, node_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> None:
    await service.delete_material(version_id, node_id)


# --- типы оборудования и шаблоны --------------------------------------------


@catalogs_router.get(
    "/versions/{version_id}/equipment-types",
    response_model=list[EquipmentTypeResponse],
)
async def list_equipment_types(
    version_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> list[EquipmentTypeResponse]:
    return [
        _type_body(equipment_type)
        for equipment_type in await service.list_equipment_types(version_id)
    ]


@catalogs_router.post(
    "/versions/{version_id}/equipment-types",
    status_code=status.HTTP_201_CREATED,
    response_model=EquipmentTypeResponse,
)
async def create_equipment_type(
    version_id: UUID,
    payload: EquipmentTypeCreate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> EquipmentTypeResponse:
    return _type_body(await service.create_equipment_type(version_id, payload))


@catalogs_router.patch(
    "/versions/{version_id}/equipment-types/{type_id}",
    response_model=EquipmentTypeResponse,
)
async def update_equipment_type(
    version_id: UUID,
    type_id: UUID,
    payload: EquipmentTypeUpdate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> EquipmentTypeResponse:
    equipment_type = await service.update_equipment_type(version_id, type_id, payload)
    return _type_body(equipment_type)


@catalogs_router.get(
    "/versions/{version_id}/equipment-types/{type_id}/templates",
    response_model=list[TemplateResponse],
)
async def list_templates(
    version_id: UUID, type_id: UUID, service: ServiceDep, user: CatalogAdmin
) -> list[TemplateResponse]:
    return [
        _template_body(template, code)
        for template, code in await service.list_templates(version_id, type_id)
    ]


@catalogs_router.post(
    "/versions/{version_id}/equipment-types/{type_id}/templates",
    status_code=status.HTTP_201_CREATED,
    response_model=TemplateResponse,
)
async def create_template(
    version_id: UUID,
    type_id: UUID,
    payload: TemplateCreate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> TemplateResponse:
    """Шаблон ТХ типа. Оператор обязан подходить виду значения параметра."""
    template, code = await service.create_template(version_id, type_id, payload)
    return _template_body(template, code)


@catalogs_router.patch(
    "/versions/{version_id}/templates/{template_id}", response_model=TemplateResponse
)
async def update_template(
    version_id: UUID,
    template_id: UUID,
    payload: TemplateUpdate,
    service: ServiceDep,
    user: CatalogAdmin,
) -> TemplateResponse:
    template, code = await service.update_template(version_id, template_id, payload)
    return _template_body(template, code)


# --- словарь и предложения синонимов ----------------------------------------


@dictionary_router.get("", response_model=DictionaryResponse)
async def get_dictionary(service: ServiceDep, user: CurrentUser) -> DictionaryResponse:
    """Проекция опубликованной версии каталога: чтение для всех ролей."""
    return DictionaryResponse(**await service.dictionary())


@dictionary_router.post(
    "/synonyms", status_code=status.HTTP_201_CREATED, response_model=SuggestionResponse
)
async def suggest_synonym(
    payload: SuggestionCreate, service: ServiceDep, user: CurrentUser
) -> SuggestionResponse:
    """Предложение синонима. Опубликованный каталог не меняется."""
    suggestion = await service.suggest_synonym(
        payload.canonical,
        payload.synonym,
        user.id,
        canonical_parameter_id=payload.canonical_parameter_id,
    )
    return _suggestion_body(suggestion)


@dictionary_router.get("/synonyms", response_model=list[SuggestionResponse])
async def list_my_suggestions(
    service: ServiceDep,
    user: CurrentUser,
    suggestion_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[SuggestionResponse]:
    """Свои предложения синонимов: что предложено, что принято, что отклонено.

    Отдельно от `/admin/synonyms`: тот список видят администраторы и он про
    чужие решения тоже, а этот — про собственные и доступен любому автору.
    """
    return [
        _suggestion_body(suggestion)
        for suggestion in await service.list_suggestions(
            suggestion_status, author_id=user.id
        )
    ]


@synonyms_router.get("", response_model=list[SuggestionResponse])
async def list_suggestions(
    service: ServiceDep,
    user: CatalogAdmin,
    suggestion_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[SuggestionResponse]:
    """Предложения синонимов для экрана «Управление»."""
    return [
        _suggestion_body(suggestion)
        for suggestion in await service.list_suggestions(suggestion_status)
    ]


@synonyms_router.patch("/{suggestion_id}", response_model=SuggestionResponse)
async def decide_suggestion(
    suggestion_id: UUID,
    payload: SuggestionDecision,
    service: ServiceDep,
    user: CatalogAdmin,
) -> SuggestionResponse:
    """Принять или отклонить предложение синонима."""
    suggestion, draft = await service.decide_suggestion(
        suggestion_id, payload.status, user.id
    )
    return _suggestion_body(suggestion, draft)
