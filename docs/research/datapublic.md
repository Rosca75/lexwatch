# data.public.lu: Chambre des Députés bill datasets

Research date: 2026-11-19. Everything below was checked against the live API and the downloaded files on that date.

## TL;DR

| | 112: Liste des projets et propositions de lois | 121: Etat des travaux |
|---|---|---|
| Dataset id | `63c6884fcfa8113ec4793d13` | `67f775f1adf67ff4c46f1b37` |
| Slug | `liste-des-projets-et-propositions-de-lois` | `etat-des-travaux` |
| Page | https://data.public.lu/fr/datasets/liste-des-projets-et-propositions-de-lois/ | https://data.public.lu/fr/datasets/etat-des-travaux/ |
| Resources | 4: csv, xlsx, xml, zip (html) | 1: xlsx |
| Latest file | 2024-07-15 (last record deposited 2024-07-04, no. 8410) | 2025-04-10 (last record deposited 2025-03-31, no. 8520) |
| Coverage | all dossiers since 1965, 4,593 rows | dossiers deposited since 2018-12-04 (legislature 2018-2023 onward), 1,156 rows |
| Licence | CC0 1.0 (`cc-zero`) | CC0 1.0 (`cc-zero`) |
| Frequency (declared) | `unknown` | `unknown` |

The "112" and "121" are the Chambre's Oracle BI Publisher report numbers. They appear as prefixes in the resource file names (`112-texte-loi.*`, `121-etat-travaux.xlsx`).

**The data is stale.** Neither dataset has been refreshed since 2024-07-15 (112) and 2025-04-10 (121). Dataset `last_modified` changed on 2026-05-05, but that was a metadata edit. Resource `last_modified` values and the files did not change. lexwatch cannot rely on these datasets alone for current bills. Treat them as a historical backfill or seed, and plan another source (for example chd.lu dossier pages) for anything newer. Other Chambre datasets, such as votes and questions, are refreshed daily (`last_update` 2026-11-19), so this lag applies only to these two.

Publisher: organization `627e56d3a9bc6b2dd946a3ea`, slug `chambre-des-deputes-du-grand-duche-de-luxembourg`. Its dataset list is at https://data.public.lu/api/1/organizations/627e56d3a9bc6b2dd946a3ea/datasets/ (20 datasets). No other dataset there lists individual bills. "Relevé général des activités Parlementaires…" contains monthly counts only.

## API

Base: `https://data.public.lu/api/1/` (udata v1, anonymous, JSON).

- Dataset plus its resources: `GET /api/1/datasets/{id}/`. The slug works too, e.g. `/api/1/datasets/etat-des-travaux/`. Resources are embedded in `resources[]`.
- Single resource: `GET /api/1/datasets/{dataset_id}/resources/{resource_id}/`
- Search: `GET /api/1/datasets/?q=etat+des+travaux`
- Org datasets: `GET /api/1/organizations/{org_id}/datasets/?page_size=50`

Useful resource fields (example from 112 CSV):

```json
{
  "id": "0a85459a-7a17-4d84-bd72-a95347be688d",
  "title": "112-texte-loi.csv",
  "format": "csv",
  "mime": "text/csv",
  "type": "main",
  "filesize": 4546489,
  "checksum": {"type": "sha1", "value": "cd8e53c27c318fc09b4af0523520ce149cf4bed4"},
  "created_at": "2023-01-17T12:36:53.247000+00:00",
  "last_modified": "2024-07-15T14:38:35.877000+00:00",
  "url": "https://download.data.public.lu/resources/la-liste-des-projets-propositions-de-lois/20240715-143833/112-texte-loi.csv",
  "latest": "https://data.public.lu/fr/datasets/r/0a85459a-7a17-4d84-bd72-a95347be688d"
}
```

Dataset-level fields: `last_update` is the max resource modification time. `last_modified` also moves on metadata edits, so do not use it as a "new data" signal.

### Resource URLs: timestamped, but there is a stable alias

