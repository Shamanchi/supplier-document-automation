"""Этап REPORTING: неизменяемый snapshot результата и файлы по нему.

Порядок действий продиктован схемой: `report_snapshots` защищён триггером
`prevent_modify`, поэтому строка вставляется один раз и сразу с итоговым
статусом. Значит, сначала собирается payload и рендерятся файлы, и только
потом появляется snapshot вместе со своими artifacts — переход
`QUEUED → GENERATING → COMPLETED` по строке выразить нельзя. Состояние
генерации до этого момента видно по самому прогону и его этапу.

Снимок собирается из `comparisons`, а не из живых параметров: отчёт обязан
воспроизводиться из одного источника (reports §QA), и `checksum` это
подтверждает.

Сводный отчёт заявки собирается **синхронно**, в отличие от отчёта позиции.
Своего прогона у него быть не может: `pipeline_runs.check_item_id` объявлен
NOT NULL, а заявка — не позиция. Заводить ради этого таблицу состояния незачем:
сборка читает уже готовые сравнения пяти позиций и рендерит один файл, это
миллисекунды. Поэтому запрос возвращает готовый снимок, а не `202`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.modules.catalogs.models import CatalogVersion
from app.modules.checks.models import (
    CheckBatch,
    CheckItem,
    PipelineDefinition,
    PipelineRun,
)
from app.modules.documents.storage import StoragePort
from app.modules.reporting import xlsx
from app.modules.reporting.models import ReportArtifact, ReportSnapshot
from app.modules.rules.models import Comparison, RuleSnapshot
from app.modules.verdicts.models import ItemVerdict

#: Префикс ключей отчётов внутри бакета документов. Своего бакета отчёты не
#: получают: его пришлось бы создавать в MinIO отдельно, а прав на это у
#: приложения нет (бакеты заводит minio-init).
STORAGE_PREFIX = "reports"

#: Форматы, которые умеет собирать этот модуль. PDF требует отдельного
#: рендерера и кириллических шрифтов в образе — он отмечен в docs/todo.
SUPPORTED_FORMATS = ("XLSX",)


class ReportingError(Exception):
    """Отчёт собрать нельзя; прогон завершается отказом с этим кодом."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ReportStats:
    report_snapshot_id: uuid.UUID
    rows: int
    formats: list[str] = field(default_factory=list)
    failed_formats: list[str] = field(default_factory=list)
    total_bytes: int = 0


