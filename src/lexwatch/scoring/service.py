"""Scoring orchestration: rule scores, matches, rescoring and the Rules preview.

Service functions flush and never commit on their own; the caller owns the transaction.
``score_pending_bills`` and ``rescore_profile`` accept a ``commit`` callback (the jobs pass
``session.commit``): it is called after each scoring batch, so a crash or a stop keeps the
scores already computed, and the run's ``details["progress"]`` (done/total pairs) is visible
on the admin page while the run is going.

What gets scored
    A (rule, bill) pair needs a score when the bill is ``pending``/``unscored`` (new, changed,
    or failed last time) or when the pair has no score from the *current* model
    (``scorer.model_name``). The second condition covers three cases with one rule:
    a model switch (every stored score is stale, so everything is rescored and scores stay
    comparable), a new rule (no scores yet), and a rescore that failed earlier (retried by
    the next daily run). One scoring request is sent per distinct rule text (the query), with
    the bills as documents, in batches of ``scorer.max_documents``.

Bill state
    After ``score_pending_bills`` a bill is ``scored`` (and ``scored_text`` holds the exact text
    sent) when every request it was part of succeeded, otherwise ``unscored``. Its data is
    never removed. With no rules at all, pending bills become ``scored`` without any request:
    there is nothing to score them against, and new rules are handled by ``rescore_profile``.
    A pending bill's pair is sent again only when it has no current-model score from after
    the bill's last change, so a run interrupted half-way resumes where it stopped.

Bills without text
    A bill whose title and summary are blank (a vote-only record, for example) is never sent
    to the scorer and never matched. It stays ``pending`` but the scoring queries skip it, so
    it is not retried every run; when a title arrives, ingestion marks it changed and it is
    scored like any other bill.

Matches
    ``profile_score`` is the plain mean of the profile's rule scores for a bill, computed only
    when every rule of the profile has a (current-model) score for it; other bills keep their
    previous match row. ``matched = profile_score >= threshold`` where the threshold is the
    profile's own, else the scorer's default (see ``factory.default_threshold_for``).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import and_, case, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.models import Bill, Match, Profile, Rule, RuleScore, Run, bill_recency_order
from lexwatch.scoring.base import Scorer, ScorerUnavailable, ScoringError
from lexwatch.scoring.text import bill_text

log = logging.getLogger(__name__)

PREVIEW_CACHE_SIZE = 4096
# Profile scores are rounded before the threshold comparison (and stored rounded) so that a
# mean equal to the threshold in decimal is not lost to float error: (0.6 + 0.3) / 2 is
# 0.44999999999999996 in binary floating point.
SCORE_DECIMALS = 6
_IN_CHUNK = 1000  # bound the size of IN (...) lists and multi-row inserts
PENDING_STATES = ("pending", "unscored")
PROGRESS_LOG_SECONDS = 15.0  # scoring.progress log lines at most this often (and at the end)

CommitFn = Callable[[], None]


def has_text():
    """SQL: the bill has a non-blank title or summary (bills without text are never scored)."""
    return or_(
        func.coalesce(Bill.title, "").op("~")(r"\S"),
        func.coalesce(Bill.summary, "").op("~")(r"\S"),
    )


@dataclass(slots=True)
class ScoreResult:
    scored: int = 0
    unscored: int = 0
    billed_units: int = 0  # units billed during this call (delta on the scorer)
    error: str | None = None
    billing_unit: str | None = None  # "search_units" | "input_tokens" | None -> runs.details


@dataclass(slots=True)
class PreviewRow:
    bill_number: str
    title: str
    profile_score: float
    matched: bool
    rule_scores: list[float] = field(default_factory=list)  # same order as the rule texts

    @property
    def number(self) -> str:
        return self.bill_number

    @property
    def score(self) -> float:
        return self.profile_score


def profile_score(rule_scores: list[float]) -> float | None:
    """Plain average of rule scores rounded to 6 decimals; ``None`` when there are no rules."""
    if not rule_scores:
        return None
    return round(sum(rule_scores) / len(rule_scores), SCORE_DECIMALS)


def is_match(score: float | None, threshold: float) -> bool:
    return score is not None and score >= threshold


# -- helpers --------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _max_chars(scorer: Scorer) -> int:
    settings = getattr(scorer, "settings", None)
    if settings is None:
        from lexwatch.config import get_settings

        settings = get_settings()
    return settings.scoring_max_chars


def _default_threshold(scorer: Scorer) -> float:
    value = getattr(scorer, "default_threshold", None)
    if value is None:
        from lexwatch.config import get_settings
        from lexwatch.scoring.factory import default_threshold_for

        return default_threshold_for(get_settings())
    return float(value)


def _rule_groups(rules: Iterable[Rule]) -> dict[str, list[int]]:
    """Distinct (stripped) rule text -> rule ids. Blank rules are ignored everywhere."""
    groups: dict[str, list[int]] = {}
    for rule in rules:
        text = (rule.text or "").strip()
        if text:
            groups.setdefault(text, []).append(rule.id)
    return groups


def _active_rule_ids(session: Session, profile_id: int) -> list[int]:
    stmt = select(Rule.id).where(Rule.profile_id == profile_id, func.trim(Rule.text) != "")
    return list(session.scalars(stmt.order_by(Rule.position, Rule.id)))


def _missing_bills(session: Session, rule_ids: list[int], model: str) -> set[str]:
    """Bills with text lacking a current-model score for at least one of ``rule_ids``."""
    return _bills_to_score(session, rule_ids, model, pending=False)


def _bills_to_score(
    session: Session, rule_ids: list[int], model: str, *, pending: bool = True
) -> set[str]:
    """Bills with text that need a score for at least one of ``rule_ids``.

    A bill needs one when it has no current-model score for the rule, or (``pending``) when
    it is ``pending``/``unscored`` and its score predates the bill's last change. A pair
    already scored after that change (by a run that stopped half-way) is not sent again.
    """
    missing: set[str] = set()
    for rule_id in rule_ids:
        has_current = exists().where(
            RuleScore.rule_id == rule_id,
            RuleScore.bill_number == Bill.number,
            RuleScore.model == model,
        )
        condition = ~has_current
        if pending:
            has_fresh = exists().where(
                RuleScore.rule_id == rule_id,
                RuleScore.bill_number == Bill.number,
                RuleScore.model == model,
                RuleScore.scored_at >= Bill.last_changed_at,
            )
            condition = or_(condition, and_(Bill.score_state.in_(PENDING_STATES), ~has_fresh))
        missing.update(session.scalars(select(Bill.number).where(has_text(), condition)))
    return missing


def _load_bills(session: Session, numbers: Iterable[str]) -> dict[str, Bill]:
    bills: dict[str, Bill] = {}
    for chunk in _chunks(sorted(set(numbers)), _IN_CHUNK):
        for bill in session.scalars(select(Bill).where(Bill.number.in_(chunk))):
            bills[bill.number] = bill
    return bills


def _upsert_rule_scores(
    session: Session,
    rule_ids: list[int],
    numbers: Sequence[str],
    scores: Sequence[float],
    model: str,
    now: datetime,
) -> None:
    rows = [
        {"rule_id": rid, "bill_number": n, "score": float(s), "model": model, "scored_at": now}
        for rid in rule_ids
        for n, s in zip(numbers, scores, strict=True)
    ]
    for chunk in _chunks(rows, _IN_CHUNK):
        stmt = pg_insert(RuleScore).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_rule_scores_rule_bill",
            set_={
                "score": stmt.excluded.score,
                "model": stmt.excluded.model,
                "scored_at": stmt.excluded.scored_at,
            },
        )
        session.execute(stmt)


@dataclass(slots=True)
class _Outcome:
    ok: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    unavailable: bool = False
    rule_ids: set[int] = field(default_factory=set)  # rules that received scores

    def error_message(self) -> str | None:
        if not self.errors:
            return None
        if self.unavailable:
            return f"scorer unavailable: {self.errors[-1]}"
        more = f" (+{len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
        return f"{self.errors[0]}{more}"


class _Progress:
    """Scoring progress in (rule text, bill) pairs: logged, written to the run, committed.

    After each batch ``run.details["progress"] = {"done": n, "total": m}`` is set and the
    ``commit`` callback (if any) is called, so completed scores survive a crash and the admin
    page shows how far a running run is.
    """

    def __init__(self, total: int, run: Run | None, commit: CommitFn | None) -> None:
        self.total = total
        self.done = 0
        self.run = run
        self.commit = commit
        self._last_log = time.monotonic()
        if total:
            self._write()

    def _write(self) -> None:
        if self.run is not None:
            progress = {"done": self.done, "total": self.total}
            self.run.details = {**(self.run.details or {}), "progress": progress}

    def advance(self, pairs: int, session: Session) -> None:
        self.done += pairs
        self._write()
        session.flush()
        if self.commit is not None:
            self.commit()
        now = time.monotonic()
        if self.done >= self.total or now - self._last_log >= PROGRESS_LOG_SECONDS:
            self._last_log = now
            log.info(
                "scoring.progress",
                extra={
                    "done": self.done,
                    "total": self.total,
                    "run_id": self.run.id if self.run is not None else None,
                },
            )


def _score_groups(
    session: Session,
    scorer: Scorer,
    groups: dict[str, list[int]],
    docs_for: dict[str, list[str]],
    texts: dict[str, str],
    *,
    run: Run | None = None,
    commit: CommitFn | None = None,
) -> _Outcome:
    """One request per distinct rule text (batched); upsert scores; collect failures.

    Bills with an empty text are never sent (callers already leave them out). After each
    batch the progress is written to ``run`` and ``commit`` is called (see ``_Progress``).
    """
    out = _Outcome()
    size = max(1, int(getattr(scorer, "max_documents", 100) or 100))
    now = _now()
    docs_for = {
        query: [n for n in docs_for.get(query, []) if texts.get(n, "").strip()] for query in groups
    }
    progress = _Progress(sum(len(d) for d in docs_for.values()), run, commit)
    for query, rule_ids in groups.items():
        for chunk in _chunks(docs_for[query], size):
            if out.unavailable:
                out.failed.update(chunk)
                progress.advance(len(chunk), session)
                continue
            try:
                scores = scorer.score_many(query, [texts[n] for n in chunk])
                if len(scores) != len(chunk):
                    raise ScoringError(
                        f"scorer returned {len(scores)} scores for {len(chunk)} documents"
                    )
            except ScorerUnavailable as exc:
                out.unavailable = True
                out.failed.update(chunk)
                out.errors.append(str(exc))
                log.error("scoring.unavailable", extra={"error": str(exc)})
                progress.advance(len(chunk), session)
                continue
            except ScoringError as exc:
                out.failed.update(chunk)
                out.errors.append(str(exc))
                log.warning(
                    "scoring.request_failed", extra={"error": str(exc), "documents": len(chunk)}
                )
                progress.advance(len(chunk), session)
                continue
            _upsert_rule_scores(session, rule_ids, chunk, scores, scorer.model_name, now)
            out.ok.update(chunk)
            out.rule_ids.update(rule_ids)
            progress.advance(len(chunk), session)
    return out


def _record_billing(scorer: Scorer, result: ScoreResult, before: int, run: Run | None) -> None:
    result.billed_units = int(getattr(scorer, "billed_units", 0) or 0) - before
    result.billing_unit = getattr(scorer, "billing_unit", None)
    if run is not None and result.billing_unit:
        run.details = {**(run.details or {}), "billing": {"unit": result.billing_unit}}


def _profiles_for_rules(session: Session, rule_ids: Iterable[int]) -> list[Profile]:
    ids = sorted(set(rule_ids))
    if not ids:
        return []
    stmt = select(Profile).where(Profile.id.in_(select(Rule.profile_id).where(Rule.id.in_(ids))))
    return list(session.scalars(stmt.order_by(Profile.id)))


# -- daily scoring ----------------------------------------------------------------------------


def score_pending_bills(
    session: Session,
    scorer: Scorer,
    run: Run | None = None,
    *,
    commit: CommitFn | None = None,
) -> ScoreResult:
    """Score pending/unscored bills and stale or missing pairs for every rule; update matches.

    ``commit`` (e.g. ``session.commit``) is called after each scoring batch; without it the
    function only flushes. Bills without text are left ``pending`` and never sent.
    """
    result = ScoreResult()
    before = int(getattr(scorer, "billed_units", 0) or 0)
    model = scorer.model_name

    pending = set(
        session.scalars(select(Bill.number).where(Bill.score_state.in_(PENDING_STATES), has_text()))
    )
    groups = _rule_groups(session.scalars(select(Rule).order_by(Rule.profile_id, Rule.position)))
    docs_for: dict[str, list[str]] = {}
    needed = set(pending)
    for query, rule_ids in groups.items():
        docs = _bills_to_score(session, rule_ids, model)
        docs_for[query] = sorted(docs)
        needed |= docs

    bills = _load_bills(session, needed)
    max_chars = _max_chars(scorer)
    texts = {n: bill_text(b, max_chars) for n, b in bills.items()}
    outcome = _score_groups(session, scorer, groups, docs_for, texts, run=run, commit=commit)

    for number, bill in bills.items():
        if number in outcome.failed:
            bill.score_state = "unscored"
            result.unscored += 1
        else:
            bill.score_state = "scored"
            bill.scored_text = texts[number]
            result.scored += 1
    session.flush()

    default = _default_threshold(scorer)
    # Pending bills whose pairs were all scored by an interrupted earlier run send nothing
    # now, so their profiles are recomputed too.
    touched = set(outcome.rule_ids)
    if pending:
        touched.update(rid for rule_ids in groups.values() for rid in rule_ids)
    for profile in _profiles_for_rules(session, touched):
        compute_matches(session, profile, default, model=model)

    result.error = outcome.error_message()
    _record_billing(scorer, result, before, run)
    log.info(
        "scoring.pending_done",
        extra={
            "scored": result.scored,
            "unscored": result.unscored,
            "rules": len(groups),
            "billed_units": result.billed_units,
            "billing_unit": result.billing_unit,
        },
    )
    return result


# -- profile rescoring ------------------------------------------------------------------------


def rescore_profile(
    session: Session,
    scorer: Scorer,
    profile_id: int,
    *,
    force: bool = False,
    run: Run | None = None,
    commit: CommitFn | None = None,
) -> ScoreResult:
    """Score every bill for every rule of a profile, then recompute its matches.

    ``commit`` is called after each scoring batch (see ``score_pending_bills``).

    Only pairs without a current-model score are sent (new or edited rules, model switch),
    unless ``force`` is true. Bills whose request failed become ``unscored`` so the next daily
    run retries them. A threshold-only change needs no scoring: use
    ``recompute_profile_matches``.
    """
    result = ScoreResult()
    before = int(getattr(scorer, "billed_units", 0) or 0)
    profile = session.get(Profile, profile_id)
    if profile is None:
        result.error = f"profile {profile_id} not found"
        return result
    model = scorer.model_name
    groups = _rule_groups(
        session.scalars(
            select(Rule).where(Rule.profile_id == profile_id).order_by(Rule.position, Rule.id)
        )
    )
    all_numbers: list[str] | None = None
    docs_for: dict[str, list[str]] = {}
    for query, rule_ids in groups.items():
        if force:
            if all_numbers is None:
                all_numbers = list(
                    session.scalars(select(Bill.number).where(has_text()).order_by(Bill.number))
                )
            docs_for[query] = all_numbers
        else:
            docs_for[query] = sorted(_missing_bills(session, rule_ids, model))

    bills = _load_bills(session, {n for docs in docs_for.values() for n in docs})
    max_chars = _max_chars(scorer)
    texts = {n: bill_text(b, max_chars) for n, b in bills.items()}
    outcome = _score_groups(session, scorer, groups, docs_for, texts, run=run, commit=commit)

    for number in outcome.failed:
        bills[number].score_state = "unscored"
    result.unscored = len(outcome.failed)
    result.scored = len(outcome.ok - outcome.failed)
    session.flush()

    compute_matches(session, profile, _default_threshold(scorer), model=model)
    result.error = outcome.error_message()
    _record_billing(scorer, result, before, run)
    log.info(
        "scoring.rescore_done",
        extra={
            "profile_id": profile_id,
            "scored": result.scored,
            "unscored": result.unscored,
            "billed_units": result.billed_units,
        },
    )
    return result


# -- matches --------------------------------------------------------------------------------


def compute_matches(
    session: Session,
    profile: Profile,
    default_threshold: float | None = None,
    *,
    model: str | None = None,
) -> int:
    """Recompute ``matches`` rows for ``profile``; return the number of matched bills.

    ``computed_at`` changes only when a row is inserted or its ``matched`` flips (the digest
    reads ``computed_at > last_digest_at AND matched`` as "newly matched"); a score-only
    change updates ``profile_score`` but keeps ``computed_at``.

    ``default_threshold`` (used when the profile has none) falls back to
    ``default_threshold_for(get_settings())``. When ``model`` is given only that model's scores
    count. A bill is (re)computed only when every rule of the profile has a score for it.
    A profile without rules has no matches.
    """
    if default_threshold is None:
        from lexwatch.config import get_settings
        from lexwatch.scoring.factory import default_threshold_for

        default_threshold = default_threshold_for(get_settings())
    threshold = profile.effective_threshold(default_threshold)
    rule_ids = _active_rule_ids(session, profile.id)
    if not rule_ids:
        session.execute(delete(Match).where(Match.profile_id == profile.id))
        session.flush()
        return 0

    # Bills without text are never matched (their old rows, if any, are removed).
    session.execute(
        delete(Match).where(
            Match.profile_id == profile.id,
            Match.bill_number.in_(select(Bill.number).where(~has_text())),
        )
    )
    stmt = (
        select(RuleScore.bill_number, func.avg(RuleScore.score))
        .join(Bill, Bill.number == RuleScore.bill_number)
        .where(RuleScore.rule_id.in_(rule_ids), has_text())
    )
    if model is not None:
        stmt = stmt.where(RuleScore.model == model)
    stmt = stmt.group_by(RuleScore.bill_number).having(
        func.count(func.distinct(RuleScore.rule_id)) == len(rule_ids)
    )
    now = _now()
    rows = []
    for number, avg in session.execute(stmt):
        score = round(float(avg), SCORE_DECIMALS)
        rows.append(
            {
                "profile_id": profile.id,
                "bill_number": number,
                "profile_score": score,
                "matched": is_match(score, threshold),
                "computed_at": now,
            }
        )
    table = Match.__table__
    for chunk in _chunks(rows, _IN_CHUNK):
        ins = pg_insert(Match).values(list(chunk))
        ins = ins.on_conflict_do_update(
            constraint="uq_matches_profile_bill",
            set_={
                "profile_score": ins.excluded.profile_score,
                "matched": ins.excluded.matched,
                # computed_at marks "newly matched" for the digest: bump it only when the
                # row is new (insert) or matched flips, never for a score-only change.
                "computed_at": case(
                    (
                        table.c.matched.is_distinct_from(ins.excluded.matched),
                        ins.excluded.computed_at,
                    ),
                    else_=table.c.computed_at,
                ),
            },
            where=or_(
                table.c.profile_score.is_distinct_from(ins.excluded.profile_score),
                table.c.matched.is_distinct_from(ins.excluded.matched),
            ),
        )
        session.execute(ins)
    session.flush()
    matched = session.scalar(
        select(func.count()).where(Match.profile_id == profile.id, Match.matched.is_(True))
    )
    return int(matched or 0)


def recompute_profile_matches(
    session: Session, profile_id: int, settings: Settings | None = None
) -> int:
    """Threshold-only change: recompute matches from stored scores, no scoring requests."""
    from lexwatch.config import get_settings
    from lexwatch.scoring.factory import default_threshold_for, model_name_for

    settings = settings or get_settings()
    profile = session.get(Profile, profile_id)
    if profile is None:
        return 0
    return compute_matches(
        session, profile, default_threshold_for(settings), model=model_name_for(settings)
    )


def invalidate_rule_scores(session: Session, rule_id: int) -> int:
    """Delete a rule's stored scores; call it when the rule's text is edited in place.

    Scores belong to the rule text, not the rule id. Without stored scores the rule has no
    current-model score for any bill, so the next ``rescore_profile`` (worker) or
    ``score_pending_bills`` (daily run) rescores it. Returns the number of rows deleted.
    The profile's match rows are kept until that rescore recomputes them.
    """
    result = session.execute(delete(RuleScore).where(RuleScore.rule_id == rule_id))
    session.flush()
    return int(result.rowcount or 0)


def stale_score_count(session: Session, model_name: str) -> int:
    """Rule scores produced by another model than ``model_name`` (admin page, model switch)."""
    stmt = select(func.count()).select_from(RuleScore).where(RuleScore.model != model_name)
    return int(session.scalar(stmt) or 0)


# -- preview --------------------------------------------------------------------------------

_preview_cache: OrderedDict[tuple[str, str, str], float] = OrderedDict()
_preview_lock = threading.Lock()


def clear_preview_cache() -> None:
    with _preview_lock:
        _preview_cache.clear()


def _cache_get(key: tuple[str, str, str]) -> float | None:
    with _preview_lock:
        value = _preview_cache.get(key)
        if value is not None:
            _preview_cache.move_to_end(key)
        return value


def _cache_put(key: tuple[str, str, str], value: float) -> None:
    with _preview_lock:
        _preview_cache[key] = value
        _preview_cache.move_to_end(key)
        while len(_preview_cache) > PREVIEW_CACHE_SIZE:
            _preview_cache.popitem(last=False)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def preview_matches(
    session: Session,
    scorer: Scorer,
    rule_texts: list[str],
    threshold: float | None = None,
    limit: int = 50,
) -> list[PreviewRow]:
    """Score unsaved ``rule_texts`` against the ``limit`` most recent bills with text.

    "Most recent" is the highest bill number (``models.bill_recency_order``): numbers are
    assigned at filing, while vote-only records have no filing date.


    Writes nothing. To keep it cheap while a user types, scores come from (in order) stored
    rule scores of saved rules with the same text and current model, an in-process LRU cache
    keyed by (model, rule text, bill text hash), and finally one scoring request per rule text
    for the remaining bills. Rows are sorted by profile score, highest first. A blank rule text
    is ignored; with no rule text the result is empty. ``threshold`` defaults to the scorer's
    default threshold. Raises ``ScoringError`` when the scorer fails.
    """
    queries = [t.strip() for t in rule_texts if t and t.strip()]
    if not queries or limit <= 0:
        return []
    if threshold is None:
        threshold = _default_threshold(scorer)
    unique = list(dict.fromkeys(queries))
    model = scorer.model_name
    bills = list(
        session.scalars(select(Bill).where(has_text()).order_by(*bill_recency_order()).limit(limit))
    )
    if not bills:
        return []
    max_chars = _max_chars(scorer)
    texts = {b.number: bill_text(b, max_chars) for b in bills}
    hashes = {n: _text_hash(t) for n, t in texts.items()}
    scores: dict[str, dict[str, float]] = {q: {} for q in unique}

    stored = session.execute(
        select(func.trim(Rule.text), RuleScore.bill_number, RuleScore.score, Bill.scored_text)
        .join(Rule, Rule.id == RuleScore.rule_id)
        .join(Bill, Bill.number == RuleScore.bill_number)
        .where(
            func.trim(Rule.text).in_(unique),
            RuleScore.model == model,
            RuleScore.bill_number.in_(list(texts)),
            Bill.score_state == "scored",
        )
    )
    for query, number, score, scored_text in stored:
        if query in scores and scored_text == texts[number]:
            scores[query][number] = float(score)

    for query in unique:
        todo: list[str] = []
        for bill in bills:
            n = bill.number
            if n in scores[query]:
                continue
            cached = _cache_get((model, query, hashes[n]))
            if cached is None:
                todo.append(n)
            else:
                scores[query][n] = cached
        if todo:
            fresh = scorer.score_many(query, [texts[n] for n in todo])
            if len(fresh) != len(todo):
                raise ScoringError(f"scorer returned {len(fresh)} scores for {len(todo)} documents")
            for n, s in zip(todo, fresh, strict=True):
                scores[query][n] = float(s)
                _cache_put((model, query, hashes[n]), float(s))

    rows: list[PreviewRow] = []
    for bill in bills:
        per_rule = [scores[q][bill.number] for q in queries]
        mean = profile_score(per_rule) or 0.0
        rows.append(
            PreviewRow(
                bill_number=bill.number,
                title=bill.title,
                profile_score=mean,
                matched=is_match(mean, threshold),
                rule_scores=per_rule,
            )
        )
    rows.sort(key=lambda r: r.profile_score, reverse=True)  # stable: recent first on ties
    return rows
