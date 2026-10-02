"""
Сохранение результатов извлечения из паспортов Supplier A в JSON
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract_from_any_passport import extract_from_passport

PASSPORTS_DIR = Path(__file__).resolve().parent.parent / "OCR_prices" / "Прайсы" / "прайсы" / "Supplier A" / "Паспорта"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "OCR_prices" / "Прайсы" / "прайсы" / "Supplier A" / "passport_extractions.json"

pdfs = sorted([f for f in PASSPORTS_DIR.iterdir() if f.suffix.lower() == '.pdf'])

results = []
for pdf in pdfs:
    result = extract_from_passport(pdf)
    results.append({
        'file': result['file'],
        'product_name': result['product_name'],
        'pages': result['pages'],
        'error': result['error']
    })

with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

found = sum(1 for r in results if r['product_name'])
print(f"Сохранено: {OUTPUT_FILE}")
print(f"Распознано: {found}/{len(results)} ({found*100//len(results)}%)")
