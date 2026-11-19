# Build prompt: lexwatch

Repository name: `lexwatch`

---

## 1. Mode of work

You are the coordinator of an autonomous build. Work end to end without asking me anything.

- **No questions.** Do not use AskUserQuestion or stop for confirmation. When something is unclear, choose the simplest reasonable option, record it in `DECISIONS.md` (date, decision, reason, alternatives), and continue.
- **You coordinate, subagents execute.** Plan the work, split it into independent tasks with clear scopes and acceptance criteria, and run them in parallel wherever dependencies allow. Do not write large parts of the code yourself; your job is planning, integration, conflict resolution and quality.

### Team

| Role | Count | Responsibility |
|---|---|---|
| **Researchers** | 2 to 3, in parallel at the start | Verify facts before code is written: data.public.lu API and datasets 112/121 (real columns, stable URLs), chd.lu page structure, `robots.txt` and terms of use, Foundry Rerank API (endpoint, model names, request and response format, auth, limits). Each delivers a short note in `docs/research/` with sources, and saves sample responses to `tests/fixtures/`. |
| **Architect** | 1 | Repo layout, data model, Alembic baseline, and the interfaces between modules (source adapters, `Scorer`, notifier). Delivers stubs and contracts so developers can work in parallel without waiting on each other. |
| **Developers** | 4, in parallel | (1) Ingestion: adapters, normalization, upsert, history. (2) Scoring: Foundry scorer, local fallback, averaging, threshold, rescoring. (3) Web/UI: routes, templates, htmx, i18n, section 7 style. (4) Auth + email: OIDC, roles, sessions, CSRF, digest. |
| **Testers** | 1 per developer stream, plus 1 integration tester | Write and run tests for each stream independently of the developer. The integration tester runs the full stack with Docker Compose and tests end to end. |
| **QA reviewer** | 1 | Did not write any code. Reviews each merged phase against this prompt, checks the UI against section 7 with screenshots, and reports issues back to you for assignment. |

### Order of work

1. **Research** (parallel) and **architecture** (starts as soon as the first research notes land).
2. **Development** (four streams in parallel, each with its tester), built on the architect's contracts.
3. **Integration:** merge the streams, run end to end, fix issues by assigning them back to the right developer.
4. **QA review and fixes** until section 12 is met.

### Git workflow (fully managed by you)

- **You own all git operations:** branches, commits, pushes and merges. I will never review or approve a PR.
- **Parallel work:** give each developer its own branch (`feat/<stream>`) and, where supported, its own git worktree so parallel agents never touch the same working copy.
- **Commits:** small and frequent, with clear messages (Conventional Commits style).
- **Merging:** you merge each branch into `main` yourself once its tests pass and the QA reviewer has no blocking issues. Resolve conflicts yourself. Merge directly; if branch protection forces a PR, create it with `gh` and merge it yourself straight away.
- **`main` stays green:** run ruff and the full test suite on the merged result before you push `main`. Never force-push `main`. Never rewrite published history.
- **Push** `main` and active branches regularly so nothing is lost if the session stops.
- **End state:** all work merged into `main` and pushed, feature branches and worktrees deleted, `main` passing every check. The session ends only when this is true.

- **Verification before you call it done.** Run the full stack with Docker Compose, ingest real data, score it, and look at every screen.

---

## 2. Goal

A small internal web app that tracks Luxembourg bills (projets de loi and propositions de loi) filed with the Chambre des Députés. It:

- collects new and updated bills every day,
- scores each bill against rules that users define,
- shows the relevant ones in a minimal web UI,
- sends a daily email digest.

The client hosts it on their own infrastructure.

---

## 3. Data sources

Check every source against the live site before you write a parser. Do not rely on assumptions. Save sample responses in `tests/fixtures/` for offline tests.

1. **data.public.lu, dataset "Liste des projets et propositions de lois (112)"** from the organization "Chambre des Députés du Grand-Duché de Luxembourg".
   - Use the udata API (`https://data.public.lu/api/1/`) to find the dataset and its latest resource. Do not hard-code a timestamped download URL.
   - Expected fields: bill number, type, filing date, closing date, status, title. Confirm the real columns.
   - Known limitation: this dataset was last refreshed in July 2024, and the Chamber refreshes it monthly at best. Use it as the baseline.
