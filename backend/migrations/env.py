import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from backend.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the sync SQLAlchemy URL.

    Precedence: -x db_url passed programmatically > DATABASE_URL env var >
    alembic.ini. The app's async URL (sqlite+aiosqlite://) is converted to its
    sync form — Alembic runs migrations over a plain sync connection.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("db_url") or os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    return url.replace("sqlite+aiosqlite://", "sqlite://")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # render_as_batch: SQLite can't ALTER most things in place; batch mode
        # recreates tables safely for column-level changes.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
