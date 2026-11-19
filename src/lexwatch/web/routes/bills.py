"""Bills list (home) and bill detail (stream 3).

Home: bills relevant to the selected profile (``matches.matched``), or all bills with the
toggle; PostgreSQL full-text search plus exact number match; simple prev/next pagination.
htmx requests targeting ``#results`` get only the table fragment (``bills/_table.html``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, func, null, or_, select
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.i18n import t
from lexwatch.models import SEARCH_CONFIG, Bill, BillEvent, Match, Profile, Rule, RuleScore, User
from lexwatch.web.deps import get_lang, require_user
from lexwatch.web.templating import render

router = APIRouter(tags=["bills"])

PAGE_SIZE = 50
MAX_PAGE = 100_000  # keeps OFFSET far below bigint overflow
SESSION_PROFILE_KEY = "profile_id"
EVENT_VALUE_MAX = 200


def visible_profiles(db: Session, user: User) -> list[Profile]:
    """Profiles the user may see: their own, or all of them for admins."""
    stmt = select(Profile).order_by(func.lower(Profile.name), Profile.id)
    if not user.is_admin:
        stmt = stmt.where(Profile.owner_id == user.id)
    return list(db.scalars(stmt))


def format_dt(value: datetime | None, request: Request) -> str:
    """Local date and time (``settings.timezone``), minute precision."""
    if value is None:
        return ""
    tz = ZoneInfo(request.app.state.settings.timezone)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def format_score(value: float | None, lang: str) -> str:
    """Two decimals with the locale decimal separator (same as the ``score`` macro)."""
    if value is None:
        return ""
    text = f"{value:.2f}"
    return text.replace(".", ",") if lang == "fr" else text


def is_htmx_fragment(request: Request, target: str) -> bool:
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
        and request.headers.get("HX-Target") == target
    )


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def bill_order() -> tuple[Any, ...]:
    # Numbers are strings ("8410"); length first gives numeric order for plain numbers.
    return (
        Bill.filing_date.desc().nullslast(),
        func.length(Bill.number).desc(),
        Bill.number.desc(),
    )


def search_filter(q: str) -> Any:
    """Full-text match (French config, websearch syntax) or exact bill number."""
    number = q.lstrip("0") or q
    return or_(
        Bill.search_vector.op("@@")(func.websearch_to_tsquery(SEARCH_CONFIG, q)),
        Bill.number == number,
    )


@router.get("/")
def bills_index(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    params = request.query_params
    profiles = visible_profiles(db, user)
    by_id = {p.id: p for p in profiles}

    requested = _int(params.get("profile"))
    if requested is not None and requested in by_id:
        request.session[SESSION_PROFILE_KEY] = requested
    remembered = _int(request.session.get(SESSION_PROFILE_KEY))
    profile = by_id.get(requested) or by_id.get(remembered) or (profiles[0] if profiles else None)

    q = (params.get("q") or "").replace("\x00", "").strip()[:200]
    show_all = params.get("all") in ("1", "on", "true") or profile is None
    page = min(max(_int(params.get("page"), 1) or 1, 1), MAX_PAGE)

    if profile is not None:
        stmt = select(Bill, Match.profile_score).outerjoin(
            Match, and_(Match.bill_number == Bill.number, Match.profile_id == profile.id)
        )
        if not show_all:
            stmt = stmt.where(Match.matched.is_(True))
    else:
        stmt = select(Bill, null())
    if q:
        stmt = stmt.where(search_filter(q))
    stmt = stmt.order_by(*bill_order()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)
    rows = db.execute(stmt).all()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    since = user.previous_visit_at
    items = [
        {
            "bill": bill,
            "score": score,
            "new": since is not None and bill.last_changed_at > since,
        }
        for bill, score in rows
    ]

    def page_url(n: int) -> str:
        query: dict[str, Any] = {}
        if profile is not None:
            query["profile"] = profile.id
        if q:
            query["q"] = q
        if show_all and profile is not None:
            query["all"] = "1"
        if n > 1:
            query["page"] = n
        return "/" + ("?" + urlencode(query) if query else "")

    context = {
        "profiles": profiles,
        "profile": profile,
        "q": q,
        "show_all": show_all,
        "items": items,
        "page": page,
        "prev_url": page_url(page - 1) if page > 1 else None,
        "next_url": page_url(page + 1) if has_next else None,
    }
    template = "bills/_table.html" if is_htmx_fragment(request, "results") else "bills/index.html"
    return render(request, template, context, headers={"Vary": "HX-Request, HX-Target"})


def _http_url(value: object) -> str | None:
    """Only http(s) URLs from source data become links."""
    if isinstance(value, str) and value.lower().startswith(("https://", "http://")):
        return value
    return None


def _https(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") else None


def bill_extra(bill: Bill) -> dict[str, Any]:
    """Display data from ``bill.extra`` (ingestion; optional until the column exists)."""
    extra = getattr(bill, "extra", None) or {}
    if not isinstance(extra, dict):
        extra = {}
    documents = []
    for doc in extra.get("documents") or []:
        if isinstance(doc, dict) and _https(doc.get("url")):
            documents.append(
                {
                    "label": str(doc.get("label") or doc["url"]),
                    "date": str(doc.get("date") or ""),
                    "url": doc["url"],
                }
            )
    return {
        "committee": str(extra.get("committee") or "") or None,
        "rapporteur": str(extra.get("rapporteur") or "") or None,
        "pdf_url": _https(extra.get("dossier_pdf_url")),
        "documents": documents,
    }


@router.get("/bills/{number}")
def bill_detail(
    number: str, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    bill = db.get(Bill, number) if "\x00" not in number and len(number) <= 32 else None
    if bill is None:
        raise HTTPException(status_code=404)

    events = db.scalars(
        select(BillEvent)
        .where(BillEvent.bill_number == number)
        .order_by(BillEvent.changed_at.desc(), BillEvent.id.desc())
    )
    history = [
        {
            "changed_at": format_dt(e.changed_at, request),
            "field": e.field,
            "old": (e.old_value or "")[:EVENT_VALUE_MAX],
            "new": (e.new_value or "")[:EVENT_VALUE_MAX],
        }
        for e in events
    ]

    profiles = visible_profiles(db, user)
    profile_ids = [p.id for p in profiles]
    matches = {
        m.profile_id: m
        for m in db.scalars(
            select(Match).where(Match.bill_number == number, Match.profile_id.in_(profile_ids))
        )
    }
    rule_rows = db.execute(
        select(Rule, RuleScore.score)
        .outerjoin(RuleScore, and_(RuleScore.rule_id == Rule.id, RuleScore.bill_number == number))
        .where(Rule.profile_id.in_(profile_ids))
        .order_by(Rule.profile_id, Rule.position, Rule.id)
    ).all()
    rules_by_profile: dict[int, list[tuple[str, float | None]]] = {}
    for rule, score in rule_rows:
        rules_by_profile.setdefault(rule.profile_id, []).append((rule.text, score))

    lang = get_lang(request)
    scores = [
        {
            "profile": p,
            "match": matches.get(p.id),
            "lines": [
                t("bill.scores.rule", lang, rule=text, score=format_score(score, lang) or "–")
                for text, score in rules_by_profile.get(p.id, [])
            ],
        }
        for p in profiles
    ]
    return render(
        request,
        "bills/detail.html",
        {
            "bill": bill,
            "history": history,
            "scores": scores,
            "extra": bill_extra(bill),
            "dossier_url": _http_url(bill.dossier_url)
            or f"https://www.chd.lu/fr/dossier/{quote(bill.number)}",
        },
    )
