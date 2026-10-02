"""
Мастер-скрипт: обработка ВСЕХ паспортов всех поставщиков.
Собирает единый CSV с наименованиями из паспортов.

Использование:
    python scripts/extract_all_passports_master.py

Результат: all_passports_results.csv в корне репозитория
"""

import csv
import os
import sys
from pathlib import Path

# Импортируем функцию из основного скрипта
sys.path.insert(0, str(Path(__file__).parent))
from extract_from_any_passport import extract_from_passport


def process_all_passports():
    """Обработать все папки Паспорта во всех подпапках."""
    
    # Корень с данными
    base_path = Path(__file__).resolve().parent.parent / "OCR_prices" / "Прайсы" / "прайсы"
    
    # Ищем все папки Паспорта
    passport_folders = sorted(base_path.glob("*/Паспорта"))
    
    print("=" * 70)
    print("📦 МАСТЕР-ОБРАБОТКА ВСЕХ ПАСПОРТОВ")
    print("=" * 70)
    print(f"\nНайдено папок: {len(passport_folders)}")
    for f in passport_folders:
        print(f"  📁 {f.parent.name}")
    
    # Выходной файл
    output_file = Path(__file__).resolve().parent.parent / "all_passports_results.csv"
    
    all_results = []
    total_success = 0
    total_failed = 0
    
    # Заголовки
    with open(output_file, 'w', newline='', encoding='utf-8-sig') as out_f:
        writer = csv.writer(out_f, delimiter=';')
        writer.writerow(['№', 'Поставщик', 'Файл', 'Наименование изделия', 'Страниц', 'Статус'])
        
        global_counter = 0
        
        for folder in passport_folders:
            supplier = folder.parent.name
            pdf_files = sorted(folder.glob('*.pdf'))
            
            print(f"\n{'='*70}")
            print(f"📂 {supplier}: {len(pdf_files)} PDF")
            print(f"{'='*70}")
            
            for pdf_file in pdf_files:
                global_counter += 1
                print(f"  [{global_counter}] {pdf_file.name}")
                
                result = extract_from_passport(pdf_file)
                
                status = 'OK' if result['product_name'] and not result['error'] else ('Ошибка' if result['error'] else 'Не найдено')
                
                writer.writerow([
                    global_counter,
                    supplier,
                    result['file'],
                    result['product_name'] or '-',
                    result['pages'],
                    status
                ])
                
                if result['product_name'] and not result['error']:
                    total_success += 1
                    print(f"      ✅ {result['product_name']}")
                elif result['error']:
                    total_failed += 1
                    print(f"      ❌ {result['error']}")
                else:
                    total_failed += 1
                    print(f"      ⚠️ Не найдено")
    
    print(f"\n{'='*70}")
    print(f"📊 ИТОГОВАЯ СТАТИСТИКА")
    print(f"{'='*70}")
    print(f"   Всего файлов:    {global_counter}")
    print(f"   ✅ Успешно:      {total_success}")
    print(f"   ❌ Ошибки:       {total_failed}")
    print(f"\n💾 Результат: {output_file}")
    print(f"{'='*70}")
    
    return output_file


if __name__ == "__main__":
    output = process_all_passports()
    print(f"\nГотово! Открой файл: {output}")