- `url` is **timestamped**: `.../resources/<slug>/<YYYYMMDD-HHMMSS>/<file>`. It changes on every upload.
- The resource `id` (UUID) has stayed the same across re-uploads. `created_at` is 2023-01-17 and `last_modified` is 2024-07-15, so the publisher replaces the file in place.
- `latest` = `https://data.public.lu/fr/datasets/r/{resource_id}` is a **stable** URL. It returns 302 to the current timestamped `url`. `https://data.public.lu/api/1/datasets/r/{resource_id}` does the same.
- `checksum.value` (sha1) matches the downloaded bytes. Verified for all 5 files. Use it to skip unchanged downloads.

### Picking the latest resource programmatically

1. `GET /api/1/datasets/{id}/`
2. Filter `resources` with `type == "main"`, then by `format`: `"csv"` for 112, `"xlsx"` for 121. Optionally also match the `title` prefix (`112-texte-loi`, `121-etat-travaux`).
3. If more than one matches (none do today), take the max `last_modified`, with `created_at` as tie-breaker.
4. Download `latest` or `url` (follow redirects). Compare `checksum.value` with the last stored sha1 and skip if it is unchanged.

Do not hard-code resource ids as the only lookup. Keep them as a fallback, but resolve by dataset id plus format so that a replaced resource still gets picked up.

## Licence and terms

- Both datasets: `license: "cc-zero"`, i.e. Creative Commons Zero (CC0) 1.0, https://creativecommons.org/publicdomain/zero/1.0/. You can check this via `GET /api/1/datasets/licenses/`.
- Platform terms (CGU): https://data.public.lu/fr/pages/legal/terms ("Conditions Générales d'Utilisation"). The site content is published "sans restriction, sous les termes du Transfert universel dans le Domaine Public Creative Commons CC0 1.0". The State asks reusers to follow its data-use guide, and reusers must respect personal-data law. The datasets contain names of deputies and ministers (public office holders). Attribution is not required, but crediting "Chambre des Députés, via data.public.lu" is good practice.

## Dataset 112: Liste des projets et propositions de lois

Resources, all modified 2024-07-15, sha1 in `dataset_112.json`:

| format | id | size |
|---|---|---|
| csv | `0a85459a-7a17-4d84-bd72-a95347be688d` | 4.5 MB |
| xlsx | `c1c1aaee-bd13-4164-b73b-3b834f47d675` | 615 KB |
| xml | `c5a74c97-a5fa-42ec-90d7-a832ab7410b2` | 4.1 MB |
| zip | `3117bb79-2f1d-4a12-9148-bc6f657ab75d` | 630 KB (contains one `112_texte_loi.html`, an Oracle BI Publisher HTML table, so ignore it) |

### CSV format (recommended source)

- Encoding: UTF-8 **with BOM**. Use `encoding="utf-8-sig"`.
- Delimiter `,`. Quote `"` with RFC-4180 doubling. Record separator `\n` (LF, no CR).
- **Fields contain embedded newlines** (4,953 cells), so parse with a real CSV parser and never split on lines. The file has 26,891 physical lines for 4,593 records.
- Header (9 columns):
  `NATURE,LAW_NUMBER,LAW_TYPE,LAW_DEPOSIT_DATE,LAW_EVACUATION_DATE,LAW_STATUS,LAW_TITLE,LAW_CONTENT,LAW_AUTHORS`

