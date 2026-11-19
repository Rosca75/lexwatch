"""Daily delta from the Chambre des Députés: plenary votes (data.public.lu dataset 109).

chd.lu HTML is **not** scraped: its robots.txt disallows dossier, search and ajax pages (see
``DECISIONS.md``). Instead this adapter reads the Chambre's daily open-data export of plenary
votes (``109-votes.csv``, regenerated about 04:30 UTC) through the same udata resolution as
datasets 112/121, and turns final votes on bills into status changes:

- one CSV row per deputy per vote (cp1252 today; strict UTF-8 is tried first); rows are
  aggregated per ``(meeting_date, vote_name, vote_type)`` and Oui/Non are counted;
- a vote is a *bill vote* when ``vote_name`` starts with ``PL``/``PPL`` + number (or
  ``Projet de loi N°xxxx`` / ``Proposition de loi N°xxxx``) and is not a motion, an
  amendment or a separate vote on an article;
- only ``vote_type`` "Premier vote constitutionnel", "Second vote constitutionnel" and the
  legacy "Not Found" (2021-2022 rows) count. "Voté" rows are amendments, separate votes and
  motions about a bill, never the vote on the whole bill;
- the latest bill vote per bill wins: ``adopted`` when Oui > Non, otherwise ``rejected``;
  ``closing_date`` = vote date; ``source_updated_at`` = meeting datetime (Europe/Luxembourg).

Isolated behind ``CHD_ENABLED`` so it can be removed once dataset 112 is refreshed daily.
Any future fetch of a chd.lu page must go through ``lexwatch.http.polite_get``, which applies
the robots.txt gate (``lexwatch.sources.robots``).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from lexwatch.sources.base import (
    STATUS_ADOPTED,
    STATUS_REJECTED,
    TYPE_PROJET,
    TYPE_PROPOSITION,
    BillRecord,
    ResourceState,
    SourceError,
    clean_text,
    dossier_url,
    normalize_number,
)
from lexwatch.sources.datapublic import DEFAULT_API_URL, UdataResourceAdapter

SOURCE_CHD = "chd"

DATASET_109 = ("liste-des-votes-en-seances-plenieres", "63c67d5dcfa8113ec4793d0c")
VOTES_TIMEZONE = ZoneInfo("Europe/Luxembourg")
REQUIRED_109_COLUMNS = ("meeting_date", "vote_name", "vote_type", "vote_result")

VOTE_FIRST = "Premier vote constitutionnel"
VOTE_SECOND = "Second vote constitutionnel"
VOTE_LEGACY = "Not Found"
BILL_VOTE_TYPES = (VOTE_FIRST, VOTE_SECOND, VOTE_LEGACY)

_SHORT_RE = re.compile(r"^\s*(PPL|PL)\s?(\d{4}[A-Za-z]?)\b(.*)$", re.S)
_LONG_RE = re.compile(
    r"^\s*(Projet|Proposition)\s+de\s+loi\s*(?:n\s*°|n°|no\.?|nr\.?)?\s*(\d{4}[A-Za-z]?)\b(.*)$",
    re.I | re.S,
)
_EXCLUDE_RE = re.compile(
    r"^\s*(?:MO\b|MO\s?\d|Motion)"  # motions (never match the bill regexes, kept for clarity)
    r"|vote\s+s[ée]par[ée]"  # separate vote on an article
    r"|\bamendement\s*(?:n\s*°|n°|no\.?|\d)"  # "Amendement n°2", "(Amendement 1)"
    r"|\(\s*amendement"
    r"|\bmotion\b",
    re.I,
)


@dataclass(slots=True)
class BillVoteName:
    kind: str  # "PL" | "PPL"
    number: str
    title: str  # short title from the vote name, may be ""


def parse_vote_name(name: str) -> BillVoteName | None:
    """``"PL 8535 - Logements abordables"`` -> (PL, 8535, "Logements abordables").

    Returns ``None`` for motions, amendments, separate votes and non-bill votes.
    """
    if not name or _EXCLUDE_RE.search(name):
        return None
    match = _SHORT_RE.match(name)
    if match:
        kind = match.group(1).upper()
    else:
        match = _LONG_RE.match(name)
        if not match:
            return None
        kind = "PL" if match.group(1).lower() == "projet" else "PPL"
    number = normalize_number(match.group(2))
    if number is None:
        return None
    rest = match.group(3)
    title = re.sub(r"^[\s\-–—:,]+", "", rest.split(" - ", 1)[1] if " - " in rest else rest)
    return BillVoteName(kind=kind, number=number, title=clean_text(title).replace("\n", " "))


@dataclass(slots=True)
class _Vote:
    meeting: datetime
    name: str
    vote_type: str
    counts: dict[str, int] = field(default_factory=dict)


def _decode(content: bytes) -> str:
    """Strict UTF-8 first (with or without BOM), then cp1252 (the current export), then
    latin-1. cp1252 text with accents is almost never valid UTF-8, so the order is safe."""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return content.decode("cp1252")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _vote_rank(vote: _Vote) -> tuple[datetime, int]:
    return (vote.meeting, 1 if vote.vote_type == VOTE_SECOND else 0)


def parse_109_votes(content: bytes, stats: dict[str, Any] | None = None) -> list[BillRecord]:
    """Aggregate per-deputy vote rows into one ``BillRecord`` per voted bill."""
    stats = stats if stats is not None else {}
    reader = csv.DictReader(io.StringIO(_decode(content), newline=""))
    missing = [c for c in REQUIRED_109_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise SourceError(f"{SOURCE_CHD}: votes CSV header changed, missing columns {missing}")
    votes: dict[tuple[str, str, str], _Vote] = {}
    rows = bad_dates = 0
    try:
        for row in reader:
            rows += 1
            key = (row["meeting_date"] or "", row["vote_name"] or "", row["vote_type"] or "")
            vote = votes.get(key)
            if vote is None:
                try:
                    meeting = datetime.strptime(key[0].strip(), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    bad_dates += 1
                    continue
                vote = _Vote(meeting.replace(tzinfo=VOTES_TIMEZONE), key[1].strip(), key[2])
                votes[key] = vote
            result = (row.get("vote_result") or "").strip()
            vote.counts[result] = vote.counts.get(result, 0) + 1
    except csv.Error as exc:
        raise SourceError(f"{SOURCE_CHD}: votes CSV parse error: {exc}") from exc
    if rows and not votes:
        raise SourceError(f"{SOURCE_CHD}: no parsable vote among {rows} rows")

    latest: dict[str, tuple[_Vote, BillVoteName]] = {}
    bill_votes = 0
    for vote in votes.values():
        if vote.vote_type.strip() not in BILL_VOTE_TYPES:
            continue
        parsed = parse_vote_name(vote.name)
        if parsed is None:
            continue
        bill_votes += 1
        current = latest.get(parsed.number)
        if current is None or _vote_rank(vote) >= _vote_rank(current[0]):
            # keep the vote-name title of an earlier vote if the latest one has none
            if current is not None and not parsed.title:
                parsed.title = current[1].title
            latest[parsed.number] = (vote, parsed)
        elif not current[1].title and parsed.title:
            current[1].title = parsed.title

    records: list[BillRecord] = []
    for number, (vote, parsed) in latest.items():
        yes = vote.counts.get("Oui", 0)
        no = vote.counts.get("Non", 0)
        status = STATUS_ADOPTED if yes > no else STATUS_REJECTED
        records.append(
            BillRecord(
                number=number,
                type=TYPE_PROJET if parsed.kind == "PL" else TYPE_PROPOSITION,
                title=parsed.title,
                status=status,
                filing_date=None,
                closing_date=vote.meeting.date(),
                author=None,
                dossier_url=dossier_url(number),
                summary=None,
                source=SOURCE_CHD,
                source_updated_at=vote.meeting,
                extra={
                    "raw_status": f"{vote.vote_type}: {'Oui' if yes > no else 'Non'}",
                    "vote_name": vote.name,
                    "vote_type": vote.vote_type,
                    "vote_date": vote.meeting.isoformat(),
                    "votes": dict(vote.counts),
                },
            )
        )
    stats.update(rows=rows, votes=len(votes), bill_votes=bill_votes, bills=len(records))
    if bad_dates:
        stats["bad_dates"] = bad_dates
    return records


class ChdAdapter(UdataResourceAdapter):
    name = SOURCE_CHD
    dataset_refs = DATASET_109
    resource_format = "csv"
    title_prefix = "109-votes"

    def __init__(self, api_url: str = DEFAULT_API_URL, request_delay_seconds: float = 2.0) -> None:
        super().__init__(api_url, request_delay_seconds)

    def parse(self, content: bytes, state: ResourceState) -> list[BillRecord]:
        return parse_109_votes(content, self.stats)
