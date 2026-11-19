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

## Architecture

- **2026-11-19 — No pull requests.** Streams are merged locally into the integration branch and `main` is
  fast-forwarded and pushed directly. *Reason:* the build prompt says nobody reviews PRs and no PR may be left open.
- **2026-11-19 — Work queue in PostgreSQL, not a scheduler library.** "Run now" inserts a `runs` row with status
  `queued`; saving rules sets `profiles.rescore_status='pending'`. The `worker` service polls every 15 s, claims work
  with `SELECT … FOR UPDATE SKIP LOCKED`, and starts the daily job at `DAILY_JOB_TIME` in `APP_TIMEZONE`.
  *Reason:* satisfies "no APScheduler, no Celery, no Redis" and lets the UI show progress from the DB.
  *Alternative:* FastAPI `BackgroundTasks` in the web process (rejected: lost on restart, invisible to the worker).
- **2026-11-19 — Enumerations as strings with CHECK constraints** instead of PostgreSQL enums, so adding a status
  needs no `ALTER TYPE`.
- **2026-11-19 — Extra runtime dependencies beyond the stack table:** `itsdangerous` (required by Starlette's
  signed-session middleware), `python-multipart` (required by FastAPI to read HTML forms), `hatchling` (build
  backend only). Nothing else.
- **2026-11-19 — Migrations at startup** use a PostgreSQL advisory lock so `app` and `worker` can both run
  `lexwatch migrate` when they start together.
- **2026-11-19 — Docker build behind a TLS-intercepting proxy.** The Dockerfile accepts an optional build secret
  `extra_ca` (a CA bundle) used only during `uv sync`; it is not stored in the image. Without the secret the build is
  unchanged.

## Scoring

- **2026-11-19 — Foundry endpoint and auth are configuration.** `FOUNDRY_ENDPOINT` (full
  `https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank` URL), `FOUNDRY_MODEL` (deployment name,
  default `Cohere-rerank-v5-fast`), `FOUNDRY_API_KEY` or Entra ID via azure-identity with `FOUNDRY_TOKEN_SCOPE`.
  *Reason:* no Microsoft Learn page documents this route, its key header or the Entra scope (see
  `docs/research/foundry-rerank.md`); community samples disagree, so the scope and header are configurable.
- **2026-11-19 — Default model `Cohere-rerank-v5-fast`.** v5 scores are calibrated (v4 are not), costs
  $0.02 per million input tokens, and the volume is tiny. v5 is in preview with retirement 2027-02-12, so the
  operator must follow Foundry retirements (see `docs/operations.md`); `Cohere-rerank-v4.0-pro/-fast` is the GA
  fallback.
- **2026-11-19 — Billed units.** v5 reports `input_tokens`, v4 reports `search_units`. `runs.billed_units` stores the
  number and `runs.details.billing.unit` the unit; the admin page sums the current month.
