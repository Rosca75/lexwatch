# Operations

How lexwatch runs day to day, how to watch it, and what to do when something breaks.
Setup and configuration: [README.md](../README.md). Design choices: [DECISIONS.md](../DECISIONS.md).

Commands below run inside the containers, for example
`docker compose exec worker lexwatch <command>`.

## The daily job

The `worker` container polls the database every `WORKER_POLL_SECONDS` (15 s). There is no
scheduler library and no cron.

**When.** The daily job is due once local time in `APP_TIMEZONE` (default `Europe/Luxembourg`)
is at or after `DAILY_JOB_TIME` (default `06:30`) and no run of kind `daily` was created since
local midnight. A worker that was down at 06:30 starts the job as soon as it is back, on the
same day. The votes dataset (109) is regenerated around 04:30 UTC, so 06:30 Luxembourg time
picks up the previous day's votes.

**What it does**, in one `runs` row, committing after each step so the admin page shows
progress:

1. **Ingestion.** For datasets 112, 121 and 109 (the `chd` adapter), resolve the latest
   resource through the data.public.lu API and download it only when its id, checksum or date
   changed since the last successful run. Records are merged by bill number; every change to a
   tracked field is written to the bill's history. A failing source is recorded and the others
   go on. Nothing is ever deleted.
2. **Scoring.** One rerank request per distinct rule text, with the bills as documents (up to
   `FOUNDRY_MAX_DOCUMENTS` per request). Bills sent are new or changed bills (title or summary),
   bills left `unscored` by an earlier run, and any (rule, bill) pair without a score from the
   current model (new rules, model switch). Matches are recomputed for the affected profiles.
3. **Digests** (when `DIGEST_ENABLED=true` and `SMTP_HOST` is set). One email per profile, only
   when a matched bill is new or changed, at most one per profile per day.

The same steps run for a manual run (admin "Run now", or `lexwatch run-job`).

**Run kinds:** `daily`, `manual` (Run now, `run-job`), `rescore` (one per profile rescore).

**Run states:** `queued` (Run now, waiting for the worker) → `running` → one of:

| Status | Meaning |
|---|---|
| `success` | every step succeeded |
| `partial` | at least one step succeeded and at least one failed |
| `failed` | no step succeeded, or the worker restarted during the run |

A step "fails" when a source failed, when some bills could not be scored, or when a digest could
not be sent. `error_message` joins the step errors, for example
`ingestion: failed sources: chd; scoring: scorer unavailable: ...`.

**Details** are kept in `runs.details` (JSON): per source status (`ok`, `unchanged`, `failed`,
`skipped`), record counts and errors; digests sent, failed and skipped; the billing unit
(`input_tokens` for Rerank 5, `search_units` for Rerank 4). Counters: bills added, changed,
scored, unscored, and billed units.

To look at a run in the database:

```sh
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT id, kind, status, started_at, finished_at, bills_added, bills_changed,
          bills_scored, bills_unscored, billed_units, error_message
     FROM runs ORDER BY id DESC LIMIT 10"'
```

## Monitoring

**Admin page** (`/admin`, admin role): the last 20 runs (time, duration, kind, status, counts,
billed units, errors), the source statuses of the latest ingestion, the number of bills, of
pending and unscored bills, of rule scores from another model than the configured one, of
profiles being rescored, and the billed units of the current month. The buttons "Run now" and
"Rescore all" queue work for the worker. CSV export of bills and matches is there too.

**Probes.**

- `GET /health`: the web process is up (no database access). Use it for liveness.
- `GET /ready`: the database answers and the scorer is configured (`503` otherwise). Use it for
  readiness. It never calls Foundry, so it does not prove that Foundry works.

The worker has no endpoint: monitor it through its logs and the runs table.

**Logs.** Both containers write one JSON object per line to stdout:
`{"ts", "level", "logger", "msg", ...extra fields}`. `msg` is a stable event name. Logs carry no
personal data beyond a user id and email. Ship them with your usual collector
(`docker compose logs -f worker` to read them by hand).

**What to alert on:**

