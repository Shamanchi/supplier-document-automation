"""
Пакетная обработка всех паспортов в папке.
Собирает наименования изделий в CSV-файл.

Использование:
    python scripts/batch_extract_passports.py <путь_к_папке_с_паспортами>

Пример:
    python scripts/batch_extract_passports.py "OCR_prices/Прайсы/прайсы/АО ИСТОЧНИК/Паспорта"
"""

import csv
import os
import sys
from pathlib import Path
from extract_from_any_passport import extract_from_passport


def process_folder(folder_path: str, output_csv: str = None):
    """Обработать все PDF в папке и сохранить в CSV."""
    folder = Path(folder_path)

    if not folder.exists():
        print(f"❌ Папка не найдена: {folder}")
        return

    # Находим все PDF
    pdf_files = sorted(folder.glob('*.pdf'))
    print(f"📂 Папка: {folder}")
    print(f"📄 Найдено PDF: {len(pdf_files)}\n")

    if not pdf_files:
        print("❌ PDF-файлы не найдены")
        return

    # Если выходной файл не указан — сохраняем рядом с папкой
    if output_csv is None:
        output_csv = str(folder.parent / 'результаты_паспорта.csv')

    results = []
    success = 0
    failed = 0

    for i, pdf_file in enumerate(pdf_files, 1):
        print(f"[{i}/{len(pdf_files)}] Обработка: {pdf_file.name}")
        result = extract_from_passport(pdf_file)
        results.append(result)

        if result['error']:
            print(f"   ❌ Ошибка: {result['error']}")
            failed += 1
        elif result['product_name']:
            print(f"   ✅ {result['product_name']}")
            success += 1
        else:
            print(f"   ⚠️ Наименование не найдено")
            failed += 1

    # Сохраняем в CSV
    with open(output_csv, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(['№', 'Файл', 'Наименование изделия', 'Страниц', 'Статус'])

        for i, r in enumerate(results, 1):
            status = 'OK' if r['product_name'] and not r['error'] else ('Ошибка' if r['error'] else 'Не найдено')
            writer.writerow([i, r['file'], r['product_name'] or '-', r['pages'], status])

    print(f"\n{'=' * 60}")
    print(f"📊 ИТОГИ:")
    print(f"   ✅ Успешно: {success}")
    print(f"   ❌ Ошибки / не найдено: {failed}")
    print(f"\n💾 Результат сохранён: {output_csv}")
    print(f"{'=' * 60}")

    return output_csv


if __name__ == "__main__":
    # Путь по умолчанию
    default_path = r"OCR_prices/Прайсы/прайсы/АО ИСТОЧНИК/Паспорта"

    if len(sys.argv) > 1:
        folder_path = sys.argv[1]
    else:
        folder_path = default_path
        print(f"Путь не указан, используем по умолчанию: {folder_path}")
        print(f"Другой путь: python scripts/batch_extract_passports.py \"путь/к/папке\"")
        print()

    process_folder(folder_path)
