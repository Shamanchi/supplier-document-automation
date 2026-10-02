"""Сверка извлечённого документа с реестром в базе.

Логика повторяет чистую версию из `app/fsa/`, но ходит в Postgres, а не в
индекс в памяти. Полярность прежняя: при сомнении не привязывать — ложно
приложенный к КП чужой или мёртвый сертификат дороже пустой ячейки с
пометкой «проверить вручную».
"""

from __future__ import annotations

from datetime import date

from app.fsa.schema import (
    ExtractedDocument,
    Field,
    MatchedBy,
    Reconciliation,
    RegistryCertificate,
    Verdict,
)
from app.repository import RegistryRepository

MIN_NUMBER_CONFIDENCE = 0.6


async def reconcile(
    document: ExtractedDocument,
    repository: RegistryRepository,
    *,
    on: date,
    min_number_confidence: float = MIN_NUMBER_CONFIDENCE,
) -> Reconciliation:
    reasons: list[str] = []
    number_field = document.registration_number

    if not number_field.is_present and document.certificate_numbers.is_present:
        # У паспорта нет собственного номера в реестре, но в 44% случаев он
        # печатает номер сертификата на изделие. Текстовый слой паспорта
        # читается без OCR, поэтому это ребро достаётся бесплатно.
        referenced = list(document.certificate_numbers.value or ())
        number_field = Field(
            referenced[0], confidence=document.certificate_numbers.confidence
        )
        reasons.append("номер взят из ссылки в паспорте, а не с бланка сертификата")
        if len(referenced) > 1:
            reasons.append(
                f"в документе указано несколько сертификатов ({len(referenced)}), "
                "проверен первый"
            )

    if not number_field.is_present:
        reasons.append("номер сертификата не извлечён")
        return _unresolved(document, on, reasons)

    if number_field.confidence < min_number_confidence:
        reasons.append(
            f"низкая уверенность распознавания номера ({number_field.confidence:.2f})"
        )
        return _unresolved(document, on, reasons)

    number = str(number_field.value)
    certificate = await repository.get(number)
    if certificate is None:
        if await repository.is_known(number):
            reasons.append(
                "номер есть в реестре, но подробной карточки нет "
                "(документ вне пожарного сегмента либо срез не загружен)"
            )
            verdict = Verdict.UNKNOWN
        else:
            reasons.append("номер не найден в реестре")
            verdict = Verdict.NOT_FOUND
        return Reconciliation(
            verdict=verdict,
            matched_by=MatchedBy.NONE,
            checked_on=on,
            document=document,
            needs_manual_review=True,
            review_reasons=tuple(reasons),
        )

    verdict = _verdict_for(certificate, on, reasons)
    successors = (
        tuple(await repository.find_successors(certificate, on))
        if verdict is not Verdict.VALID
        else ()
    )
    if successors:
        reasons.append("предложена вероятная замена — требует подтверждения менеджером")

    _warn_on_disagreement(document, certificate, reasons)

    return Reconciliation(
        verdict=verdict,
        matched_by=MatchedBy.REGISTRATION_NUMBER,
        checked_on=on,
        document=document,
        certificate=certificate,
        successors=successors,
        needs_manual_review=verdict is not Verdict.VALID or bool(reasons),
        review_reasons=tuple(reasons),
    )


def _unresolved(
    document: ExtractedDocument, on: date, reasons: list[str]
) -> Reconciliation:
    return Reconciliation(
        verdict=Verdict.UNKNOWN,
        matched_by=MatchedBy.NONE,
        checked_on=on,
        document=document,
        needs_manual_review=True,
        review_reasons=tuple(reasons),
    )


def _verdict_for(
    certificate: RegistryCertificate, on: date, reasons: list[str]
) -> Verdict:
    status = certificate.status

    if status is status.SUSPENDED:
        reasons.append("действие сертификата приостановлено")
        return Verdict.SUSPENDED
    if status in {status.TERMINATED, status.INVALID}:
        reasons.append(f"статус в реестре: {certificate.status_raw or status.value}")
        return Verdict.TERMINATED
    if certificate.valid_until is None:
        reasons.append("в реестре не указан срок действия")
        return Verdict.UNKNOWN
    if certificate.valid_until < on:
        reasons.append(f"срок действия истёк {certificate.valid_until.isoformat()}")
        return Verdict.EXPIRED
    if not status.is_alive:
        reasons.append(f"статус в реестре: {certificate.status_raw or status.value}")
        return Verdict.TERMINATED
    return Verdict.VALID


def _warn_on_disagreement(
    document: ExtractedDocument,
    certificate: RegistryCertificate,
    reasons: list[str],
) -> None:
    """Расхождение бумаги и реестра не меняет вердикт, но должно быть видно."""
    document_until = document.valid_until.value
    if (
        document_until is not None
        and certificate.valid_until is not None
        and document_until != certificate.valid_until
    ):
        reasons.append(
            f"срок в документе ({document_until.isoformat()}) не совпадает "
            f"с реестром ({certificate.valid_until.isoformat()})"
        )
