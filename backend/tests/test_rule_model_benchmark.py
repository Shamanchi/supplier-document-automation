"""Scoring contract for comparing model proposals with reviewed recipes."""
from app.rule_model_benchmark import (
    choose_sample_rows_winner,
    choose_winner,
    score_mapping,
    summarize_by_sample_rows,
)


def test_mapping_score_compares_only_semantic_gold_columns() -> None:
    gold = {
        "header_row": 4,
        "columns": [
            {"index": 0, "role": "name"},
            {"index": 1, "role": "ignore"},
            {"index": 2, "role": "price"},
            {"index": 4, "role": "name"},
            {"index": 6, "role": "price"},
        ],
    }
    proposal = {
        "header_row_index": 4,
        "columns": [
            {"index": 0, "role": "name"},
            {"index": 2, "role": "price"},
            {"index": 4, "role": "name"},
            {"index": 6, "role": "price"},
            {"index": 1, "role": "attribute"},
        ],
    }

    score = score_mapping(gold, proposal)

    assert score["header_row_exact"] is True
    assert score["semantic_role_f1"] == 1.0
    assert score["name_price_columns_exact"] is True


def test_winner_prefers_more_locally_valid_cases_before_latency() -> None:
    summary = {
        "large": {"valid_cases": 9, "mean_semantic_role_f1": 0.98, "mean_latency_seconds": 8.0},
        "small": {"valid_cases": 10, "mean_semantic_role_f1": 0.96, "mean_latency_seconds": 2.0},
    }

    assert choose_winner(summary) == "small"


def test_sample_row_summary_reports_delta_against_one_row() -> None:
    results = [
        {
            "sample_rows_requested": 1,
            "valid": False,
            "schema_valid": True,
            "compiler_valid": False,
            "dry_run_valid": False,
            "name_price_columns_exact": False,
            "semantic_role_f1": 0.5,
            "latency_seconds": 2.0,
        },
        {
            "sample_rows_requested": 5,
            "valid": True,
            "schema_valid": True,
            "compiler_valid": True,
            "dry_run_valid": True,
            "name_price_columns_exact": True,
            "semantic_role_f1": 1.0,
            "latency_seconds": 3.0,
        },
    ]

    summary = summarize_by_sample_rows(results, row_counts=(1, 5))

    assert summary[1]["valid_cases"] == 0
    assert summary[5]["valid_cases"] == 1
    assert summary[5]["valid_cases_delta_vs_one"] == 1
    assert summary[5]["semantic_role_f1_delta_vs_one"] == 0.5


def test_sample_row_winner_prefers_fewer_rows_after_equal_quality() -> None:
    summary = {
        3: {"valid_cases": 8, "mean_semantic_role_f1": 0.95},
        5: {"valid_cases": 8, "mean_semantic_role_f1": 0.95},
    }

    assert choose_sample_rows_winner(summary) == 3
