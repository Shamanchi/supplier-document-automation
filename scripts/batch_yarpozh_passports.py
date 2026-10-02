"""
Обработка всех паспортов Supplier A
"""
import sys
from pathlib import Path

# Добавляем scripts в путь
sys.path.insert(0, str(Path(__file__).parent))

from extract_from_any_passport import extract_from_passport

PASSPORTS_DIR = Path(__file__).resolve().parent.parent / "OCR_prices" / "Прайсы" / "прайсы" / "Supplier A" / "Паспорта"

pdfs = sorted([f for f in PASSPORTS_DIR.iterdir() if f.suffix.lower() == '.pdf'])
print(f"Найдено паспортов: {len(pdfs)}\n")

results = []
for pdf in pdfs:
    result = extract_from_passport(pdf)
    results.append(result)
    status = "✅" if result['product_name'] else "❌"
    print(f"{status} {result['file']:<60} → {result['product_name'] or 'НЕ НАЙДЕНО'}")
    if result['error']:
        print(f"   Ошибка: {result['error']}")

# Статистика
found = sum(1 for r in results if r['product_name'])
print(f"\n{'='*60}")
print(f"ИТОГО: {found}/{len(results)} паспортов распознано ({found*100//len(results)}%)")
