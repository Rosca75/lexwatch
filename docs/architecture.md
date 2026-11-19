# lexwatch architecture

Binding spec: `docs/build-prompt.md`. Research notes: `docs/research/`. This document holds
the module map, the contracts between modules, the file ownership of each developer stream,
and shared conventions. If you need a contract changed, ask the coordinator. Do not edit a file
your stream does not own.

## 1. Runtime overview

```
            +-------------------- docker compose --------------------+
 browser -> | app (lexwatch serve)        worker (lexwatch worker)   |
            |  FastAPI + Jinja + htmx      poll loop every 15 s      |
            |        \                       /                       |
            |         +------ db (postgres:16) ------+               |
            +--------------------------------------------------------+
 outbound:  data.public.lu, chd.lu (ingestion) | Foundry rerank (scoring)
            OIDC provider (login, Entra tokens) | SMTP relay (digest)
```

- **app** serves the UI. It never runs ingestion or scoring. Long work is queued in the DB.
- **worker** runs the daily job, queued manual runs and pending profile rescores.
- Both containers run `lexwatch migrate` at start. A Postgres advisory lock makes this safe when
  they start together.

## 2. Repository layout

```
pyproject.toml, uv.lock        hatchling, Python 3.12; extra `local`; dev group pytest/ruff
alembic.ini, migrations/       env.py reads DATABASE_URL; versions/0001_baseline.py
Dockerfile, docker-entrypoint.sh, docker-compose.yml, .env.example, .dockerignore, .gitignore
src/lexwatch/
  config.py        Settings (frozen dataclass) + get_settings() (lru_cache)
  db.py            get_engine(), make_sessionmaker(), session_scope(), get_db (FastAPI dep)
  models.py        SQLAlchemy 2 typed models, enum value tuples, TRACKED_BILL_FIELDS
  logging.py       JSON logs to stdout (configure_logging)
  http.py          make_client(settings) -> httpx.Client (User-Agent, timeout, proxy via env)
  cli.py           lexwatch migrate|serve|worker|run-job|rescore [--profile ID|--all]|download-model|send-digest
  i18n/            __init__.py (t, catalog, load_catalogs), fr.json, en.json (+ part files)
  sources/         base.py (BillRecord, SourceAdapter, SourceError), datapublic.py, chd.py
  ingest/          merge.py (upsert_bills, MergeStats), pipeline.py (run_ingestion, IngestResult)
  scoring/         base.py (Scorer, ScoringError, ScorerUnavailable), foundry.py, local.py,
                   fake.py, factory.py (get_scorer, scorer_configured), text.py (bill_text),
                   service.py (score_pending_bills, rescore_profile, compute_matches, preview)
  notify/          digest.py (Digest, build_digest, send_digests), mailer.py (smtplib)
  jobs/            daily.py (create_run, execute_run, run_daily_job), worker.py (tick, loop),
                   rescore.py (execute_rescore, execute_rescore_all, queue_rescore_all,
                   finish_rescore_status, recompute_default_threshold_matches)
  web/
    app.py         create_app(settings): middleware, static, routers, error handlers
    templating.py  Jinja env, per-request globals, render()
    deps.py        current_user, require_user, require_admin, get_lang, dev users
    csrf.py        get_csrf_token, verify_csrf (global dependency)
    auth.py        /login, /auth/callback, /logout
    routes/        bills.py, rules.py, admin.py, health.py, i18n.py
    templates/     base.html, error.html, bills/, rules/, admin/, email/ (+ auth/ for stream 4)
    static/        style.css, htmx.min.js (htmx 2.0.4, vendored)
tests/
  conftest.py, helpers.py, test_smoke.py, test_jobs_skeleton.py
  ingest/, scoring/, web/, auth/, integration/   one package per stream (each has __init__.py)
  fixtures/        owned by the researchers (read-only for everyone else)
```

## 3. Data model

String columns with CHECK constraints instead of PG enums. Allowed values are exported from
`lexwatch.models` (`ROLES`, `LANGUAGES`, `SCORE_STATES`, `RESCORE_STATUSES`, `RUN_KINDS`,
`RUN_STATUSES`). All timestamps are `timestamptz` and stored in UTC.