| Column | Meaning | Format / examples | Empty |
|---|---|---|---|
| `NATURE` | junk: the same constant on every row, the BI report's filter list `"[Autre,ComptesDuServiceInterieur,…,RapportsDeCommissionDivers]"` | ignore | 0 |
| `LAW_NUMBER` | dossier number, the **unique key** (no duplicates) | string. Mostly digits `"1129"`…`"8410"`, plus split dossiers with a letter suffix: `3837A`, `3837B`, `4232A`, … | 0 |
| `LAW_TYPE` | type (CamelCase enum) | see below | 0 |
| `LAW_DEPOSIT_DATE` | date de dépôt | `DD/MM/YYYY`, e.g. `01/07/2024` | 0 |
| `LAW_EVACUATION_DATE` | date d'évacuation (closed) | `DD/MM/YYYY` or empty (open dossiers) | 367 |
| `LAW_STATUS` | status enum | see below | 0 |
| `LAW_TITLE` | full title incl. type prefix | `Projet de loi portant création d'un Observatoire de la culture`. May contain `\n`. | 0 |
| `LAW_CONTENT` | list of documents in the deposit (not a summary) | `Document de dépôt`; `1) Exposé des motifs\n2) Texte de la proposition de loi\n3) Commentaire des articles` | 53 |
| `LAW_AUTHORS` | author(s): civility, name, function | `Monsieur Gilles Roth, Ministre des Finances`; `Monsieur Marc Baum, Député` | 7 |

`LAW_TYPE` values (count): ProjetDeLoi 3357, ProjetDeReglementGrandDucal 487, PropositionDeLoi 430, PropositionDeRevisionReglementCHD 91, DebatOrientation 75, PropositionDeRevisionConstitution 56, ProjetDeRevisionDeConstitution 28, ComptesDuServiceInterieur 28, RapportsActivites 17, DebatSurEtatNation 8, RapportsDeCommissionDivers 5, DebatDivers 4, RapportsDeCommissionDebat 3, Autre 3, ProprositionDeDeclarationRevisionConstitution 1 (the upstream spelling has a typo). **lexwatch scope = `ProjetDeLoi` + `PropositionDeLoi`** (3,787 rows). The constitutional revision types may also be of interest.

`LAW_STATUS` values (count): Publie 3540, Retire 516, EnCommission 239, Vide 145, Transforme 42, VoteAccepte 37, EvacueConjointement 31, VoteRefuse 19, Cree 10, Fusionne 10, AviseParConferencePresidents 3, EnAttenteDispenseSecondVote 1.
Suggested meaning: Cree = just deposited; EnCommission = in committee; VoteAccepte = adopted, not yet published; VoteRefuse = rejected; Publie = published in the Journal officiel; Retire = withdrawn; Transforme / Fusionne / EvacueConjointement = merged or transformed into another dossier; Vide = no status (mostly debates and reports).
An empty evacuation date occurs with EnCommission (239), Publie (96), VoteRefuse (19), Cree (10), VoteAccepte (2), EnAttenteDispenseSecondVote (1). So "open" must be derived from status, not from the evacuation date.

Row order is by number (ascending, roughly chronological). The deposit date range is 1965-06-01 to 2024-07-04.

### XLSX / XML variants of 112

- XLSX: a single sheet `Sheet1`. Lowercase header `law_number…law_authors` (no `NATURE`), plus a 9th empty column and a trailing empty row. Every value is a **string** (dates are text `DD/MM/YYYY`). Newlines in titles are replaced by `|`. It is a lossy copy of the CSV, so prefer the CSV.
- XML: `<DATA_DS><NATURE>…</NATURE><DOCPA><LAW_NUMBER>…</LAW_NUMBER>…</DOCPA>…`, UTF-8, same fields as the CSV. It is a fine alternative if a CSV quirk ever bites.

### 112 gotchas

- `LAW_AUTHORS` uses `", "` both between authors and inside function names (`Ministre de la Famille, du Logement social et de la Solidarité sociale`), and some cells repeat an author (`Monsieur Jean Gremling, Député, Monsieur Jean Gremling, Député`). Do not split naively. Store the raw string. If splitting is needed, split on `, (?=Monsieur |Madame )`.
- 10 `LAW_CONTENT` cells start with a stray `'` (Excel text escape, e.g. `'-Exposé des motifs`). Strip one leading `'`.
- Titles keep upstream line breaks (`…portant\n1. création d'un fonds…`). Normalise whitespace for display or search, but keep the raw text.
- `LAW_NUMBER` must be a string (because of the `A`/`B` suffixes). For sorting, use `(int(digits), suffix)`.

