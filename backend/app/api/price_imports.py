"""Задание импорта прайса: создать, посмотреть статус, забрать результат.

Ровно этот контракт описан как публичный API сервиса. Всё, что раньше жило под
этим же префиксом, вынесено в собственные ресурсы: ручная проверка — в
`review.py`, позиции — в `price_items.py`, версии — в `price_versions.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal, InvalidOperation
import uuid
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.api.suppliers import ALLOWED_EXTENSIONS
from app.api.versions import item_load_options, item_to_dto
from app.auth import AuthUser
from app.currencies import normalise_currency_hint
from app.db import get_session
from app.import_auth import (
    enforce_service_quotas,
    get_import_admin,
    get_import_reader,
    get_import_writer,
    scoped_import,
)
from app.config import settings
from app.import_service import backfill_vat_rate_percent
from app.llm_providers import resolve_rule_model_config
from app.models import (
    PriceFile,
    PriceImport,
    PriceImportEvent,
    PriceImportStatus,
    PriceImportWebhookDelivery,
    PriceParseIssue,
    PriceVersion,
    Project,
    ServiceClient,
    Supplier,
    SupplierPriceItem,
    VersionStatus,
)
from app.project_context import get_current_project
from app.schemas import (
    IssueOut,
    PriceImportAccepted,
    PriceImportIssuesPage,
    PriceImportItemsPage,
    PriceImportListPage,
    PriceImportResultOut,
    PriceImportStatusOut,
    RuleSectionOut,
    RuleSectionRetryIn,
    RuleSectionRetryOut,
)
from app.rule_generation_status_service import (
    RetryImportStateError,
    RetryScopeError,
    RetryUnavailableError,
    get_rule_generation_summary,
    list_rule_sections,
    retry_rule_sections,
)
from app.webhook_service import validate_callback_url


router = APIRouter(prefix="/price-imports", tags=["price-imports"])


def _links(import_id: uuid.UUID) -> tuple[str, str]:
    base = f"/api/v1/price-imports/{import_id}"
    return base, f"{base}/result"


def _validate_idempotency_key(value: str) -> str:
    if not 8 <= len(value) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key должен содержать 8–128 печатных ASCII-символов без пробелов",
        )
    return value


def _normalise_vat_rate_percent(value: str | None) -> Decimal | None:
    if value is None or value.strip() == "":
        return None
    try:
        rate = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("vat_rate_percent должен быть числом") from exc

    if rate not in {Decimal("0"), Decimal("10"), Decimal("22")}:
        raise ValueError("vat_rate_percent должен быть 0, 10 или 22")

    return rate


def _request_fingerprint(file_sha256: str, **parameters: str | None) -> str:
    canonical = json.dumps(
        {"file_sha256": file_sha256, **parameters},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


async def _webhook_summary(session: AsyncSession, import_id: uuid.UUID) -> dict | None:
    delivery = (
        await session.execute(
            select(PriceImportWebhookDelivery).where(
                PriceImportWebhookDelivery.price_import_id == import_id
            )
        )
    ).scalar_one_or_none()
    if delivery is None:
        return None
    return {
        "status": delivery.status,
        "attempt_count": delivery.attempt_count,
        "last_http_status": delivery.last_http_status,
        "last_error": delivery.last_error,
        "delivered_at": delivery.delivered_at,
        "next_attempt_at": delivery.next_attempt_at,
    }


async def _status_dto(
    session: AsyncSession,
    price_import: PriceImport,
    *,
    supplier: Supplier | None = None,
    price_file: PriceFile | None = None,
    version: PriceVersion | None = None,
    include_webhook: bool = True,
) -> PriceImportStatusOut:
    status_url, result_url = _links(price_import.id)
    supplier = supplier or await session.get(Supplier, price_import.supplier_id)
    price_file = price_file or await session.get(PriceFile, price_import.price_file_id)
    version = version or await session.get(PriceVersion, price_import.price_version_id)
    if supplier is None or price_file is None or version is None:
        raise HTTPException(status_code=500, detail="IMPORT_SOURCE_INTEGRITY_ERROR")
    return PriceImportStatusOut(
        id=price_import.id,
        status=price_import.status,
        stage=price_import.stage,
        progress_percent=price_import.progress_percent,
        supplier_id=price_import.supplier_id,
        supplier_external_id=supplier.external_id,
        supplier_name=supplier.name,
        price_version_id=price_import.price_version_id,
        filename=price_file.original_filename,
        item_count=version.item_count,
        warnings_count=version.warnings_count,
        errors_count=version.errors_count,
        error_code=price_import.error_code,
        error_message=price_import.error_message,
        vat_mode=None,  # TODO: aggregate from SupplierPriceValue if needed
        completeness=price_import.completeness_summary,
        telemetry=price_import.telemetry_summary,
        webhook=await _webhook_summary(session, price_import.id) if include_webhook else None,
        rule_generation=(
            await get_rule_generation_summary(
                session,
                price_import_id=price_import.id,
                batch_id=price_import.rule_batch_id,
            )
            if settings.durable_rule_generation_enabled
            else None
        ),
        created_at=price_import.created_at,
        started_at=price_import.started_at,
        finished_at=price_import.finished_at,
        status_url=status_url,
        result_url=result_url,
    )


@router.get("/{import_id}/rule-sections", response_model=list[RuleSectionOut])
async def get_price_import_rule_sections(
    import_id: uuid.UUID,
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> list[RuleSectionOut]:
    if not settings.durable_rule_generation_enabled:
        raise HTTPException(status_code=404, detail="DURABLE_RULE_GENERATION_DISABLED")
    price_import = await scoped_import(session, principal, import_id)
    return await list_rule_sections(
        session, price_import_id=import_id, batch_id=price_import.rule_batch_id
    )


@router.post("/{import_id}/rule-sections/retry", response_model=RuleSectionRetryOut)
async def retry_price_import_rule_sections(
    import_id: uuid.UUID,
    payload: RuleSectionRetryIn,
    admin: AuthUser = Depends(get_import_admin),
    session: AsyncSession = Depends(get_session),
) -> RuleSectionRetryOut:
    price_import = await scoped_import(session, admin, import_id)
    try:
        return await retry_rule_sections(
            session,
            price_import_id=import_id,
            batch_id=price_import.rule_batch_id,
            section_ids=payload.section_ids,
            retryable_only=payload.retryable_only,
            actor=admin.username,
        )
    except RetryUnavailableError as exc:
        raise HTTPException(status_code=404, detail="DURABLE_RULE_GENERATION_DISABLED") from exc
    except RetryScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RetryImportStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _provenance(price_import: PriceImport, price_file: PriceFile) -> dict:
    return {
        "file_id": str(price_file.id),
        "file_sha256": price_file.sha256,
        "loader": price_import.loader,
        "parser_version": price_import.parser_version,
        "parse_rule_id": str(price_import.parse_rule_id) if price_import.parse_rule_id else None,
        "parse_rule_revision": price_import.parse_rule_revision,
        "model_provider": price_import.provider,
        "model": price_import.model,
        "prompt_version": price_import.prompt_version,
    }


@router.post("", response_model=PriceImportAccepted, status_code=202)
async def create_price_import(
    file: Annotated[UploadFile, File()],
    supplier_external_id: Annotated[str, Form(min_length=1, max_length=255)],
    supplier_name: Annotated[str | None, Form(max_length=255)] = None,
    currency_hint: Annotated[str | None, Form()] = None,
    vat_rate_percent: Annotated[str | None, Form()] = None,
    locale: Annotated[str, Form(min_length=2, max_length=32)] = "ru-RU",
    callback_url: Annotated[str | None, Form(max_length=2048)] = None,
    provider: Annotated[str | None, Form(max_length=32)] = None,
    model: Annotated[str | None, Form(max_length=128)] = None,
    policy: Annotated[Literal["strict"], Form()] = "strict",
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    client: ServiceClient = Depends(get_import_writer),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> PriceImportAccepted:
    key = _validate_idempotency_key(idempotency_key)
    external_id = supplier_external_id.strip()
    display_name = (supplier_name or external_id).strip()
    if not external_id or not display_name:
        raise HTTPException(status_code=422, detail="Идентификатор и имя поставщика не пусты")
    filename = file.filename or "price.xlsx"
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=422, detail="Формат файла не поддерживается")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Пустой файл")
    try:
        currency = normalise_currency_hint(currency_hint)
        vat_rate = _normalise_vat_rate_percent(vat_rate_percent)
        selected = resolve_rule_model_config(provider=provider, model=model)
        callback = (
            await asyncio.to_thread(validate_callback_url, callback_url)
            if callback_url
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    locale = locale.strip()
    sha256 = hashlib.sha256(data).hexdigest()
    fingerprint = _request_fingerprint(
        sha256,
        supplier_external_id=external_id,
        supplier_name=display_name,
        currency_hint=currency,
        vat_rate_percent=format(vat_rate, "f") if vat_rate is not None else None,
        locale=locale,
        callback_url=callback,
        provider=selected.provider,
        model=selected.model,
        policy=policy,
    )
    # Serialise create operations inside one client scope.  The unique key is
    # still the final invariant, while this lock makes concurrent identical
    # requests return a replay instead of leaking an IntegrityError after both
    # have created dependent supplier/version rows.
    client = (
        await session.execute(
            select(ServiceClient).where(ServiceClient.id == client.id).with_for_update()
        )
    ).scalar_one()
    existing = (
        await session.execute(
            select(PriceImport).where(
                PriceImport.service_client_id == client.id,
                PriceImport.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        await session.commit()
        status_url, result_url = _links(existing.id)
        return PriceImportAccepted(
            import_id=existing.id,
            status=existing.status,
            status_url=status_url,
            result_url=result_url,
            replayed=True,
        )

    await enforce_service_quotas(client, session)
    supplier = (
        await session.execute(
            select(Supplier).where(
                Supplier.service_client_id == client.id,
                Supplier.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if supplier is None:
        external_hash = hashlib.sha256(external_id.encode()).hexdigest()[:24]
        supplier = Supplier(
            service_client_id=client.id,
            external_id=external_id,
            code=f"svc-{client.id.hex[:12]}-{external_hash}",
            name=display_name,
        )
        session.add(supplier)
        await session.flush()

    storage_key = storage.put_price_file(
        data,
        sha256,
        filename,
        file.content_type or "application/octet-stream",
    )
    price_file = PriceFile(
        project_id=project.id,
        supplier_id=supplier.id,
        original_filename=filename,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=len(data),
        mime=file.content_type or "application/octet-stream",
    )
    session.add(price_file)
    await session.flush()
    max_version = (
        await session.execute(
            select(func.coalesce(func.max(PriceVersion.version_no), 0)).where(
                PriceVersion.supplier_id == supplier.id
            )
        )
    ).scalar_one()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=max_version + 1,
        status=VersionStatus.uploaded.value,
    )
    session.add(version)
    await session.flush()
    price_import = PriceImport(
        service_client_id=client.id,
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        price_version_id=version.id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        locale=locale,
        currency_hint=currency,
        vat_rate_percent=vat_rate,
        provider=selected.provider,
        model=selected.model,
        callback_url=callback,
        policy=policy,
    )
    session.add(price_import)
    await session.flush()
    session.add(
        PriceImportEvent(
            price_import_id=price_import.id,
            event_type="import_created",
            to_status=PriceImportStatus.queued.value,
            payload={"file_sha256": sha256},
        )
    )
    await session.commit()

    from app.tasks import run_price_import

    try:
        run_price_import.delay(str(price_import.id))
    except Exception as exc:  # noqa: BLE001 - a created job must be terminal or queued
        price_import.status = PriceImportStatus.failed.value
        price_import.stage = PriceImportStatus.failed.value
        price_import.progress_percent = 100
        price_import.error_code = "QUEUE_PUBLISH_FAILED"
        price_import.error_message = type(exc).__name__
        session.add(
            PriceImportEvent(
                price_import_id=price_import.id,
                event_type="queue_publish_failed",
                from_status=PriceImportStatus.queued.value,
                to_status=PriceImportStatus.failed.value,
            )
        )
        await session.commit()
    status_url, result_url = _links(price_import.id)
    return PriceImportAccepted(
        import_id=price_import.id,
        status=price_import.status,
        status_url=status_url,
        result_url=result_url,
    )


@router.get("", response_model=PriceImportListPage)
async def list_price_imports(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> PriceImportListPage:
    conditions = [PriceFile.project_id == project.id]
    if isinstance(principal, ServiceClient):
        conditions.append(PriceImport.service_client_id == principal.id)
    if status_filter:
        if status_filter not in {status.value for status in PriceImportStatus}:
            raise HTTPException(status_code=422, detail="Неизвестный статус импорта")
        conditions.append(PriceImport.status == status_filter)
    total = (
        await session.execute(
            select(func.count(PriceImport.id))
            .join(PriceFile, PriceFile.id == PriceImport.price_file_id)
            .where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(PriceImport, Supplier, PriceFile, PriceVersion)
            .join(Supplier, Supplier.id == PriceImport.supplier_id)
            .join(PriceFile, PriceFile.id == PriceImport.price_file_id)
            .join(PriceVersion, PriceVersion.id == PriceImport.price_version_id)
            .where(*conditions)
            .order_by(PriceImport.created_at.desc(), PriceImport.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [
        await _status_dto(
            session,
            price_import,
            supplier=supplier,
            price_file=price_file,
            version=version,
            include_webhook=False,
        )
        for price_import, supplier, price_file, version in rows
    ]
    await session.commit()
    return PriceImportListPage(total=total, items=items)


@router.get("/{import_id}", response_model=PriceImportStatusOut)
async def get_price_import(
    import_id: uuid.UUID,
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> PriceImportStatusOut:
    price_import = await scoped_import(session, principal, import_id)
    result = await _status_dto(session, price_import)
    await session.commit()
    return result


@router.patch("/{import_id}", response_model=PriceImportStatusOut)
async def update_price_import(
    import_id: uuid.UUID,
    vat_rate_percent: Annotated[str | None, Form()] = None,
    principal: ServiceClient | AuthUser = Depends(get_import_writer),
    session: AsyncSession = Depends(get_session),
) -> PriceImportStatusOut:
    """Обновить параметры импорта (например, ставку НДС после парсинга)."""
    price_import = await scoped_import(session, principal, import_id)

    if vat_rate_percent is not None:
        rate = _normalise_vat_rate_percent(vat_rate_percent)
        price_import.vat_rate_percent = rate
        # Backfill the rate into already-persisted SupplierPriceValue rows of
        # this import version in the same transaction as the PriceImport write
        # (P0-3): values are materialised before the operator picks the rate,
        # so a PATCH must propagate it before the workflow resumes.
        await backfill_vat_rate_percent(
            session,
            price_version_id=price_import.price_version_id,
            vat_rate_percent=rate,
        )

    await session.commit()

    # Попытаться продолжить импорт, если он был в review_required из-за отсутствия НДС
    if price_import.status == PriceImportStatus.review_required.value:
        if price_import.error_code == "VAT_RATE_REQUIRED":
            # Синхронно перевести в quality_check, чтобы фронтенд увидел не-терминальный статус
            price_import.status = PriceImportStatus.quality_check.value
            price_import.error_code = None
            price_import.error_message = None
            await session.commit()

            from app.tasks import advance_price_import_task
            advance_price_import_task.delay(str(import_id))

    result = await _status_dto(session, price_import)
    await session.commit()
    return result


@router.get("/{import_id}/items", response_model=PriceImportItemsPage)
async def get_price_import_items(
    import_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> PriceImportItemsPage:
    price_import = await scoped_import(session, principal, import_id)
    if price_import.status not in PriceImportStatus.terminal_values():
        raise HTTPException(status_code=409, detail="IMPORT_NOT_TERMINAL")
    price_file = await session.get(PriceFile, price_import.price_file_id)
    stmt = select(SupplierPriceItem).where(
        SupplierPriceItem.price_version_id == price_import.price_version_id
    )
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (
        await session.execute(
            stmt.order_by(
                SupplierPriceItem.source_sheet,
                SupplierPriceItem.source_row,
                func.coalesce(SupplierPriceItem.source_panel_index, -1),
                SupplierPriceItem.id,
            )
            .limit(limit)
            .offset(offset)
            .options(*item_load_options())
        )
    ).scalars().all()
    provenance = _provenance(price_import, price_file)
    await session.commit()
    return PriceImportItemsPage(
        import_id=price_import.id,
        status=price_import.status,
        total=total,
        items=[item_to_dto(item, provenance=provenance) for item in items],
    )


@router.get("/{import_id}/issues", response_model=PriceImportIssuesPage)
async def get_price_import_issues(
    import_id: uuid.UUID,
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> PriceImportIssuesPage:
    price_import = await scoped_import(session, principal, import_id)
    if price_import.status not in PriceImportStatus.terminal_values():
        raise HTTPException(status_code=409, detail="IMPORT_NOT_TERMINAL")
    conditions = [PriceParseIssue.price_version_id == price_import.price_version_id]
    if severity:
        if severity not in {"error", "warning"}:
            raise HTTPException(status_code=422, detail="Неизвестный severity")
        conditions.append(PriceParseIssue.severity == severity)
    total = (
        await session.execute(select(func.count(PriceParseIssue.id)).where(*conditions))
    ).scalar_one()
    issues = (
        await session.execute(
            select(PriceParseIssue)
            .where(*conditions)
            .order_by(PriceParseIssue.severity, PriceParseIssue.sheet, PriceParseIssue.row_no)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    await session.commit()
    return PriceImportIssuesPage(
        import_id=price_import.id,
        status=price_import.status,
        total=total,
        issues=[IssueOut.model_validate(issue) for issue in issues],
    )


@router.get("/{import_id}/result", response_model=PriceImportResultOut)
async def get_price_import_result(
    import_id: uuid.UUID,
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> PriceImportResultOut:
    price_import = await scoped_import(session, principal, import_id)
    if price_import.status not in PriceImportStatus.terminal_values():
        raise HTTPException(status_code=409, detail="IMPORT_NOT_TERMINAL")
    price_file = await session.get(PriceFile, price_import.price_file_id)
    items = (
        await session.execute(
            select(SupplierPriceItem)
            .where(SupplierPriceItem.price_version_id == price_import.price_version_id)
            .order_by(
                SupplierPriceItem.source_sheet,
                SupplierPriceItem.source_row,
                func.coalesce(SupplierPriceItem.source_panel_index, -1),
                SupplierPriceItem.id,
            )
            .options(*item_load_options())
        )
    ).scalars().all()
    issues = (
        await session.execute(
            select(PriceParseIssue)
            .where(PriceParseIssue.price_version_id == price_import.price_version_id)
            .order_by(PriceParseIssue.severity, PriceParseIssue.sheet, PriceParseIssue.row_no)
        )
    ).scalars().all()
    provenance = _provenance(price_import, price_file)
    result = PriceImportResultOut(
        import_=await _status_dto(session, price_import),
        source={
            "file_id": str(price_file.id),
            "filename": price_file.original_filename,
            "sha256": price_file.sha256,
            "size_bytes": price_file.size_bytes,
        },
        provenance=provenance,
        items=[item_to_dto(item, provenance=provenance) for item in items],
        issues=[IssueOut.model_validate(issue) for issue in issues],
    )
    await session.commit()
    return result