2. **data.public.lu, dataset "Etat des travaux (121)"** (XLSX, column definitions in the `LISTE_COLONNES` tab). Use it if it adds useful fields or links.
3. **chd.lu, parliamentary documents** for the daily delta (new bills, status changes, links to the dossier and documents).
   - Check `robots.txt` and the site's terms of use first. Record what you found in `DECISIONS.md`.
   - Fetch politely: one request at a time, a delay between requests, a clear User-Agent, and a cache of what was already fetched.
   - Isolate this in its own adapter so it can be removed once dataset 112 updates daily.
   - If the parse fails or the page structure changes, log a clear error, mark the run as failed on the admin page, and keep the existing data.
4. **Attribution:** show "Source: Chambre des Députés du Grand-Duché de Luxembourg" with a link to the source in the footer and on each bill page.

Merge rule: the bill number is the key. chd.lu data wins for status and dates when it is newer. Every status change goes into the history table.

---

## 4. Stack and dependency budget

Keep dependencies few. Anything outside this list must be justified in `DECISIONS.md`.

| Area | Choice |
|---|---|
| Language | Python 3.12 |
| Web | FastAPI + Uvicorn, Jinja2 server-rendered templates |
| Interactivity | htmx, one vendored file in `static/` (no CDN, no npm, no JS build) |
| CSS | One hand-written stylesheet, no framework |
| Database | PostgreSQL 16, SQLAlchemy 2 (sync is fine), psycopg 3, Alembic |
| Full-text search | PostgreSQL built-in (`french` text search configuration) for the search box |
| HTTP client | httpx |
| HTML parsing | selectolax (or BeautifulSoup if selectolax cannot handle the pages; record the choice) |
| XLSX | openpyxl |
| Auth | Authlib (OIDC), signed session cookie |
| Scoring | Cohere Rerank on Microsoft Foundry, called over REST with httpx (no Cohere SDK). azure-identity only for Entra ID token auth |
| Local fallback scoring (optional extra, not in the default image) | onnxruntime + tokenizers, installed only with `pip install lexwatch[local]` |
| Email | Python standard library `smtplib` + `email` |
| i18n | Two small message catalogs (FR, EN) loaded at startup, no gettext toolchain |
| Scheduling | A `worker` service running a simple loop that triggers the daily job at a configured time. No APScheduler, no Celery, no Redis |
| Tests | pytest, FastAPI TestClient |
| Lint/format | ruff |

Packaging: `pyproject.toml`, pinned versions in a lock file (`uv` is fine).

---

## 5. Relevance scoring

This is the core feature. Use a dedicated relevance model (a reranker), not a generative LLM. The data is public, so a hosted endpoint on Microsoft Foundry is allowed and is the default.

### Concepts

- **Profile:** a named watch subject, for example "AML and financial regulation". It has an owner, email recipients, a threshold, and a list of rules.
- **Rule:** a short natural-language statement of what matters, in French or English. Example: "Lutte contre le blanchiment de capitaux et le financement du terrorisme".
- **Score:** for each bill and each rule, the model returns a relevance score between 0 and 1.
- **Profile score:** the plain average of the rule scores for that bill.
- **Match:** the bill is relevant to the profile when the profile score is at or above the profile's threshold.

### Model

- **Default: Cohere Rerank 5 on Microsoft Foundry** (`Cohere-rerank-v5-pro`, or `Cohere-rerank-v5-fast` to save cost).
  - Rerank 5 returns a calibrated relevance score between 0 and 1 for each document and supports 100+ languages, French included. Calibrated scores matter here: they make the average and the threshold meaningful.
  - API: Cohere's v2 rerank format. Foundry endpoint pattern: `https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank`. The request must include the `model` (deployment) name. Read each `results[].relevance_score`.
  - Verify the exact endpoint, model name and request format against the current Microsoft Foundry and Cohere documentation before coding. Do not trust these notes blindly.
