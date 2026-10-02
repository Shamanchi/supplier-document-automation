"""Нормализация наименований и векторизация для семантического поиска.

Вектор строится детерминированно из символьных триграмм, а не внешней моделью,
и на то две причины. Первая: подбор обязан работать без сети и без ключей —
иначе расчёт КП зависит от чужой доступности. Вторая: опечатки и переносы в
наименованиях («ОП-5(з)» против «ОП 5 з») ближе по символам, чем по смыслу, и
триграммы ловят это лучше, чем эмбеддинг общего назначения.

Вектор кладётся в pgvector и ищется оператором `<=>`; на SQLite (тесты) то же
сравнение считается в Python — результат совпадает, отличается только скорость.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata

# Размерность колонки в pgvector. Значение продиктовано архитектурой: локальный
# `paraphrase-multilingual-mpnet-base-v2` отдаёт 768 измерений, а колонка и
# HNSW-индекс у pgvector фиксированы по длине. Триграммный эмбеддер считает
# вектор той же длины — просто с большим числом корзин и меньшим числом
# коллизий, так что обе модели живут в одной таблице.
EMBEDDING_DIM = 768

NGRAM = 3

# Кириллические буквы, неотличимые от латинских: в прайсах и сертификатах их
# смешивают постоянно, и без сведения «ОП» и «OП» окажутся разными товарами.
HOMOGLYPHS = str.maketrans(
    {
        "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
        "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    }
)

# Слова, которые есть почти в каждой строке прайса и потому ничего не
# различают. Из ключа сравнения они выбрасываются, из текста — нет.
STOPWORDS = frozenset(
    {
        "ОГНЕТУШИТЕЛЬ", "ОГНЕТУШИТЕЛИ", "ШКАФ", "ШКАФЫ", "МОДУЛЬ", "МОДУЛИ",
        "ПОЖАРНЫЙ", "ПОЖАРНАЯ", "ПОЖАРНОЕ", "ПОЖАРНЫЕ", "ДЛЯ", "И", "С", "ПО",
        "ШТ", "ШТУК", "КОМПЛЕКТ", "НАБОР", "ТИП", "МАРКА", "МОДЕЛЬ",
    }
)

_TOKEN = re.compile(r"[A-ZА-Я0-9]+(?:[.,/-][A-ZА-Я0-9]+)*")
_SPACE = re.compile(r"\s+")


def fold(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").upper().replace("Ё", "Е")
    return text.translate(HOMOGLYPHS)


def normalize_name(value: str) -> str:
    """Каноническая форма наименования: регистр, омоглифы, пробелы, шум.

    Пробелы схлопываются только вокруг дефиса и слэша — это разделители внутри
    обозначений («ОП - 4» и «ОП-4» одно и то же). Точка и запятая так не
    схлопываются: «акрил. RAL 3002» превращалось в «АКРИЛ.RAL», и дальше ни
    поиск по токенам, ни триграммы этого слова уже не находили.
    """
    text = fold(value)
    text = re.sub(r"[«»\"'()\[\]{}]", " ", text)
    text = re.sub(r"\s*([-/])\s*", r"\1", text)
    text = re.sub(r"([.,;:])(?=\S)", r"\1 ", text)
    return _SPACE.sub(" ", text).strip()


def tokens(value: str) -> list[str]:
    return _TOKEN.findall(normalize_name(value))


def significant_tokens(value: str) -> list[str]:
    """Токены без общих слов — то, чем позиции реально различаются."""
    return [token for token in tokens(value) if token not in STOPWORDS]


# Обозначение изделия внутри наименования: «ОП-4», «МПП-2,5», «К-01», «ШПК-320».
# Именно оно, а не длинное слово вроде «огнетушитель», отличает одну позицию от
# другой, поэтому по нему идёт отдельная ступень каскада.
DESIGNATION = re.compile(r"\b[A-ZА-Я]{1,8}-\d[\dA-ZА-Я,./]*\b")


def designations(value: str) -> list[str]:
    """Обозначения, встреченные в тексте, в порядке появления."""
    found: dict[str, None] = {}
    for match in DESIGNATION.finditer(normalize_name(value)):
        found.setdefault(match.group(0), None)
    return list(found)


def normalize_article(value: str | None) -> str:
    """Ключ артикула: без разделителей, регистр и омоглифы сведены.

    Разделители убираются намеренно: один и тот же артикул поставщики пишут
    как `МПП-2.5`, `МПП 2,5` и `МПП2.5`, и это одна позиция.
    """
    if not value:
        return ""
    text = fold(value)
    # Кириллица остаётся: после сведения омоглифов в артикуле живут и «П», и
    # «Б», и «Ж» — выбрасывать их значит превратить «ОП-5» в «O5».
    return re.sub(r"[^A-ZА-Я0-9]", "", text)


# Артикул короче трёх символов не идентифицирует товар, а без цифры это
# обычно обрывок наименования («ОП», «ШПК»), а не артикул.
MIN_ARTICLE_KEY = 3


def article_key(value: str | None) -> str:
    """Артикул, пригодный как ключ слияния и поиска; иначе пустая строка."""
    key = normalize_article(value)
    if len(key) < MIN_ARTICLE_KEY or not any(char.isdigit() for char in key):
        return ""
    return key


def ngrams(value: str) -> list[str]:
    text = f" {normalize_name(value)} "
    if len(text) <= NGRAM:
        return [text]
    return [text[index : index + NGRAM] for index in range(len(text) - NGRAM + 1)]


def embed(value: str) -> list[float]:
    """Нормированный вектор символьных триграмм с хешированием в EMBEDDING_DIM.

    Хеш берётся устойчивый (blake2s), а не встроенный `hash()`: тот солится на
    каждый процесс, и вектор одного и того же текста менялся бы между
    перезапусками — индекс в базе стал бы мусором после рестарта.
    """
    vector = [0.0] * EMBEDDING_DIM
    for gram in ngrams(value):
        digest = hashlib.blake2s(gram.encode("utf-8"), digest_size=4).digest()
        bucket = int.from_bytes(digest, "big") % EMBEDDING_DIM
        # Знак от старшего бита: без него все веса положительные и разные
        # тексты сходятся к одному «среднему» направлению.
        sign = 1.0 if digest[0] & 1 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))
