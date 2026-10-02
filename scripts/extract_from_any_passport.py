"""
Универсальный скрипт извлечения наименования изделия из PDF-паспорта.

Использование:
    python scripts/extract_from_any_passport.py "путь/к/паспорту.pdf"

Примеры:
    python scripts/extract_from_any_passport.py "C:/Users/.../Pasport-GGPT-7.pdf"
    python scripts/extract_from_any_passport.py "OCR_prices/Паспорта/МойПаспорт.pdf"
"""

import pdfplumber
import re
import sys
from pathlib import Path


def _clean_name(name: str) -> str:
    """Очистить и обрезать наименование."""
    name = name.strip()
    name = ' '.join(name.split())
    # Убираем маркеры списка в начале
    name = re.sub(r'^[□\-\*•]\s*', '', name)
    # Обрезаем по служебным словам, если они есть
    for cut_word in ['Руководство по эксплуатации', 'РУКОВОДСТВО ПО ЭКСПЛУАТАЦИИ', 'ПБАК', 'Настоящее', 'В комплект', 'КОРПУС ОГНЕТУШИТЕЛЯ', 'Срок службы']:
        idx = name.find(cut_word)
        if idx != -1:
            name = name[:idx].strip()
    # Обрезаем до разумной длины
    if len(name) > 80:
        name = name[:80]
    return name


def _is_valid_candidate(candidate: str) -> bool:
    """Проверить, что строка похожа на название изделия."""
    if not candidate or len(candidate) < 5:
        return False
    if any(x in candidate.lower() for x in ['www.', '.com', '.ru', 'тел.', 'факс', 'адрес', 'россия', 'ул.', 'дата', 'лист']):
        return False
    if re.match(r'^[\d\s\-\(\)\.]+$', candidate):
        return False
    if not re.search(r'[А-ЯA-Z]', candidate):
        return False
    return True


def find_product_name(text: str) -> str:
    """Извлечь наименование изделия с 1-й страницы паспорта."""
    if not text:
        return None

    lines = text.split('\n')

    # Способ 1: найти индекс строки "Паспорт" и взять строки ПЕРЕД ней
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^(Паспорт|ПАСПОРТ)\b', stripped, re.IGNORECASE):
            product_lines = []
            for j in range(max(0, i - 3), i):
                candidate = lines[j].strip()
                if _is_valid_candidate(candidate):
                    product_lines.append(candidate)
            if product_lines:
                name = _clean_name(' '.join(product_lines))
                if name:
                    return name

    # Способ 2: текст до слова "Паспорт"
    match = re.search(
        r'([А-ЯA-Z][А-Яа-яa-zA-Z\s\-\(\)\.0-9]{5,80})\n?\s*(?:Паспорт|ПАСПОРТ)',
        text, re.IGNORECASE
    )
    if match:
        name = _clean_name(match.group(1))
        if name and len(name) > 5:
            return name

    # Способ 3: текст до "Руководство по эксплуатации"
    match = re.search(
        r'([А-ЯA-Z][А-Яа-яa-zA-Z\s\-\(\)\.0-9«»"]{5,80})\n?\s*(?:Руководство по эксплуатации|РУКОВОДСТВО ПО ЭКСПЛУАТАЦИИ)',
        text, re.IGNORECASE
    )
    if match:
        name = _clean_name(match.group(1))
        if name and len(name) > 5:
            return name

    # Способ 4: первая непустая строка (если она похожа на название)
    for line in lines:
        candidate = line.strip()
        if _is_valid_candidate(candidate):
            name = _clean_name(candidate)
            if name:
                return name

    return None


def extract_from_passport(pdf_path: Path) -> dict:
    """Извлечь наименование из паспорта. Возвращает словарь с результатом."""
    result = {
        'file': pdf_path.name,
        'path': str(pdf_path),
        'product_name': None,
        'pages': 0,
        'error': None
    }

    if not pdf_path.exists():
        result['error'] = 'Файл не найден'
        return result

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            result['pages'] = len(pdf.pages)
            text1 = pdf.pages[0].extract_text()
            result['product_name'] = find_product_name(text1)
    except Exception as e:
        result['error'] = str(e)

    return result


def process_passport(pdf_path: Path):
    """Обработать один PDF-паспорт и вывести в консоль."""
    result = extract_from_passport(pdf_path)

    print("=" * 60)
    print(f"📄 Файл: {result['file']}")
    print(f"📂 Путь: {result['path']}")
    print("=" * 60)
    print(f"📄 Страниц: {result['pages']}")

    if result['error']:
        print(f"\n❌ Ошибка: {result['error']}")
    else:
        print(f"\n📦 Наименование изделия: {result['product_name']}")

    print("\n" + "=" * 60)
    print("ГОТОВО!")
    print("=" * 60)


if __name__ == "__main__":
    # Проверяем аргументы командной строки
    if len(sys.argv) < 2:
        print("Использование:")
        print('  python scripts/extract_from_any_passport.py "путь/к/паспорту.pdf"')
        print()
        print("Примеры:")
        print('  python scripts/extract_from_any_passport.py "C:/Users/.../Pasport-GGPT-7.pdf"')
        print('  python scripts/extract_from_any_passport.py "OCR_prices/Паспорта/МойПаспорт.pdf"')
        sys.exit(1)

    # Берём путь из аргумента
    pdf_path = Path(sys.argv[1])

    # Если путь относительный, ищем от корня репозитория
    if not pdf_path.is_absolute():
        repo_root = Path(__file__).parent.parent
        pdf_path = repo_root / pdf_path

    process_passport(pdf_path)
