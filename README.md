# lexwatch

lexwatch tracks the bills (projets de loi and propositions de loi) filed with the Chambre des
Députés of Luxembourg. Every day it:

1. collects new and changed bills from the Chambre's open data,
2. scores each bill against rules that users write (a reranker model gives a relevance score
   between 0 and 1 for each rule and bill),
3. shows the relevant bills per profile in a small web UI (French and English),
4. sends one email digest per profile when something is new or changed.

It runs as three containers: `app` (web UI), `worker` (daily job, manual runs, rescoring) and
`db` (PostgreSQL 16). Scoring uses Cohere Rerank on Microsoft Foundry by default, with an
optional local model for setups without Azure.

Operations (daily job, monitoring, failures, model changes): [docs/operations.md](docs/operations.md).
Choices made during the build: [DECISIONS.md](DECISIONS.md).
Module map and contracts: [docs/architecture.md](docs/architecture.md).

## Data sources

All data comes from the Chambre's datasets on [data.public.lu](https://data.public.lu)
(udata API, no hard-coded download URLs):

| Adapter | Dataset | Role |
|---|---|---|
| `datapublic-112` | 112, "Liste des projets et propositions de lois" (CSV) | baseline, every bill since 1965 |
| `datapublic-121` | 121, "Etat des travaux" (XLSX) | newer snapshot: status, procedure dates, document links, committee |
| `chd` | 109, plenary votes (CSV, regenerated daily around 04:30 UTC) | daily delta: final votes, new bill numbers |

The bill number is the key. Each change of a tracked field is written to the bill's history.
Details: [docs/ingestion.md](docs/ingestion.md).

**chd.lu pages are not fetched.** The site's `robots.txt` disallows the dossier, search, ajax
and paginated pages, which are all the pages that list or describe bills. So the "daily delta"
uses the Chambre's daily open-data export of plenary votes instead, and dossier pages are only
linked to. A robots.txt gate refuses any future chd.lu fetch that robots.txt disallows.

**Known gap.** Dataset 112 stops at bill 8410 (refreshed 2024-07-15) and 121 at dossier 8520
(refreshed 2025-04-10). Bills filed since then appear only once they are put to a plenary vote,
with the short title from the vote. Status changes before a vote are not visible until the
Chambre refreshes 112 and 121. See [DECISIONS.md](DECISIONS.md) (Data sources) for the full
reasoning and the recommended step: ask the Chambre (relations.publiques@chd.lu) to refresh
112/121 daily, or for written permission to fetch dossier pages at a low rate.

Every page shows "Source: Chambre des Députés du Grand-Duché de Luxembourg" with a link.

## Quick start

Requirements: Docker with the Compose plugin.

```sh
cp .env.example .env
# edit .env: at least SECRET_KEY, POSTGRES_PASSWORD, BASE_URL, the OIDC_* values,
# the FOUNDRY_* values (or SCORER=local) and the SMTP_* values
docker compose up -d --build
```

The app listens on port `APP_PORT` (default 8000). Put a TLS-terminating reverse proxy in front
of it and set `BASE_URL` to the public HTTPS URL. Both `app` and `worker` apply the database
migrations when they start (a PostgreSQL advisory lock makes this safe).

Generate a secret key with:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

With `ENV=production` the app refuses to start when `DEV_AUTH=true` or `SECRET_KEY` is empty or
left at its default.

### Development mode

No OIDC provider, no Azure and no SMTP needed. In `.env`:

```sh
ENV=development
BASE_URL=http://localhost:8000
SESSION_HTTPS_ONLY=false
SECRET_KEY=dev-only-secret
DEV_AUTH=true
DEV_AUTH_ROLE=admin
SCORER=fake
```

Then `docker compose up -d --build` and open <http://localhost:8000/login?as=admin>
(or `?as=user` for a plain user). `SCORER=fake` scores by keyword overlap, which is enough to
try the UI but says nothing about real relevance. Without `SMTP_HOST` no email is sent; use
`lexwatch send-digest --dry-run` to see the digests.

## Configuration

Every setting is an environment variable, read by `src/lexwatch/config.py`. `.env.example`
lists them all with comments. An empty value means "use the default".

### General