class ReportingService:
    def __init__(
        self, session: AsyncSession, settings: Settings, storage: StoragePort
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage

    async def build(self, run: PipelineRun, item: CheckItem) -> ReportStats:
        """Собирает snapshot и файлы отчёта позиции."""
        verdict = await self._session.scalar(
            select(ItemVerdict).where(ItemVerdict.check_item_id == item.id)
        )
        if verdict is None or verdict.manager_verdict == "PENDING":
            raise ReportingError(
                "VERDICT_NOT_APPROVED",
                "Отчёт формируется только после подтверждения вердикта",
            )

        comparisons = list(
            (
                await self._session.scalars(
                    select(Comparison)
                    .where(
                        Comparison.check_item_id == item.id,
                        Comparison.stale.is_(False),
                    )
                    .order_by(Comparison.row_kind, Comparison.created_at)
                )
            ).all()
        )
        if not comparisons:
            raise ReportingError(
                "NO_COMPARISONS", "Для позиции нет актуальных результатов сравнения"
            )

        payload = await self._payload(run, item, verdict, comparisons)
        checksum = _checksum(payload)
        snapshot_id = uuid.uuid4()

        artifacts: list[ReportArtifact] = []
        failed: list[str] = []
        for report_format in SUPPORTED_FORMATS:
            try:
                artifacts.append(
                    await self._render(snapshot_id, report_format, payload)
                )
            except Exception:  # noqa: BLE001 — формат падает, отчёт остаётся
                failed.append(report_format)

        if not artifacts:
            raise ReportingError(
                "REPORT_RENDER_FAILED", "Не удалось сформировать файл отчёта"
            )
        status = "COMPLETED" if not failed else "PARTIALLY_COMPLETED"

        snapshot = ReportSnapshot(
            id=snapshot_id,
            check_batch_id=item.batch_id,
            check_item_id=item.id,
            status=status,
            payload=payload,
            checksum=checksum,
            language=payload.get("language", "ru"),
        )
        self._session.add(snapshot)
        await self._session.flush()
        for artifact in artifacts:
            self._session.add(artifact)
        await self._session.flush()

        return ReportStats(
            report_snapshot_id=snapshot_id,
            rows=len(payload["rows"]),
            formats=[artifact.format for artifact in artifacts],
            failed_formats=failed,
            total_bytes=sum(artifact.size_bytes or 0 for artifact in artifacts),
        )

    async def build_batch(self, batch: CheckBatch) -> ReportStats:
        """Собирает сводный snapshot и файл отчёта по заявке."""
        if batch.manager_verdict == "PENDING":
            raise ReportingError(
                "VERDICT_NOT_APPROVED",
                "Сводный отчёт формируется после подтверждения вердикта заявки",
            )

        sections = [await self._batch_item(item) for item in batch.items]
        if not any(section["rows"] for section in sections):
            raise ReportingError(
                "NO_COMPARISONS", "По заявке нет актуальных результатов сравнения"
            )

        payload = self._batch_payload(batch, sections)
        checksum = _checksum(payload)
        snapshot_id = uuid.uuid4()
        artifact = await self._render(
            snapshot_id, "XLSX", payload, renderer=xlsx.render_batch, kind="batch"
        )

        snapshot = ReportSnapshot(
            id=snapshot_id,
            check_batch_id=batch.id,
            check_item_id=None,
            status="COMPLETED",
            payload=payload,
            checksum=checksum,
            language=payload.get("language", "ru"),
        )
        self._session.add(snapshot)
        await self._session.flush()
        self._session.add(artifact)
        await self._session.flush()

        return ReportStats(
            report_snapshot_id=snapshot_id,
            rows=sum(len(section["rows"]) for section in sections),
            formats=[artifact.format],
            total_bytes=artifact.size_bytes or 0,
        )

    async def _batch_item(self, item: CheckItem) -> dict:
        """Раздел сводного отчёта по одной позиции."""
        verdict = await self._session.scalar(
            select(ItemVerdict).where(ItemVerdict.check_item_id == item.id)
        )
        comparisons = list(
            (
                await self._session.scalars(
                    select(Comparison)
                    .where(
                        Comparison.check_item_id == item.id,
                        Comparison.stale.is_(False),
                    )
                    .order_by(Comparison.row_kind, Comparison.created_at)
                )
            ).all()
        )
        run = await self._session.scalar(
            select(PipelineRun)
            .where(PipelineRun.check_item_id == item.id)
            .order_by(PipelineRun.run_no.desc())
            .limit(1)
        )
        catalog = (
            await self._session.get(CatalogVersion, run.catalog_version_id)
            if run and run.catalog_version_id
            else None
        )
        rule_version = None
        rule_snapshot_id = _rule_snapshot_id(comparisons)
        if rule_snapshot_id is not None:
            snapshot = await self._session.get(RuleSnapshot, rule_snapshot_id)
            rule_version = snapshot.rule_version if snapshot else None

        return {
            "id": str(item.id),
            "positionNo": item.position_no,
            "name": item.name,
            "supplier": item.supplier,
            "status": item.status,
            "verdict": {
                "computed": verdict.computed_verdict if verdict else "PENDING",
                "manager": verdict.manager_verdict if verdict else "PENDING",
                "comment": verdict.comment if verdict else None,
                "basis": (verdict.basis or {}) if verdict else {},
            },
            "versions": {"catalog": catalog.code if catalog else None,
                         "rules": rule_version},
            "rows": [_row(comparison) for comparison in comparisons],
        }

    def _batch_payload(self, batch: CheckBatch, sections: list[dict]) -> dict:
        """Данные сводного отчёта: заявка, её вердикт и разделы позиций."""
        return {
            "language": "ru",
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "batch": {
                "id": str(batch.id),
                "title": batch.title,
                "customer": batch.customer,
                "supplier": batch.supplier,
                "itemsTotal": len(sections),
            },
            "verdict": {
                "computed": batch.final_verdict,
                "manager": batch.manager_verdict,
                "comment": batch.verdict_comment,
                "basis": batch.verdict_basis or {},
                "approvedAt": (
                    batch.verdict_approved_at.isoformat().replace("+00:00", "Z")
                    if batch.verdict_approved_at
                    else None
                ),
            },
            "items": sections,
        }

    # --- сборка ---------------------------------------------------------

    async def _payload(
        self,
        run: PipelineRun,
        item: CheckItem,
        verdict: ItemVerdict,
        comparisons: list[Comparison],
    ) -> dict:
        """Данные отчёта: позиция, вердикт, версии и строки сравнения."""
        batch = await self._session.get(CheckBatch, item.batch_id)
        catalog = (
            await self._session.get(CatalogVersion, run.catalog_version_id)
            if run.catalog_version_id
            else None
        )
        definition = await self._session.get(
            PipelineDefinition, run.pipeline_definition_id
        )
        rule_version = None
        rule_snapshot_id = _rule_snapshot_id(comparisons)
        if rule_snapshot_id is not None:
            snapshot = await self._session.get(RuleSnapshot, rule_snapshot_id)
            rule_version = snapshot.rule_version if snapshot else None

        return {
            "language": "ru",
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "item": {
                "id": str(item.id),
                "name": item.name,
                "positionNo": item.position_no,
                "supplier": item.supplier,
                "batchId": str(item.batch_id),
                "batchTitle": batch.title if batch else None,
                "customer": batch.customer if batch else None,
            },
            "verdict": {
                "computed": verdict.computed_verdict,
                "manager": verdict.manager_verdict,
                "comment": verdict.comment,
                "basis": verdict.basis or {},
                "approvedAt": (
                    verdict.approved_at.isoformat().replace("+00:00", "Z")
                    if verdict.approved_at
                    else None
                ),
            },
            "versions": {
                "catalog": catalog.code if catalog else None,
                "rules": rule_version,
                "pipeline": definition.version if definition else None,
                "reportingRunId": str(run.id),
            },
            "rows": [_row(comparison) for comparison in comparisons],
        }

    async def _render(
        self,
        snapshot_id: uuid.UUID,
        report_format: str,
        payload: dict,
        *,
        renderer=xlsx.render,
        kind: str = "comparison",
    ) -> ReportArtifact:
        content = renderer(payload)
        file_name = f"{kind}-{snapshot_id.hex[:12]}.xlsx"
        storage_key = f"{STORAGE_PREFIX}/{snapshot_id}/{file_name}"
        await self._storage.put(
            self._settings.upload_bucket,
            storage_key,
            content,
            content_type=xlsx.MEDIA_TYPE,
        )
        return ReportArtifact(
            id=uuid.uuid4(),
            report_snapshot_id=snapshot_id,
            format=report_format,
            file_name=file_name,
            storage_key=storage_key,
            size_bytes=len(content),
            checksum=hashlib.sha256(content).hexdigest(),
        )


def _row(comparison: Comparison) -> dict:
    """Строка отчёта из сравнения; всё нужное уже лежит в `explanation`."""
    explanation = comparison.explanation or {}
    required = explanation.get("required") or {}
    fact = explanation.get("fact") or {}
    tolerance = explanation.get("tolerance") or {}
    return {
        "comparisonId": str(comparison.id),
        "parameter": explanation.get("parameterName"),
        "rowKind": comparison.row_kind,
        "requirementValue": required.get("value"),
        "requirementUnit": required.get("unit"),
        "factValue": fact.get("value"),
        "factUnit": fact.get("unit"),
        "operator": comparison.operator,
        "expression": explanation.get("expression") or explanation.get("issue"),
        "status": comparison.effective_status,
        "computedStatus": comparison.computed_status,
        "criticality": explanation.get("criticality"),
        "mandatory": explanation.get("mandatory"),
        "tolerance": _tolerance_text(tolerance),
        "overridden": comparison.overridden,
        "comment": "Статус изменён менеджером" if comparison.overridden else None,
    }


def _tolerance_text(tolerance: dict) -> str | None:
    """Допуск словами: значение и откуда взялось (канон §15.3.1)."""
    value = tolerance.get("value")
    if value is None:
        return None
    unit = "%" if tolerance.get("type") == "PERCENT" else ""
    return f"±{value}{unit} ({tolerance.get('source')})"


def _rule_snapshot_id(comparisons: list[Comparison]) -> uuid.UUID | None:
    for comparison in comparisons:
        for step in comparison.lineage_chain or []:
            if step.get("step") == "rule" and step.get("rule_snapshot_id"):
                return uuid.UUID(step["rule_snapshot_id"])
    return None


def _checksum(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
