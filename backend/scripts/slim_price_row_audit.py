#!/usr/bin/env python3
"""Remove per-row payloads from a pretty-printed reconciliation JSON.

The aggregate counters and per-price verdict inputs remain unchanged.  The
full audit can be kept locally, while report rendering does not need to load
hundreds of megabytes of already classified item rows into memory.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def slim(source: Path, destination: Path) -> None:
    skipping_rows = False
    with source.open("r", encoding="utf-8") as reader, destination.open(
        "w", encoding="utf-8"
    ) as writer:
        for line in reader:
            if not skipping_rows and line == '      "rows": [\n':
                writer.write('      "rows": []\n')
                skipping_rows = True
                continue
            if skipping_rows:
                if line == "      ]\n":
                    skipping_rows = False
                continue
            writer.write(line)
    if skipping_rows:
        raise ValueError("Незавершённый массив rows в исходном аудите")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    slim(args.source, args.destination)


if __name__ == "__main__":
    main()
