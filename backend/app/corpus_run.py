"""CLI: run a corpus directory through the parser in-process.

    python -m app.corpus_run <dir> [--force]

Creates a corpus_runs row and executes the run synchronously (no celery
worker required); prints the run id and final totals.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.corpus_service import run_corpus
from app.db import make_task_engine
from app.models import CorpusRun


async def _main(path: str, force: bool) -> int:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            run = CorpusRun(source_path=path, status="running")
            session.add(run)
            await session.commit()
            run_id = run.id
        print(f"corpus_run_id: {run_id}", flush=True)
        status = await run_corpus(session_factory, run_id, path, force)
        async with session_factory() as session:
            run = await session.get(CorpusRun, run_id)
            print(f"status: {status}")
            print(json.dumps(run.totals, ensure_ascii=False, indent=2))
            if run.report_storage_key:
                print(f"report: {run.report_storage_key}")
        return 0 if status == "finished" else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Прогон корпуса прайсов")
    parser.add_argument("path", help="Каталог с файлами прайсов")
    parser.add_argument("--force", action="store_true", help="Пересоздавать версии для уже загруженных файлов")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.path, args.force)))
