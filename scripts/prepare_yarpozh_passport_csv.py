# -*- coding: utf-8 -*-
"""Подготовка all_passports_results.csv для Supplier A (демо wave-2).

Наименования изделий и размерные ряды сверены с текстом первых страниц
PDF-паспортов (pdfplumber). Дубликаты одного паспорта ОП-25..100 (3 копии)
исключены; pass.pdf (скан без текстового слоя) пропущен — это честный кейс
«требует ручной обработки».

Формат CSV совпадает с extract_all_passports_master.py:
    №;Поставщик;Файл;Наименование изделия;Страниц;Статус

Запуск из корня репозитория:
    python scripts/prepare_yarpozh_passport_csv.py
"""

import csv
from pathlib import Path

SUPPLIER = "Supplier A"

# (файл, страниц, наименование изделия) — наименование из паспорта,
# ряд исполнений — из текста документа.
PASSPORTS = [
    (
        "Pasport-na-poroshkovye-ognetushiteli-OP-25-35-40-50-70-75-100-Yarpozhinvest.pdf",
        17,
        "Огнетушители порошковые передвижные закачные ОП-25, ОП-35, ОП-40, ОП-50, "
        "ОП-70, ОП-75, ОП-100 (АВСЕ/ВСЕ, Supplier A)",
    ),
    (
        "Pasport-na-poroshkovye-ognetushiteli-OP-1-2-3-4-5-6-8-9-10-MIG-Pozhtehnika.pdf",
        4,
        "Огнетушители порошковые переносные закачные ОП-1, ОП-2, ОП-3, ОП-4, ОП-5, "
        "ОП-6, ОП-8, ОП-9, ОП-10 (МИГ, Пожтехника)",
    ),
    (
        "pasport-ognetushiteli-op-1_10-yarpozhinvest.pdf",
        17,
        "Огнетушители порошковые переносные закачные ОП-1, ОП-2, ОП-3, ОП-4, ОП-5, "
        "ОП-6, ОП-7, ОП-8, ОП-9, ОП-10 (АВСЕ-01/ВСЕ-02, Supplier A)",
    ),
    (
        "911769_other.pdf",
        2,
        "Огнетушители порошковые переносные закачные ОП-3, ОП-4, ОП-5, ОП-6, ОП-8, "
        "ОП-10 (АВСЕ-01, РЭ 28.29.22-001-44684525-2021, Меланти)",
    ),
    (
        "7d1fd044401a2a95ac7b9063e4930c36.pdf",
        8,
        "Огнетушители порошковые передвижные закачные ОП-12, ОП-25, ОП-40, ОП-50, "
        "ОП-80, ОП-100 (МИГ, Пожтехника)",
    ),
    (
        "Pasport_OU_1_8_Yarpozhinvest.pdf",
        4,
        "Огнетушители углекислотные переносные ОУ-1, ОУ-2, ОУ-3, ОУ-4, ОУ-5, ОУ-6, "
        "ОУ-7, ОУ-8 (ВСЕ, Supplier A)",
    ),
    (
        "Pasport-na-uglekislotnye-ognetushiteli-OU-1-2-3-5-INEY-Pozhtehnika.pdf",
        8,
        "Огнетушители углекислотные переносные ОУ-1, ОУ-2, ОУ-3, ОУ-5 (ИНЕЙ, Пожтехника)",
    ),
    (
        "Pasport-na-uglekislotnye-ognetushiteli-OU-4-5-7-PK-RIF.pdf",
        6,
        "Огнетушители углекислотные переносные ОУ-4, ОУ-5, ОУ-7 "
        "(Риф, ТУ 4854-001-77055514-2007)",
    ),
    (
        "pasport-ognetushiteli-iney-7_50-pozhtechnika.pdf",
        8,
        "Огнетушители углекислотные передвижные ОУ-7, ОУ-10, ОУ-15, ОУ-25, ОУ-50 "
        "(ИНЕЙ, Пожтехника)",
    ),
    (
        "Pasport-na-vozdushno-pennye-ognetushiteli-OVP-40-50-80-MIG-Pozhtehnika.pdf",
        8,
        "Огнетушители воздушно-пенные передвижные ОВП-40, ОВП-50, ОВП-80 "
        "(МИГ, Пожтехника)",
    ),
    (
        "Pasport-na-moduli-poroshkovogo-pozharotusheniya-MPP-2,5-5-7-MIG-Pozhtehnika.pdf",
        8,
        "Модули порошкового пожаротушения МПП-2,5, МПП-5, МПП-7 "
        "(КД-1-3-УХЛ1, МИГ, Пожтехника)",
    ),
    (
        "Pasport-MPP-10st.pdf",
        19,
        "Модуль порошкового пожаротушения МПП-10 (ст)-И-ГЭ-У2 (Тунгус, Источник Плюс)",
    ),
]


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    out = root / "all_passports_results.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["№", "Поставщик", "Файл", "Наименование изделия", "Страниц", "Статус"])
        for idx, (filename, pages, title) in enumerate(PASSPORTS, start=1):
            writer.writerow([idx, SUPPLIER, filename, title, pages, "OK"])
    print(f"Записано строк: {len(PASSPORTS)} -> {out}")


if __name__ == "__main__":
    main()
