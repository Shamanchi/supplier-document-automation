"""
Извлечение номеров сертификатов из PDF-паспортов
Версия 3: ищем номер на ВСЕХ страницах, наименование — с 1-й
"""

import pdfplumber
import re
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional
import json


@dataclass
class CertificateExtraction:
    filename: str
    product_name: Optional[str] = None       # С 1-й страницы
    certificate_number: Optional[str] = None  # С любой страницы
    certificate_number_normalized: Optional[str] = None
    page_found: int = 1
    status: str = "unknown"


def normalize_certificate_number(number: str) -> str:
    cyrillic_to_latin = {
        'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E', 'К': 'K',
        'М': 'M', 'Н': 'H', 'О': 'O', 'Р': 'P', 'Т': 'T',
        'У': 'Y', 'Х': 'X'
    }
    normalized = ' '.join(number.upper().split())
    return ''.join(cyrillic_to_latin.get(c, c) for c in normalized)


def extract_certificate_numbers(text: str) -> List[str]:
    if not text:
        return []
    # Шаблон номера сертификата
    pattern = r'(?:ЕАЭС\s+)?RU\s+[СC]-RU\.[А-ЯA-Z0-9]+\.[АВB]\.\d+/\d{2}'
    matches = re.findall(pattern, text, re.IGNORECASE)
    
    unique = []
    seen = set()
    for match in matches:
        normalized = normalize_certificate_number(match)
        if normalized not in seen:
            seen.add(normalized)
            unique.append(match.strip())
    return unique


def extract_product_name_from_page1(text: str) -> Optional[str]:
    """Извлечь наименование продукции с 1-й страницы"""
    if not text:
        return None
    
    patterns = [
        r'(?:^|\n)\s*([А-ЯA-Z][А-Яа-яa-zA-Z\s\-()]+(?:пожаротушения|изделия|оборудования|насос|гидрант|модуль|установка)[А-Яа-яa-zA-Z\s\-()0-9]*)',
        r'Наименование\s+(?:изделия|продукции|оборудования)[\s:]+(.+?)(?:\n|\r|$)',
        r'Продукция[\s:]+(.+?)(?:\n|\r|$)',
        r'Изделие[\s:]+(.+?)(?:\n|\r|$)',
        r'(?:Паспорт|ПАСПОРТ)\s*(?:и\s+руководство)?\s*\n?\s*([А-Я][А-Яа-я\s\-()]+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            name = ' '.join(name.split())
            if len(name) > 3:
                return name[:200]
    
    return None


def process_pdf_file(pdf_path: Path) -> List[CertificateExtraction]:
    """Обработать один PDF-файл"""
    results = []
    
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            total_pages = len(pdf.pages)
            
            # 1. Берём наименование с 1-й страницы
            product_name = None
            if total_pages > 0:
                text_page1 = pdf.pages[0].extract_text()
                product_name = extract_product_name_from_page1(text_page1)
            
            # 2. Ищем номер сертификата на ВСЕХ страницах
            cert_numbers = []
            found_page = 1
            
            for page_num, page in enumerate(pdf.pages, 1):
                text = page.extract_text()
                if text:
                    nums = extract_certificate_numbers(text)
                    if nums:
                        cert_numbers.extend(nums)
                        found_page = page_num
                        break  # Нашли — достаточно
            
            if cert_numbers:
                for cert_num in cert_numbers:
                    results.append(CertificateExtraction(
                        filename=pdf_path.name,
                        product_name=product_name,
                        certificate_number=cert_num,
                        certificate_number_normalized=normalize_certificate_number(cert_num),
                        page_found=found_page,
                        status="found"
                    ))
            else:
                # Номер не найден ни на одной странице
                has_text = any(page.extract_text() for page in pdf.pages)
                results.append(CertificateExtraction(
                    filename=pdf_path.name,
                    product_name=product_name,
                    status="no_text_layer" if not has_text else "not_found",
                    page_found=total_pages
                ))
    
    except Exception as e:
        results.append(CertificateExtraction(
            filename=pdf_path.name,
            status=f"error: {str(e)}"
        ))
    
    return results


def process_folder(folder_path: Path, output_file: Optional[Path] = None) -> List[CertificateExtraction]:
    """Обработать все PDF в папке"""
    all_results = []
    pdf_files = list(folder_path.rglob("*.pdf"))
    
    print(f"Найдено PDF: {len(pdf_files)}\n")
    
    for i, pdf_path in enumerate(pdf_files, 1):
        print(f"[{i}/{len(pdf_files)}] {pdf_path.name}")
        results = process_pdf_file(pdf_path)
        all_results.extend(results)
        
        found_certs = [r for r in results if r.status == "found"]
        if found_certs:
            for r in found_certs:
                print(f"   ✅ Номер: {r.certificate_number_normalized}")
                print(f"   📄 Страница: {r.page_found}")
                if r.product_name:
                    print(f"   📦 Наименование: {r.product_name[:80]}")
        elif any(r.status == "no_text_layer" for r in results):
            print(f"   ⚠️  Нет текстового слоя (скан — нужен OCR)")
        elif any(r.status == "not_found" for r in results):
            print(f"   ❌ Номер не найден (текст есть)")
        else:
            print(f"   💥 Ошибка")
    
    if output_file:
        save_results(all_results, output_file)
    
    return all_results


def save_results(results: List[CertificateExtraction], output_file: Path):
    """Сохранить в JSON"""
    data = []
    for r in results:
        data.append({
            "filename": r.filename,
            "product_name": r.product_name,
            "certificate_number": r.certificate_number,
            "certificate_number_normalized": r.certificate_number_normalized,
            "page_found": r.page_found,
            "status": r.status
        })
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 Сохранено: {output_file}")


def print_summary(results: List[CertificateExtraction]):
    """Сводка"""
    total = len(results)
    found = len([r for r in results if r.status == "found"])
    no_text = len([r for r in results if r.status == "no_text_layer"])
    not_found = len([r for r in results if r.status == "not_found"])
    errors = total - found - no_text - not_found
    
    print("\n" + "=" * 60)
    print("СВОДКА")
    print("=" * 60)
    print(f"Всего документов:  {total}")
    print(f"  ✅ Номер найден:  {found}")
    print(f"  ⚠️  Скан (OCR):   {no_text}")
    print(f"  ❌ Не найден:     {not_found}")
    print(f"  💥 Ошибки:        {errors}")
    
    if found > 0:
        print("\n📋 Найденные сертификаты:")
        for r in results:
            if r.status == "found":
                print(f"\n   📄 {r.filename}")
                print(f"   🔢 {r.certificate_number_normalized}")
                if r.product_name:
                    print(f"   📦 {r.product_name}")


# ============ ЗАПУСК ============
if __name__ == "__main__":
    search_folder = Path("./data/OCR_prices")
    output_file = Path("./data/extracted_certificates_v3.json")
    
    print("=" * 70)
    print("ИЗВЛЕЧЕНИЕ НОМЕРОВ СЕРТИФИКАТОВ ИЗ PDF")
    print("=" * 70)
    print(f"Папка: {search_folder}")
    print("Ищем номер на ВСЕХ страницах, наименование — с 1-й")
    print()
    
    if not search_folder.exists():
        print(f"❌ Папка не найдена: {search_folder}")
        exit(1)
    
    results = process_folder(search_folder, output_file)
    print_summary(results)
