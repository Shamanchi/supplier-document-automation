"""Parameter-level HITL operations.

Manual edits are deliberately separate from extraction. The original raw
parameter remains addressable, every correction is recorded in
`parameter_revisions`, and the mutable raw row carries the current value used by
normalization and matching.

Правка не отменяет провенанс, а переоформляет его. Ревизия хранит и новое
значение, и прежнее (`previous_*`), поэтому исходное извлечённое значение
восстановимо по цепочке ревизий. Машинная цитата остаётся в
`extraction_evidence` как история — таблица неизменяема, — но у правленого
значения появляется собственное evidence с локатором `MANUAL`, actor и
временем. Различает их поколение значения (`value_revision`): инвариант
«value_text ⊂ quote» проверяется по актуальному evidence, а не по всем строкам
параметра.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.modules.checks.models import CheckItem, PipelineRun
from app.modules.checks.schemas import (
    ItemParameterResponse,
    ParameterEvidence,
    ParameterIssue,
    ParameterNormalizedValue,
    ParameterRawValue,
)
from app.modules.checks.service import WRITE_ROLES, ChecksService
from app.modules.documents.models import Document
from app.modules.extraction.models import (
    ExtractionEvidence,
    ExtractionRun,
    RawParameter,
    SourceFragment,
    SourceVerification,
)
from app.modules.identity.models import AuditEvent, User
from app.modules.normalization.models import NormalizationIssue, NormalizedParameter
from app.modules.normalization.service import ParameterNormalizer
from app.modules.parameters.models import ParameterRevision
from app.modules.parameters.schemas import (
    ManualParameterCreate,
    ParameterRevisionRequest,
    ParameterStateRequest,
)
from app.modules.rules.models import Comparison
from app.modules.verdicts.batch import refresh_batch_verdict
from app.modules.verdicts.models import ItemVerdict

#: `NEEDS_INPUT` в списке намеренно: это единственный вход в ручной ввод, когда
#: из документа не подтвердилось ни одного значения. Без него позиция после
#: плохого скана оставалась бы тупиком.
PARAMETER_EDITABLE_STATUSES = (
    "NEEDS_INPUT",
    "AWAITING_PARAMETER_REVIEW",
    "READY_FOR_MANAGER_REVIEW",
    "REVIEW_REQUIRED",
)
MANUAL_VERSION = "manual-hitl-v1"


class ParametersService:
    def __init__(self, session: AsyncSession, checks: ChecksService) -> None:
        self._session = session
        self._checks = checks

    async def get(self, parameter_id: uuid.UUID) -> RawParameter:
        raw = await self._session.get(RawParameter, parameter_id)
        if raw is None:
            raise ApiError(
                "PARAMETER_NOT_FOUND",
                "Параметр не найден",
                status_code=404,
                details={"parameterId": str(parameter_id)},
            )
        if raw.check_item_id is not None:
            await self._checks.get_item(raw.check_item_id)
        return raw

    async def add_manual(
        self, item_id: uuid.UUID, payload: ManualParameterCreate, user: User
    ) -> RawParameter:
        self._assert_can_write(user)
        item = await self._editable_item(item_id)
        comment = self._required_text(payload.comment, "COMMENT_REQUIRED")
        run = await self._latest_extraction_run(item.id, payload.role)
        if run is None:
            raise ApiError(
                "EXTRACTION_RUN_NOT_FOUND",
                "Ручной параметр можно добавить после извлечения документов",
                status_code=409,
                details={"checkItemId": str(item.id), "role": payload.role},
            )
        document_version_id = await self._require_document_version(
            run.document_version_id, item.id, payload.role
        )

        raw = RawParameter(
            id=uuid.uuid4(),
            check_item_id=item.id,
            extraction_run_id=run.id,
            role=payload.role,
            name_text=payload.name.strip(),
            value_text=self._optional_text(payload.value_text),
            unit_text=self._optional_text(payload.unit_text),
            extraction_method="MANUAL",
            confidence=Decimal("1.0"),
            quality_flags=[],
            excluded=False,
            version=1,
        )
        self._session.add(raw)
        await self._session.flush()
        await self._manual_evidence(
            raw,
            document_version_id=document_version_id,
            user=user,
        )
        await self._normalize(raw, item)
        await self._reset_matching(
            item,
            user,
            action="parameter.added",
            entity_id=raw.id,
            reason=comment,
        )
        return raw

    async def revise(
        self,
        parameter_id: uuid.UUID,
        payload: ParameterRevisionRequest,
        changed_fields: set[str],
        user: User,
    ) -> RawParameter:
        self._assert_can_write(user)
        raw = await self._locked_parameter(parameter_id)
        item = await self._editable_item(raw.check_item_id)
        self._assert_version(raw.version, payload.expected_version, "Параметр")
        comment = self._required_text(payload.comment, "COMMENT_REQUIRED")
        editable_fields = {"name", "value_text", "unit_text"}
        if not changed_fields & editable_fields:
            raise ApiError(
                "NO_PARAMETER_CHANGES",
                "Передайте хотя бы одно поле: name, valueText или unitText",
                status_code=422,
            )

        # Версия документа нужна до правки: без неё правленому значению нечем
        # подтвердиться, а оставлять его с чужой машинной цитатой нельзя.
        document_version_id = await self._evidence_document_version(raw, item)
        previous = await self._latest_revision(raw.id)
        self._session.add(
            ParameterRevision(
                id=uuid.uuid4(),
                raw_parameter_id=raw.id,
                previous_revision_id=previous,
                name_text=payload.name if "name" in changed_fields else None,
                value_text=(
                    payload.value_text if "value_text" in changed_fields else None
                ),
                unit_text=payload.unit_text if "unit_text" in changed_fields else None,
                previous_name_text=raw.name_text,
                previous_value_text=raw.value_text,
                previous_unit_text=raw.unit_text,
                reason_code=payload.reason_code.strip(),
                comment=comment,
                actor_id=user.id,
            )
        )
        if "name" in changed_fields and payload.name is not None:
            raw.name_text = payload.name.strip()
        if "value_text" in changed_fields:
            raw.value_text = self._optional_text(payload.value_text)
        if "unit_text" in changed_fields:
            raw.unit_text = self._optional_text(payload.unit_text)
        raw.excluded = False
        raw.version += 1
        raw.value_revision += 1
        await self._manual_evidence(
            raw,
            document_version_id=document_version_id,
            user=user,
        )
        await self._normalize(raw, item)
        await self._reset_matching(
            item,
            user,
            action="parameter.revised",
            entity_id=raw.id,
            reason=comment,
        )
        return raw

    async def exclude(
        self, parameter_id: uuid.UUID, payload: ParameterStateRequest, user: User
    ) -> RawParameter:
        return await self._set_excluded(
            parameter_id,
            payload,
            user,
            excluded=True,
            action="parameter.excluded",
        )

    async def restore(
        self, parameter_id: uuid.UUID, payload: ParameterStateRequest, user: User
    ) -> RawParameter:
        return await self._set_excluded(
            parameter_id,
            payload,
            user,
            excluded=False,
            action="parameter.restored",
        )

    async def response(self, raw: RawParameter) -> ItemParameterResponse:
        normalized = await self._session.scalar(
            select(NormalizedParameter).where(
                NormalizedParameter.raw_parameter_id == raw.id
            )
        )
        issues = await self._issues([normalized.id] if normalized else [])
        evidence = await self._evidence({raw.id: raw.value_revision})
        manually_edited = raw.extraction_method == "MANUAL" or await self._has_revision(
            raw.id
        )
        return ItemParameterResponse(
            id=raw.id,
            role=raw.role,
            raw=ParameterRawValue(
                name=raw.name_text,
                value_text=raw.value_text,
                unit_text=raw.unit_text,
            ),
            normalized=(
                ParameterNormalizedValue(
                    id=normalized.id,
                    canonical_parameter_id=normalized.canonical_parameter_id,
                    canonical_name=normalized.canonical_name,
                    value_kind=normalized.value_kind,
                    typed_value=normalized.typed_value,
                    normalized_ok=normalized.normalized_ok,
                    version=normalized.version,
                )
                if normalized
                else None
            ),
            extraction_method=raw.extraction_method,
            confidence=None if raw.confidence is None else str(raw.confidence),
            issues=issues.get(normalized.id if normalized else None, []),
            evidence=evidence.get(raw.id, []),
            manually_edited=manually_edited,
            excluded=raw.excluded,
            version=raw.version,
            created_at=raw.created_at,
        )

    async def _set_excluded(
        self,
        parameter_id: uuid.UUID,
        payload: ParameterStateRequest,
        user: User,
        *,
        excluded: bool,
        action: str,
    ) -> RawParameter:
        self._assert_can_write(user)
        raw = await self._locked_parameter(parameter_id)
        item = await self._editable_item(raw.check_item_id)
        self._assert_version(raw.version, payload.expected_version, "Параметр")
        comment = self._required_text(payload.comment, "COMMENT_REQUIRED")
        if raw.excluded == excluded:
            raise ApiError(
                "PARAMETER_STATE_UNCHANGED",
                "Параметр уже находится в запрошенном состоянии",
                status_code=422,
                details={"parameterId": str(raw.id)},
            )
        raw.excluded = excluded
        raw.version += 1
        if not excluded:
            await self._normalize(raw, item)
        await self._reset_matching(
            item,
            user,
            action=action,
            entity_id=raw.id,
            reason=comment,
        )
        return raw

    async def _editable_item(self, item_id: uuid.UUID | None) -> CheckItem:
        if item_id is None:
            raise ApiError(
                "PARAMETER_SCOPE_UNSUPPORTED",
                "Ручная правка поддержана только для параметров позиции проверки",
                status_code=422,
            )
        item = await self._checks.get_item(item_id)
        if await self._checks.active_run(item.id) is not None:
            raise ApiError(
                "RUN_ALREADY_ACTIVE",
                "Нельзя менять параметры, пока у позиции есть активный прогон",
                status_code=409,
            )
        if item.status not in PARAMETER_EDITABLE_STATUSES:
            raise ApiError(
                "PARAMETER_LOCKED",
                "Параметры можно менять до утверждения вердикта,"
                f" статус позиции: {item.status}",
                status_code=409,
                details={"status": item.status},
            )
        return item

    async def _locked_parameter(self, parameter_id: uuid.UUID) -> RawParameter:
        raw = await self._session.scalar(
            select(RawParameter)
            .where(RawParameter.id == parameter_id)
            .with_for_update()
        )
        if raw is None:
            raise ApiError(
                "PARAMETER_NOT_FOUND",
                "Параметр не найден",
                status_code=404,
                details={"parameterId": str(parameter_id)},
            )
        return raw

    async def _latest_revision(self, raw_parameter_id: uuid.UUID) -> uuid.UUID | None:
        return await self._session.scalar(
            select(ParameterRevision.id)
            .where(ParameterRevision.raw_parameter_id == raw_parameter_id)
            .order_by(ParameterRevision.created_at.desc(), ParameterRevision.id.desc())
            .limit(1)
        )

    async def _latest_extraction_run(
        self, item_id: uuid.UUID, role: str
    ) -> ExtractionRun | None:
        return await self._session.scalar(
            select(ExtractionRun)
            .where(
                ExtractionRun.check_item_id == item_id,
                ExtractionRun.status == "COMPLETED",
                ExtractionRun.idempotency_key.like(f"%:{role}:%"),
            )
            .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
            .limit(1)
        )

    async def _evidence_document_version(
        self, raw: RawParameter, item: CheckItem
    ) -> uuid.UUID:
        """Версия документа, к которой привязывается evidence правленого значения."""
        run_version = None
        if raw.extraction_run_id is not None:
            run_version = await self._session.scalar(
                select(ExtractionRun.document_version_id).where(
                    ExtractionRun.id == raw.extraction_run_id
                )
            )
        return await self._require_document_version(run_version, item.id, raw.role)

    async def _require_document_version(
        self, candidate: uuid.UUID | None, item_id: uuid.UUID, role: str
    ) -> uuid.UUID:
        document_version_id = candidate or await self._document_version(item_id, role)
        if document_version_id is None:
            raise ApiError(
                "DOCUMENT_VERSION_NOT_FOUND",
                "Для ручного параметра не найдена версия документа",
                status_code=409,
                details={"checkItemId": str(item_id), "role": role},
            )
        return document_version_id

    async def _document_version(
        self, item_id: uuid.UUID, role: str
    ) -> uuid.UUID | None:
        return await self._session.scalar(
            select(Document.active_version_id)
            .where(
                Document.check_item_id == item_id,
                Document.role == role,
                Document.active_version_id.is_not(None),
            )
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(1)
        )

    async def _manual_evidence(
        self,
        raw: RawParameter,
        *,
        document_version_id: uuid.UUID,
        user: User,
    ) -> None:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        quote = self._quote(raw)
        value_start = quote.find(raw.value_text or "")
        if raw.value_text is None or value_start < 0:
            value_start = None
            value_end = None
        else:
            value_end = value_start + len(raw.value_text)
        fragment = SourceFragment(
            id=uuid.uuid4(),
            document_version_id=document_version_id,
            page_id=None,
            method="TEXT",
            fragment_raw_text=quote,
            normalized_text=quote.casefold(),
            text_hash=hashlib.sha256(quote.casefold().encode("utf-8")).hexdigest(),
            locator_kind="MANUAL",
            locator={
                "kind": "MANUAL",
                "actorId": str(user.id),
                "createdAt": now,
                "role": raw.role,
            },
            parser_version=MANUAL_VERSION,
        )
        self._session.add(fragment)
        await self._session.flush()
        evidence = ExtractionEvidence(
            id=uuid.uuid4(),
            raw_parameter_id=raw.id,
            source_fragment_id=fragment.id,
            quote=quote,
            quote_start=0,
            quote_end=len(quote),
            value_start=value_start,
            value_end=value_end,
            verified=True,
            raw_parameter_value_revision=raw.value_revision,
        )
        self._session.add(evidence)
        await self._session.flush()
        self._session.add(
            SourceVerification(
                extraction_evidence_id=evidence.id,
                passed=True,
                method="MANUAL",
                rejection_reason=None,
                verifier_version=MANUAL_VERSION,
            )
        )

    async def _normalize(self, raw: RawParameter, item: CheckItem) -> None:
        await ParameterNormalizer(self._session).normalize(
            [raw],
            catalog_version_id=await self._catalog_version(raw, item),
        )

    async def _catalog_version(
        self, raw: RawParameter, item: CheckItem
    ) -> uuid.UUID | None:
        current = await self._session.scalar(
            select(NormalizedParameter.catalog_version_id).where(
                NormalizedParameter.raw_parameter_id == raw.id
            )
        )
        if current is not None:
            return current
        return await self._session.scalar(
            select(PipelineRun.catalog_version_id)
            .where(
                PipelineRun.check_item_id == item.id,
                PipelineRun.catalog_version_id.is_not(None),
            )
            .order_by(PipelineRun.run_no.desc(), PipelineRun.created_at.desc())
            .limit(1)
        )

    async def _reset_matching(
        self,
        item: CheckItem,
        user: User,
        *,
        action: str,
        entity_id: uuid.UUID,
        reason: str,
    ) -> None:
        await self._session.execute(
            update(Comparison)
            .where(
                Comparison.check_item_id == item.id,
                Comparison.stale.is_(False),
            )
            .values(stale=True, version=Comparison.version + 1)
        )
        verdict = await self._session.scalar(
            select(ItemVerdict).where(ItemVerdict.check_item_id == item.id)
        )
        if verdict is not None:
            verdict.computed_verdict = "PENDING"
            verdict.manager_verdict = "PENDING"
            verdict.basis = None
            verdict.comment = None
            verdict.approved_by = None
            verdict.approved_at = None
            verdict.version += 1
        if item.status != "AWAITING_PARAMETER_REVIEW":
            item.status = "AWAITING_PARAMETER_REVIEW"
            item.version += 1
        item.updated_at = datetime.now(UTC)
        self._session.add(
            AuditEvent(
                actor_id=user.id,
                action=action,
                entity_type="parameter",
                entity_id=entity_id,
                reason=reason,
            )
        )
        await self._session.flush()
        await refresh_batch_verdict(self._session, item.batch_id)

    async def _issues(
        self, normalized_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID | None, list[ParameterIssue]]:
        ids = list(normalized_ids)
        if not ids:
            return {}
        rows = (
            await self._session.scalars(
                select(NormalizationIssue)
                .where(NormalizationIssue.normalized_parameter_id.in_(ids))
                .order_by(NormalizationIssue.created_at, NormalizationIssue.id)
            )
        ).all()
        result: dict[uuid.UUID | None, list[ParameterIssue]] = {}
        for issue in rows:
            result.setdefault(issue.normalized_parameter_id, []).append(
                ParameterIssue(
                    issue_type=issue.issue_type,
                    description=issue.description,
                    severity=issue.severity,
                )
            )
        return result

    async def _evidence(
        self, current_revisions: dict[uuid.UUID, int]
    ) -> dict[uuid.UUID, list[ParameterEvidence]]:
        """Evidence параметров с пометкой, какие относятся к текущему значению."""
        ids = list(current_revisions)
        if not ids:
            return {}
        rows = (
            await self._session.execute(
                select(ExtractionEvidence, SourceFragment)
                .join(
                    SourceFragment,
                    SourceFragment.id == ExtractionEvidence.source_fragment_id,
                )
                .where(ExtractionEvidence.raw_parameter_id.in_(ids))
                .order_by(ExtractionEvidence.created_at, ExtractionEvidence.id)
            )
        ).all()
        result: dict[uuid.UUID, list[ParameterEvidence]] = {}
        for evidence, fragment in rows:
            locator = dict(fragment.locator or {})
            locator.setdefault("kind", fragment.locator_kind)
            result.setdefault(evidence.raw_parameter_id, []).append(
                ParameterEvidence(
                    fragment_id=fragment.id,
                    quote=evidence.quote,
                    quote_start=evidence.quote_start,
                    quote_end=evidence.quote_end,
                    value_start=evidence.value_start,
                    value_end=evidence.value_end,
                    verified=evidence.verified,
                    locator=locator,
                    current=(
                        evidence.raw_parameter_value_revision
                        == current_revisions[evidence.raw_parameter_id]
                    ),
                )
            )
        return result

    async def _has_revision(self, raw_parameter_id: uuid.UUID) -> bool:
        revision_id = await self._session.scalar(
            select(ParameterRevision.id)
            .where(ParameterRevision.raw_parameter_id == raw_parameter_id)
            .limit(1)
        )
        return revision_id is not None

    @staticmethod
    def _assert_can_write(user: User) -> None:
        if not WRITE_ROLES & set(user.role_names):
            raise ApiError(
                "PARAMETER_EDIT_DENIED",
                "Недостаточно прав для ручной правки параметров",
                status_code=403,
            )

    @staticmethod
    def _assert_version(actual: int, expected: int, entity: str) -> None:
        if actual != expected:
            raise ApiError(
                "EDIT_CONFLICT",
                f"{entity} изменён другим пользователем",
                status_code=409,
                details={"currentVersion": actual, "expectedVersion": expected},
            )

    @staticmethod
    def _required_text(value: str | None, code: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ApiError(code, "Комментарий обязателен", status_code=422)
        return text

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @staticmethod
    def _quote(raw: RawParameter) -> str:
        value = " ".join(part for part in (raw.value_text, raw.unit_text) if part)
        return f"{raw.name_text}: {value}".strip()
