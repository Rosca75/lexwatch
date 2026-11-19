"""Integration of jobs, worker, CLI and the error page (test DB, fake scorer, no network)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from lexwatch import cli
from lexwatch import db as db_module
from lexwatch.i18n import t
from lexwatch.ingest import pipeline
from lexwatch.jobs import worker
from lexwatch.jobs.daily import create_run, execute_run
from lexwatch.jobs.rescore import (
    execute_rescore,
    finish_rescore_status,
    queue_rescore_all,
    recompute_default_threshold_matches,
)
from lexwatch.models import Bill, Match, Profile, Rule, Run, User
from lexwatch.notify import digest
from lexwatch.scoring import factory, service
from lexwatch.scoring.base import ScorerUnavailable
from lexwatch.scoring.fake import FakeScorer

RULE = "blanchiment capitaux"


class BillingFakeScorer(FakeScorer):
    """FakeScorer that reports a billing unit, like Foundry does."""

    def score_many(self, query, bill_texts):
        self.billing_unit = "search_units"
        return super().score_many(query, bill_texts)


class DownScorer(FakeScorer):
    def score_many(self, query, bill_texts):
        raise ScorerUnavailable("endpoint down")


@pytest.fixture
def profile(db) -> Profile:
    owner = User(oidc_sub="jobs-owner", email="jobs@example.test", role="user")
    db.add(owner)
    db.flush()
    prof = Profile(name="AML", owner_id=owner.id, recipients="a@example.test")
    prof.rules = [Rule(text=RULE, position=0)]
    db.add(prof)
    db.add(
        Bill(
            number="9001",
            title="Projet de loi blanchiment de capitaux",
            source="test",
            score_state="pending",
        )
    )
    db.add(Bill(number="9002", title="Projet de loi agriculture", source="test"))
    db.flush()
    return prof


@pytest.fixture
def no_ingest(monkeypatch):
    def ingest(session, run, adapters=None, client=None):
        return pipeline.IngestResult(added=0, changed=0, sources={})

    monkeypatch.setattr(pipeline, "run_ingestion", ingest)


@pytest.fixture(autouse=True)
def no_daily(monkeypatch):
    """worker.tick must not start a real daily job (network) whatever the time of day."""
    monkeypatch.setattr(worker, "daily_due", lambda session, settings, now=None: False)


def use_scorer(monkeypatch, scorer) -> None:
    monkeypatch.setattr(factory, "get_scorer", lambda settings: scorer)


# -- 1. daily job: digests and billing ---------------------------------------------------------


def test_daily_run_records_digest_counts_and_billing_unit(
    db, settings, profile, no_ingest, monkeypatch
):
    use_scorer(monkeypatch, BillingFakeScorer())
    seen = {}

    def report(session, now=None, dry_run=False, *, settings=None, echo=None):
        seen["settings"] = settings
        return digest.DigestReport(sent=2, skipped=3)

    monkeypatch.setattr(digest, "send_digests_report", report)
    run = execute_run(db, create_run(db, "manual"), settings)

    assert run.status == "success"
    assert seen["settings"] is settings
    assert run.details["digests_sent"] == 2
    assert run.details["digests_failed"] == 0
    assert run.details["digests_skipped"] == 3
    assert run.details["billing"] == {"unit": "search_units"}
    assert run.billed_units >= 1
    assert run.bills_scored >= 1


def test_digest_failure_makes_run_partial_and_keeps_counts(
    db, settings, profile, no_ingest, monkeypatch
):
    def report(session, now=None, dry_run=False, *, settings=None, echo=None):
        return digest.DigestReport(sent=1, skipped=0, failed={profile.id: "relay refused"})

    monkeypatch.setattr(digest, "send_digests_report", report)
    run = execute_run(db, create_run(db, "manual"), settings)

    assert run.status == "partial"
    assert (run.details["digests_sent"], run.details["digests_failed"]) == (1, 1)
    assert run.details["digests_skipped"] == 0
    assert "digest" in run.error_message and "relay refused" in run.error_message


def test_daily_run_with_real_digest_and_smtp_failure(db, settings, profile, no_ingest, monkeypatch):
    """End to end with the real digest code: the mailer fails, the run is partial."""
    from lexwatch.notify import mailer

    def fail(settings, message):
        raise mailer.MailerError("connection refused")

    monkeypatch.setattr(mailer, "send_message", fail)
    profile.last_digest_at = datetime.now(UTC) - timedelta(days=2)
    db.flush()
    run = execute_run(db, create_run(db, "manual"), replace(settings, smtp_host="smtp.test"))

    assert run.status == "partial"
    assert run.details["digests_failed"] == 1
    assert run.details["digests_sent"] == 0
    assert "connection refused" in run.error_message


def test_daily_run_uses_the_scorers_default_threshold(
    db, settings, profile, no_ingest, monkeypatch
):
    use_scorer(monkeypatch, FakeScorer(default_threshold=0.01))
    monkeypatch.setattr(digest, "send_digests_report", lambda *a, **k: digest.DigestReport())
    execute_run(db, create_run(db, "manual"), settings)
    match = db.scalar(select(Match).where(Match.bill_number == "9001"))
    assert match is not None and match.matched is True


# -- 2. worker rescore ------------------------------------------------------------------------


def rescore_runs(db) -> list[Run]:
    return list(db.scalars(select(Run).where(Run.kind == "rescore").order_by(Run.id)))


def test_worker_rescore_records_a_run_counted_in_monthly_cost(db, settings, profile, monkeypatch):
    use_scorer(monkeypatch, BillingFakeScorer())
    profile.rescore_status = "pending"
    db.flush()
    worker.tick(db, settings)

    db.refresh(profile)
    assert profile.rescore_status == "idle"
    (run,) = rescore_runs(db)
    assert run.status == "success"
    assert run.requested_by is None
    assert run.details["profile_id"] == profile.id
    assert run.details["billing"] == {"unit": "search_units"}
    assert run.billed_units >= 1
    assert run.started_at and run.finished_at
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = db.scalar(select(func.sum(Run.billed_units)).where(Run.started_at >= month_start))
    assert total >= run.billed_units


def test_profile_saved_during_rescore_stays_pending(db, settings, profile, monkeypatch):
    """CAS: the worker must not overwrite a 'pending' set while it was rescoring."""
    real = service.rescore_profile

    def rescore_and_resave(session, scorer, profile_id, *, force=False, run=None, commit=None):
        result = real(session, scorer, profile_id, force=force, run=run, commit=commit)
        # The user saves the profile again meanwhile (the rules route sets 'pending').
        session.execute(
            update(Profile).where(Profile.id == profile_id).values(rescore_status="pending")
        )
        return result

    monkeypatch.setattr(service, "rescore_profile", rescore_and_resave)
    profile.rescore_status = "pending"
    db.flush()
    worker.tick(db, settings)

    db.refresh(profile)
    assert profile.rescore_status == "pending"
    assert len(rescore_runs(db)) == 1


def test_finish_rescore_status_is_compare_and_set(db, profile):
    profile.rescore_status = "running"
    db.flush()
    assert finish_rescore_status(db, profile.id) is True
    db.refresh(profile)
    assert profile.rescore_status == "idle"

    profile.rescore_status = "pending"
    db.flush()
    assert finish_rescore_status(db, profile.id) is False
    db.refresh(profile)
    assert profile.rescore_status == "pending"


def test_failed_rescore_is_recorded_and_not_retried(db, settings, profile, monkeypatch):
    use_scorer(monkeypatch, DownScorer())
    profile.rescore_status = "pending"
    db.flush()
    worker.tick(db, settings)

    db.refresh(profile)
    assert profile.rescore_status == "idle"
    (run,) = rescore_runs(db)
    assert run.status == "failed"
    assert "endpoint down" in run.error_message
    # Left for the next daily run, not retried by the worker.
    assert db.get(Bill, "9001").score_state == "unscored"
    worker.tick(db, settings)
    assert len(rescore_runs(db)) == 1


def test_rescore_exception_is_recorded_as_failed_run(db, settings, profile, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(service, "rescore_profile", boom)
    profile.rescore_status = "pending"
    db.flush()
    worker.tick(db, settings)

    db.refresh(profile)
    assert profile.rescore_status == "idle"
    (run,) = rescore_runs(db)
    assert run.status == "failed" and "db hiccup" in run.error_message


def test_execute_rescore_counts_only_its_own_billed_units(db, settings, profile):
    scorer = FakeScorer()
    scorer.billed_units = 40  # a shared scorer already used by an earlier profile
    run = execute_rescore(db, settings, profile.id, scorer=scorer)
    assert run.billed_units == scorer.billed_units - 40 >= 1


def test_worker_startup_recovers_interrupted_work(db, profile):
    profile.rescore_status = "running"
    stale = Run(kind="rescore", status="running", started_at=datetime.now(UTC))
    done = Run(kind="daily", status="success")
    queued = Run(kind="manual", status="queued")
    db.add_all([stale, done, queued])
    db.flush()

    assert worker.recover_interrupted(db) == (1, 1)

    for obj in (profile, stale, done, queued):
        db.refresh(obj)
    assert profile.rescore_status == "pending"
    assert stale.status == "failed"
    assert stale.error_message == "interrupted (worker restarted)"
    assert stale.finished_at is not None
    assert (done.status, queued.status) == ("success", "queued")
    assert worker.recover_interrupted(db) == (0, 0)


def test_queued_rescore_all_forces_every_bill_in_one_run(db, settings, profile, monkeypatch):
    scorer = BillingFakeScorer()
    use_scorer(monkeypatch, scorer)
    execute_rescore(db, settings, profile.id, scorer=scorer)  # every pair now has a score
    assert queue_rescore_all(db, requested_by=None) is not None
    assert queue_rescore_all(db) is None  # one queued full rescore at a time
    db.refresh(profile)
    assert profile.rescore_status == "pending"

    calls_before = len(scorer.calls)
    worker.tick(db, settings)

    db.refresh(profile)
    assert profile.rescore_status == "idle"
    forced = [r for r in rescore_runs(db) if (r.details or {}).get("force")]
    assert len(forced) == 1
    run = forced[0]
    assert run.status == "success" and run.details["profiles"] == 1
    # Forced: every bill was sent again although all scores were current.
    sent = sum(n for _, n in scorer.calls[calls_before:])
    assert sent == db.scalar(select(func.count(Bill.number)))
    assert run.billed_units >= 1 and run.bills_scored == sent
    # The pending flag set by "Rescore all" was consumed by the forced run (no extra run).
    worker.tick(db, settings)
    assert len(rescore_runs(db)) == 2


def test_worker_startup_recomputes_default_threshold_matches(db, settings, profile, monkeypatch):
    scorer = FakeScorer(default_threshold=0.01)
    use_scorer(monkeypatch, scorer)
    execute_rescore(db, settings, profile.id, scorer=scorer)
    match = db.scalar(select(Match).where(Match.bill_number == "9002"))  # fake score 0
    assert match is not None and match.matched is False

    # The operator lowers DEFAULT_THRESHOLD to 0; no scoring request is made.
    calls = len(scorer.calls)
    lowered = replace(settings, default_threshold=0.0, default_threshold_explicit=True)
    assert recompute_default_threshold_matches(db, lowered) == 1
    db.refresh(match)
    assert match.matched is True
    assert len(scorer.calls) == calls

    # A profile with its own threshold is left alone.
    profile.threshold = Decimal("0.500")
    db.flush()
    assert recompute_default_threshold_matches(db, settings) == 0
    db.refresh(match)
    assert match.matched is True


# -- 2b. CLI rescore ----------------------------------------------------------------------------


@pytest.fixture
def cli_db(db, monkeypatch):
    """Point the CLI's own engine/session at the test transaction."""

    @contextmanager
    def scope(factory=None):
        yield db

    monkeypatch.setattr(db_module, "get_engine", lambda url=None: None)
    monkeypatch.setattr(db_module, "make_sessionmaker", lambda engine: None)
    monkeypatch.setattr(db_module, "session_scope", scope)
    return db


