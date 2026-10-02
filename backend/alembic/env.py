"""Alembic environment: runs migrations over a synchronous psycopg2 engine.

The app itself uses an async engine (asyncpg); for migrations the async URL
from DATABASE_URL is converted to its sync psycopg2 form so `alembic upgrade
head` works from a shell or the container entrypoint without an event loop.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import settings
from app.models import Base
from app.modules import identity  # noqa: F401 — регистрирует модели фазы 3 в метаданных

# Шаг миграции 03: ордерный домен фазы 3 — таблицы должны попасть в
# target_metadata, иначе autogenerate шага 05 их не увидит. Импортируются
# именно подпакеты models: `__init__` модулей (кроме identity) их не тянет.
from app.modules.catalogs import models as _catalogs_models  # noqa: F401
from app.modules.certificates import models as _certificates_models  # noqa: F401
from app.modules.checks import models as _checks_models  # noqa: F401
from app.modules.documents import models as _documents_models  # noqa: F401
from app.modules.extraction import models as _extraction_models  # noqa: F401
from app.modules.integrations.bitrix import models as _integrations_models  # noqa: F401
from app.modules.normalization import models as _normalization_models  # noqa: F401
from app.modules.order_matching import models as _order_matching_models  # noqa: F401
from app.modules.parameters import models as _parameters_models  # noqa: F401
from app.modules.pipeline import models as _pipeline_models  # noqa: F401
from app.modules.reporting import models as _reporting_models  # noqa: F401
from app.modules.rules import models as _rules_models  # noqa: F401
from app.modules.verdicts import models as _verdicts_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    url = settings.database_url.replace("+asyncpg", "+psycopg2")
    # Шаг миграции 05: `alembic -x db=<имя>` — прогон на тестовой БД того же
    # кластера (merge_test) без правки DATABASE_URL (docs/migration/05-migrations.md).
    override_db = context.get_x_argument(as_dictionary=True).get("db")
    if override_db:
        head, _, old_db = url.rpartition("/")
        url = f"{head}/{override_db}"
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
