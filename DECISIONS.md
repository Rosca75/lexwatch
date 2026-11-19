# Decisions

Every choice made without the product owner, in the order it was made.
Format: date — decision. *Reason.* Alternatives considered.

## Process and repository

- **2026-11-19 — Integration branch.** Work is integrated on `claude/vibrant-brahmagupta-vajiup` (the branch this
  build session is bound to) and `main` is fast-forwarded to it once it is green. Feature streams use
  `feat/<stream>` branches in separate git worktrees and are merged back locally.
  *Reason:* the session environment designates that branch for development; the build prompt asks for
  everything to end up on `main`. *Alternatives:* PR per stream (rejected: the prompt says no PR review is wanted).
- **2026-11-19 — Docker daemon started inside the build container.** Docker was installed but not running; it
  was started with `dockerd` so that `docker compose up` could be verified end to end.
- **2026-11-19 — Tests run against a real PostgreSQL 16.** Full-text search (`french` configuration), generated
  `tsvector` columns and `JSONB` are PostgreSQL features; SQLite would hide bugs. Tests read `TEST_DATABASE_URL`
  (default `postgresql+psycopg://lexwatch:lexwatch@localhost:55432/lexwatch_test`).
  *Alternative:* SQLite in-memory (rejected).

## Data sources

- **2026-11-19 — chd.lu is not scraped; its robots.txt forbids it.** `https://www.chd.lu/robots.txt`
  (Last-Modified 2026-10-23, copy in `tests/fixtures/chd/robots.txt`) disallows `/*/dossier/*`, `/*/search*`,
  `/*/searchactivities`, `/*/ajax/*` and every `?page=` URL, i.e. every page that lists or describes bills.
  The legal notice (https://www.chd.lu/fr/notice-legale) allows non-commercial reproduction with attribution but says
  nothing about automated access; the open-data page (https://www.chd.lu/fr/open-data) points to data.public.lu.
  There is no RSS feed and no public JSON API for bills. *Decision:* the "chd.lu daily delta" adapter uses the
  Chambre's **daily** open-data export of plenary votes (dataset 109 on data.public.lu, regenerated about 04:30 UTC)
  instead of HTML pages. It is isolated in `lexwatch.sources.chd` so it can be removed once dataset 112 is refreshed
  daily. Dossier pages are **linked to, never fetched**. A robots.txt gate (`lexwatch.sources.robots`) is in place
  so that any future chd.lu fetch is refused when robots.txt disallows it.
  *Alternatives:* scrape `/fr/dossier/{n}` anyway (rejected: explicitly disallowed); use the sitemap
  (rejected: sub-sitemaps are `?page=` URLs, also disallowed, and the index was last modified 2025-07-24).
  *Recommended next step for the client:* email the open-data contact (relations.publiques@chd.lu) to ask for
  dataset 112/121 to join the daily refresh, or for written permission to fetch dossier pages at a low rate.
- **2026-11-19 — Known gap.** Dataset 112 stops at bill 8410 (refreshed 2024-07-15) and 121 at dossier 8520
  (refreshed 2025-04-10); bill numbers have reached at least 8767. Bills filed after April 2025 appear only once
  they are put to a plenary vote (number and the short title from the vote name). Status changes before a vote
  are not visible until the Chambre refreshes 112/121.
- **2026-11-19 — Source precedence.** Bill number is the key. Order of application: 112 (baseline) → 121
  (newer snapshot; wins for status, dates, links, authors) → 109 votes (wins for status and closing date when the
  vote is newer than the bill's last known change). Every change of a tracked field is written to `bill_events`.