| Table | Columns (key points) |
|---|---|
| `users` | id, `oidc_sub` unique, email, name, role `admin`/`user`, language `fr`/`en`, last_visit_at, previous_visit_at, created_at |
| `bills` | `number` PK (string, e.g. `"8410"`), type, title, status, filing_date, closing_date, author, dossier_url, summary, scored_text, score_state `pending`/`scored`/`unscored`, source, first_seen_at, last_changed_at, source_updated_at, `search_vector` (generated `to_tsvector('french', number ' ' title ' ' summary)`, GIN index) |
| `bill_events` | id, bill_number FK, field, old_value, new_value, changed_at, source |
| `profiles` | id, name, owner_id FK users, threshold numeric(4,3) NULL (NULL = default), recipients text, rescore_status `idle`/`pending`/`running`, last_digest_at, created_at, updated_at |
| `rules` | id, profile_id FK (cascade), text, position |
| `rule_scores` | id, rule_id FK (cascade), bill_number FK, score float, model, scored_at, unique(rule_id, bill_number) |
| `matches` | id, profile_id FK (cascade), bill_number FK, profile_score float, matched bool, computed_at, unique(profile_id, bill_number) |
| `runs` | id, kind `daily`/`manual`/`rescore`/`preview`, status `queued`/`running`/`success`/`partial`/`failed`, requested_by FK users (SET NULL), created_at, started_at, finished_at, bills_added, bills_changed, bills_scored, bills_unscored, billed_units, error_message, details JSONB |

Refinements to the requested model (and why):

- `runs.created_at` was added. A queued run has no `started_at`, and the worker needs a queue
  order and a "daily run already created today" check.
- `runs.requested_by` is an FK to `users.id` with `ON DELETE SET NULL`.
- Bill FKs use `ON UPDATE CASCADE` and `ON DELETE CASCADE`, so a renumbered or removed bill
  never leaves orphan rows. Bills are never deleted in normal operation.
- `profiles.owner_id` is `ON DELETE RESTRICT`, so deleting a user never silently deletes their
  profiles.
- `rule_scores.model` holds `scorer.model_name`. Scores from another model count as stale.
- `runs.billed_units` is an integer of whatever unit the endpoint reports. Rerank v5 reports
  `input_tokens` and v4 reports `search_units` (see `docs/research/foundry-rerank.md`).
  The scoring stream records the unit name in `runs.details["billing"] = {"unit": ...}`.
- `runs.kind='preview'` (migration 0004): a Rules preview that sent scoring requests writes one
  row (`status='success'`, `started_at = finished_at`, `requested_by` = the user,
  `billed_units` > 0). Previews served from stored scores or the cache write nothing. These
  rows count in the monthly total and are hidden from the admin "last runs" table.

Schema changes: models and migrations must stay in sync. `tests/test_smoke.py::
test_models_match_migrations` fails on drift, and `uv run alembic check` does the same check.
Each new migration is a new file `migrations/versions/000N_<slug>.py` with `down_revision`
set to the current head. Only the coordinator merges migrations, and resolves numbering if two
streams add one in parallel.

## 4. Contracts

### Configuration (`lexwatch.config`)

```python
@dataclass(frozen=True)
class Settings: ...                 # every field maps to one env var, documented in .env.example
    @classmethod
    def from_env(cls, environ=None) -> Settings
    def validate(self) -> None        # ValueError: DEV_AUTH or default SECRET_KEY in production, bad SCORER
    is_production: bool; effective_oidc_redirect_uri: str
def get_settings() -> Settings       # cached; tests build Settings directly (tests/helpers.py)
```

Pass `settings` explicitly where you can. Use `get_settings()` only at entry points (CLI, app
factory default).

### Database (`lexwatch.db`)

```python
get_engine(database_url: str | None = None) -> Engine            # cached per URL
make_sessionmaker(engine) -> sessionmaker[Session]                # expire_on_commit=False
session_scope(factory=None) -> ContextManager[Session]            # commit / rollback / close
get_db(request) -> Iterator[Session]                              # FastAPI dependency
```

Routes call `db.commit()` explicitly after writes. Service functions (`upsert_bills`,
`score_pending_bills`, ...) do **not** commit. They flush, and the caller owns the transaction.

### Sources (stream 1): `lexwatch.sources.base`

