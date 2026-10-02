"""Расчёт по заявке: цены поставщиков, скидки, ручные правки.

Порядок счёта фиксирован и важен: сначала цена поставщика, затем уровневая
скидка по позиции, затем объёмная по сумме заказа у этого поставщика, и только
после — ручная правка менеджера. Ручная цена стоит последней намеренно: пересчёт
скидок не должен её затирать, иначе менеджер потеряет согласованную с клиентом
цену при любом обновлении прайса.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.discounts import (
    NO_DISCOUNT,
    DiscountService,
    apply_percent,
)
from app.matching.physical import resolve_physical
from app.matching.variants import price_version_is_searchable
from app.pricing import select_prices_for_quantity
from app.models import (
    Calculation,
    CalculationItem,
    DocumentLink,
    PriceDocumentType,
    PriceFile,
    PriceKind,
    PriceOverrideLog,
    PriceRequest,
    PriceVersion,
    ProductDocument,
    Supplier,
    SupplierPriceItem,
    SupplierPriceValue,
)


ZERO = Decimal("0")

TWOPLACES = Decimal("0.01")


def money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value).quantize(TWOPLACES)


def split_vat(
    amount: Decimal | None,
    vat_rate_percent: Decimal | None,
    vat_mode: str | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if amount is None or vat_rate_percent is None:
        return money(amount), None, money(amount)

    rate = Decimal(vat_rate_percent)
    coefficient = Decimal("1") + rate / Decimal("100")

    if vat_mode == "included":
        price_with_vat = Decimal(amount)
        price_without_vat = price_with_vat / coefficient
    elif vat_mode == "excluded":
        price_without_vat = Decimal(amount)
        price_with_vat = price_without_vat * coefficient
    else:
        # vat_mode=unknown/None: режим НДС не известен — fail-closed. НДС сверху
        # не начисляем (иначе КП завышается), НДС-компонент не выделяем.
        return money(amount), None, money(amount)

    vat_amount = price_with_vat - price_without_vat
    return money(price_without_vat), money(vat_amount), money(price_with_vat)

# У позиции может быть несколько видов цены сразу. Для расчёта берётся та, по
# которой мы реально покупаем: дилерская, затем оптовая, и только потом
# розничная. Брать первую попавшуюся нельзя — на одной строке дилерская и
# розничная различаются в полтора раза.
PRICE_KIND_PRIORITY = (
    PriceKind.dealer.value,
    PriceKind.wholesale.value,
    PriceKind.retail.value,
    PriceKind.purchase.value,
    PriceKind.other.value,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class LineInput:
    """Строка, которую менеджер положил в расчёт из подбора."""

    price_item_id: uuid.UUID
    quantity: Decimal = Decimal("1")
    source_line: str = ""
    canonical_id: uuid.UUID | None = None


@dataclass(slots=True)
class CalculationTotals:
    """Итоги расчёта: до скидок, после скидок и с учётом ручных правок."""

    base: Decimal = ZERO
    discounted: Decimal = ZERO
    final: Decimal = ZERO
    total_discount_amount: Decimal = ZERO
    total_without_vat: Decimal = ZERO
    total_vat_amount: Decimal = ZERO
    total_with_vat: Decimal = ZERO
    by_supplier: dict[str, Decimal] = field(default_factory=dict)
    weight_kg: Decimal = ZERO
    volume_m3: Decimal = ZERO


class CalculationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.discounts = DiscountService(session)

    async def _price_of(
        self, item_id: uuid.UUID, quantity: Decimal
    ) -> tuple[
        Decimal | None,
        str | None,
        Decimal | None,
        str | None,
        SupplierPriceValue | None,
    ]:
        """Цена позиции под заказанное количество вместе с НДС-режимом.

        Возвращается и сама выбранная цена: из неё в строку расчёта пишется
        снимок — вид цены и ступень количества, по которой она взята.
        """
        rows = list(
            await self.session.scalars(
                select(SupplierPriceValue).where(SupplierPriceValue.item_id == item_id)
            )
        )
        if not rows:
            return None, None, None, None, None

        tiers = select_prices_for_quantity(rows, quantity)
        by_kind = {kind: value for (kind, _vat), value in tiers.items()}

        selected_value = None
        for kind in PRICE_KIND_PRIORITY:
            value = by_kind.get(kind)
            if value is not None and value.amount is not None:
                selected_value = value
                break

        if selected_value is None:
            for value in tiers.values():
                if value.amount is not None:
                    selected_value = value
                    break

        if selected_value is None:
            return None, None, None, None, None

        return (
            Decimal(selected_value.amount),
            selected_value.currency,
            Decimal(selected_value.vat_rate_percent)
            if selected_value.vat_rate_percent is not None
            else None,
            selected_value.vat_mode,
            selected_value,
        )

    async def _check_batch_id_of_request(
        self, request_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Заявка Ф3, к которой привязан расчётный лист (если связь есть)."""
        return await self.session.scalar(
            select(PriceRequest.check_batch_id).where(PriceRequest.id == request_id)
        )

    @staticmethod
    def _price_scope_ok(version: PriceVersion, check_batch_id: uuid.UUID | None) -> bool:
        """Разовый счёт/КП допустим только в своей заявке, общий прайс — везде."""
        if version.document_type != PriceDocumentType.supplier_quote.value:
            return True
        return (
            check_batch_id is not None
            and version.check_batch_id is not None
            and version.check_batch_id == check_batch_id
        )

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        lines: list[LineInput],
        title: str = "",
        request_id: uuid.UUID | None = None,
        check_batch_id: uuid.UUID | None = None,
    ) -> Calculation:
        # `items=[]` в конструкторе — не косметика: без него обращение к
        # коллекции у нового объекта уходит в ленивую загрузку и падает вне
        # greenlet-контекста SQLAlchemy.
        calculation = Calculation(
            project_id=project_id,
            request_id=request_id,
            title=title,
            status="draft",
            items=[],
        )
        self.session.add(calculation)
        await self.session.flush()

        batch_id = check_batch_id
        if batch_id is None and request_id is not None:
            batch_id = await self._check_batch_id_of_request(request_id)

        for ordinal, line in enumerate(lines, start=1):
            item = await self.session.get(SupplierPriceItem, line.price_item_id)
            if item is None:
                continue
            version = await self.session.get(PriceVersion, item.price_version_id)
            # Цена берётся только из версии, участвующей в подборе: снятый с
            # публикации прайс не должен попасть в КП задним числом.
            if version is None or not price_version_is_searchable(version, batch_id):
                continue
            (
                price,
                currency,
                vat_rate_percent,
                vat_mode,
                selected_value,
            ) = await self._price_of(item.id, Decimal(line.quantity))
            supplier = await self.session.get(Supplier, version.supplier_id)
            price_file = await self.session.get(PriceFile, version.price_file_id)
            physical = await resolve_physical(
                self.session, item=item, project_id=project_id
            )
            certificate = await self._confirmed_certificate(line.canonical_id)
            row = CalculationItem(
                calculation_id=calculation.id,
                ordinal=ordinal,
                source_line=line.source_line,
                price_item_id=item.id,
                canonical_product_id=line.canonical_id,
                supplier_id=version.supplier_id,
                supplier_name=supplier.name if supplier is not None else "",
                name=item.source_name,
                quantity=Decimal(line.quantity),
                unit_code=item.unit_code,
                price_version_id=version.id,
                price_document_id=version.price_file_id,
                price_document_type=version.document_type,
                price_source=price_file.source if price_file is not None else None,
                price_kind=(
                    selected_value.price_kind if selected_value is not None else None
                ),
                price_tier_min_quantity=(
                    selected_value.min_quantity if selected_value is not None else None
                ),
                price_tier_max_quantity=(
                    selected_value.max_quantity if selected_value is not None else None
                ),
                base_price=price,
                currency=currency,
                vat_rate_percent=vat_rate_percent,
                vat_mode=vat_mode,
                weight_kg=physical.weight_kg,
                volume_m3=physical.volume_m3,
                weight_source=physical.weight_source,
                volume_source=physical.volume_source,
                certificate_number=certificate[0],
                certificate_valid_until=certificate[1],
                certificate_confirmed_by=certificate[2],
                certificate_reference=certificate[3],
            )
            calculation.items.append(row)
        await self.session.flush()
        await self.recalculate(calculation)
        return calculation

    async def _confirmed_certificate(
        self, canonical_id: uuid.UUID | None
    ) -> tuple[str | None, date | None, str | None, str | None]:
        if canonical_id is None:
            return None, None, None, None
        result = await self.session.execute(
            select(DocumentLink, ProductDocument)
            .join(ProductDocument, ProductDocument.id == DocumentLink.document_id)
            .where(
                DocumentLink.canonical_product_id == canonical_id,
                DocumentLink.confirmed_by.is_not(None),
            )
            .order_by(DocumentLink.confirmed_at.desc(), DocumentLink.created_at.desc())
        )
        passport_fallback = None
        for link, document in result:
            evidence = link.evidence or {}
            if evidence.get("verdict") == "valid":
                valid_until = document.valid_until
                if not valid_until and evidence.get("valid_until"):
                    try:
                        valid_until = date.fromisoformat(str(evidence["valid_until"]))
                    except ValueError:
                        valid_until = None
                if valid_until is not None and valid_until < date.today():
                    continue
                reference = document.storage_key or document.registration_number
                return document.registration_number, valid_until, link.confirmed_by, reference
            # Паспорта приходят без вердикта FSA: они подтверждены пайплайном
            # привязки. Действующего сертификата нет — берём первый подтверждённый.
            if passport_fallback is None and (document.kind or "").lower() == "passport":
                passport_fallback = (link, document)
        if passport_fallback is not None:
            link, document = passport_fallback
            number = document.title or document.registration_number
            reference = (
                document.storage_key or document.registration_number or document.title
            )
            return number, document.valid_until, link.confirmed_by, reference
        return None, None, None, None

    async def recalculate(
        self,
        calculation: Calculation,
        *,
        special_discount_percent: Decimal | None = None,
    ) -> CalculationTotals:
        """Пересчитать скидки и итоги. Ручные цены не трогаются."""
        items = list(calculation.items)
        supplier_ids = [row.supplier_id for row in items if row.supplier_id is not None]
        rules = await self.discounts.rules_for(list(dict.fromkeys(supplier_ids)))
        manual_discount = ZERO
        if special_discount_percent is not None:
            manual_discount = Decimal(str(special_discount_percent))
            if manual_discount < ZERO:
                manual_discount = ZERO
            if manual_discount > Decimal("100"):
                manual_discount = Decimal("100")

        # Первый проход — уровневые скидки; они не зависят от суммы заказа.
        level: dict[uuid.UUID, tuple[Decimal, str, uuid.UUID | None]] = {}
        subtotal: dict[uuid.UUID, Decimal] = {}
        for row in items:
            if row.price_item_id is None or row.supplier_id is None:
                level[row.id] = (ZERO, "none", None)
                continue
            item = await self.session.get(SupplierPriceItem, row.price_item_id)
            key = (
                await self.discounts.position_key(item, row.supplier_id)
                if item is not None
                else None
            )
            match = self.discounts.match(key, rules) if key is not None else NO_DISCOUNT
            level[row.id] = (match.percent, match.source, match.rule_id)
            price = apply_percent(row.base_price, match.percent)
            if price is not None:
                subtotal[row.supplier_id] = subtotal.get(row.supplier_id, ZERO) + price * Decimal(
                    row.quantity
                )

        # Второй проход — объёмная скидка: порог считается по сумме позиций
        # этого поставщика уже со скидками, потому что именно столько клиент
        # заплатит, и именно эту сумму поставщик видит в заказе.
        totals = CalculationTotals()
        for row in items:
            percent, source, rule_id = level[row.id]
            volume = self.discounts.volume(
                row.supplier_id, subtotal.get(row.supplier_id, ZERO), rules
            )
            if volume.percent > percent:
                percent, source, rule_id = volume.percent, volume.source, volume.rule_id
            if manual_discount > percent:
                percent, source, rule_id = manual_discount, "manual", None
            row.discount_percent = percent
            row.discount_source = source
            row.discount_rule_id = rule_id
            row.price_after_discount = apply_percent(row.base_price, percent)
            row.discount_amount = (
                money(Decimal(row.base_price) - Decimal(row.price_after_discount))
                if row.base_price is not None and row.price_after_discount is not None
                else None
            )

            effective = row.manual_price if row.manual_price is not None else row.price_after_discount
            (
                row.price_without_vat,
                row.vat_amount,
                row.price_with_vat,
            ) = split_vat(effective, row.vat_rate_percent, row.vat_mode)

            quantity = Decimal(row.quantity)
            row.line_total_without_vat = (
                money(Decimal(row.price_without_vat) * quantity)
                if row.price_without_vat is not None
                else None
            )
            row.line_vat_amount = (
                money(Decimal(row.vat_amount) * quantity)
                if row.vat_amount is not None
                else None
            )
            row.line_total_with_vat = (
                money(Decimal(row.price_with_vat) * quantity)
                if row.price_with_vat is not None
                else None
            )
            if row.discount_amount is not None:
                totals.total_discount_amount += Decimal(row.discount_amount) * quantity
            if row.line_total_without_vat is not None:
                totals.total_without_vat += Decimal(row.line_total_without_vat)
            if row.line_vat_amount is not None:
                totals.total_vat_amount += Decimal(row.line_vat_amount)
            if row.line_total_with_vat is not None:
                totals.total_with_vat += Decimal(row.line_total_with_vat)
            if row.base_price is not None:
                totals.base += Decimal(row.base_price) * quantity
            if row.price_after_discount is not None:
                totals.discounted += Decimal(row.price_after_discount) * quantity
            effective = row.manual_price if row.manual_price is not None else row.price_after_discount
            if effective is not None:
                amount = Decimal(effective) * quantity
                totals.final += amount
                if row.supplier_id is not None:
                    key_id = str(row.supplier_id)
                    totals.by_supplier[key_id] = totals.by_supplier.get(key_id, ZERO) + amount
            if row.weight_kg is not None:
                totals.weight_kg += Decimal(row.weight_kg) * quantity
            if row.volume_m3 is not None:
                totals.volume_m3 += Decimal(row.volume_m3) * quantity

        calculation.total_amount = totals.final
        await self.session.flush()
        return totals

    async def override_price(
        self,
        item_id: uuid.UUID,
        *,
        price: Decimal | None,
        reason: str,
        changed_by: str,
    ) -> CalculationItem | None:
        """Ручная правка цены. Причина обязательна, каждая правка попадает в журнал."""
        if not reason.strip():
            raise ValueError("причина корректировки обязательна")
        row = await self.session.get(CalculationItem, item_id)
        if row is None:
            return None
        previous = row.manual_price if row.manual_price is not None else row.price_after_discount
        self.session.add(
            PriceOverrideLog(
                calculation_item_id=row.id,
                previous_price=previous,
                new_price=price,
                reason=reason.strip(),
                changed_by=changed_by,
            )
        )
        row.manual_price = Decimal(price) if price is not None else None
        row.manual_reason = reason.strip()
        row.manual_by = changed_by
        row.manual_at = _utcnow()
        await self.session.flush()

        calculation = await self.session.get(Calculation, row.calculation_id)
        if calculation is not None:
            await self.recalculate(calculation)
        return row

    async def history(self, item_id: uuid.UUID) -> list[PriceOverrideLog]:
        return list(
            await self.session.scalars(
                select(PriceOverrideLog)
                .where(PriceOverrideLog.calculation_item_id == item_id)
                .order_by(PriceOverrideLog.created_at)
            )
        )
