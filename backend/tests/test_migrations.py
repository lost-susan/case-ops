"""The migrations must build exactly the schema the models describe, and make the audit log append-only."""
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.db import Base

BACKEND = Path(__file__).resolve().parents[1]
PG = os.environ.get("CASEOPS_TEST_DATABASE_URL", "")


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'mig.db'}"
        return
    if not PG.startswith("postgresql"):
        pytest.skip("set CASEOPS_TEST_DATABASE_URL to a Postgres URL to run the Postgres migration tests")
    schema = "mig_" + uuid.uuid4().hex[:10]
    admin = create_engine(PG)
    with admin.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    try:  # an isolated schema keeps this test away from the tables other tests use
        yield make_url(PG).update_query_string(f"options=-csearch_path={schema},public").render_as_string(hide_password=False)
    finally:
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def test_migrations_match_the_models_and_are_reversible(db_url):
    cfg = alembic_cfg(db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    with engine.connect() as conn:
        opts = {"compare_type": True,  # same filter as migrations/env.py: the trigram index is Postgres-only
                "include_object": lambda o, name, type_, reflected, cmp: not (type_ == "index" and name == "ix_doc_lines_text_trgm")}
        diff = compare_metadata(MigrationContext.configure(conn, opts=opts), Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"
    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert not [t for t in conn.dialect.get_table_names(conn) if t != "alembic_version"]
    command.upgrade(cfg, "head")  # and forward again
    engine.dispose()


def test_audit_log_is_append_only_in_the_database(db_url):
    command.upgrade(alembic_cfg(db_url), "head")
    engine = create_engine(db_url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_log (chain_key, seq, action, created_at, prev_hash, entry_hash) "
                       "VALUES ('global', 1, 'x', :now, :h, :h)"), {"now": "2026-01-01 00:00:00", "h": "0" * 64})
    for sql in ("UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError):
            with engine.begin() as c:
                c.execute(text(sql))
    if db_url.startswith("postgresql"):
        with pytest.raises(DBAPIError):
            with engine.begin() as c:
                c.execute(text("TRUNCATE audit_log"))
    with engine.connect() as c:
        assert c.execute(text("SELECT action FROM audit_log")).scalar_one() == "x"
    engine.dispose()