| Variable | Default | Meaning |
|---|---|---|
| `ENV` | `development` | `development`, `test` or `production`. Production refuses `DEV_AUTH` and a default `SECRET_KEY`, and forces the Secure cookie flag. |
| `LOG_LEVEL` | `INFO` | Log level of the JSON logs on stdout. |
| `BASE_URL` | `http://localhost:8000` | Public URL. Used in email links and for the default OIDC redirect URI. |
| `SECRET_KEY` | `change-me` | Signs the session cookie. Required in production. |
| `SESSION_HTTPS_ONLY` | `false` | Secure cookie flag. Always on when `ENV=production`. |
| `DEFAULT_LANGUAGE` | `fr` | `fr` or `en`, until a user picks a language. |

### Database and containers

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `lexwatch` (user, database); password required | Used by the `db` container. Compose builds `DATABASE_URL` from them and refuses to start without `POSTGRES_PASSWORD` (URL-safe characters). |
| `DATABASE_URL` | `postgresql+psycopg://lexwatch:lexwatch@localhost:5432/lexwatch` | Set it yourself only outside compose (compose overrides it). |
| `APP_PORT` | `8000` | Host port of the `app` container. |
| `LEXWATCH_TAG` | `latest` | Tag of the locally built image (compose only). |

### Authentication

| Variable | Default | Meaning |
|---|---|---|
| `DEV_AUTH` | `false` | Log in a fixed local user without OIDC. Development only. |
| `DEV_AUTH_ROLE` | `admin` | Role of that user: `admin` or `user`. |
| `OIDC_ISSUER` | | Issuer URL. Discovery is read from `{OIDC_ISSUER}/.well-known/openid-configuration`. |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | | Client credentials. |
| `OIDC_REDIRECT_URI` | `{BASE_URL}/auth/callback` | Redirect URI registered at the provider. |
| `OIDC_SCOPES` | `openid email profile` | Requested scopes. |
| `OIDC_ROLE_CLAIM` | `roles` | Claim holding roles or groups (list or string). Dotted paths reach nested claims, e.g. `realm_access.roles`. |
| `OIDC_ADMIN_VALUE` | `lexwatch-admin` | Value(s) of that claim that grant the admin role, comma-separated. Everyone else is a user. |
| `OIDC_END_SESSION` | `true` | Also sign out at the provider when it offers an `end_session_endpoint`. |

### Sources and outbound HTTP

| Variable | Default | Meaning |
|---|---|---|
| `HTTP_USER_AGENT` | `lexwatch/0.1 (+internal bill tracker)` | User-Agent for every outbound request. Put a contact address in it. |
| `HTTP_TIMEOUT_SECONDS` | `30` | Timeout for source downloads and OIDC calls. |
| `DATAPUBLIC_API_URL` | `https://data.public.lu/api/1/` | udata API. |
| `DATAPUBLIC_REQUEST_DELAY_SECONDS` | `1` | Minimum delay between two requests to data.public.lu. |
| `CHD_ENABLED` | `true` | Use the daily votes dataset (109). `false` relies on 112 and 121 only. |
| `CHD_REQUEST_DELAY_SECONDS` | `2` | Delay between requests of the `chd` adapter. |
| `HTTPS_PROXY`, `NO_PROXY` | | Outbound proxy, honoured by httpx, Authlib and azure-identity. Keep `db` in `NO_PROXY`. |

### Scoring