## Dataset 121: Etat des travaux

One resource: xlsx `8d8bf90f-aba9-4928-98bb-66bbdc3bae6b`, 914 KB, `last_modified` 2025-04-10. Two sheets: **`ETAT_TRAVAUX`** (data) and **`LISTE_COLONNES`** (data dictionary). It contains one row per parliamentary dossier deposited since 2018-12-04, 1,156 rows, numbers 7389 to 8520 plus older split dossiers like `6539A` or `7139A`. `Dossier` is unique.

### Cell typing (openpyxl)

- `Dossier` is an `int` (e.g. `7389`), or a `str` when suffixed (`'7258A'`). Normalise with `str(v)`.
- Date columns are real Excel dates (`datetime` at midnight, display format `dd.mm.yy`). Empty cells are `None`.
- Counts are `int`. Text is `str`.
- **Hyperlinks**: the main value of this dataset. They live in cell `.hyperlink.target`, not in the value, so load *without* `read_only=True`, which drops hyperlinks. pandas also drops them.

### Columns (`LISTE_COLONNES`: N°, Code, Nom (DB), Nom (XLS), Commentaires, Valeurs) plus observed data

| # | XLS header | DB name | Definition (from LISTE_COLONNES) | Observed / hyperlink |
|---|---|---|---|---|
| A | `Dossier` | dos_numero | N° du dossier | int/str; link `https://www.chd.lu/fr/dossier/{n}` |
| B | `Nature` | dos_nature | Nature du dossier | `PL`=ProjetDeLoi (959), `PPL`=PropositionDeLoi (94), `PRREG`=PropositionDeRevisionReglementCHD (34), `PRGD`=ProjetDeReglementGrandDucal (27), `DO`=DebatOrientation (19), `PRCONS`=PropositionDeRevisionConstitution (10), `CSI`=ComptesDuServiceInterieur (7), `RAC`=RapportsActivites (5), plus one undocumented raw value `RapportsDeCommissionDivers` |
| C | `Relatif à` | dos_intitule | Intitulé | title **without** the type prefix, starting with `.`: `.portant création…`; newlines replaced by `¯` |
| D | `Etat` | dos_etat | État du dossier | `PUB_JO` 815, `COMM` 223, `RETIRE` 52, `.` 30 (= none), `VOTE_OK` 14, `TRANSFO` 9, `VOTE_KO` 5, `CREE` 5, `AVIS.CP` 2, `2EME.VOTE` 1. No value list in LISTE_COLONNES. |
| E | `EUR` | dos_transp_dir_euro | transposes an EU directive | `OUI` or empty |
| F | `co_co` | co_co | consolidated full-dossier PDF available | `X` or empty; link `https://wdocs-pub.chd.lu/docs/compilation/docpa/pdf/{n}_Dossier_Complet.pdf` |
| G | `Dépôt` | date_depot | date de dépôt | date, never empty; link = deposit document PDF `https://wdocs-pub.chd.lu/docs/exped/NNNN/NNN/NNNNNN.pdf` |
| H | `Recev?` | date_recevabilite | date de recevabilité | header is literally `Recev?` (a character was lost upstream; the DB name is `date_recevabilite`); 48 filled |
| I | `RenvComm` | date_renvoi_commission | renvoi en commission | date |
| J | `1erAvisCE` | date_1_avis_ce | 1er avis Conseil d'État | date + PDF link |
| K | `DerAvisCE` | date_dernier_avis_ce | dernier avis CE | date + PDF link |
| L | `1erRapp` | date_1_rapporteur | nomination 1er rapporteur | date |
| M | `DerRapp` | date_dernier_rapporteur | nomination dernier rapporteur | date |
| N | `RappCommPrev` | date_rapport_commission_previsionnelle | date prévisionnelle du rapport | date |
| O | `RappComm` | date_rapport_commission | rapport de commission | date + PDF link |
| P | `Comm1` | date_premiere_commission_rmp | 1re réunion de commission | date + PDF link (minutes) |
| Q | `CommLast` | date_derniere_commission_rmp | dernière réunion de commission | date + PDF link. Can be in the **future** (3 rows, up to 2028-03-31). |
| R | `SeanPub1` | date_premiere_spub_rmp | 1re séance publique | date |
| S | `Vote1` | date_1_vote | 1er vote | date + PDF link |
| T | `Vote2` | date_2_vote | 2e vote | date (5 rows) |
| U | `DispVote` | date_dispense_2_vote | dispense du 2e vote | date |
| V | `1erAmdtGvt` | date_1_amendement_gouvernement | 1er amendement gouvernemental | date |
| W | `DerAmdtGvt` | date_dernier_amendement_gouvernement | dernier amendement gouv. | date |
| X | `1erAmdtComm` | date_1_amendement_commission | 1er amendement de commission | date |
| Y | `DerAmdtComm` | date_dernier_amendement_commission | dernier amendement comm. | date |
| Z | `AvisCP` | date_avis_conference_president | avis Conférence des Présidents | date |
| AA | `Publication` | date_publication | publication au Journal officiel | date (no link) |
| AB | `NbrAut` | nbr_auteurs | nombre d'auteurs | int 0..14 |
| AC | `Auteurs` | list_auteurs | liste des auteurs | `\|`-separated short names: `J.Asselborn (Min. Affaires étrangères & européennes)`, `S.Clement\|M.Goergen`; link `https://www.chd.lu/fr/deputy/{id}` or `/fr/commission/{id}`, **first author only** |
| AD | `NbrComm` | nbr_comm_dossier | nombre de commissions | int |
| AE | `Comm` | list_comm_dossier_abrev | commission(s) en charge (abbrev.) | `FI`, `AEECA`, `JUST\|J`; link `https://www.chd.lu/fr/commission/{id}` (first only) |
| AF | `NbrPres` | nbr_comm_pressentie_dossier | nombre de commissions pressenties | int |
| AG | `Pressentie` | list_comm_pressentie_dossier_abrev | commission(s) pressentie(s) | abbrev, or `=` (same as `Comm`, 865 rows) |
| AH | `NbrRapp` | nbr_rapporteurs | nombre de rapporteurs | int |
| AI | `Rapporteur` | list_rapporteurs | rapporteur(s) | `J.Elvinger`; link `https://www.chd.lu/fr/deputy/{id}` |
| AJ | `NbrAjPPGvt` | nbr_ajout_prise_position_gouvernement | nb prises de position gouv. | int |
| AK | `AjAvisCHD` | nbr_ajout_avis_chambre | nb avis (Chambre) | int |
| AL | `AjAvisCE` | nbr_ajout_avis_conseil_etat | nb avis Conseil d'État | int |
| AM | `AjAmdtGvt` | nbr_ajout_amendement_gouvernement | nb amendements gouv. | int |
| AN | `AjAmdtComm` | nbr_ajout_amendement_commission | nb amendements comm. | int |
| AO | `NbrComm2` | nbr_commission_rmp | nb commissions concernées | int |
| AP | `cmt_commission` | cmt_commission | committee full name | `C. Finances & Budget` |
| AQ | `cmt_commission_pressentie` | cmt_commission_pressentie | expected committee full name | `C. Mobilité & Travaux publics` |

