"""Контракт эмбеддера: длина вектора, устойчивость, подменяемость."""

from __future__ import annotations

from app.matching.embedder import (
    VECTOR_DIM,
    TrigramEmbedder,
    build_embedder,
    cosine,
)


def test_trigram_vector_has_column_dimension():
    """Длина вектора продиктована колонкой pgvector, а не удобством модели."""
    vectors = TrigramEmbedder().encode(["Огнетушитель ОП-4 (з)"])

    assert len(vectors) == 1
    assert len(vectors[0]) == VECTOR_DIM


def test_trigram_vector_is_stable_between_calls():
    first = TrigramEmbedder().encode(["Рукав пожарный РПК-50"])
    second = TrigramEmbedder().encode(["Рукав пожарный РПК-50"])

    assert first == second


def test_unknown_provider_falls_back_to_trigram():
    """Опечатка в настройке не должна оставлять подбор без эмбеддера."""
    assert build_embedder("нет такого").name == "trigram-hash"
    assert build_embedder("").name == "trigram-hash"


def test_model_provider_is_selected_by_name():
    embedder = build_embedder("mpnet")

    assert "mpnet" in embedder.name
    # Веса не грузятся при создании: воркер, который эмбеддинги не считает, не
    # должен платить за гигабайт в памяти.
    assert embedder._model is None


def test_cosine_of_orthogonal_and_identical_vectors():
    left = [1.0] + [0.0] * (VECTOR_DIM - 1)
    right = [0.0, 1.0] + [0.0] * (VECTOR_DIM - 2)

    assert cosine(left, left) == 1.0
    assert cosine(left, right) == 0.0
    assert cosine(left, []) == 0.0
