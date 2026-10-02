"""Воспроизводимая Excel-выгрузка коммерческого предложения."""

from __future__ import annotations

import io
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models import Calculation


HEADERS = (
    "№",
    "Исходная строка",
    "Наименование",
    "Ед.",
    "Кол-во",
    "Валюта",
    "Цена до скидки",
    "Скидка, %",
    "Сумма скидки",
    "Цена после скидки",
    "Ставка НДС, %",
    "Сумма без НДС",
    "Сумма НДС",
    "Сумма с НДС",
    "Вес ед., кг",
    "Вес итого, кг",
    "Объём ед., м³",
    "Объём итого, м³",
    "Ссылка/файл сертификата",
)


def _excel_text(value: object) -> object:
    """Keep supplier-controlled text from becoming a spreadsheet formula."""
    if not isinstance(value, str) or not value.startswith(("=", "+", "-", "@")):
        return value
    return f"'{value}"


def build_quote_workbook(calculation: Calculation) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Коммерческое предложение"
    request = calculation.request
    if request is not None:
        sheet.append(["", "ID тендера", _excel_text(request.number)])
        sheet.append(["", "Заказчик тендера", _excel_text(request.client)])
        sheet.append([])

    sheet.append(list(HEADERS))
    header_row = sheet.max_row
    for cell in sheet[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")

    for item in sorted(calculation.items, key=lambda row: row.ordinal):
        quantity = Decimal(item.quantity)
        final_price = (
            item.manual_price if item.manual_price is not None else item.price_after_discount
        )
        amount = Decimal(final_price) * quantity if final_price is not None else None
        total_weight = Decimal(item.weight_kg) * quantity if item.weight_kg is not None else None
        total_volume = Decimal(item.volume_m3) * quantity if item.volume_m3 is not None else None
        sheet.append(
            [
                item.ordinal,
                _excel_text(item.source_line),
                _excel_text(item.name),
                _excel_text(item.unit_code),
                float(quantity),
                _excel_text(item.currency),
                float(item.base_price) if item.base_price is not None else None,
                float(item.discount_percent),
                float(item.discount_amount) if item.discount_amount is not None else None,
                float(item.price_after_discount)
                if item.price_after_discount is not None
                else None,
                float(item.vat_rate_percent) if item.vat_rate_percent is not None else None,
                float(item.line_total_without_vat)
                if item.line_total_without_vat is not None
                else None,
                float(item.line_vat_amount) if item.line_vat_amount is not None else None,
                float(item.line_total_with_vat)
                if item.line_total_with_vat is not None
                else None,
                float(item.weight_kg) if item.weight_kg is not None else None,
                float(total_weight) if total_weight is not None else None,
                float(item.volume_m3) if item.volume_m3 is not None else None,
                float(total_volume) if total_volume is not None else None,
                _excel_text(item.certificate_reference),
            ]
        )
        reference_cell = sheet.cell(sheet.max_row, len(HEADERS))
        if isinstance(item.certificate_reference, str) and item.certificate_reference.startswith(
            ("http://", "https://")
        ):
            reference_cell.hyperlink = item.certificate_reference
            reference_cell.style = "Hyperlink"

    summary_start = sheet.max_row + 2

    total_discount_amount = Decimal("0")
    total_without_vat = Decimal("0")
    total_vat_amount = Decimal("0")
    total_with_vat = Decimal("0")
    total_weight = Decimal("0")
    total_volume = Decimal("0")

    for item in calculation.items:
        quantity = Decimal(item.quantity)

        if item.discount_amount is not None:
            total_discount_amount += Decimal(item.discount_amount) * quantity
        if item.line_total_without_vat is not None:
            total_without_vat += Decimal(item.line_total_without_vat)
        if item.line_vat_amount is not None:
            total_vat_amount += Decimal(item.line_vat_amount)
        if item.line_total_with_vat is not None:
            total_with_vat += Decimal(item.line_total_with_vat)
        if item.weight_kg is not None:
            total_weight += Decimal(item.weight_kg) * quantity
        if item.volume_m3 is not None:
            total_volume += Decimal(item.volume_m3) * quantity

    summary_rows = (
        ("Итого сумма скидки", total_discount_amount),
        ("Итого без НДС", total_without_vat),
        ("Итого НДС", total_vat_amount),
        ("Итого с НДС", total_with_vat),
        ("Итого вес, кг", total_weight),
        ("Итого объём, м³", total_volume),
    )

    for offset, (label, value) in enumerate(summary_rows):
        row_number = summary_start + offset
        sheet.cell(row_number, 2, label)
        sheet.cell(row_number, 3, float(value))
        sheet.cell(row_number, 2).font = Font(bold=True)
        sheet.cell(row_number, 3).font = Font(bold=True)
        sheet.cell(row_number, 3).number_format = "#,##0.00"

    sheet.cell(summary_start + 4, 3).number_format = "#,##0.000000"
    sheet.cell(summary_start + 5, 3).number_format = "#,##0.000000"

    sheet.freeze_panes = f"A{header_row + 1}"
    sheet.auto_filter.ref = sheet.dimensions
    widths = (6, 34, 42, 10, 12, 10, 16, 12, 16, 18, 14, 16, 16, 16, 14, 14, 14, 14, 40)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in sheet.iter_rows(min_row=2):
        for index in (7, 9, 10, 12, 13, 14):
            row[index - 1].number_format = "#,##0.00"
        for index in (15, 16, 17, 18):
            row[index - 1].number_format = "#,##0.000000"

    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()
