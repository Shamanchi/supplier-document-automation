"""
Быстрый тест извлечения номера сертификата из PDF-паспорта.

Использование:
    python scripts/test_single_passport.py

Ожидаемый результат:
    - Наименование изделия с 1-й страницы (то, к чему прилагается паспорт)
    - Номер сертификата со страницы 16
"""

import pdfplumber
import re
from pathlib import Path


def normalize_number(number: str) -> str:
    """Кириллические омоглифы → латиница для поиска в реестре."""
    cyr_to_lat = {
        'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E', 'К': 'K',
        'М': 'M', 'Н': 'H', 'О': 'O', 'Р': 'P', 'Т': 'T',
        'У': 'Y', 'Х': 'X'
    }
    return ''.join(cyr_to_lat.get(c, c) for c in ' '.join(number.upper().split()))


def find_cert_numbers(text: str) -> list:
    """Найти номера сертификатов в тексте."""
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
    """Извлечь наименование ИЗДЕЛИЯ с 1-й страницы паспорта.

    Наименование — это то, к чему прилагается паспорт.
    Оно находится ДО слова 'Паспорт'.
    """
    if not text:
        return None

    lines = text.split('\n')

    # Способ 1: найти индекс строки "Паспорт" и взять 1-2 строки ПЕРЕД ней
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^(Паспорт|ПАСПОРТ)\b', stripped, re.IGNORECASE):
            product_lines = []
            # Берём до 3 строк перед "Паспорт"
            for j in range(max(0, i - 3), i):
                candidate = lines[j].strip()
                # Пропускаем: пустые, url, email, телефоны, адреса
                if not candidate:
                    continue
                if any(x in candidate.lower() for x in ['www.', '.com', '.ru', 'тел.', 'факс', 'адрес', 'россия', 'ул.']):
                    continue
                if re.match(r'^[\d\s\-\(\)\.]+$', candidate):  # только цифры
                    continue
                # Хорошая строка: начинается с заглавной, содержит буквы
                if re.search(r'[А-ЯA-Z]', candidate):
                    product_lines.append(candidate)
            if product_lines:
                return ' '.join(product_lines)

    # Способ 2: регулярка — текст до слова "Паспорт"
    match = re.search(
        r'([А-ЯA-Z][А-Яа-яa-zA-Z\s\-\(\)\.0-9]{5,80})\n?\s*(?:Паспорт|ПАСПОРТ)',
        text, re.IGNORECASE
    )
    if match:
        name = match.group(1).strip()
        name = ' '.join(name.split())
        if len(name) > 5:
            return name

    return None


if __name__ == "__main__":
    # Ищем паспорт в нескольких возможных местах
    possible_paths = [
        Path(__file__).parent.parent / "OCR_prices" / "Документы от заказчика" / "ТЗ и паспорта" / "Модуль" / "Pasport-MPP-9 (1).pdf",
        Path("./data/OCR_prices/Документы от заказчика/ТЗ и паспорта/Модуль/Pasport-MPP-9 (1).pdf"),
    ]

    pdf_path = None
    for p in possible_paths:
        if p.exists():
            pdf_path = p
            break

    if not pdf_path:
        print("❌ Паспорт не найден. Проверьте путь к папке OCR_prices")
        exit(1)

    print("=" * 60)
    print("ТЕСТ: Извлечение номера сертификата из паспорта")
    print("=" * 60)
    print(f"Файл: {pdf_path.name}")

    with pdfplumber.open(str(pdf_path)) as pdf:
        print(f"Страниц: {len(pdf.pages)}")

        # Наименование изделия с 1-й страницы
        text1 = pdf.pages[0].extract_text()
        product = find_product_name(text1)
        print(f"\n📦 Наименование изделия (с 1-й стр.): {product}")

        # Ищем номер сертификата на всех страницах
        print("\n🔍 Поиск номера сертификата:")
        found = False
        for page_num in range(len(pdf.pages)):
            text = pdf.pages[page_num].extract_text()
            nums = find_cert_numbers(text)
            if nums:
                print(f"   ✅ Найден на странице {page_num + 1}: {nums[0]}")
                print(f"   🔢 Нормализованный: {normalize_number(nums[0])}")
                found = True
                break
            else:
                print(f"   Стр. {page_num + 1}: не найден")

        if not found:
            print("   ❌ Номер не найден ни на одной странице")

    print("\n" + "=" * 60)
    print("ГОТОВО!")
    print("=" * 60)
