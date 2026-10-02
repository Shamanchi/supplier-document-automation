"""
Скрипт для просмотра текста всех страниц паспорта.
Помогает понять, как написан номер сертификата.
"""
import pdfplumber
import sys

if len(sys.argv) < 2:
    print("Использование: python show_text.py <путь_к_PDF>")
    sys.exit(1)

pdf_path = sys.argv[1]

with pdfplumber.open(pdf_path) as pdf:
    print(f"Всего страниц: {len(pdf.pages)}\n")
    for i, page in enumerate(pdf.pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            print(f"=== СТРАНИЦА {i} ===")
            print(text)
            print("\n")
