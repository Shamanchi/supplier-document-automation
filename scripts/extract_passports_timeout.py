import pdfplumber
import re
from pathlib import Path
import json
import threading


def normalize_number(number: str) -> str:
    cyr_to_lat = {'А':'A','В':'B','С':'C','Е':'E','К':'K','М':'M','Н':'H','О':'O','Р':'P','Т':'T','У':'Y','Х':'X'}
    return ''.join(cyr_to_lat.get(c, c) for c in ' '.join(number.upper().split()))


def find_cert_numbers(text: str) -> list:
    if not text:
        return []
    pattern = r'(?:ЕАЭС\s+)?RU\s+[СC]-RU\.[А-ЯA-Z0-9]+\.[АВB]\.\d+/\d{2}'
    matches = re.findall(pattern, text, re.IGNORECASE)
    seen = set()
    result = []
    for m in matches:
        n = normalize_number(m)
        if n not in seen:
            seen.add(n)
            result.append(m.strip())
    return result


def find_product_name(text: str) -> str:
    if not text:
        return None
    patterns = [
        r'(?:^|\n)\s*([А-ЯA-Z][А-Яа-яa-zA-Z\s\-()]+(?:пожаротушения|изделия|оборудования|насос|гидрант|модуль|установка)[А-Яа-яa-zA-Z\s\-()0-9]*)',
        r'Наименование\s+(?:изделия|продукции)[\s:]+(.+?)(?:\n|$)',
        r'(?:Паспорт|ПАСПОРТ)\s*(?:и\s+руководство)?\s*\n?\s*([А-Я][А-Яа-я\s\-()]+)',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            name = ' '.join(m.group(1).strip().split())
            if len(name) > 3:
                return name[:200]
    return None


def process_one_file(pdf_path: Path):
    """Обработать один файл с таймаутом"""
    result = {"filename": pdf_path.name, "status": "unknown"}
    
    # Пропускаем очень большие файлы (>10MB)
    if pdf_path.stat().st_size > 10 * 1024 * 1024:
        return {**result, "status": "too_large"}
    
    def timeout_handler():
        raise TimeoutError("PDF processing timeout")
    
    timer = threading.Timer(15.0, timeout_handler)  # 15 секунд на файл
    timer.start()
    
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            text1 = pdf.pages[0].extract_text()
            if not text1:
                timer.cancel()
                return {**result, "status": "no_text_layer"}
            
            product = find_product_name(text1)
            
            # Ищем номер (макс 5 страниц)
            for page_num in range(min(len(pdf.pages), 5)):
                text = pdf.pages[page_num].extract_text()
                nums = find_cert_numbers(text)
                if nums:
                    timer.cancel()
                    return {
                        **result,
                        "product_name": product,
                        "certificate_number": nums[0],
                        "certificate_number_normalized": normalize_number(nums[0]),
                        "page_found": page_num + 1,
                        "status": "found"
                    }
            
            timer.cancel()
            return {**result, "product_name": product, "status": "not_found"}
    
    except TimeoutError:
        return {**result, "status": "timeout"}
    except Exception as e:
        timer.cancel()
        return {**result, "status": f"error: {str(e)[:50]}"}


# === ЗАПУСК ===
folder = Path("./data/OCR_prices/Документы от заказчика/ТЗ и паспорта")
pdfs = list(folder.rglob("*.pdf"))

print(f"Паспортов: {len(pdfs)}\n")

results = []
for i, pdf_path in enumerate(pdfs, 1):
    print(f"[{i}/{len(pdfs)}] {pdf_path.name[:40]}", end=" ")
    r = process_one_file(pdf_path)
    results.append(r)
    
    status_msg = {
        "found": f"✅ {r.get('certificate_number_normalized', '')} (стр.{r.get('page_found', '')})",
        "no_text_layer": "⚠️ скан",
        "not_found": "❌ не найден",
        "timeout": "⏱️ таймаут",
        "too_large": "📦 слишком большой",
    }
    print(f"→ {status_msg.get(r['status'], r['status'])}")

# Сохраняем
output = Path("./data/extracted_from_passports.json")
with open(output, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

# Сводка
found = len([r for r in results if r.get("status") == "found"])
scan = len([r for r in results if r.get("status") == "no_text_layer"])
notfound = len([r for r in results if r.get("status") == "not_found"])
timeout = len([r for r in results if r.get("status") == "timeout"])

print(f"\n{'='*60}")
print(f"Всего: {len(pdfs)} | ✅ {found} | ⚠️ {scan} | ❌ {notfound} | ⏱️ {timeout}")
print(f"💾 {output}")

if found > 0:
    print("\n📋 Найденные сертификаты:")
    for r in results:
        if r.get("status") == "found":
            print(f"\n  📄 {r['filename']}")
            print(f"  🔢 {r['certificate_number_normalized']} (стр.{r['page_found']})")
            if r.get('product_name'):
                print(f"  📦 {r['product_name']}")
