"""Safe on-disk review queues for corpus files that need follow-up."""
from __future__ import annotations

from pathlib import Path

from app.corpus_service import collect_files, queue_problem_file


def test_problem_files_are_copied_to_queue_and_not_collected_again(tmp_path: Path) -> None:
    source = tmp_path / "pdf" / "price.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-1.4 test")

    queued = queue_problem_file(tmp_path, source, "complex")

    assert queued == tmp_path / "complex" / "pdf" / "price.pdf"
    assert queued.read_bytes() == source.read_bytes()
    assert collect_files(tmp_path) == [source]


def test_ocr_queue_uses_need_ocr_directory_and_ignores_other_statuses(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 scan")

    assert queue_problem_file(tmp_path, source, "needs_ocr") == tmp_path / "need_ocr" / "scan.pdf"
    assert queue_problem_file(tmp_path, source, "validated") is None
