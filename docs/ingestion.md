# Ingestion

Owner: stream 1. Code: `src/lexwatch/sources/` (adapters), `src/lexwatch/ingest/` (merge,
pipeline), `src/lexwatch/http.py` (client, polite GET). Binding background: `DECISIONS.md`
(Data sources) and `docs/research/`.

## Sources and order

| Order | Adapter (`name`) | Upstream | What it gives |
|---|---|---|---|
| 1 | `datapublic-112` | data.public.lu dataset 112, main CSV | baseline: every bill since 1965 (stale since 2024-07-15, max 8410) |
| 2 | `datapublic-121` | data.public.lu dataset 121, main XLSX | newer snapshot since 2018 (stale since 2025-04-10, max 8520), links, committee, timeline |
| 3 | `chd` | data.public.lu dataset 109 (plenary votes), main CSV, refreshed daily | final votes on bills: adopted / rejected, new bill numbers after 8520 |

chd.lu pages are never fetched (robots.txt). `chd` only reads data.public.lu. Any future
fetch of a chd.lu URL must use `lexwatch.http.polite_get`. It checks robots.txt through
`lexwatch.sources.robots` and refuses dossier, search, ajax and `?page=` URLs.
`urllib.robotparser` does not support the `*`/`$` wildcards that chd.lu uses, so the gate
has its own small matcher. It is stricter than RFC 9309: any matching `Disallow` wins.

## Resolution, politeness, skip

- `GET {DATAPUBLIC_API_URL}datasets/{id}/`. The slug is the fallback (for 109, the slug
  `liste-des-votes-en-seances-plenieres` comes first). The adapter takes the `type == "main"`
  resources of the expected format, prefers a title prefix (`112-texte-loi`,
  `121-etat-travaux`, `109-votes`), and picks the highest `last_modified`.
- Download: `https://data.public.lu/fr/datasets/r/{resource_id}` (stable alias, 302 to the
  timestamped file, redirects followed). A checksum mismatch after download is logged, not
  fatal.
- One request at a time. Requests to the same host are spaced by
  `DATAPUBLIC_REQUEST_DELAY_SECONDS` (default 1) or `CHD_REQUEST_DELAY_SECONDS` (default 2).
  The User-Agent comes from `HTTP_USER_AGENT`.
- Skip: table `source_state` (migration `0002`) keeps, per adapter, the resource id,
  `last_modified` and checksum (`sha1:` or `md5:`, as published by udata) of the last
  successful run. When the resolved resource has the same id and checksum (or the same
  `last_modified` if there is no checksum), the file is not downloaded. The adapter then
  returns no records and the source status is `unchanged`. A failed run does not move the
  stored identity, so the next run downloads again.
- Source statuses (`IngestResult.sources[name]["status"]`, `source_state.status`): `ok`,
  `unchanged`, `failed`, `skipped` (CHD_ENABLED=false). Each entry also carries `records`,
  `resource_id`, `last_modified`, parse `stats` (excluded types, unknown statuses), and
  `error` on failure.

## Normalized vocabulary (`sources/base.py`)

Types: `projet`, `proposition`. Statuses: `deposited`, `in_committee`, `council_opinion`,
`second_vote_pending`, `adopted`, `rejected`, `published`, `withdrawn`, `merged`,
`transformed`, `other`. The raw code is always kept in `extra["raw_status"]`.

| Normalized | 112 `LAW_STATUS` | 121 `Etat` | 109 vote |
|---|---|---|---|
| deposited | Cree | CREE | |
| in_committee | EnCommission | COMM | |
| second_vote_pending | EnAttenteDispenseSecondVote | 2EME.VOTE | |
| adopted | VoteAccepte | VOTE_OK | Oui > Non |
| rejected | VoteRefuse | VOTE_KO | Oui <= Non |
| published | Publie | PUB_JO | |
| withdrawn | Retire | RETIRE | |
| transformed | Transforme | TRANSFO | |
| merged | Fusionne, EvacueConjointement | | |
| other | AviseParConferencePresidents, any unknown code | AVIS.CP, any unknown code | |
| (none) | Vide | `.` | |

- "Vide" and "." mean "no status". They map to `None`, so they never overwrite a known
  status. None of the 2026-11-19 bills (PL/PPL) has either code.
- `council_opinion` is part of the vocabulary, but no source code maps to it today. Neither
  dataset has a "waiting for the Conseil d'État" state, and AVIS.CP is the Conférence des
  Présidents, not the Conseil d'État.

## Dataset 112 (CSV)