```python
class SourceError(Exception)          # fetch/parse/structure failure of one source

@dataclass(slots=True)
class BillRecord:
    number: str; type: str | None; title: str; status: str | None
    filing_date: date | None; closing_date: date | None; author: str | None
    dossier_url: str | None; summary: str | None; source: str
    source_updated_at: datetime | None; extra: dict[str, Any]

class SourceAdapter(Protocol):
    name: str                          # "datapublic-112" | "datapublic-121" | "chd"
    def fetch(self, client: httpx.Client) -> list[BillRecord]   # raises SourceError

sources.default_adapters(settings) -> list[SourceAdapter]       # merge order: 112, 121, chd
```

Adapters never touch the DB. They normalize numbers (string with no leading zeros), types
(`projet` | `proposition`), and dates (`date`).

### Ingestion (stream 1): `lexwatch.ingest`

```python
@dataclass class MergeStats: added: int; changed: int; unchanged: int
upsert_bills(session, records: Iterable[BillRecord], run: Run | None) -> MergeStats

@dataclass class IngestResult:
    added: int; changed: int; unchanged: int
    sources: dict[str, dict]           # name -> {"status": "ok"|"failed"|"skipped", "records": n, "error": str}
    failed_sources: list[str]          # property
run_ingestion(session, run: Run | None, adapters=None, client=None) -> IngestResult
```

Merge rules:

- The bill number is the key.
- chd.lu wins for status and dates when its `source_updated_at` is newer.
- Every change to a field in `models.TRACKED_BILL_FIELDS` writes one `bill_events` row.
- A new or changed bill gets `score_state='pending'` and `last_changed_at=now`.
- A failing adapter is recorded in `IngestResult.sources` and does not abort the others.
  Existing data is never deleted.

Note from research: the paths that chd.lu's `robots.txt` disallows include the dossier pages
(`docs/research/chd.md`). The chd adapter stays isolated behind `CHD_ENABLED`. The coordinator
decides what it may fetch and records the decision in `DECISIONS.md`.

### Scoring (stream 2): `lexwatch.scoring`

```python
class ScoringError(Exception); class ScorerUnavailable(ScoringError)   # circuit open

class Scorer(Protocol):
    model_name: str                    # stored in rule_scores.model
    max_documents: int                 # per upstream request (default FOUNDRY_MAX_DOCUMENTS=100)
    billed_units: int                  # accumulated across calls on this instance
    def score_many(self, rule_text: str, bill_texts: list[str]) -> list[float]  # [0,1], input order

get_scorer(settings) -> Scorer         # SCORER=foundry|local|fake; one fresh instance per run
scorer_configured(settings) -> bool    # cheap check for /ready, never calls the endpoint
bill_text(bill, max_chars=4000) -> str # title + summary, whitespace-collapsed, truncated

@dataclass class ScoreResult: scored: int; unscored: int; billed_units: int; error: str | None
score_pending_bills(session, scorer, run: Run | None) -> ScoreResult
rescore_profile(session, scorer, profile_id: int) -> ScoreResult
compute_matches(session, profile, default_threshold: float | None = None) -> int
preview_matches(session, scorer, rule_texts, threshold, limit=50) -> list[PreviewRow]
profile_score(rule_scores: list[float]) -> float | None     # plain mean (implemented)
is_match(score, threshold) -> bool                          # score >= threshold (implemented)
```

- Threshold: `profile.effective_threshold(settings.default_threshold)`. A NULL column means the
  default.
- Scoring failure: affected bills get `score_state='unscored'` and are retried next run
  (`score_pending_bills` picks up `pending` **and** `unscored`). Nothing is dropped.
- `FakeScorer` is deterministic keyword overlap (accent- and case-insensitive) with
  `model_name='fake'`. It adds 1 billed unit per batch, and `calls` records each call for
  assertions.
- `LocalScorer` lazy-imports onnxruntime and tokenizers (`lexwatch[local]`).
  `download_model(settings)` backs `lexwatch download-model`.

### Notification (stream 4): `lexwatch.notify`

