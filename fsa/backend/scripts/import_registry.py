#!/usr/bin/env python3
"""Импорт зеркала реестра Росаккредитации в Postgres.

Источники — артефакты, собранные скриптами
`Nikolay_Stepankov/tools/fetch_fsa_registry_mirror.py` и
`build_fsa_registry_index.py`:

* `registry_index.csv` — карточки пожарного сегмента;
* `registry_all_numbers.txt` — все номера реестра, нужны чтобы отличать
  «номера нет» от «есть, но карточки не держим».

Загрузка идёт через COPY во временную таблицу и UPSERT: 54 тыс. карточек и
2.6 млн номеров построчными INSERT грузились бы минутами.

    python3 scripts/import_registry.py --mirror ../../fsa_registry_mirror
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.fsa.mapping import DATE_FIELDS, INDEX_COLUMNS  # noqa: E402
from app.fsa.normalize import normalize_number, parse_date  # noqa: E402
from app.fsa.schema import CertificateStatus  # noqa: E402

COLUMNS = (
    "id",
    "registration_number_key",
    "registration_number",
    "status",
    "status_raw",
    *[name for name in INDEX_COLUMNS if name not in {"registration_number", "status"}],
    "source_slice",
    "imported_at",
)


def sync_dsn() -> str:
    return settings.database_url.replace("+asyncpg", "")


def load_certificates(connection, path: Path) -> int:
    csv.field_size_limit(2**31 - 1)
    now = datetime.now(timezone.utc).isoformat()

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
    seen: set[str] = set()
    total = 0

    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            number = (row.get(INDEX_COLUMNS["registration_number"]) or "").strip()
            if not number:
                continue
            key = (row.get("Ключ номера") or normalize_number(number)).strip()
            # Индекс собирается склейкой срезов, дубликаты ключа возможны;
            # ON CONFLICT их бы отсёк, но COPY внутри одной пачки — нет.
            if key in seen:
                continue
            seen.add(key)

            status_raw = (row.get(INDEX_COLUMNS["status"]) or "").strip()
            values: list[str] = [
                str(uuid.uuid4()),
                key,
                number,
                CertificateStatus.parse(status_raw).value,
                status_raw,
            ]
            for name in COLUMNS[5:-2]:
                raw = (row.get(INDEX_COLUMNS[name]) or "").strip()
                if name in DATE_FIELDS:
                    parsed = parse_date(raw)
                    values.append(parsed.isoformat() if parsed else "\\N")
                else:
                    values.append(raw.replace("\t", " ").replace("\n", " "))
            values.append((row.get("Источник среза") or "").strip())
            values.append(now)
            writer.writerow(values)
            total += 1

    buffer.seek(0)
    with connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE tmp_cert (LIKE registry_certificate) ON COMMIT DROP")
        cursor.copy_expert(
            "COPY tmp_cert (" + ", ".join(COLUMNS) + ") FROM STDIN WITH "
            # NULL-маркер задан явно: пустая строка в CSV означает пустой
            # текст, а не отсутствие даты.
            "(FORMAT csv, DELIMITER E'\t', NULL '\\N')",
            buffer,
        )
        cursor.execute(
            f"""
            INSERT INTO registry_certificate ({', '.join(COLUMNS)})
            SELECT {', '.join(COLUMNS)} FROM tmp_cert
            ON CONFLICT (registration_number_key) DO UPDATE SET
              {', '.join(f'{name} = EXCLUDED.{name}' for name in COLUMNS if name not in {'id', 'registration_number_key'})}
            """
        )
    return total


def load_numbers(connection, path: Path) -> int:
    buffer = io.StringIO()
    total = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            key = line.strip()
            if key:
                buffer.write(key + "\n")
                total += 1
    buffer.seek(0)
    with connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE tmp_num (registration_number_key text) ON COMMIT DROP")
        cursor.copy_expert("COPY tmp_num FROM STDIN", buffer)
        cursor.execute(
            """
            INSERT INTO registry_number (registration_number_key)
            SELECT DISTINCT registration_number_key FROM tmp_num
            ON CONFLICT DO NOTHING
            """
        )
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mirror",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "fsa_registry_mirror",
    )
    parser.add_argument("--skip-numbers", action="store_true")
    args = parser.parse_args()

    index_path = args.mirror / "registry_index.csv"
    numbers_path = args.mirror / "registry_all_numbers.txt"
    if not index_path.exists():
        raise SystemExit(f"нет файла {index_path}; сначала собрать зеркало")

    started = datetime.now(timezone.utc)
    connection = psycopg2.connect(sync_dsn())
    try:
        certificates = load_certificates(connection, index_path)
        print(f"карточек загружено: {certificates}")
        numbers = 0
        if not args.skip_numbers and numbers_path.exists():
            numbers = load_numbers(connection, numbers_path)
            print(f"номеров загружено: {numbers}")
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO registry_import
                  (id, source, certificates_loaded, numbers_loaded, started_at, finished_at, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    str(args.mirror),
                    certificates,
                    numbers,
                    started,
                    datetime.now(timezone.utc),
                    "",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
