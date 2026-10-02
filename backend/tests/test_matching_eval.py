"""Гейт качества подбора: голдсет не должен ухудшаться молча.

Цифры подбора живут в `tests/data/matching_baseline.json` и обновляются
осознанно — вместе с изменением каскада, а не «потому что тест упал». Допуск
взят небольшой: голдсет детерминированный, любое падение метрики означает
изменение поведения, а не шум.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scripts.eval_matching import GOLDSET, run

BASELINE = json.loads(
    (Path(__file__).parent / "data" / "matching_baseline.json").read_text(encoding="utf-8")
)

# Метрика может просесть на один кейс из-за правки нормализации и это не повод
# ронять сборку; падение глубже означает регресс подбора.
TOLERANCE = 0.02


@pytest.mark.skipif(not GOLDSET.exists(), reason="голдсет недоступен в этом окружении")
def test_goldset_metrics_do_not_regress():
    report = asyncio.run(run(GOLDSET, 0))
    summary = report["summary"]

    assert summary["cases"] == BASELINE["cases"]
    assert (
        summary["answerable"]["top1_accuracy"]
        >= BASELINE["answerable"]["top1_accuracy"] - TOLERANCE
    )
    assert (
        summary["answerable"]["top3_recall"]
        >= BASELINE["answerable"]["top3_recall"] - TOLERANCE
    )
    assert (
        summary["auto_decisions"]["precision"]
        >= BASELINE["auto_decisions"]["precision"] - TOLERANCE
    )
    # Автоприём — единственное решение, уходящее в КП без человека, поэтому
    # число ошибочных автоприёмов проверяется отдельно от средней точности.
    assert summary["auto_decisions"]["cases"] - summary["auto_decisions"]["correct"] == 0


@pytest.mark.skipif(not GOLDSET.exists(), reason="голдсет недоступен в этом окружении")
def test_absent_products_stay_not_found():
    """Товар не из прайсов не должен получать кандидата.

    Пятнадцать кейсов голдсета — промышленное оборудование из реальной заявки,
    которого в прайсах нет вовсе. Любой ответ, кроме «не найдено», означает, что
    в КП поедет чужой товар.
    """
    report = asyncio.run(run(GOLDSET, 0))
    absent = report["summary"]["absent_from_catalog"]

    assert absent["not_found"] == absent["cases"]
    assert absent["auto"] == 0


@pytest.mark.skipif(not GOLDSET.exists(), reason="голдсет недоступен в этом окружении")
def test_forbidden_targets_never_win():
    """Запрещённый разметкой товар не должен становиться первым кандидатом.

    Это жёстче любой средней метрики: «ОП-4(г) газогенераторный» вместо
    «ОП-4 закачной» — не неточность, а другой товар в коммерческом предложении.
    """
    report = asyncio.run(run(GOLDSET, 0))

    assert report["summary"]["answerable"]["forbidden_top1"] == 0