- **Fallback hosted model:** `Cohere-rerank-v4.0-pro` or `-fast`, same API, selected by configuration.
- **Model names are configuration, never code.** Foundry retires model versions regularly (Rerank 3.5 is already retired), so switching versions must be an env change plus a rescore.
- **Optional local fallback:** `BAAI/bge-reranker-v2-m3` as ONNX with onnxruntime on CPU, sigmoid-normalized to 0 to 1. Only for an offline or no-Azure setup. Packaged as an optional extra, with model files downloaded by `lexwatch download-model` into a volume. Not part of the default image.
- **Pluggable interface:** `Scorer.score_many(rule_text, bill_texts) -> list[float]`, selected by `SCORER=foundry|local`.
- **Bill text to score:** title, plus any summary or object text available from the sources, truncated to the model's input limit. Record what text you used in `DECISIONS.md`.

### Calling the endpoint

- **One request per rule:** the rule is the `query`, and the new or changed bills are the `documents`. Batch within the API's document limit.
- **Authentication:** Microsoft Entra ID (managed identity or service principal, via azure-identity) as the default; API key as an alternative. Both configured through env vars.
- **Deployment type:** Global Standard is acceptable because the data is public. Make the endpoint configurable so the client can point to a Data Zone (EU) deployment if their policy requires it. Record this in the README.
- **Resilience:** timeouts, retries with exponential backoff on 429 and 5xx, and a circuit breaker for the run. If scoring fails, keep the bills, mark them `unscored`, show it on the admin page, and retry on the next run. Never drop data because the endpoint is down.
- **Cost tracking:** log the billed units returned by the API for each run and show the monthly total on the admin page. The expected volume (about 250 bills a year, a few dozen profiles) should cost very little; confirm with real numbers in `DECISIONS.md`.
- **Tests:** use a fake scorer and recorded API responses. Unit tests never call Foundry. One optional integration test runs only when the Foundry env vars are present.

### Behavior

- Score new and changed bills during the daily job.
- Rescore all bills of a profile when its rules or threshold change, as a background task that the UI shows as in progress.
- Store each rule score, so the bill page can show which rules drove the result (on hover, see section 7).
- Calibrate a sensible default threshold on real data and record how you chose it. Each profile can override it.
- Store the model name with every score. When the model changes, rescore everything so scores stay comparable.

---

## 6. Features and screens

All screens available in French and English, switchable, with the choice remembered per user. Default: French.

1. **Bills (home):**
   - Table of bills relevant to the selected profile, newest first: number, title, type, status, filing date, score.
   - Profile selector, a search box, and a toggle to show all bills.
   - New or changed since the user's last visit: shown by text weight only, no badge.
2. **Bill detail:**
   - Metadata, status history, link to the dossier on chd.lu.
   - Score per profile; the per-rule scores appear on hover.
3. **Rules (dedicated section):**
   - List of profiles. Create, edit, delete.
   - For each profile: name, rules (add, edit, remove, reorder), threshold, email recipients.
   - Preview: while editing, show which recent bills would match with the current rules and threshold.
4. **Admin (admin role only):**
   - Last runs: time, duration, bills added or changed, errors.
   - Run now, rescore all.
   - CSV export of bills and matches.
5. **Email digest:** one email per profile per day, only when there is something new or changed. Plain HTML and text versions, same minimal style, links back to the app. Sent through the client's SMTP relay (host, port, TLS, credentials from env).

Roles: `admin` and `user`, mapped from an OIDC group or role claim named in configuration. Users manage their own profiles; admins manage all of them.

---

## 7. UI style: minimal

This matters as much as the features.

- **Only what is essential.** Every element on screen must earn its place. When in doubt, remove it.
- **No label clutter.** No small labels, tags, chips, pills, badges, helper captions, or "info" lines scattered around the page. This "AI dashboard" look is explicitly unwanted.
- **Guidance on hover.** When guiding information is needed, show it on mouse hover, using the `title` attribute or a small CSS tooltip. Also connect it with `aria-describedby` so keyboard and screen-reader users still get it.
- **Layout:** one column, generous white space, plain tables for lists, a simple top bar with the app name, the main sections, the language switch, and the user menu.
- **Typography:** system font stack, two or three sizes at most, hierarchy through weight and spacing rather than color.
- **Color:** near-black text on white, one muted accent color for links and the primary action, one color for errors. No gradients, shadows, or decorative icons.
- **No:** stat cards, charts, emoji, avatars, illustrations, empty-state art, animations beyond a subtle htmx loading indicator.
- **Forms:** short, labels above fields, one primary button per form.
- **Responsive:** readable on a laptop and a tablet. Mobile is not a priority.
- **Accessible:** sufficient contrast, visible focus, everything usable with the keyboard.

