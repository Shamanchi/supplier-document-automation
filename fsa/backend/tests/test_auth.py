from __future__ import annotations

import csv
import pytest

from app.auth import token_is_valid
from app.config import Settings
from app.fsa.mapping import LEGACY_COLUMN_ORDER
from app.services.registry_import import (
    COLUMNS,
    RegistryFileError,
    _certificate_rows,
    detect_delimiter,
    detect_kind,
)
from app.models import RegistryImportKind


def settings_with(token: str | None) -> Settings:
    return Settings(api_token=token, database_url="postgresql+asyncpg://x/y")


class TestToken:
    def test_correct_token_passes(self):
        assert token_is_valid("secret-token", settings_with("secret-token"))

    def test_wrong_token_fails(self):
        assert not token_is_valid("nope", settings_with("secret-token"))

    def test_empty_configuration_keeps_the_service_closed(self):
        # Незаполненная переменная не должна открывать контур.
        assert not token_is_valid("", settings_with(None))
        assert not token_is_valid("anything", settings_with(""))

    def test_surrounding_whitespace_is_tolerated(self):
        assert token_is_valid("  secret-token  ", settings_with("secret-token"))


class TestRegistryFileDetection:
    def test_index_csv_is_detected_by_header(self):
        payload = "Ключ номера;Номер СС;Статус\nX;ЕАЭС RU С-RU.ПБ97.В.1/21;Действует\n"
        assert detect_kind("any.csv", payload.encode()) is RegistryImportKind.index

    def test_numbers_list_is_detected(self):
        payload = "EAЭC RU C-RU.ПБ97.B.00272/21\nEAЭC RU C-RU.ЧC13.B.00043/19\n"
        assert detect_kind("numbers.txt", payload.encode()) is RegistryImportKind.numbers

    def test_7z_archive_is_detected_by_signature(self):
        assert detect_kind("data.7z", b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32) is (
            RegistryImportKind.archive
        )

    def test_garbage_is_rejected_with_a_reason(self):
        with pytest.raises(RegistryFileError, match="содержимое"):
            detect_kind("x.bin", b"\x01\x02\x03 random bytes")

    def test_legacy_header_and_headless_csv_are_registry_indexes(self):
        header = ",".join(LEGACY_COLUMN_ORDER)
        row = [""] * len(LEGACY_COLUMN_ORDER)
        row[3] = "ЕАЭС RU С-RU.ПБ97.В.00001/19"
        assert detect_kind("rss_od.csv", f"{header}\n{','.join(row)}\n".encode()) is RegistryImportKind.index
        assert detect_kind("rss_month.csv", f"{','.join(row)}\n".encode()) is RegistryImportKind.index

    def test_headless_row_is_mapped_by_fixed_legacy_order(self):
        row = [""] * len(LEGACY_COLUMN_ORDER)
        values = {
            "cert_status": "Действует",
            "reg_number": "ЕАЭС RU С-RU.ПБ97.В.00001/19",
            "date_begining": "01.02.2019",
            "date_finish": "01.02.2024",
            "product_okpd2": "28.29.22.110",
            "product_tn_ved": "8424100000",
            "product_name": "Огнетушитель порошковый",
            "manufacturer_name": "Завод",
        }
        for key, value in values.items():
            row[LEGACY_COLUMN_ORDER.index(key)] = value
        buffer, total = _certificate_rows((",".join(row) + "\n").encode(), "legacy.csv")
        parsed = next(csv.reader(buffer, delimiter="\t"))
        assert total == 1
        assert parsed[COLUMNS.index("registration_number")] == values["reg_number"]
        assert parsed[COLUMNS.index("product_full_name")] == values["product_name"]
        assert parsed[COLUMNS.index("okpd2_tnved")] == "28.29.22.110 8424100000"


class TestRegistryDelimiter:
    """Выгрузки реестра приходят с разными разделителями — и это не мелочь.

    Срез с вертикальной чертой, прочитанный как срез с точкой с запятой, даёт
    одну колонку на строку: номера в ней нет, и загрузка молча заканчивается
    нулём карточек вместо ошибки.
    """

    def test_semicolon_is_detected(self):
        assert detect_delimiter('"id";"Статус";"Номер СС"') == ";"

    def test_pipe_is_detected(self):
        assert detect_delimiter('"id"|"Статус"|"Номер СС"') == "|"

    def test_the_more_frequent_separator_wins(self):
        # В заголовке со смесью побеждает тот, что встречается чаще: одиночная
        # запятая внутри названия колонки не должна перебивать разделитель.
        assert detect_delimiter('"Коды ОКПД2, ТНВЭД";"Статус";"Номер"') == ";"
