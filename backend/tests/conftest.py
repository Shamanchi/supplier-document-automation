"""Общие фикстуры тестов, перенесённые из фазы 3 (шаг миграции 04).

Тесты интеграции Bitrix24 работают с настоящей PostgreSQL: схема содержит
enum-типы и составные внешние ключи, которых нет в SQLite. Пайс-тесты фазы 2
продолжают использовать свой локальный паттерн (sqlite+aiosqlite в каждом
тесте) — этот conftest добавляет только то, что нужно контрактным тестам
фазы 3, и не меняет поведение остальных.

База берётся из TEST_DATABASE_URL. Если переменная не задана, тесты, которым
нужна БД, пропускаются — так набор остаётся запускаемым без Docker.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL не задан: тесты с БД пропущены",
)

if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/none")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")


def _sync_url() -> str:
    """Async-драйвер приложения → sync-драйвер для подготовки данных."""
    return (TEST_DATABASE_URL or "").replace("+asyncpg", "+psycopg2")


@pytest.fixture
def db() -> Iterator:
    """Синхронное подключение для подготовки и проверки данных.

    `connect()`, а не `begin()`: тест часто готовит данные в несколько приёмов,
    коммитя каждый — между вызовами API, которые должны эти данные видеть. Внутри
    `begin()` первый же `commit()` закрывает транзакцию контекст-менеджера, и
    следующий `execute` падает с «Can't operate on closed transaction». Незакоммиченные
    изменения при выходе откатываются — это и нужно.
    """
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url(), future=True)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


@pytest.fixture
def clean_users() -> Iterator[None]:
    """Пустая таблица пользователей до и после теста.

    CASCADE снимает и зависимые строки (роли, сессии, заявки, журнал), поэтому
    каждый тест видит одинаковое исходное состояние.

    `role_permissions` указана явно: ссылок на `users` у неё нет, поэтому CASCADE
    её не задевает, и правка прав из одного теста протекала бы в следующие.
    Startup приложения заполняет таблицу базовыми наборами заново.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(_sync_url(), future=True)

    def wipe() -> None:
        with engine.begin() as connection:
            connection.execute(
                text("TRUNCATE users, check_batches, role_permissions CASCADE")
            )

    wipe()
    yield
    wipe()
    engine.dispose()


@pytest.fixture(autouse=True)
def dispose_engine() -> Iterator[None]:
    """Сбрасывает пул соединений между тестами.

    asyncpg привязывает соединение к event loop, а TestClient создаёт новый loop
    на каждый тест: соединения из пула предыдущего теста становятся нерабочими,
    и запросы падают внутри startup, где ошибка гасится.
    """
    import asyncio

    from app.db import engine

    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def reset_login_limiter() -> None:
    """Сбрасывает счётчик неудачных входов между тестами.

    Ограничитель живёт в процессе, поэтому тест на превышение лимита иначе
    блокировал бы вход в последующих тестах.
    """
    from app.modules.identity.router import _limiter

    _limiter._failures.clear()


@pytest.fixture
def client(clean_users: None) -> Iterator:
    """TestClient с поднятым приложением: startup создаёт стартовые учётки."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
