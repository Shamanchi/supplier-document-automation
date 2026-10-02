#!/usr/bin/env python3
"""Stream unresolved row details from a large reconciliation audit as JSONL."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


PROBLEM_CLASSES = {"pending_model_rule", "parser_issue", "unclassified"}


def extract(source: Path, destination: Path) -> int:
    current_price: str | None = None
    in_rows = False
    object_lines: list[str] = []
    written = 0
    with source.open("r", encoding="utf-8") as reader, destination.open(
        "w", encoding="utf-8"
    ) as writer:
        for line in reader:
            if not in_rows and line.startswith('      "price": '):
                current_price = json.loads(line.split(":", 1)[1].rstrip().rstrip(","))
                continue
            if not in_rows and line == '      "rows": [\n':
                in_rows = True
                continue
            if not in_rows:
                continue
            if line == "      ]\n":
                in_rows = False
                continue
            if line == "        {\n":
                object_lines = [line]
                continue
            if not object_lines:
                continue
            object_lines.append(line)
            if line in {"        },\n", "        }\n"}:
                raw = "".join(object_lines).rstrip()
                if raw.endswith(","):
                    raw = raw[:-1]
                row = json.loads(raw)
                object_lines = []
                if row.get("class") not in PROBLEM_CLASSES:
                    continue
                writer.write(
                    json.dumps({"price": current_price, **row}, ensure_ascii=False)
                    + "\n"
                )
                written += 1
    if in_rows or object_lines:
        raise ValueError("Незавершённый rows/object в исходном аудите")
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(extract(args.source, args.destination))


if __name__ == "__main__":
    main()
