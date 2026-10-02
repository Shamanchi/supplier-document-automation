"""Эмбеддеры наименований: детерминированный триграммный и локальная модель.

Архитектура предписывает локальный `paraphrase-multilingual-mpnet-base-v2`
(768 измерений): он связывает «спринклер» с «оросителем» и «закачной» с «(з)»,
чего символьные триграммы не умеют. Триграммный эмбеддер остаётся рядом не как
временная заглушка, а как рабочий режим для окружения без модели: подбор не
должен переставать работать оттого, что веса не выкачаны.

Выбор делается переменной `EMBEDDING_PROVIDER`. Имя, ревизия и версия
нормализатора попадают в ключ `item_embeddings`, поэтому смена провайдера не
переписывает историю — новые векторы ложатся рядом со старыми.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Protocol

from app.matching.text import EMBEDDING_DIM, embed as trigram_embed

# Размерность колонки в базе. Обе модели обязаны отдавать вектор этой длины:
# у pgvector размерность фиксируется на колонке и индексе.
VECTOR_DIM = EMBEDDING_DIM

MPNET_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"


class Embedder(Protocol):
    name: str
    revision: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class TrigramEmbedder:
    """Символьные триграммы с устойчивым хешированием.

    Работает без сети и без весов, поэтому годится и для тестов, и для стенда,
    где модель не развёрнута.
    """

    name: str = "trigram-hash"
    revision: str = "v1"

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [trigram_embed(text) for text in texts]


class SentenceTransformerEmbedder:
    """Локальная модель предложений; веса берутся из кэша, сеть не нужна.

    Модель грузится один раз на процесс и лениво: воркер, который никогда не
    считает эмбеддинги, не должен платить за 1 ГБ весов в памяти.
    """

    def __init__(self, model_name: str = MPNET_MODEL, revision: str = "v1") -> None:
        self.name = model_name
        self.revision = revision
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(self.name)
                    dimension = self._model.get_sentence_embedding_dimension()
                    if dimension != VECTOR_DIM:
                        raise RuntimeError(
                            f"модель {self.name} отдаёт {dimension} измерений, "
                            f"а колонка рассчитана на {VECTOR_DIM}"
                        )
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure()
        vectors = model.encode(
            texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
        )
        return [[float(value) for value in vector] for vector in vectors]


_ACTIVE: Embedder | None = None
_ACTIVE_LOCK = threading.Lock()


def build_embedder(provider: str = "") -> Embedder:
    """Эмбеддер по имени провайдера; при неизвестном имени — триграммный."""
    if provider.strip().lower() in {"mpnet", "sentence-transformers", "model"}:
        return SentenceTransformerEmbedder()
    return TrigramEmbedder()


def active_embedder() -> Embedder:
    """Эмбеддер, выбранный конфигурацией процесса."""
    global _ACTIVE
    if _ACTIVE is None:
        with _ACTIVE_LOCK:
            if _ACTIVE is None:
                from app.config import settings

                _ACTIVE = build_embedder(getattr(settings, "embedding_provider", ""))
    return _ACTIVE


def set_embedder(embedder: Embedder | None) -> None:
    """Подменить активный эмбеддер — для тестов и разовых прогонов."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = embedder


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    # Векторы обеих моделей нормированы, но защита от ненормированного входа
    # дешевле, чем молчаливо неверная оценка похожести.
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    if norm == 0.0:
        return 0.0
    return dot / norm if abs(norm - 1.0) > 1e-6 else dot