| Condition | How to detect |
|---|---|
| No daily run finished today | no `job.finished` event by about 08:00 local, or no `runs` row of kind `daily` today |
| A run failed or was partial | `job.finished` with `status` `failed` or `partial` |
| A source failed | `ingest.source_failed` (level ERROR) |
| Foundry down for the run | `scoring.foundry.circuit_open`, `scoring.unavailable` |
| Bills stay unscored | unscored count on the admin page above 0 for more than one day |
| Digests not sent | `digest.send_failed`, `digest.smtp_not_configured`, `digest.build_failed` |
| Worker loop errors | `worker.tick_failed`, `worker.recover_failed` |
| Worker restarted mid-work | `worker.recovered` (warning) |
| Login broken | `auth.login_redirect_failed`, `auth.callback_failed` |
| App down | `/health` or `/ready` failing |
| Data stale | see [Stale datasets](#stale-datasets) |

Retries are logged as warnings (`scoring.foundry.retry`); a few of them are normal.

## When the data sources fail

**What it looks like.** The run ends `partial` (or `failed` if scoring and digests also failed)
with `ingestion: failed sources: <names>`. The admin page shows the source as failed with its
error. The log has `ingest.source_failed` with the adapter name and message. Typical causes:
data.public.lu unreachable or returning an error, a dataset or resource not found, a changed
CSV header or XLSX sheet or column (a renamed column is an error, not silent data loss), a
truncated download (checksum mismatch), an undecodable file.

**Data is kept.** A failing adapter changes nothing in the database: bills, history and scores
stay as they were. The other adapters still run, and the bills already present are still scored
and sent in digests. The stored resource identity (`source_state`) is not moved, so the next run
downloads the file again.

**Retry.** Transient errors usually clear by the next day. To retry now, click **Run now** on the
admin page (or `docker compose exec worker lexwatch run-job`). Files that did not change are not
downloaded again.

**Format changes.** When the error names a missing column, sheet or header, the Chambre changed
the file. Compare the new file with `tests/fixtures/` and update the adapter in
`src/lexwatch/sources/datapublic.py` or `chd.py` (column mapping is documented in
[ingestion.md](ingestion.md)). Until then you can turn off a broken `chd` adapter with
`CHD_ENABLED=false`; 112 and 121 cannot be turned off individually.

**robots.txt refusal.** No adapter fetches chd.lu pages today. If future code does, it must go
through `lexwatch.http.polite_get`, which checks `https://www.chd.lu/robots.txt` and fails with
`robots.txt disallows <url>` for dossier, search, ajax and `?page=` URLs. The gate fails closed:
when robots.txt cannot be read (network error, 401/403/429/5xx, or an HTML page instead of
robots.txt) the whole host is refused for 24 h. This is intended. Do not work around it; see
the next section.

### Stale datasets

Datasets 112 and 121 are not refreshed daily: 112 was last refreshed 2024-07-15 (up to bill
8410) and 121 on 2025-04-10 (up to dossier 8520). Their status `unchanged` on the admin page is
normal. Until they are refreshed, newer bills appear only when they are put to a plenary vote
(dataset 109), with a short title from the vote name, and their status changes before the vote
are not visible.

To see when each source last changed:

```sh
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT name, status, last_modified, fetched_at, error FROM source_state"'
```

When 112 or 121 is refreshed, the next run downloads it and fills in titles, statuses and
links automatically (a dataset title replaces a vote label).

Dataset 109 should change after every plenary sitting. Several weeks without a change outside
the parliamentary recess (roughly mid-July to mid-September) is worth a look on data.public.lu.

### If the Chambre publishes daily or grants permission

- **Datasets 112 and 121 refreshed daily:** nothing to change. The adapters already download a
  resource only when it changed. Once 112/121 carry new bills within a day, the `chd` votes
  adapter is redundant; keep it (it adds vote dates) or set `CHD_ENABLED=false`. It is isolated
  in `lexwatch.sources.chd` and can be removed.
- **Written permission to fetch dossier pages:** record it in `DECISIONS.md` with the scope and
  rate agreed. The robots.txt gate still applies, so the Chambre must also allow the paths in
  robots.txt (or the permission must name an exception, which then needs a code change in
  `lexwatch.sources.robots`, reviewed). Fetch through `polite_get` at no more than one request
  every 5 s, only for dossiers that changed, with a contact address in `HTTP_USER_AGENT`, and
  outside 06:00 to 09:00 CET. Allow `www.chd.lu` in the outbound allow-list.

## When Foundry is unavailable

**What happens.** Each request has a timeout and up to `FOUNDRY_MAX_RETRIES` retries with
exponential backoff on 429, 5xx, timeouts and connection errors (honouring `Retry-After`).
After `FOUNDRY_CIRCUIT_THRESHOLD` consecutive failed requests the circuit breaker opens: the
rest of the run sends no more requests. Bills that could not be scored are marked `unscored`;
nothing is dropped. The run ends `partial` with `scoring: scorer unavailable: ...` (or the
first request error), the admin page shows the unscored count, and the log has
`scoring.foundry.circuit_open`.

**Recovery is automatic.** Every run picks up `unscored` bills and any pair without a
current-model score. Once Foundry answers again, the next daily run (or **Run now**) scores
them; they show up in the next digest when they match. Rescores that failed are not retried
by themselves; their bills are picked up by the next daily run.

**What to check**, from the error in the run:

| Error | Check |
|---|---|
| `HTTP 401` | API key wrong or rotated; for Entra ID: role **Cognitive Services User** on the Foundry resource, the right tenant, `FOUNDRY_TOKEN_SCOPE` (try `https://ai.azure.com/.default`) |
| `HTTP 403` | role missing or network rules on the Foundry resource (firewall, private endpoint) |
| `Entra ID token request failed` | `AZURE_*` variables, managed identity assignment, `NO_PROXY` for `169.254.169.254`, access to `login.microsoftonline.com` |
| `HTTP 404 Resource not found` | `FOUNDRY_ENDPOINT` path (no `/models`, no `/openai`) |
| `HTTP 404 DeploymentNotFound` | `FOUNDRY_MODEL` must equal the deployment name, case-sensitive; deployment deleted or retired |
| `HTTP 429` | quota or rate limit of the deployment; raise it in the Foundry portal or lower `FOUNDRY_MAX_DOCUMENTS` |
| `HTTP 5xx`, timeouts | Azure status page; retry later. If it persists, test with the live test and open a support case with the `ref` shown in the error |

Run the live test from a machine with the same configuration to check the endpoint in
isolation: `uv run pytest tests/scoring/test_foundry_live.py -v -s` (see the README).

## Switching model versions and rescoring

Every rule score stores the model that produced it. Scores from another model count as stale.

1. Deploy the new model in Foundry (see the README) and note its deployment name.
2. Set `FOUNDRY_MODEL` to it in `.env` (and `FOUNDRY_ENDPOINT` if it lives in another resource).
   To switch to the local scorer, set `SCORER=local` instead.
3. Restart: `docker compose up -d` (recreates `app` and `worker` with the new environment).
4. Rescore. Any of these works:
   - wait for the next daily run: it scores every pair that has no current-model score, so it
     rescores everything;
   - admin page, **Rescore all**, or `lexwatch rescore --all`: queues every profile for the
     worker;
   - `docker compose exec worker lexwatch rescore`: rescores every profile now, in that process,
     printing one line per profile (`--profile ID` for one). Exit code 1 if a rescore failed or
     was partial.

   Each profile rescore is a `rescore` run with its own billed units. Until it finishes, bills
   whose scores are stale keep their previous match. The admin page shows how many stale rule
   scores are left; it should reach 0.
5. **Re-check thresholds.** Default thresholds follow the model: 0.45 for Rerank 5 Fast, 0.5 for
   Rerank 5 Pro, 0.5 for Rerank 4, 0.05 for the local scorer. **Rerank 4 scores are not
   calibrated**: a fixed 0.5 may be too strict or too loose and depends on the rule. Profiles
   with their own threshold keep it whatever the model. After the rescore, open the main
   profiles, use the Rules preview to compare matches, and adjust. A `DEFAULT_THRESHOLD` set
   in `.env` applies to every model; review it too.

Bills that newly match after the switch appear in the next digest.

**Cost of a full rescore.** One request per distinct rule text per 100 bills. With about 4,000
bills and 120 distinct rules:

- Rerank 5 is billed per input token (query plus bill text, for every bill). At most about
  200 million tokens: under $5 on Fast, about $10 to $12 on Pro. Bill texts are short (title
  and author), so the real figure is usually lower.
- Rerank 4 is billed per search unit (one query with up to 100 documents): 120 × 40 = 4,800
  search units, about $10 to $14.

Check the billed units of the `rescore` runs on the admin page afterwards. Price details:
`docs/research/foundry-rerank.md`.

## Following Foundry model retirements

Foundry retires model versions on a schedule; a retired deployment stops answering (expect
`404 DeploymentNotFound` or similar), and lexwatch then leaves new bills unscored.

Dates known on 2026-11-19 (from `docs/research/foundry-rerank.md`):

| Model | Lifecycle | Retirement |
|---|---|---|
| `Cohere-rerank-v5-fast`, `Cohere-rerank-v5-pro` | preview | 2027-02-12 |
| `Cohere-rerank-v4.0-fast`, `Cohere-rerank-v4.0-pro` | GA | 2027-09-14 (replacement: v5) |
| `Cohere-rerank-v3.5` | retired 2026-05-14 | |

Preview retirement dates usually move out when the model reaches GA. Do not assume it.

**Where to watch:** the Microsoft Learn page "Model deprecations and retirements" for Foundry
(<https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule>),
the Azure Service Health notifications for the subscription (set an alert for the Foundry
resource), and the model card in the Foundry catalog.

**Plan:**

1. Put the retirement date of the deployed model in the team calendar, with a reminder two
   months before. Check the retirement page once a month, and when Azure sends a notice.
2. About a month before retirement, deploy the successor (the GA v5 version, or v4.0 as the
   fallback if v5 is not yet GA when v5 preview retires) next to the old deployment.
3. Run the live test against the new deployment.
4. Switch as described above (change `FOUNDRY_MODEL`, restart, rescore, re-check thresholds).
5. Delete the old deployment once the stale-score count on the admin page is 0.
6. Record the change and the date in `DECISIONS.md`.

## Digests

Digests are sent at the end of each run (daily or manual). Each profile gets one email a day
at most, only when a matched bill is new, changed, or newly matched (after a rule or threshold
change). The first digest of a new profile covers the last 24 hours of bill changes only.
Emails are in the profile owner's language.

| Symptom | Cause and fix |
|---|---|
| Nobody gets anything | `SMTP_HOST` empty (`digest.smtp_not_configured` in the log) or `DIGEST_ENABLED=false` |
| Run `partial` with `digest: N failed: profile X: ...` | SMTP error: host, port, TLS mode (`SMTP_STARTTLS` for 587 or `SMTP_SSL` for 465), credentials, relay refusing `SMTP_FROM`. Profiles sent before the error are not resent |
| One profile gets nothing | no valid recipient (`digest.invalid_recipients` logs how many were rejected), nothing new that matches, or it already got one today |
| A second "Run now" sends nothing | by design: one digest per profile per calendar day in `APP_TIMEZONE`; later changes go out the next day |
| Links point to the wrong host | `BASE_URL` |
| Bills missing because unscored | they are sent once scored, see [When Foundry is unavailable](#when-foundry-is-unavailable) |

`last_digest_at` is updated only after a successful send, so a failed send is retried by the
next run with nothing lost.

Preview what would be sent, without sending or changing anything:

```sh
docker compose exec worker lexwatch send-digest --dry-run
```

`lexwatch send-digest` (without `--dry-run`) sends now, with the same once-a-day rule; exit code
1 when a profile failed.

## Worker restarts

Stopping the worker (`docker compose stop`, deploy, crash) is safe:

- On start, it migrates the database, then cleans up: runs left `running` become `failed` with
  `interrupted (worker restarted)`, and profiles left `running` (rescore) go back to `pending`
  and are rescored again.
- Queued runs ("Run now") stay queued and are picked up.
- Data from completed steps is kept (each step commits). Bills that were being scored keep
  their state and are scored by the next run.
- An interrupted **daily** run still counts as today's daily run, so the worker does not start
  it again the same day. Click **Run now** to redo it; otherwise the next daily run catches up.
- If the worker was down at `DAILY_JOB_TIME`, it runs the daily job when it comes back the same
  day.

Run one worker only. Row locks make a second replica harmless, but it brings nothing.

## CLI reference

`lexwatch <command>`. In the image, the entrypoint applies migrations before every command
except `migrate` and `download-model`. Inside a running container use
`docker compose exec worker lexwatch <command>`; for a one-off container use
`docker compose run --rm worker <command>`.

| Command | What it does |
|---|---|
| `migrate [--revision REV]` | apply database migrations (default `head`), under an advisory lock |
| `serve [--host H] [--port P]` | run the web app (default `0.0.0.0:8000`) |
| `worker` | run the worker loop (daily job, queued runs, pending rescores) until SIGTERM |
| `run-job` | run ingestion, scoring and digests once, now, in this process (a `manual` run) |
| `rescore [--profile ID]` | rescore every profile (or one) now, in this process; only pairs without a current-model score are sent; exit 1 on a failed or partial rescore |
| `rescore --all` | queue every profile for the worker (like "Rescore all"), score nothing here |
| `download-model` | download the local ONNX model and tokenizer into `LOCAL_MODEL_DIR` (needs `INSTALL_LOCAL_SCORER=1` image) |
| `send-digest [--dry-run]` | build and send digests now; `--dry-run` prints them and changes nothing |

Prefer **Run now** over `run-job` while the worker is running, so that two runs do not overlap.
