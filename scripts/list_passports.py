"""
Скрипт для просмотра содержимого папки с паспортами.
"""
import os
import glob
from pathlib import Path

# Путь к папке с паспортами
folder = str(Path(__file__).resolve().parent.parent / "OCR_prices" / "Прайсы" / "прайсы" / "АО ИСТОЧНИК" / "Паспорта")

print(f"Проверяем папку: {folder}")
print(f"Существует: {os.path.exists(folder)}")

if os.path.exists(folder):
    files = os.listdir(folder)
    print(f"\nВсего файлов: {len(files)}")
    for f in files:
        print(f"  - {f}")
else:
    # Попробуем относительный путь
    folder2 = "OCR_prices/Прайсы/прайсы/АО ИСТОЧНИК/Паспорта"
    print(f"\nПробуем относительный путь: {folder2}")
    if os.path.exists(folder2):
        files = os.listdir(folder2)
        print(f"Всего файлов: {len(files)}")
        for f in files:
            print(f"  - {f}")
    else:
        print("Папка не найдена!")
