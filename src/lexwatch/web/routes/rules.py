"""Profiles and rules management (stream 3).

Saving rules or the threshold sets ``profile.rescore_status='pending'`` (the worker rescores).
A threshold-only change recomputes matches from stored scores
(``scoring.service.recompute_profile_matches``); if that fails a rescore is queued instead.
Users manage their own profiles; admins manage all of them (403 otherwise, 404 when the
profile does not exist).

The edit form works without JavaScript: the add, remove, up and down buttons post the
unsaved form back with an ``action`` and the page is rendered again with the change applied;
only ``save`` writes. The preview (``POST /rules/preview``) is an htmx fragment loaded when the
user clicks "Preview" (never on typing: each preview may cost scoring requests), also
reachable without JavaScript through the ``preview`` action. A preview that billed units
writes one ``runs`` row with ``kind='preview'`` so the admin monthly total stays complete.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.i18n import t
from lexwatch.jobs.daily import utcnow
from lexwatch.models import Profile, Rule, Run, User
from lexwatch.scoring import service as scoring_service
from lexwatch.scoring.base import ScoringError
from lexwatch.scoring.factory import default_threshold_for, get_scorer, scorer_configured
from lexwatch.web.deps import can_manage_profile, get_lang, require_user
from lexwatch.web.routes.bills import format_score
from lexwatch.web.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/rules", tags=["rules"])

NAME_MAX = 200
RULE_MAX = 1000
MAX_RULES = 50
PREVIEW_LIMIT = 50
PREVIEW_LIMIT_LOCAL = 30  # the local CPU scorer is slow: fewer bills keep the preview usable


def preview_limit(settings: Any) -> int:
    return PREVIEW_LIMIT_LOCAL if settings.scorer == "local" else PREVIEW_LIMIT


EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
MAX_RULES_PARSED = 200  # more is a validation error (never silently dropped) up to this cap
ACTION_RE = re.compile(r"^(remove|up|down)-(\d+)$")
# Control characters never belong in these fields (NUL also breaks PostgreSQL text).
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
INT32_MAX = 2**31 - 1


def _clean(value: object, field_name: str, bad: set[str]) -> str:
    text = str(value or "")
    if CONTROL_RE.search(text):
        bad.add(field_name)
        text = CONTROL_RE.sub("", text)
    return text


@dataclass
class ProfileForm:
    name: str = ""
    threshold: str = ""
    recipients: str = ""
    rules: list[tuple[int | None, str]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    bad_chars: set[str] = field(default_factory=set)
    too_many_rules: bool = False

    @classmethod
    def from_profile(cls, profile: Profile, lang: str) -> ProfileForm:
        threshold = "" if profile.threshold is None else format_threshold(profile.threshold, lang)
        return cls(
            name=profile.name,
            threshold=threshold,
            recipients="\n".join(profile.recipient_list()),
            rules=[(r.id, r.text) for r in profile.rules],
        )

    @classmethod
    async def from_request(cls, request: Request) -> tuple[ProfileForm, str]:
        form = await request.form()
        bad: set[str] = set()
        ids = form.getlist("rule_id")
        texts = form.getlist("rule_text")
        rules: list[tuple[int | None, str]] = []
        for i, text in enumerate(texts[:MAX_RULES_PARSED]):
            raw_id = ids[i] if i < len(ids) else ""
            rule_id = (
                int(raw_id)
                if isinstance(raw_id, str) and raw_id.isdigit() and int(raw_id) <= INT32_MAX
                else None
            )
            rules.append((rule_id, _clean(text, "rules", bad)))
        result = cls(
            # Whitespace (CR/LF included) collapses to single spaces in the name.
            name=" ".join(_clean(form.get("name"), "name", bad).split()),
            threshold=_clean(form.get("threshold"), "threshold", bad).strip(),
            recipients=_clean(form.get("recipients"), "recipients", bad),
            rules=rules,
            bad_chars=bad,
            too_many_rules=len(texts) > MAX_RULES,
        )
        return result, str(form.get("action") or "save")

    def rule_texts(self) -> list[str]:
        return [" ".join(text.split()) for _, text in self.rules if text.strip()]

    def parsed_threshold(self) -> Decimal | None:
        """``None`` for blank (default threshold); raises ``ValueError`` when invalid."""
        if not self.threshold:
            return None
        try:
            value = Decimal(self.threshold.replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError(self.threshold) from exc
        if not value.is_finite() or value < 0 or value > 1:
            raise ValueError(self.threshold)
        return value.quantize(Decimal("0.001"))

    def parsed_recipients(self) -> tuple[list[str], list[str]]:
        parts = re.split(r"[\s,;]+", self.recipients)
        emails = [p for p in (x.strip() for x in parts) if p]
        valid = [e for e in emails if EMAIL_RE.match(e)]
        invalid = [e for e in emails if not EMAIL_RE.match(e)]
        return list(dict.fromkeys(valid)), invalid

    def validate(self) -> tuple[Decimal | None, list[str]]:
        threshold: Decimal | None = None
        recipients: list[str] = []
        if "name" in self.bad_chars:
            self.errors["name"] = "rules.error.chars"
        elif not self.name:
            self.errors["name"] = "rules.error.name"
        elif len(self.name) > NAME_MAX:
            self.errors["name"] = "rules.error.name_long"
        try:
            if "threshold" in self.bad_chars:
                raise ValueError(self.threshold)
            threshold = self.parsed_threshold()
        except ValueError:
            self.errors["threshold"] = "rules.error.threshold"
        recipients, invalid = self.parsed_recipients()
        if "recipients" in self.bad_chars:
            self.errors["recipients"] = "rules.error.chars"
        elif invalid:
            self.errors["recipients"] = "rules.error.recipients"
            self.errors["recipients_detail"] = ", ".join(invalid)
        texts = self.rule_texts()
        if "rules" in self.bad_chars:
            self.errors["rules"] = "rules.error.chars"
        elif self.too_many_rules or len(texts) > MAX_RULES:
            self.errors["rules"] = "rules.error.too_many"
        elif not texts:
            self.errors["rules"] = "rules.error.no_rules"
        elif any(len(text) > RULE_MAX for text in texts):
            self.errors["rules"] = "rules.error.rule_long"
        return threshold, recipients

    def apply(self, action: str) -> str | None:
        """Apply an editing action in place; return the id of the control to focus."""
        if action == "add":
            if len(self.rules) < MAX_RULES:
                self.rules.append((None, ""))
            return f"rule-{len(self.rules) - 1}"
        match = ACTION_RE.match(action)
        if not match:
            return None
        verb, index = match.group(1), int(match.group(2))
        if not 0 <= index < len(self.rules):
            return None
        if verb == "remove":
            del self.rules[index]
            return "add-rule" if not self.rules else f"rule-{min(index, len(self.rules) - 1)}"
        target = index - 1 if verb == "up" else index + 1
        if not 0 <= target < len(self.rules):
            return f"{verb}-{index}"
        self.rules[index], self.rules[target] = self.rules[target], self.rules[index]
        edge = (verb == "up" and target == 0) or (verb == "down" and target == len(self.rules) - 1)
        return f"rule-{target}" if edge else f"{verb}-{target}"


def format_threshold(value: Decimal | float, lang: str) -> str:
    """Same format as scores (``i18n.format_score``): three decimals, 0.5 -> 0.500."""
    return format_score(value, lang)


def get_profile_for(db: Session, user: User, profile_id: int) -> Profile:
    profile = db.get(Profile, profile_id) if 0 < profile_id <= INT32_MAX else None
    if profile is None:
        raise HTTPException(status_code=404)
    if not can_manage_profile(user, profile):
        raise HTTPException(status_code=403)
    return profile


def _default_threshold(request: Request) -> float:
    return float(default_threshold_for(request.app.state.settings))


def _form_context(
    request: Request,
    form: ProfileForm,
    profile: Profile | None,
    focus: str | None = None,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lang = get_lang(request)
    return {
        "form": form,
        "profile": profile,
        "action_url": f"/rules/{profile.id}" if profile else "/rules/new",
        "default_threshold": format_threshold(_default_threshold(request), lang),
        "focus": focus,
        "preview": preview,
    }


@router.get("")
def rules_index(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    stmt = (
        select(Profile, func.count(Rule.id))
        .outerjoin(Rule, Rule.profile_id == Profile.id)
        .group_by(Profile.id)
        .order_by(func.lower(Profile.name), Profile.id)
    )
    if not user.is_admin:
        stmt = stmt.where(Profile.owner_id == user.id)
    rows = [{"profile": p, "rule_count": n} for p, n in db.execute(stmt).all()]
    lang = get_lang(request)
    return render(
        request,
        "rules/index.html",
        {
            "rows": rows,
            "show_owner": user.is_admin,
            "default_threshold": format_threshold(_default_threshold(request), lang),
            "fmt": lambda v: format_threshold(v, lang),
        },
    )


@router.get("/new")
def new_profile(request: Request, user: User = Depends(require_user)):
    form = ProfileForm(rules=[(None, "")])
    return render(request, "rules/edit.html", _form_context(request, form, None))


@router.post("/new")
async def create_profile(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    form, action = await ProfileForm.from_request(request)
    return await _handle_form(request, db, user, form, action, None)


@router.post("/preview")
async def preview(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    form, _ = await ProfileForm.from_request(request)
    data = run_preview(request, db, form, user)
    return render(request, "rules/_preview.html", {"preview": data})


@router.get("/{profile_id}")
def edit_profile(
    profile_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    profile = get_profile_for(db, user, profile_id)
    form = ProfileForm.from_profile(profile, get_lang(request))
    if not form.rules:
        form.rules.append((None, ""))
    return render(request, "rules/edit.html", _form_context(request, form, profile))


@router.post("/{profile_id}")
async def update_profile(
    profile_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    profile = get_profile_for(db, user, profile_id)
    form, action = await ProfileForm.from_request(request)
    return await _handle_form(request, db, user, form, action, profile)


@router.get("/{profile_id}/status")
def profile_status(
    profile_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    profile = get_profile_for(db, user, profile_id)
    return render(request, "rules/_status.html", {"profile": profile})


@router.get("/{profile_id}/delete")
def confirm_delete(
    profile_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    profile = get_profile_for(db, user, profile_id)
    return render(request, "rules/delete.html", {"profile": profile})


@router.post("/{profile_id}/delete")
def delete_profile(
    profile_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    profile = get_profile_for(db, user, profile_id)
    db.delete(profile)
    db.commit()
    log.info("rules.profile_deleted", extra={"profile_id": profile_id, "user_id": user.id})
    if request.session.get("profile_id") == profile_id:
        request.session.pop("profile_id", None)
    return RedirectResponse("/rules", status_code=303)


async def _handle_form(
    request: Request,
    db: Session,
    user: User,
    form: ProfileForm,
    action: str,
    profile: Profile | None,
) -> Response:
    if action == "preview":
        preview_data = run_preview(request, db, form, user)
        return render(
            request, "rules/edit.html", _form_context(request, form, profile, preview=preview_data)
        )
    if action != "save":
        focus = form.apply(action)
        return render(request, "rules/edit.html", _form_context(request, form, profile, focus))

    threshold, recipients = form.validate()
    if form.errors:
        if not form.rules:
            form.rules.append((None, ""))
        return render(
            request, "rules/edit.html", _form_context(request, form, profile), status_code=422
        )

    if profile is None:
        profile = Profile(owner_id=user.id, name=form.name)
        db.add(profile)
        old_rules: dict[int, Rule] = {}
        old_threshold: Decimal | None = None
    else:
        old_rules = {r.id: r for r in profile.rules}
        old_threshold = profile.threshold

    profile.name = form.name
    profile.threshold = threshold
    profile.recipients = "\n".join(recipients)

    rules_changed = False
    kept: set[int] = set()
    position = 0
    new_rules: list[Rule] = []
    for rule_id, raw_text in form.rules:
        text = " ".join(raw_text.split())
        if not text:
            continue
        rule = old_rules.get(rule_id) if rule_id is not None else None
        if rule is None or rule.id in kept:
            rule = Rule(text=text, position=position)
            rules_changed = True
        elif rule.text != text:
            # New text, new meaning: stored scores of this rule no longer apply.
            scoring_service.invalidate_rule_scores(db, rule.id)
            rule.text = text
            rules_changed = True
        rule.position = position
        if rule.id is not None:
            kept.add(rule.id)
        new_rules.append(rule)
        position += 1
    if set(old_rules) - kept:
        rules_changed = True
    profile.rules = new_rules
    db.flush()

    if rules_changed:
        profile.rescore_status = "pending"
    elif old_threshold != threshold:
        _recompute_matches(request, db, profile)
    db.commit()
    log.info("rules.profile_saved", extra={"profile_id": profile.id, "user_id": user.id})
    return RedirectResponse("/rules", status_code=303)


def _recompute_matches(request: Request, db: Session, profile: Profile) -> None:
    """Threshold-only change: recompute matches from stored scores, else queue a rescore."""
    try:
        with db.begin_nested():
            scoring_service.recompute_profile_matches(db, profile.id, request.app.state.settings)
    except Exception:
        log.exception("rules.compute_matches_failed", extra={"profile_id": profile.id})
        profile.rescore_status = "pending"


def _record_preview_billing(db: Session, scorer: Any, user: User) -> None:
    """One ``runs`` row (kind ``preview``) per preview that billed units, for the monthly total.

    Previews served entirely from stored scores or the in-process cache bill nothing and leave
    no row. Never raises: a failed insert must not break the preview.
    """
    units = int(getattr(scorer, "billed_units", 0) or 0)
    if units <= 0:
        return
    try:
        now = utcnow()
        unit = getattr(scorer, "billing_unit", None)
        db.add(
            Run(
                kind="preview",
                status="success",
                requested_by=user.id,
                started_at=now,
                finished_at=now,
                billed_units=units,
                details={"billing": {"unit": unit}} if unit else {},
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("rules.preview_billing_failed", extra={"user_id": user.id})


def run_preview(request: Request, db: Session, form: ProfileForm, user: User) -> dict[str, Any]:
    """Score the unsaved rules against recent bills; returns template data (never raises).

    Billed units of the scoring requests are recorded as a ``preview`` run, even when the
    preview fails part-way (the requests that succeeded were billed).
    """
    texts = form.rule_texts()
    if not texts:
        return {"error": "rules.preview.no_rules"}
    try:
        threshold_value = form.parsed_threshold()
    except ValueError:
        return {"error": "rules.error.threshold"}
    settings = request.app.state.settings
    threshold = (
        float(threshold_value) if threshold_value is not None else _default_threshold(request)
    )
    if not scorer_configured(settings):
        return {"error": "rules.preview.unavailable"}
    scorer = None
    try:
        scorer = get_scorer(settings)
        rows = scoring_service.preview_matches(
            db, scorer, texts, threshold, limit=preview_limit(settings)
        )
    except NotImplementedError:
        return {"error": "rules.preview.unavailable"}
    except ScoringError:
        log.warning("rules.preview_scoring_failed")
        return {"error": "rules.preview.failed"}
    except Exception:
        log.exception("rules.preview_failed")
        return {"error": "rules.preview.failed"}
    finally:
        if scorer is not None:
            _record_preview_billing(db, scorer, user)
    lang = get_lang(request)
    matched = [
        {
            "row": row,
            "lines": [
                t("bill.scores.rule", lang, rule=text, score=format_score(s, lang))
                for text, s in zip(texts, row.rule_scores, strict=False)
            ],
        }
        for row in rows
        if row.matched
    ]
    return {"matched": matched, "total": len(rows)}
