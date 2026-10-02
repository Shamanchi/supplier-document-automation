from app.parsing.pipeline import ParseResult, ParsedAttribute, ParsedItem
from app.parsing.validation_preview import build_validation_samples


def _item(
    sheet: str, row: int, name: str, *, attribute: tuple[str, str] | None = None
) -> ParsedItem:
    return ParsedItem(
        source_name=name,
        normalized_name=name.casefold(),
        source_article=None,
        normalized_article=None,
        unit_code=None,
        category_path=sheet,
        dimensions=None,
        description=None,
        attribute_values=(
            [
                ParsedAttribute(
                    source_header=attribute[0],
                    raw_value=attribute[1],
                    role="attribute",
                    attribute_code=attribute[0].casefold(),
                    value_kind="text",
                    canonical_unit=None,
                    ordinal=0,
                    source_column_index=1,
                )
            ]
            if attribute is not None
            else []
        ),
        source_sheet=sheet,
        source_row=row,
    )


def test_validation_preview_represents_different_sheets_before_repeating_one() -> None:
    result = ParseResult(items=[
        _item("Лист А", 2, "Товар А1"),
        _item("Лист А", 3, "Товар А2"),
        _item("Лист Б", 2, "Товар Б1"),
        _item("Матрица", 7, "Матричный товар"),
    ])

    samples = build_validation_samples(result, limit=3)

    assert [sample["source_sheet"] for sample in samples] == [
        "Лист А",
        "Лист Б",
        "Матрица",
    ]


def test_validation_preview_includes_matrix_variant_after_sheet_coverage() -> None:
    result = ParseResult(items=[
        _item("Матрица", 70, "Обычная нижняя позиция"),
        _item("Лист Б", 2, "Товар Б"),
        _item("Матрица", 71, "Обычная позиция 2"),
        _item("Матрица", 72, "Обычная позиция 3"),
        _item("Матрица", 7, "Позиция из ячейки", attribute=("Диаметр", "100")),
    ])

    samples = build_validation_samples(result, limit=4)

    assert samples[2]["name"] == "Позиция из ячейки"
    assert samples[2]["attributes"] == [
        {
            "code": "диаметр",
            "name": "Диаметр",
            "raw_value": "100",
            "unit": None,
        }
    ]
