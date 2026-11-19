"""``lexwatch`` console script.

Commands: migrate | serve | worker | run-job | rescore [--profile ID | --all] | download-model |
send-digest [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from lexwatch.config import Settings, get_settings
from lexwatch.logging import configure_logging

log = logging.getLogger("lexwatch.cli")


def _alembic_config(settings: Settings):
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    ini = root / "alembic.ini"
    if ini.exists():
        cfg = Config(str(ini))
    else:  # installed without the repo checkout (should not happen in the image)
        cfg = Config()
        cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


MIGRATION_LOCK_ID = 7_431_001  # pg advisory lock: app and worker may both migrate at start


def cmd_migrate(settings: Settings, args: argparse.Namespace) -> int:
    from alembic import command
    from sqlalchemy import create_engine, text

    cfg = _alembic_config(settings)
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            conn.commit()
            try:
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, args.revision)
                conn.commit()
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
                conn.commit()
    finally:
        engine.dispose()
    log.info("migrate.done", extra={"revision": args.revision})
    return 0


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "lexwatch.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
        # No client IP in the logs: the app logs http.request (method, path, status, user id).
        access_log=False,
    )
    return 0


def cmd_worker(settings: Settings, args: argparse.Namespace) -> int:
    from lexwatch.jobs.worker import run_worker

    run_worker(settings)
    return 0


def cmd_run_job(settings: Settings, args: argparse.Namespace) -> int:
    from lexwatch.jobs.daily import run_daily_job

    run_id = run_daily_job(settings, kind="manual")
    log.info("run-job.done", extra={"run_id": run_id})
    return 0


def cmd_rescore(settings: Settings, args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from lexwatch.db import get_engine, make_sessionmaker, session_scope
    from lexwatch.jobs.rescore import execute_rescore, queue_rescore_all
    from lexwatch.models import Profile
    from lexwatch.scoring.factory import get_scorer

    factory = make_sessionmaker(get_engine(settings.database_url))
    with session_scope(factory) as session:
        if args.all:
            run = queue_rescore_all(session)
            if run is None:
                print("a full rescore is already queued for the worker")
            else:
                print(f"queued run {run.id}: the worker will rescore every profile in full")
            return 0
        ids = (
            [args.profile]
            if args.profile is not None
            else list(session.scalars(select(Profile.id).order_by(Profile.id)))
        )
        scorer = get_scorer(settings)
        failed = 0
        for profile_id in ids:
            run = execute_rescore(session, settings, profile_id, scorer=scorer)
            print(
                f"profile {profile_id}: run {run.id} {run.status}, "
                f"{run.bills_scored} scored, {run.bills_unscored} unscored, "
                f"{run.billed_units} billed units"
                + (f" ({run.error_message})" if run.error_message else "")
            )
            failed += run.status != "success"
    return 1 if failed else 0


def cmd_download_model(settings: Settings, args: argparse.Namespace) -> int:
    from lexwatch.scoring.local import download_model

    download_model(settings)
    return 0


def cmd_send_digest(settings: Settings, args: argparse.Namespace) -> int:
    from lexwatch.db import get_engine, make_sessionmaker, session_scope
    from lexwatch.notify.digest import DigestError, send_digests

    factory = make_sessionmaker(get_engine(settings.database_url))
    with session_scope(factory) as session:
        try:
            sent = send_digests(session, dry_run=args.dry_run, settings=settings)
        except DigestError as exc:
            report = exc.report
            print(
                f"send-digest: {report.sent} sent, {len(report.failed)} failed, "
                f"{report.skipped} skipped (failed profiles: "
                + ", ".join(str(pid) for pid in sorted(report.failed))
                + ")",
                file=sys.stderr,
            )
            return 1
    log.info("send-digest.done", extra={"sent": sent, "dry_run": args.dry_run})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lexwatch")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply database migrations")
    p.add_argument("--revision", default="head")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="run the scheduler/worker loop")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("run-job", help="run ingestion + scoring + digests once, now")
    p.set_defaults(func=cmd_run_job)

    p = sub.add_parser(
        "rescore",
        help="rescore all bills for one or all profiles",
        description=(
            "Rescore now, in this process: every profile (default) or one (--profile). Each "
            "profile gets a runs row (kind=rescore) with its billed units. Only pairs without a "
            "current-model score are sent (new/edited rules, model switch). Exit code 1 when "
            "a rescore failed or was partial. With --all, nothing is scored here: a forced "
            "rescore of every profile (all bills sent again) is queued for the worker, like "
            "the admin 'Rescore all' button."
        ),
    )
    target = p.add_mutually_exclusive_group()
    target.add_argument("--profile", type=int, default=None, help="profile id (default: all)")
    target.add_argument(
        "--all",
        action="store_true",
        help="queue a full (forced) rescore of every profile for the worker",
    )
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("download-model", help="download the local ONNX reranker model")
    p.set_defaults(func=cmd_download_model)

    p = sub.add_parser("send-digest", help="build and send email digests now")
    p.add_argument("--dry-run", action="store_true", help="build but do not send")
    p.set_defaults(func=cmd_send_digest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.validate()
    return int(args.func(settings, args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
