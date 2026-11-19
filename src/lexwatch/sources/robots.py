"""robots.txt gate for any fetch of a chd.lu page.

``is_allowed(client, url, user_agent)`` downloads ``/robots.txt`` of the URL's host once
(cached for ``CACHE_TTL_SECONDS``) and answers whether ``url`` may be fetched.

The standard library's ``urllib.robotparser`` only does prefix matching: it ignores the ``*``
and ``$`` wildcards that www.chd.lu relies on (``Disallow: /*/dossier/*``), so it would wrongly
allow dossier pages. We use ``RobotFileParser`` semantics for agent groups but match paths
ourselves following RFC 9309: ``*`` matches any sequence, a trailing ``$`` anchors the end, the
longest matching rule wins and ``Allow`` wins a tie.

lexwatch is stricter than the RFC by default (``conservative=True``): a URL matched by any
``Disallow`` rule is refused even when a longer ``Allow`` rule also matches. On www.chd.lu this
matters for pagers such as ``/fr/deputies?page=2``, which ``Allow: /*/deputies`` would
otherwise let through although the site clearly means ``Disallow: /*?page=``.

Failure policy (RFC 9309 section 2.3.1): robots.txt 4xx (other than 401/403) means "everything
allowed"; 401/403, 5xx or a network error means "everything disallowed".
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 24 * 3600


@dataclass(slots=True)
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass(slots=True)
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


@dataclass(slots=True)
class RobotsRules:
    """Parsed robots.txt. ``allow_all`` / ``deny_all`` short-circuit matching."""

    groups: list[_Group] = field(default_factory=list)
    allow_all: bool = False
    deny_all: bool = False

    @classmethod
    def parse(cls, text: str) -> RobotsRules:
        groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if key in {"allow", "disallow"} and current is not None:
                if not value:  # "Disallow:" (empty) allows everything: no rule
                    continue
                current.rules.append(_Rule(key == "allow", value, _compile(value)))
        return cls(groups=groups)

    def _group_for(self, user_agent: str) -> _Group | None:
        token = user_agent.split("/", 1)[0].strip().lower()
        best: _Group | None = None
        best_len = -1
        for group in self.groups:
            for agent in group.agents:
                if agent != "*" and agent and agent in token and len(agent) > best_len:
                    best, best_len = group, len(agent)
        if best is not None:
            return best
        return next((g for g in self.groups if "*" in g.agents), None)

    def allowed(self, url: str, user_agent: str, conservative: bool = True) -> bool:
        if self.deny_all:
            return False
        if self.allow_all:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        if path == "/robots.txt":
            return True
        group = self._group_for(user_agent)
        if group is None:
            return True
        verdict, length = True, -1
        for rule in group.rules:
            if rule.regex.match(path):
                if conservative and not rule.allow:
                    return False
                size = len(rule.pattern)
                if size > length or (size == length and rule.allow):
                    verdict, length = rule.allow, size
        return verdict


_cache: dict[str, tuple[float, RobotsRules]] = {}


def clear_cache() -> None:
    _cache.clear()


def _load(client: httpx.Client, base: str, user_agent: str) -> RobotsRules:
    try:
        response = client.get(
            f"{base}/robots.txt", headers={"User-Agent": user_agent}, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        log.warning("robots.fetch_failed", extra={"host": base, "error": repr(exc)})
        return RobotsRules(deny_all=True)
    if response.status_code in (401, 403) or response.status_code >= 500:
        return RobotsRules(deny_all=True)
    if response.status_code >= 400:
        return RobotsRules(allow_all=True)
    return RobotsRules.parse(response.text)


def is_allowed(client: httpx.Client, url: str, user_agent: str) -> bool:
    """True when ``url`` may be fetched by ``user_agent`` according to its host's robots.txt."""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    now = time.monotonic()
    cached = _cache.get(base)
    if cached is None or now - cached[0] > CACHE_TTL_SECONDS:
        cached = (now, _load(client, base, user_agent))
        _cache[base] = cached
    return cached[1].allowed(url, user_agent)