### What 121 adds beyond 112

- **Links**: dossier page `https://www.chd.lu/fr/dossier/{n}` (also constructible for any 112 number) and the full-dossier PDF `…/{n}_Dossier_Complet.pdf`. There are also per-step document PDFs: deposit, Conseil d'État opinions, committee report, committee minutes and vote.
- **Procedure timeline**: committee referral, Conseil d'État opinions, rapporteur, committee meetings, votes, publication date. 112 has only deposit and evacuation dates.
- **Committee** in charge (abbrev, full name, chd.lu id) and **rapporteur** (name and deputy id).
- **Ministry**: short author form `G.Roth (Min. Finances)`. 112 has the long form `Monsieur Gilles Roth, Ministre des Finances`.
- **EU transposition** flag.
- Neither dataset has a **summary** or abstract. 112 `LAW_CONTENT` is a list of documents, not a summary.

Join key: `str(Dossier)` == `LAW_NUMBER`. 1,043 dossiers are in both files, 113 only in 121 (deposited after 2024-07-04), and 1 with number >= 7389 only in 112 (112 also holds all pre-2018 dossiers). Types map 1:1 (`PL`↔`ProjetDeLoi`, `PPL`↔`PropositionDeLoi`, …). Status maps mostly (`PUB_JO`↔`Publie`, `COMM`↔`EnCommission`, `RETIRE`↔`Retire`, `TRANSFO`↔`Transforme`, `VOTE_KO`↔`VoteRefuse`, `.`↔`Vide`). Because the snapshots differ in date, 87 of the 1,043 shared rows have different statuses (mostly 112 `EnCommission`/`VoteAccepte` vs 121 `PUB_JO`, i.e. progress made after 2024-07-15); 121 is newer and wins.