```python
@dataclass class Digest: profile_id; recipients: list[str]; subject; html; text; bill_numbers
build_digest(session, profile, since: datetime | None, lang="fr") -> Digest | None   # None = nothing new
send_digests(session, now=None, dry_run=False) -> int        # all profiles; updates last_digest_at
mailer.build_message(settings, to, subject, html, text) -> EmailMessage   # implemented
mailer.send_message(settings, message) -> None                            # smtplib
```

Email templates: `web/templates/email/digest.html` and `digest.txt`. Links are absolute, built
from `BASE_URL`.

### Jobs (coordinator): `lexwatch.jobs`

```python
daily.create_run(session, kind, requested_by=None) -> Run     # status 'queued', flushed
daily.execute_run(session, run, settings) -> Run              # ingest -> score -> digests; commits per step
daily.run_daily_job(settings, kind="daily", session=None) -> int
worker.tick(session, settings) -> None                        # one poll iteration
worker.run_worker(settings) -> None                           # loop until SIGTERM
```

`execute_run` final status: `success` when every step succeeded, `failed` when none did, and
`partial` otherwise. `error_message` joins the step errors. `billed_units` comes from the
run's scorer.

### Web (`lexwatch.web`)

```python
create_app(settings: Settings | None = None) -> FastAPI
    app.state.settings / engine / sessionmaker / templates
render(request, "path.html", context: dict, status_code=200, headers=None) -> Response
deps.current_user(request, db) -> User | None   # also sets request.state.user
deps.require_user -> User                       # raises NotAuthenticated -> 303 /login (htmx: HX-Redirect)
deps.require_admin -> User                      # 403 for non-admins
deps.get_lang(request) -> "fr"|"en"             # user.language > session["lang"] > DEFAULT_LANGUAGE
csrf.get_csrf_token(request) -> str; csrf.verify_csrf  # global app dependency
```

Session cookie: `lexwatch_session`, signed by `SECRET_KEY`, HttpOnly, SameSite=Lax, Secure when
`SESSION_HTTPS_ONLY` or `ENV=production`, 12 h max age. It holds `user_id`, `lang`, `csrf_token`,
and whatever Authlib needs for the OIDC state.

Routes:

| Route | Owner | Notes |
|---|---|---|
| `GET /health` | architect | 200 `{"status":"ok"}`, no DB |
| `GET /ready` | architect | DB `SELECT 1` and `scorer_configured`, 503 when not ready |
| `GET /login`, `GET /auth/callback`, `POST /logout` | stream 4 | DEV_AUTH: `/login?as=admin\|user` |
| `POST /lang` | stream 3 | form `lang`, `next`; stores on user and in session |
| `GET /`, `GET /bills/{number}` | stream 3 | list (profile selector, search, show all), detail |
| `/rules...` | stream 3 | profiles and rules CRUD, preview (htmx) |
| `/admin...` | stream 3 | runs, Run now, Rescore all, CSV export, monthly billed units |

## 5. Worker and run-state design

There is no scheduler library. The worker polls every `WORKER_POLL_SECONDS` (default 15) and
runs `tick()`:

1. **Daily job.** The daily job is due when local time in `APP_TIMEZONE` (default
   `Europe/Luxembourg`) is at or after `DAILY_JOB_TIME` (`HH:MM`) and no `runs` row with
   `kind='daily'` has `created_at` on or after local midnight today. When it is due, the worker
   inserts the run and executes it at once. A worker that was down at the scheduled time
   catches up when it next starts on the same day.
2. **Queued runs.** The admin "Run now" button inserts
   `runs(kind='manual', status='queued', requested_by=user.id)` with `create_run` and commits.
   The worker claims the oldest queued run with `SELECT ... FOR UPDATE SKIP LOCKED` and
   executes it.
3. **Pending rescores.** Saving a profile's rules sets `profile.rescore_status='pending'`
   (a threshold-only change recomputes matches at once, without scoring). The worker claims one pending profile (`SKIP LOCKED`), sets it to `running`, calls
   `rescore_profile` inside a `runs(kind='rescore', requested_by NULL, details={"profile_id": id})`
   row (so its billed units count in the monthly cost), and then sets it back to `idle` with a
   compare-and-set (`WHERE rescore_status='running'`): a profile saved again meanwhile stays
   `pending` and is rescored once more. A failed rescore is recorded in its run and not retried
   by the worker; its unscored bills and pairs are picked up by the next daily run. The UI shows "in progress" while the
   status is `pending` or `running` (htmx polling on the Rules page). A normal rescore only
   sends pairs without a current-model score.