- UTF-8 with BOM (`utf-8-sig`), real CSV parser (cells contain line breaks), `NATURE` dropped.
- Kept `LAW_TYPE`: `ProjetDeLoi` and `PropositionDeLoi` (`KEPT_112_TYPES`). Excluded on
  2026-11-19 (counted in `stats.excluded_types`): ProjetDeReglementGrandDucal 487,
  PropositionDeRevisionReglementCHD 91, DebatOrientation 75,
  PropositionDeRevisionConstitution 56, ProjetDeRevisionDeConstitution 28,
  ComptesDuServiceInterieur 28, RapportsActivites 17, DebatSurEtatNation 8,
  RapportsDeCommissionDivers 5, DebatDivers 4, RapportsDeCommissionDebat 3, Autre 3,
  ProprositionDeDeclarationRevisionConstitution 1 (sic).
- Dates `DD/MM/YYYY`: filing = `LAW_DEPOSIT_DATE`, closing = `LAW_EVACUATION_DATE`.
- Title: one leading Excel quote `'` is stripped. Each line is trimmed and spaces are
  collapsed. Line breaks are kept.
- Author: the raw `LAW_AUTHORS` string, not split.
- `summary` = None. `LAW_CONTENT` is a list of documents, not a summary, and goes to
  `extra["documents_list"]`.
- `dossier_url` = `https://www.chd.lu/fr/dossier/{n}`. `source_updated_at` = resource
  `last_modified`.

## Dataset 121 (XLSX)

- openpyxl without `read_only`, because hyperlinks are needed. Sheet `ETAT_TRAVAUX`.
  Columns are mapped by header name, and a header starting with `Recev` is read as
  `Recev?`.
- Kept `Nature`: `PL` and `PPL`.
- Title: the leading `.` is stripped and `¯` becomes a line break. The type prefix
  ("Projet de loi " / "Proposition de loi ") is added unless the text already has it, so 121
  titles look like 112 titles.
- Status from `Etat`. Filing date from `Dépôt`.
- Closing date, by normalized status (first non-empty column wins):
  - published: DispVote, Vote2, Publication, Vote1
  - adopted: DispVote, Vote2, Vote1
  - rejected: Vote2, Vote1
  - every other status: none

  112's evacuation date equals `DispVote` for adopted and published bills (checked on the
  1,043 shared bills), so `DispVote` comes first. For withdrawn and transformed bills no 121
  column holds the date, so 112's date stays.
- Author: `Auteurs` with `|` replaced by `"; "` (short form, e.g. `G.Roth (Min. Finances)`).
- Dossier URL: the hyperlink of the `Dossier` cell, falling back to the pattern.
- `extra`:
  - `committee`, `committee_name`, `committee_url`
  - `rapporteur`, `rapporteur_url`, `author_url`
  - `dossier_pdf_url` (the `co_co` hyperlink, or the pattern when `co_co == "X"`)
  - `eu_transposition`
  - `dates` (every procedure-step date)
  - `documents`: list of `{label, date, url}`, one per step cell that has a hyperlink
    (label in French, e.g. "Premier avis du Conseil d'État"; date ISO or null)
  - `raw_status`, `raw_type`
- `source_updated_at` = resource `last_modified`.

## chd: plenary votes (dataset 109)

- Encoding: cp1252 (UTF-8 when a BOM is present). One row per deputy per vote. Rows are
  aggregated per `(meeting_date, vote_name, vote_type)`.
- Bill vote name:
  - Accepted forms: `^(PL|PPL)\s?(\d{4}[A-Z]?)\b`, or `Projet de loi [N°]xxxx` /
    `Proposition de loi [N°]xxxx`.
  - Excluded: motions (`MO…`, `Motion`), separate votes (`vote séparé`), amendments
    (`Amendement n°…`, `(Amendement …)`), and other dossier kinds (`PPC`, `PPMRCHD`).
    `PL 7942 - Amendement Art.8 Statut de Rome` is a real title and is kept.
- Vote type: only `Premier vote constitutionnel`, `Second vote constitutionnel` and the
  legacy `Not Found` (2021-2022) count. `Voté` rows are amendments, separate votes and
  motions about a bill, never the vote on the whole bill (checked on the 2026-11-19 file).
- Outcome: the latest qualifying vote per bill wins. A second constitutional vote beats a
  first vote held at the same time. Oui > Non gives `adopted`, otherwise `rejected`.
  Abstentions and "Pas participé" are not counted.
  - "Premier vote constitutionnel" adopted means voted, with the Conseil d'État's dispensation
    of the second vote still to come. It maps to `adopted`, as 121's VOTE_OK does.
  - A "Second vote constitutionnel" (held when the dispensation is refused) is final, so its
    outcome replaces the first vote's.