def run_cli(settings, monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    return cli.main(argv)


def test_cli_rescore_profile_creates_rescore_run(cli_db, settings, profile, monkeypatch, capsys):
    code = run_cli(settings, monkeypatch, ["rescore", "--profile", str(profile.id)])
    assert code == 0
    (run,) = rescore_runs(cli_db)
    assert run.status == "success" and run.details["profile_id"] == profile.id
    assert run.billed_units >= 1
    assert f"profile {profile.id}: run {run.id} success" in capsys.readouterr().out


def test_cli_rescore_unknown_profile_exits_1(cli_db, settings, monkeypatch):
    assert run_cli(settings, monkeypatch, ["rescore", "--profile", "999999"]) == 1


def test_cli_rescore_all_queues_forced_rescore(cli_db, settings, profile, monkeypatch, capsys):
    assert run_cli(settings, monkeypatch, ["rescore", "--all"]) == 0
    cli_db.refresh(profile)
    assert profile.rescore_status == "pending"
    (run,) = rescore_runs(cli_db)
    assert (run.status, run.details) == ("queued", {"force": True})
    assert "queued" in capsys.readouterr().out
    # A second call does not queue another one.
    assert run_cli(settings, monkeypatch, ["rescore", "--all"]) == 0
    assert len(rescore_runs(cli_db)) == 1


def test_cli_rescore_help_documents_all(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["rescore", "--help"])
    out = capsys.readouterr().out
    assert "--all" in out and "--profile" in out and "worker" in out


# -- 3. CSRF error page -------------------------------------------------------------------------


def test_csrf_403_renders_csrf_message(client):
    response = client.post("/logout", headers={"Accept": "text/html"})
    assert response.status_code == 403
    assert t("error.csrf", "fr") in response.text
    assert t("error.forbidden", "fr") not in response.text


def test_csrf_403_htmx_keeps_refresh_header(client):
    response = client.post("/logout", headers={"Accept": "text/html", "HX-Request": "true"})
    assert response.status_code == 403
    assert response.headers.get("HX-Refresh") == "true"


def test_other_403_and_404_keep_generic_messages(client):
    response = client.get("/no-such-page", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert t("error.not_found", "fr") in response.text
    assert t("error.csrf", "fr") not in response.text


# -- 5. CLI send-digest -------------------------------------------------------------------------


def test_cli_send_digest_failure_prints_summary_and_exits_1(cli_db, settings, monkeypatch, capsys):
    def send(session, now=None, dry_run=False, *, settings=None):
        raise digest.DigestError(digest.DigestReport(sent=2, skipped=1, failed={7: "boom"}))

    monkeypatch.setattr(digest, "send_digests", send)
    assert run_cli(settings, monkeypatch, ["send-digest"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["send-digest: 2 sent, 1 failed, 1 skipped (failed profiles: 7)"]


def test_cli_send_digest_success_exits_0(cli_db, settings, monkeypatch):
    monkeypatch.setattr(digest, "send_digests", lambda *a, **k: 3)
    assert run_cli(settings, monkeypatch, ["send-digest", "--dry-run"]) == 0
