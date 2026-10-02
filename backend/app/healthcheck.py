"""Container healthcheck без внешних зависимостей (только stdlib).

Используется в compose как:
    python -m app.healthcheck --url http://127.0.0.1:8000/health/ready
Exit 0 при HTTP 200, иначе 1.
"""

import argparse
import sys
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    try:
        with urllib.request.urlopen(args.url, timeout=args.timeout) as response:
            return 0 if response.status == 200 else 1
    except Exception:  # noqa: BLE001 — любой сбой = unhealthy
        return 1


if __name__ == "__main__":
    sys.exit(main())
