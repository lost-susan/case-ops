"""Alembic environment. The URL comes from CASEOPS_DATABASE_URL (or a URL set programmatically on the config)."""
import os

from alembic import context
from sqlalchemy import create_engine, pool

from app import models  # noqa: F401  (registers every table on Base.metadata)
from app.db import Base

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """The trigram index exists only on Postgres (migration 0002), so autogenerate must not try to drop it."""
    return not (type_ == "index" and name == "ix_doc_lines_text_trgm")


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("CASEOPS_DATABASE_URL")
    if not url:
        raise RuntimeError("Set CASEOPS_DATABASE_URL to the database to migrate")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True,
                      include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                          include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
