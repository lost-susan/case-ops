"""Admin commands (there is deliberately no public registration endpoint).

  python -m app.cli migrate                    # create/upgrade the schema with Alembic (use this in production)
  python -m app.cli init-db                    # development shortcut: create_all, no migrations or audit triggers
  python -m app.cli create-user --email lawyer@firm.com --role attorney
  python -m app.cli add-member --case-id <uuid> --email paralegal@firm.com --role editor
  python -m app.cli reap [--minutes 15]        # re-run ingestion stuck in queued/processing (schedule it)
"""
import argparse
import getpass
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from app import models  # noqa: F401
from app.config import get_settings
from app.core.security import MIN_PASSWORD_LENGTH, hash_password
from app.db import Base, get_engine, init_engine, session_factory
from app.models.orm import Case, CaseMember, User
from app.models.schemas import LoginIn
from app.services import audit_logger


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("migrate")
    cu = sub.add_parser("create-user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--role", choices=["admin", "attorney", "paralegal"], default="attorney")
    am = sub.add_parser("add-member")
    am.add_argument("--case-id", required=True, type=uuid.UUID)
    am.add_argument("--email", required=True)
    am.add_argument("--role", choices=["owner", "editor", "viewer"], default="viewer")
    rp = sub.add_parser("reap")
    rp.add_argument("--minutes", type=int, default=None)
    args = p.parse_args(argv)

    settings = get_settings()
    if args.cmd == "migrate":
        from alembic import command
        from alembic.config import Config

        backend = Path(__file__).resolve().parents[1]
        cfg = Config(str(backend / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend / "migrations"))
        cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        print("Database is up to date.")
        return 0

    init_engine(settings.database_url)
    if args.cmd == "init-db":
        Base.metadata.create_all(get_engine())
        print("Tables created (development only: run `migrate` instead for real deployments).")
        return 0

    if args.cmd == "reap":
        from app.services.reaper import reap

        print(f"Processed {reap(get_settings(), args.minutes)} stuck document(s).")
        return 0

    with session_factory()() as db:
        if args.cmd == "create-user":
            email = LoginIn(email=args.email, password="x").email.lower()  # reuse the API's email validation
            password = getpass.getpass("Password: ")
            if len(password) < MIN_PASSWORD_LENGTH or password != getpass.getpass("Repeat: "):
                print(f"Passwords must match and be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
                return 1
            user = User(email=email, password_hash=hash_password(password), role=args.role)
            db.add(user)
            db.flush()
            audit_logger.append(db, action="user.created", detail={"user_id": str(user.id), "role": args.role})
            db.commit()
            print(f"Created {args.role} {email}")
        else:
            user = db.scalar(select(User).where(User.email == args.email.lower()))
            case = db.get(Case, args.case_id)
            if not user or not case:
                print("User or case not found.", file=sys.stderr)
                return 1
            db.merge(CaseMember(case_id=case.id, user_id=user.id, role=args.role))
            audit_logger.append(db, action="case.member_set", case_id=case.id,
                                detail={"user_id": str(user.id), "role": args.role})
            db.commit()
            print(f"{user.email} is now {args.role} on {case.docket}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