## Recommended parsing approach

1. Resolve resources via the API (see above). Store `resource_id`, `last_modified` and `checksum.value`, and skip unchanged files.
2. **112**: `csv.DictReader(open(p, encoding="utf-8-sig", newline=""))`. Drop `NATURE`. Keep `LAW_NUMBER` as str. Parse dates with `datetime.strptime(v, "%d/%m/%Y").date()` when not empty. Strip one leading `'` from text fields. Filter `LAW_TYPE in {"ProjetDeLoi","PropositionDeLoi"}` (configurable).
3. **121**: `openpyxl.load_workbook(p)` (not read-only, to get hyperlinks). Select the sheet by name `ETAT_TRAVAUX` and map columns **by header name**, not position. Handle header `Recev?` defensively (startswith `Recev`). For each cell take `.value` (`datetime` → `.date()`) and `.hyperlink.target` when present. Title: `v.lstrip(".").replace("¯", "\n").strip()`. Split `Auteurs`/`Comm`/`Rapporteur` on `|`. Treat `Etat == "."` as null and `Pressentie == "="` as "same as Comm".
4. Merge on dossier number. 121 overrides status and dates where both exist. 112 provides pre-2018 history and long-form author names.
5. Don't trust the data to be current (last refresh 2025-04-10). Show a "source as of" date from `resource.last_modified` in the UI.

## Fixtures

In `tests/fixtures/datapublic/` (commit these):

| File | What |
|---|---|
| `dataset_112.json` | API response `GET /api/1/datasets/63c6884fcfa8113ec4793d13/` |
| `dataset_121.json` | API response `GET /api/1/datasets/67f775f1adf67ff4c46f1b37/` |
| `112-texte-loi.sample.csv` | 70 raw records cut byte-for-byte from the real CSV (BOM, header, quoting, multiline cells kept): the 30 oldest, suffixed numbers (`3837A`…), the 25 most recent (`Cree`/`EnCommission`, empty evacuation date) |
| `121-etat-travaux.xlsx` | full real file (914 KB, under the 3 MB limit) |
| `121-etat-travaux.sample.xlsx` | 56 rows (first 30, 6 suffixed dossiers, last 20) rebuilt with openpyxl. It keeps both sheets (`ETAT_TRAVAUX`, `LISTE_COLONNES`), values, number formats and hyperlinks. Cell styles are not copied. |

Not committed (over 3 MB, or redundant), kept in `/tmp/claude-0/research/dl/`: `112-texte-loi.csv` (4.5 MB, full), `112-texte-loi.xml` (4.1 MB), `112-texte-loi.xlsx` (615 KB, lossy duplicate of the CSV), `112-texte-loi.zip` (HTML report). Re-download any of them via `https://data.public.lu/fr/datasets/r/{resource_id}`.