- `closing_date` = vote date for adopted and rejected. Note that 112/121 use the dispensation
  date instead, which is usually a few weeks later.
- `source_updated_at` = meeting datetime (Europe/Luxembourg, stored in UTC).
- Title: the text after the first ` - ` in the vote name (or after the number). It is a short
  label, used only when the bill has no title (new bills after 8520). 2 bills on 2026-11-19
  have no label (`PL 8627`, `PL 8669`), so their title is empty.
- Type: PL gives `projet`, PPL gives `proposition`.

## Merge (`ingest/merge.py`)

- The pipeline collects the records of all successful adapters in order 112, 121, chd. It
  calls `upsert_bills` once, which loads all bills into a dict (one query), folds every
  record onto the bill state in memory, then writes the net difference. Rows are inserted
  with `add_all` and events with one bulk `INSERT`. There are no per-row queries.
- A bill created in this run gets no events. A value changed twice in one run gives one
  event.
- Precedence:
  - A record overwrites a field when its `source_updated_at` is at least the bill's
    `source_updated_at`. That column stores the timestamp of the record that last won.
  - When timestamps are equal, the later adapter wins.
  - An older record only fills empty fields, and never `status` or `closing_date`. Example:
    a 2023 vote cannot close a bill that the 2025 snapshot shows in committee.
- `None` or an empty string never blanks a field.
- Title rules:
  - A chd vote label never replaces a title.
  - Any 112/121 title replaces a vote label.
  - Titles that differ only in whitespace or case count as equal.
- Every change to a `TRACKED_BILL_FIELDS` field writes a `bill_events` row with the field,
  the old and new values (dates as ISO text), `changed_at` and the winning record's source.
- `last_changed_at = now` for new and changed bills.
- `score_state = 'pending'` only for new bills and when `title` or `summary` changed, since
  those are the scored text. Status, date, author and link changes do not trigger a rescore.
- `bills.source` = the adapter that last changed the bill.

## `bills.extra` (JSONB, migration 0002)

Adapter `extra` dicts are persisted in `bills.extra` as a shallow merge: a key is set when the
bill does not have it yet or when the record wins (same precedence as the fields); keys are
never dropped. It is not a tracked field: no `bill_events`, no rescore, and an extra-only change
does not move `last_changed_at`.

Stable keys (the web stream may rely on them):

| Key | Type | From | Meaning |
|---|---|---|---|
| `raw_status` | str | 112, 121, chd | upstream status code of the winning source (chd: `"<vote_type>: Oui|Non"`) |
| `committee` | list[str] | 121 | committee abbreviations, e.g. `["FI"]` |
| `rapporteur` | list[str] | 121 | rapporteur short names |
| `dossier_pdf_url` | str | 121 | consolidated full-dossier PDF on wdocs-pub.chd.lu |
| `documents` | list[{label, date, url}] | 121 | procedure documents (PDF links), in procedure order |

Other keys are informative and may change: `raw_type`, `documents_list` (112),
`committee_name`, `committee_url`, `rapporteur_url`, `author_url`, `dates`,
`eu_transposition` (121), `vote_name`, `vote_type`, `vote_date`, `votes` (chd).

## Failures

- An adapter raises `SourceError` for HTTP errors, a missing dataset or resource, a changed
  header or sheet, or an undecodable file. The pipeline catches any exception per adapter and
  records `failed` plus the message in `IngestResult.sources` and `source_state`. It logs
  `ingest.source_failed` as a structured error and goes on with the other adapters.
- Nothing is deleted.
- Nothing is committed inside `run_ingestion`. The job commits bills and `source_state`
  together.

## Live run (2026-11-19, empty database)

- 4,013 bills added in 12.6 s. The 112 CSV gave 3,787 bills, the 121 overlay 1,053 and chd
  674 voted bills, of which 115 are numbered after 8520.
- Highest bill number: 8767.
- Statuses: published 3,078, withdrawn 450, adopted 219, in_committee 169, transformed 40,
  merged 38, rejected 18, second_vote_pending 1.
- Last source by bill: 112 for 2,845, 121 for 960, chd for 208.
- A second run reported all three sources `unchanged` and downloaded nothing.

## Known limitations

- Bills adopted at first vote since April 2025 stay `adopted` until 112/121 are refreshed:
  publication in the Journal officiel is not visible in the open data. Bills filed after
  8520 appear only once they are voted on.
