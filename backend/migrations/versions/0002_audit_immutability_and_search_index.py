"""audit log becomes append-only at the database level; trigram index for search on Postgres

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("""
            CREATE FUNCTION caseops_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'integrity_constraint_violation';
            END $$;
        """)
        op.execute("CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
                   "FOR EACH ROW EXECUTE FUNCTION caseops_audit_append_only()")
        op.execute("CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
                   "FOR EACH STATEMENT EXECUTE FUNCTION caseops_audit_append_only()")
        # Makes the search endpoint's ILIKE '%term%' use an index instead of scanning every line.
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("CREATE INDEX ix_doc_lines_text_trgm ON doc_lines USING gin (text gin_trgm_ops)")
    elif dialect == "sqlite":
        for verb in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER audit_log_no_{verb.lower()} BEFORE {verb} ON audit_log "
                       "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_doc_lines_text_trgm")
        op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
        op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
        op.execute("DROP FUNCTION IF EXISTS caseops_audit_append_only()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
        op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