4. **Rescore all (forced).** The admin button and `lexwatch rescore --all` call
   `jobs.rescore.queue_rescore_all`: it inserts one queued run
   `runs(kind='rescore', status='queued', requested_by=admin, details={"force": true})` (not
   when one is already queued) and sets every idle profile to `pending` so the UI shows it in
   progress. No schema change: the queued run carries the "force" flag. The worker claims it
   in step 2 like any queued run and, because of its kind, executes `execute_rescore_all`:
   each profile in turn goes `running`, is rescored with `force=True` (every bill sent again),
   and goes back to `idle` with the same compare-and-set. Billed units, counts and errors add
   up in that single run. "Run now" ignores a queued rescore run when checking for duplicates.
5. **Model change.** When `FOUNDRY_MODEL` changes, rule scores whose `model` differs from
   `scorer.model_name` are stale. The operator runs `lexwatch rescore`, or uses "Rescore all".
6. **Default threshold change.** At startup (after `recover_interrupted`) the worker runs
   `recompute_default_threshold_matches`: matches of every profile with a NULL threshold are
   recomputed from stored scores (no scoring request), so a new `DEFAULT_THRESHOLD` or scorer
   default takes effect after a restart.

Run status lifecycle: `queued -> running -> success | partial | failed`. Per-source and per-step
detail goes in `runs.details`, for example
`{"sources": {"chd": {"status": "failed", "error": "..."}}, "digests_sent": 2, "digests_failed": 0, "digests_skipped": 5, "billing": {"unit": "input_tokens"}}`.
A failed digest makes the digest step fail (run `partial`); the counts are kept.
The admin page reads `status`, `error_message` and `details`. The monthly cost is
`sum(billed_units)` over `runs.started_at` in the current month, preview runs included.

## 6. Conventions

### i18n

- Use flat dotted keys: `<area>.<thing>[.<detail>]`. Examples: `nav.bills`, `bills.col.title`,
  `rules.form.threshold`, `rules.form.threshold.help` (hover text), `admin.runs.status.failed`,
  `error.csrf`, `digest.subject`.
- Areas: `app`, `nav`, `lang`, `footer`, `bills`, `bill`, `rules`, `admin`, `auth`, `error`,
  `digest`, `email`.
- Formatting uses `str.format`: `t('bill.title', number=bill.number)`. A missing key falls back
  to French, then to the key itself.
- Catalogs: `i18n/fr.json` and `i18n/en.json` (stream 3). Another stream puts its keys in part
  files `i18n/fr.<part>.json` and `i18n/en.<part>.json`, which are merged at load time. A key
  defined twice is an error. Stream 4 uses `fr.auth.json`/`en.auth.json` (`auth.*`) and
  `fr.mail.json`/`en.mail.json` (`digest.*`, `email.*`). Move the existing `digest.subject` and
  `auth.login.title` keys there when you create those files.
- Every key exists in both languages. `test_i18n_catalogs_have_same_keys` enforces this.

### Templates

- Every page `{% extends "base.html" %}` and fills `{% block title %}` and
  `{% block content %}`. Use `{% block head %}` only when a page truly needs it.
- Globals available everywhere: `t(key, **fmt)`, `lang`, `languages`, `csrf_token()`,
  `current_user`, `settings`, `request`, `url_for`.
- htmx fragments live in `templates/<area>/_<name>.html` (leading underscore) and are
  rendered with the same `render()`.
- Hover guidance (spec section 7) uses
  `title="{{ t('x.help') }}" aria-describedby="x-help"`, plus a visually hidden
  `<span id="x-help">` where needed. Do not add helper captions.
- Attribution "Source: Chambre des Députés du Grand-Duché de Luxembourg" with a link to
  https://www.chd.lu appears in the footer (base.html) and on each bill page.

### CSRF

- Every form that changes data includes
  `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`.
- htmx requests send the header from `<body hx-headers='{"X-CSRF-Token": "{{ csrf_token() }}"}'>`
  (already set in base.html).
- `verify_csrf` runs on every POST/PUT/PATCH/DELETE as a global dependency. It returns 403 when
  the token is missing or wrong. Use GET only for safe actions (CSV export is a GET).