The QA reviewer must screenshot each screen (for example with a headless browser run only in the test environment, not as an app dependency) and check it against this section. Fix anything that breaks these rules.

---

## 8. Security and operations

- **OIDC:** issuer URL, client ID, client secret, redirect URI, scopes, and role claim from environment variables. Works with Entra ID and Keycloak.
- **Dev mode:** a `DEV_AUTH` setting that logs in a fixed local user, off by default, refused when `ENV=production`.
- **Sessions:** signed, HttpOnly, Secure, SameSite=Lax cookies. CSRF protection on every form and htmx request that changes data.
- **Secrets:** only from environment variables. Provide `.env.example` with no real values.
- **Outbound network:** only data.public.lu, chd.lu, the Foundry endpoint, the OIDC provider (also used for Entra ID tokens), and the SMTP relay. Configurable proxy support (`HTTPS_PROXY`).
- **Data sent to Foundry:** only public bill text and the rule texts. Never user data. Rule texts are written by users, so say in the README that they are sent to the scoring endpoint.
- **Logging:** structured logs to stdout. No personal data beyond the user's ID and email.
- **Health:** `/health` (app) and `/ready` (database reachable, scorer configured; do not call the paid endpoint on every probe).
- **Containers:**
  - `Dockerfile` (slim, non-root user).
  - `docker-compose.yml` with `app`, `worker`, and `db` services, and a volume for the database. A compose profile `local-scorer` adds the model volume for the optional local fallback.
  - Alembic migrations run at startup.

---

## 9. Data model (starting point, refine as needed)

- `bills`: number (key), type, title, status, filing date, closing date, author or ministry, dossier URL, scored text, source, first seen, last changed.
- `bill_events`: bill number, field, old value, new value, changed at, source.
- `profiles`: name, owner, threshold, recipients, created, updated.
- `rules`: profile, text, position.
- `rule_scores`: rule, bill, score, model, scored at.
- `matches`: profile, bill, profile score, matched (yes or no), computed at.
- `runs`: started, finished, status, counts, error message.
- `users`: OIDC subject, email, name, role, language, last visit.

---

## 10. Deliverables

- Working app as described, runnable with `docker compose up`.
- `README.md` (English): what it does, setup, configuration, Foundry setup (deploy the rerank model, choose the deployment type, grant the app identity access), optional local scorer, OIDC setup for Entra ID and Keycloak, SMTP setup, backup and restore of PostgreSQL.
- `DECISIONS.md`: every choice made without me.
- `docs/operations.md`: daily job, monitoring, what to do when the chd.lu parser breaks or Foundry is unavailable, how to switch model versions and rescore, how to follow Foundry model retirements.
- Tests: parsers against saved fixtures, scoring (average and threshold logic, with a fake scorer), merge and history logic, auth guards, CSRF, i18n coverage (every key exists in FR and EN). Target: the core logic is covered, and every test passes.

---

## 11. Out of scope

- Generative LLMs (chat or completion models) for scoring or summaries. Keep a note in `DECISIONS.md` on how an LLM could later explain why a bill matched.
- AI providers other than Microsoft Foundry for the hosted scorer.
- Tracking bills after they become law (Legilux). Keep a note in `DECISIONS.md` on how it could be added.
- Teams or other notifications beyond email.
- Mobile layout, dark mode, multi-tenant setup.

---

## 12. Definition of done

1. `docker compose up` starts all services from a clean checkout, with only `.env` filled in.
2. The daily job ingests real data from the sources, scores it through Foundry, and records the run with its billed units. If no Foundry credentials are available during the build, use the local scorer for this check and say so in `DECISIONS.md`.
3. A test profile with three rules produces a sensible list of matches. Record the example in `DECISIONS.md`.
4. All screens work in French and English and pass the QA review against section 7.
5. The email digest renders correctly; test it against a local SMTP catcher during development only.
6. ruff is clean and all tests pass.
7. A final summary in `DECISIONS.md`: what was built, known limitations, and recommended next steps.
8. Everything is merged into `main` and pushed, with no open PRs, feature branches or worktrees left, and `main` passing ruff and every test.