| Variable | Default | Meaning |
|---|---|---|
| `SCORER` | `foundry` | `foundry`, `local` or `fake` (development only, refused when `ENV=production`). |
| `DEFAULT_THRESHOLD` | scorer default | Threshold for profiles without their own. Empty: Rerank 5 Pro 0.5, Rerank 5 Fast 0.45, Rerank 4 0.5 (uncalibrated), local 0.05, fake 0.3. |
| `SCORING_MAX_CHARS` | `4000` | Maximum characters of bill text sent to the scorer. |
| `FOUNDRY_ENDPOINT` | | Rerank URL, see [Foundry setup](#foundry-setup). |
| `FOUNDRY_MODEL` | `Cohere-rerank-v5-fast` | Deployment name, sent as `model`. Stored with every score. |
| `FOUNDRY_API_KEY` | | API key. Leave empty to use Entra ID. |
| `FOUNDRY_API_KEY_HEADER` | `Authorization` | `Authorization` (sent as `Bearer <key>`) or `api-key` (raw key). |
| `FOUNDRY_TOKEN_SCOPE` | `https://cognitiveservices.azure.com/.default` | Entra ID token scope. Alternative: `https://ai.azure.com/.default`. |
| `FOUNDRY_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `FOUNDRY_MAX_RETRIES` | `4` | Retries on 429, 5xx, timeouts and connection errors (exponential backoff, honours `Retry-After`). |
| `FOUNDRY_MAX_DOCUMENTS` | `100` | Bills per request. |
| `FOUNDRY_MAX_TOKENS_PER_DOC` | `0` | Sent as `max_tokens_per_doc` when above 0 (server default 4096). |
| `FOUNDRY_CIRCUIT_THRESHOLD` | `3` | Consecutive failed requests (after retries) that stop scoring for the rest of the run. |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | | Service principal for azure-identity. `AZURE_CLIENT_ID` alone selects a user-assigned managed identity. Leave all empty for a system-assigned managed identity. |
| `INSTALL_LOCAL_SCORER` | `0` | Build arg: `1` installs the local scorer's libraries in the image. |
| `LOCAL_MODEL_DIR` | `/models/bge-reranker-v2-m3` | Model directory (compose sets it to the `models` volume). |
| `LOCAL_MODEL_REPO` | `onnx-community/bge-reranker-v2-m3-ONNX` | Hugging Face repository. |
| `LOCAL_MODEL_REVISION` | pinned commit | Repository revision. |
| `LOCAL_MODEL_FILE` | `onnx/model_int8.onnx` | ONNX file (int8, about 571 MB). |
| `LOCAL_MAX_LENGTH` | `512` | Tokens per (rule, bill) pair. |
| `LOCAL_BATCH_SIZE` | `8` | Pairs per ONNX batch. |

### Email

| Variable | Default | Meaning |
|---|---|---|
| `SMTP_HOST` | | SMTP relay. Empty: no digest is sent (a warning is logged). |
| `SMTP_PORT` | `587` | Port. |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | | Login, only when `SMTP_USERNAME` is set. |
| `SMTP_STARTTLS` | `true` | STARTTLS on a plain connection (port 587). |
| `SMTP_SSL` | `false` | Implicit TLS (port 465). Use either this or STARTTLS. |
| `SMTP_FROM` | `lexwatch@localhost` | Sender address. |
| `SMTP_TIMEOUT_SECONDS` | `30` | Connection timeout. |
| `DIGEST_ENABLED` | `true` | `false` skips the digest step of the daily job. |

### Worker

| Variable | Default | Meaning |
|---|---|---|
| `DAILY_JOB_TIME` | `06:30` | Daily job time, `HH:MM`, in `APP_TIMEZONE`. |
| `APP_TIMEZONE` | `Europe/Luxembourg` | Time zone of the daily job and of "one digest per day". |
| `WORKER_POLL_SECONDS` | `15` | How often the worker looks for work. |

## Foundry setup

lexwatch calls Cohere Rerank deployed in a Microsoft Foundry resource, over REST with the
Cohere v2 rerank format.

**What is sent to the endpoint:** the rule texts that users write, and public bill text (title,
plus summary when a source has one, plus an author line such as "Auteur : Ministre des
Finances"). No user data is sent: no names, email addresses, recipients or profile names. Tell
users that their rule texts leave the network.

1. **Create or pick a Foundry resource** (Azure AI Foundry / AI Services) in the Azure portal.
2. **Deploy a rerank model** from the Foundry model catalog. The deployment name is what you
   put in `FOUNDRY_MODEL`; keep the default name unless you have a reason not to.
   - `Cohere-rerank-v5-fast` (default, cheapest) or `Cohere-rerank-v5-pro`. Rerank 5 gives
     calibrated scores, so a profile threshold means the same thing for every rule. It is in
     preview on Foundry, with retirement currently set for 2027-02-12.
   - `Cohere-rerank-v4.0-fast` or `Cohere-rerank-v4.0-pro`: generally available, retirement
     2027-09-14. Scores are not calibrated: check thresholds after switching.
   - Names are case-sensitive. v4 names contain `v4.0`, v5 names do not (`v5-fast`).
3. **Choose the deployment type.** Global Standard may process requests in any Azure region; it
   is acceptable because the data is public. If your policy requires processing inside the EU,
   choose **Data Zone Standard (EU)** where the portal offers it for the model (v4.0 has it;
   for v5, check the deployment-type list when you deploy). The endpoint is configuration, so
   point `FOUNDRY_ENDPOINT` at the resource that holds the Data Zone deployment. Record the
   type you chose.
4. **Set the endpoint:**
   ```
   FOUNDRY_ENDPOINT=https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank
   ```
   A bare resource root (`https://<resource>.services.ai.azure.com`) gets the path appended.
   Any other path is used as given (for example an API gateway in front of Foundry). Do not
   add `/models` or `/openai`.
5. **Choose the authentication.**
   - **Entra ID (recommended):** leave `FOUNDRY_API_KEY` empty. lexwatch uses azure-identity's
     `DefaultAzureCredential`:
     - managed identity: system-assigned needs nothing; for a user-assigned identity set
       `AZURE_CLIENT_ID`. Add `169.254.169.254` to `NO_PROXY` when you use a proxy.
     - service principal: set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET`.

     Assign the role **Cognitive Services User** to that identity on the Foundry resource.
     Owner and Contributor do not grant data access. Role assignments can take a few minutes
     to apply. The token scope is `FOUNDRY_TOKEN_SCOPE`, default
     `https://cognitiveservices.azure.com/.default`; if you get 401 with a valid identity, try
     `https://ai.azure.com/.default`.
   - **API key:** copy a key from the resource's "Keys and Endpoint" page into
     `FOUNDRY_API_KEY`. It is sent as `Authorization: Bearer <key>`; set
     `FOUNDRY_API_KEY_HEADER=api-key` if your deployment wants the `api-key` header instead.
6. **Verify** with the optional live test (one request, a fraction of a cent):
   ```sh
   FOUNDRY_ENDPOINT=https://<resource>.services.ai.azure.com \
   FOUNDRY_MODEL=Cohere-rerank-v5-fast FOUNDRY_API_KEY=<key> \
     uv run pytest tests/scoring/test_foundry_live.py -v -s
   ```
   For Entra ID, set `FOUNDRY_LIVE_ENTRA=1` and the `AZURE_*` variables instead of the key. The
   test is skipped when these variables are absent. `/ready` only checks that the endpoint and
   model are set; it never calls the paid endpoint.

Cost is very small: about $0.02 (Fast) or $0.05 (Pro) per million tokens on v5. Daily scoring
costs well under a dollar a year; a full rescore of every bill costs a few dollars. The admin
page shows the billed units of the current month. Estimates: `docs/research/foundry-rerank.md`.

## Writing good rules

A profile is a watch subject ("AML and financial regulation"). It has rules, a threshold and
email recipients.

- **One short sentence per rule, one subject per rule.** For example "Lutte contre le
  blanchiment de capitaux et le financement du terrorisme". French or English both work.
- **Describe the topic, do not give instructions.** "Protection des données personnelles" works;
  "Find bills that mention data protection but not health" does not. The reranker judges how
  well a bill matches a description; it does not follow instructions.
- **The profile score is the plain average of its rule scores.** A bill must match the rules
  of a profile together to pass the threshold. Use several rules in one profile to describe
  the same subject from different angles. Put unrelated subjects in separate profiles,
  otherwise a bill that matches one subject well is pulled down by the others.
- **Use the preview** on the Rules page: it shows which recent bills would match with the
  current rules and threshold before you save.
- **Thresholds.** Leave the profile threshold empty to use the default (0.45 for Rerank 5 Fast,
  0.5 for Rerank 5 Pro). Raise it if you get too many bills, lower it if you miss some. Bills
  are scored on their title and author, which are short, so a rule written with the words a
  bill title would use scores better.

Saving a profile's rules or threshold rescores its bills in the background; the Rules page
shows "In progress" meanwhile.

## Optional local scorer

For an offline or no-Azure setup, lexwatch can score with `BAAI/bge-reranker-v2-m3` as ONNX on
CPU. It is not in the default image.

1. In `.env`: `SCORER=local` and `INSTALL_LOCAL_SCORER=1` (build arg that installs
   onnxruntime, tokenizers and numpy).
2. Download the model (about 590 MB, pinned revision, checked by size and sha256) into the
   `models` volume:
   ```sh
   docker compose --profile local-scorer up model-download
   ```
   This runs `lexwatch download-model` once and exits. It needs access to huggingface.co.
3. Rebuild and start: `docker compose up -d --build`. `/ready` reports the scorer as ready once
   the model and tokenizer files are present.

Its scores are on another scale than Cohere's: the default threshold is **0.05** (calibrated on
real bills), not 0.5. Profiles with their own threshold must be re-checked after switching.
Switching scorer rescores every bill (see [operations](docs/operations.md#switching-model-versions-and-rescoring)).
A full rescore on CPU can take hours.

## OIDC setup

lexwatch uses the authorization code flow with PKCE. Two URLs must be registered at the
provider:

- redirect URI: `{BASE_URL}/auth/callback` (or `OIDC_REDIRECT_URI`),
- post-logout redirect URI: `{BASE_URL}/logged-out`.

Users are created at first login. Their role is set at every login from `OIDC_ROLE_CLAIM`:
`admin` when the claim contains one of the `OIDC_ADMIN_VALUE` values, `user` otherwise. The
claim is read from the ID token, then the userinfo endpoint, then the access token.

### Microsoft Entra ID

1. Entra admin center, App registrations, New registration. Platform **Web**, redirect URI
   `https://lexwatch.example.internal/auth/callback`.
2. Authentication: add `https://lexwatch.example.internal/logged-out` as a second Web
   redirect URI. Entra ID accepts a `post_logout_redirect_uri` only when it is registered
   there.
3. Certificates & secrets: create a client secret.
4. App roles: create a role with value `lexwatch-admin` (allowed member types: users/groups).
   In Enterprise applications, Users and groups, assign the admins to that role. Assigned
   roles appear in the `roles` claim of the ID token.
5. `.env`:
   ```sh
   OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
   OIDC_CLIENT_ID=<application (client) id>
   OIDC_CLIENT_SECRET=<secret value>
   OIDC_ROLE_CLAIM=roles
   OIDC_ADMIN_VALUE=lexwatch-admin
   ```
   Use the tenant-specific issuer (a tenant id, not `common` or `organizations`): the ID token's
   issuer must match it.

To use security groups instead of app roles, add the `groups` claim (Token configuration), set
`OIDC_ROLE_CLAIM=groups` and put the admin group's object id in `OIDC_ADMIN_VALUE`.

### Keycloak

1. In the realm, create a client: OpenID Connect, client authentication on (confidential),
   standard flow on. Valid redirect URIs `https://lexwatch.example.internal/auth/callback`,
   valid post-logout redirect URIs `https://lexwatch.example.internal/logged-out`.
2. Copy the client secret from the Credentials tab.
3. Create a realm role `lexwatch-admin` and assign it to the admins.
4. `.env`:
   ```sh
   OIDC_ISSUER=https://kc.example.internal/realms/<realm>
   OIDC_CLIENT_ID=lexwatch
   OIDC_CLIENT_SECRET=<secret>
   OIDC_ROLE_CLAIM=realm_access.roles
   OIDC_ADMIN_VALUE=lexwatch-admin
   ```
   By default Keycloak puts `realm_access` only in the access token; lexwatch reads it there.
   For a client role use `resource_access.<client-id>.roles`. For groups, add a group
   membership mapper (claim `groups`) and set `OIDC_ROLE_CLAIM=groups`. Any claim you map with
   a protocol mapper works.

## SMTP setup

Digests go through your SMTP relay with Python's `smtplib`.

```sh
SMTP_HOST=smtp.example.internal
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_SSL=false
SMTP_USERNAME=lexwatch          # leave empty for an unauthenticated relay
SMTP_PASSWORD=...
SMTP_FROM=lexwatch@example.internal
```

For implicit TLS use `SMTP_PORT=465`, `SMTP_SSL=true`, `SMTP_STARTTLS=false`. The certificate is
verified against the system CA store. Make sure the relay accepts `SMTP_FROM` as sender.
Check the setup without sending anything:

```sh
docker compose exec worker lexwatch send-digest --dry-run
```

Each profile gets at most one digest per day, only when a matched bill is new or changed.
Recipients are set per profile (commas, semicolons or new lines).

## Outbound network

Allow these destinations from the `app` and `worker` containers, and nothing else:

| Destination | Used for |
|---|---|
| `data.public.lu`, `download.data.public.lu` (HTTPS) | datasets 112, 121 and 109 |
| `<resource>.services.ai.azure.com` (HTTPS) | Foundry rerank (`SCORER=foundry`) |
| `login.microsoftonline.com` (HTTPS) | Entra ID login and Foundry tokens for a service principal |
| `169.254.169.254` (HTTP, not through the proxy) | managed identity tokens, when used |
| your OIDC provider (HTTPS) | Keycloak or other provider |
| your SMTP relay (SMTP) | digests; `smtplib` connects directly, not through `HTTPS_PROXY` |
| `huggingface.co` and its download CDN (HTTPS) | only `lexwatch download-model`, local scorer |

`www.chd.lu` is not contacted today: the app only links to it.

Set `HTTPS_PROXY` (and `NO_PROXY`, which must keep `db`) to send HTTPS through a proxy. For a
TLS-intercepting proxy, mount its CA bundle and point `SSL_CERT_FILE` (httpx, Authlib, smtplib)
and `REQUESTS_CA_BUNDLE` (azure-identity) at it in the containers. To build
the image behind such a proxy, pass the bundle as a build secret (it is not stored in the image):

```sh
docker build --secret id=extra_ca,src=/path/to/ca-bundle.crt -t lexwatch:latest .
```

## Health endpoints

- `GET /health`: `200 {"status": "ok"}` when the web process is up. No database access. The
  compose health check of `app` uses it.
- `GET /ready`: `200` when the database answers and the scorer is configured, `503` otherwise.
  The body names each check, for example `{"status": "unavailable", "database": true,
  "scorer": false}`. It never calls Foundry.

The worker has no HTTP endpoint. Watch its logs and the admin page (see
[docs/operations.md](docs/operations.md#monitoring)).

## Backup and restore

All state is in PostgreSQL, in the compose volume `pgdata` (named `<project>_pgdata`, for
example `lexwatch_pgdata`). The local model volume `models` can be downloaded again and needs
no backup. The database holds users' names and email addresses: store backups accordingly.

Backup (custom format, consistent while the app runs):

```sh
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > lexwatch-$(date +%F).dump
```

Restore into an empty database:

```sh
docker compose stop app worker
docker compose exec -T db sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' \
  < lexwatch-2026-11-19.dump
docker compose start app worker
```

`app` and `worker` apply any newer migrations when they start, so a backup from an older
version can be restored into a newer one. Test a restore now and then, for example into a
second compose project (`docker compose -p lexwatch-restore-test up -d db`).

## Development

Requirements: [uv](https://docs.astral.sh/uv/) and Docker (for PostgreSQL).

```sh
uv sync                       # Python 3.12, runtime and dev dependencies
uv sync --extra local         # also the local scorer libraries
```

The tests need a real PostgreSQL 16 (full-text search, generated columns, JSONB). They read
`TEST_DATABASE_URL`, default
`postgresql+psycopg://lexwatch:lexwatch@localhost:55432/lexwatch_test`. The schema is dropped and
rebuilt, so never point it at real data.

```sh
docker run -d --name lexwatch-test-db -p 55432:5432 \
  -e POSTGRES_USER=lexwatch -e POSTGRES_PASSWORD=lexwatch -e POSTGRES_DB=lexwatch_test \
  postgres:16-alpine
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run alembic check          # models and migrations in sync
```

Tests never touch the network; they use saved fixtures in `tests/fixtures/`. The only live test
is `tests/scoring/test_foundry_live.py` (see [Foundry setup](#foundry-setup)).

To run the app outside Docker, export the variables (the app does not read `.env` itself),
then start the web process and the worker:

```sh
set -a; . ./.env; set +a      # with DATABASE_URL pointing at your database
uv run lexwatch migrate
uv run lexwatch serve --port 8000
uv run lexwatch worker
```

## License

Apache-2.0, see [LICENSE](LICENSE).