### Code

- Python 3.12, ruff (`line-length 100`, rules `E,F,I,UP,B,SIM`). Run
  `uv run ruff check . && uv run ruff format --check .`.
- Logging: `log = logging.getLogger(__name__)`, then
  `log.info("area.event", extra={...})`. Log no personal data beyond user id and email.
- No new runtime dependency without a `DECISIONS.md` entry approved by the coordinator.
- Tests never touch the network. Use `tests/fixtures/` (read-only) and `httpx.MockTransport`.

### Tests

- Run `uv run pytest`. This needs Postgres at `TEST_DATABASE_URL` (default
  `postgresql+psycopg://lexwatch:lexwatch@localhost:55432/lexwatch_test`). The schema is dropped
  and rebuilt with `alembic upgrade head` once per session.
- Fixtures (`tests/conftest.py`):
  - `settings`: test Settings.
  - `engine`.
  - `db`: a Session in a transaction rolled back after each test. `commit()` only releases a
    savepoint.
  - `app`: `get_db` is overridden to return `db`.
  - `client`: auto-logged-in DEV_AUTH admin.
  - `admin_client` and `user_client`.
  - `fake_scorer`.
- Helpers (`tests/helpers.py`):
  - `make_test_settings(**overrides)`.
  - `login_as(client, role)`.
  - `csrf_token(client, path="/")`.
- Each stream puts its tests in its own package: `tests/ingest/`, `tests/scoring/`,
  `tests/web/`, `tests/auth/`. Integration tests go in `tests/integration/`.

## 7. File ownership

Edit only the files your stream owns. To change a shared file, send the change to the
coordinator. Two exceptions:

- **New settings.** A stream may add its own fields inside its own section of `config.py`
  (fields and `from_env`) and of `.env.example`. Keep the additions together so the merge is
  trivial.
- **New migrations.** Follow the rule in section 3.

| Stream | Owns |
|---|---|
| Architect / coordinator | `pyproject.toml`, `uv.lock`, `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_baseline.py`, `src/lexwatch/{__init__,config,db,models,logging,cli}.py`, `src/lexwatch/jobs/**`, `src/lexwatch/web/{app,templating}.py`, `src/lexwatch/web/routes/health.py`, `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, `.env.example`, `.dockerignore`, `.gitignore`, `tests/{__init__,conftest,helpers,test_smoke,test_jobs_skeleton}.py`, `docs/architecture.md` |
| 1 Ingestion | `src/lexwatch/http.py`, `src/lexwatch/sources/**`, `src/lexwatch/ingest/**`, `tests/ingest/**` |
| 2 Scoring | `src/lexwatch/scoring/**`, `tests/scoring/**` |
| 3 Web/UI | `src/lexwatch/web/routes/{bills,rules,admin,i18n}.py`, `src/lexwatch/web/templates/**` except `email/` and `auth/`, `src/lexwatch/web/static/**`, `src/lexwatch/i18n/fr.json`, `src/lexwatch/i18n/en.json`, `tests/web/**` |
| 4 Auth + email | `src/lexwatch/web/{auth,deps,csrf}.py`, `src/lexwatch/web/templates/{auth,email}/**`, `src/lexwatch/notify/**`, `src/lexwatch/i18n/{fr,en}.{auth,mail}.json`, `tests/auth/**` |
| Researchers | `docs/research/**`, `tests/fixtures/**` (read-only for developers) |
| Integration tester | `tests/integration/**` |

Cross-stream touch points (agreed, no shared edits needed):

- Stream 3 calls `scoring.service.preview_matches` (stream 2) for the Rules preview. It sets
  `profile.rescore_status='pending'` on save and inserts queued runs with
  `jobs.daily.create_run`.
- Stream 3 reads `user.previous_visit_at` to mark rows new or changed since the last visit (by
  text weight). Stream 4 maintains it at login: when a new session starts,
  `previous_visit_at = last_visit_at` and `last_visit_at = now`. The DEV_AUTH login does the
  same.
- Stream 4's digest uses `matches`, `bills.last_changed_at` and `bills.first_seen_at`, which
  streams 1 and 2 write.
- Admin CSV export (stream 3) reads `bills` and `matches` directly.
