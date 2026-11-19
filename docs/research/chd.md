# chd.lu research note (Chambre des Députés, Luxembourg)

Researched 2026-11-19. Fetched politely (one request at a time, ≥2.5 s apart, UA
`lexwatch-research/0.1 (+internal bill tracker)`). About 15 requests in total.

## TL;DR

1. **robots.txt on www.chd.lu disallows every path we would scrape**:
   `/*/dossier/*`, `/*/search`, `/*/searchfolders`, `/*/searchactivities`, `/*/ajax/*`
   and any `?page=` pager. I therefore **did not fetch any dossier or search page**.
   There are no HTML dossier fixtures and no verified CSS selectors for dossier pages.
   Building the planned HTML scraper would mean ignoring robots.txt.
2. The Chambre's sanctioned channel is **Open Data on data.public.lu** (licence CC0,
   but "mention de l'origine requise" per chd.lu/fr/open-data). Dataset 112 lists all
   bills with the exact fields we need. **It has not been refreshed since 2024-07-15**
   (highest bill 8410). Bill numbers are now at ≥8767, so it is useless for a daily
   delta until the Chambre refreshes it.
3. Four other Chambre datasets **are regenerated daily at about 04:30 UTC** (questions,
   deputies, organs and **votes**). The votes CSV gives a daily, robots-safe signal for
   "bill N was voted on date D", but nothing about new filings.
4. **Recommendation**: before writing a scraper, email `relations.publiques@chd.lu`
   (the open-data contact) and ask for (a) dataset 112 (bills) and/or 121 (état des
   travaux, with document links) to join the daily 04:30 refresh that 103/104/105/109
   already have, or (b) written permission to fetch `/fr/dossier/{n}` at a low rate.
   Meanwhile, build the ingest against the 112/121/109 open-data formats documented
   below.

## robots.txt (verbatim, https://www.chd.lu/robots.txt, Last-Modified 2026-10-23)

Also saved byte-for-byte to `tests/fixtures/chd/robots.txt`.

```
#
# robots.txt
#
# This file is to prevent the crawling and indexing of certain parts
# of your site by web crawlers and spiders run by sites like Yahoo!
# and Google. By telling these "robots" where not to go on your site,
# you save bandwidth and server resources.
#
# This file will be ignored unless it is at the root of your host:
# Used:    http://example.com/robots.txt
# Ignored: http://example.com/site/robots.txt
#
# For more information about the robots.txt standard, see:
# http://www.robotstxt.org/robotstxt.html

User-agent: *

# Drupal content
Allow: /*/room
Allow: /*/webform/*
Allow: /*/glossaire
Allow: /*/breves

# Chamber composition 
Allow: /*/room
Allow: /*/deputy/*
Allow: /*/deputies 
Allow: /*/group/*  
Allow: /*/commission/* 
Allow: /*/commissions 
Allow: /*/delegation/*
Allow: /*/delegations 
Allow: /*/bureau 
Allow: /*/conference 

# Dossiers
Disallow: /*/dossier/* 
Disallow: /*/techdossier/* 
Disallow: /*/eurodossier/*
Disallow: /*/question/* 
Disallow: /*/motion_resolution/* 
Disallow: /*/debate/*
Disallow: /*/comexbu/*

# Agenda
Disallow: /*/agenda 
Disallow: /*/meeting/*
Disallow: /*/seance/*

# Autres
Allow: /*/reportages/*
Allow: /*/podcast

# Paths (clean URLs)
Disallow: /*/search
Disallow: /*/searchfolders
Disallow: /*/searchactivities
Disallow: /*/searchagenda
Disallow: /*/searchinterventions

# Appels passerelles
Disallow: /*/ajax/*

# Abonnements
Disallow: /*/abonnements/*

# Sitemap
Allow: /sitemap.xml
Sitemap: /sitemap.xml

# Pages paginées (Drupal pager)
Disallow: /*?page=
Disallow: /*&page=
```

Notes:
- `wdocs-pub.chd.lu` (the document/PDF host) and `data.public.lu` both return 404
  for /robots.txt, so neither restricts crawling.
- The sitemap (`/sitemap.xml` → `?page=1..6`) is a Drupal "simple_sitemap" with
  lastmod 2025-07-24. It is stale and in any case points to disallowed dossier URLs.
- The site is Drupal behind an F5/BIG-IP (`TS01…` cookie). Expect bot mitigation if
  someone scrapes aggressively.

## Terms of use / reuse

- **Notice légale**, https://www.chd.lu/fr/notice-legale: content is "à titre
  indicatif" and may be incomplete or out of date. Where they diverge, the original
  documents prevail (document parlementaire, Journal officiel). The relevant clause:
  *"Sauf indication contraire, la reproduction des informations contenues sur ce site
  est autorisée à des fins non commerciales à condition que la source soit
  expressément mentionnée."* There is no clause about automated access. Contact:
  info@chd.lu.
- **Open Data**, https://www.chd.lu/fr/open-data: the data is "pour une utilisation
  libre", published on data.public.lu under a Creative Commons licence ("La mention de
  l'origine des données est requise"). The dataset metadata says `cc-zero`. The page
  says the data is refreshed "tous les deux mois en moyenne". Formats: HTML, XML,
  XLSX, CSV. Contact: relations.publiques@chd.lu. Legal basis cited: loi du 29
  novembre 2021 (open data / Directive (UE) 2019/1024).
- **Privacy**: https://www.chd.lu/fr/protection-des-donnees (not fetched).
- **For lexwatch**: an internal, non-commercial use with attribution ("Source :
  Chambre des Députés du Grand-Duché de Luxembourg, chd.lu / data.public.lu") fits
  both texts. Automated fetching of dossier and search pages is not covered and is
  excluded by robots.txt.

## URL patterns

| What | URL | robots | Verified how |
|---|---|---|---|
| Dossier page | `https://www.chd.lu/fr/dossier/{number}` (also `/de/`, `/en/`, `/lb/`) | **Disallowed** | hyperlinks inside dataset 121 |
| Full dossier PDF | `https://wdocs-pub.chd.lu/docs/compilation/docpa/pdf/{number}_Dossier_Complet.pdf` | ok (no robots) | dataset 121 hyperlinks |
| Individual document PDF | `https://wdocs-pub.chd.lu/docs/exped/{4 digits}/{3 digits}/{docid}.pdf` (e.g. `/docs/exped/0013/055/26551.pdf`) | ok | dataset 121 hyperlinks |
| Author / deputy / minister | `https://www.chd.lu/fr/deputy/{id}` | Allowed | dataset 121 hyperlinks |
| Commission | `https://www.chd.lu/fr/commission/{id}` | Allowed | dataset 121 hyperlinks |
| Recent legislative activity | `https://www.chd.lu/fr/searchactivities?filterActivity=Dossiers Législatifs&dDateF=2026/11/12&dDateT=2026/11/19` (params `filterActivity`, `searchTerm=ALL`, `dDateF`/`dDateT` as `YYYY/MM/DD`) | **Disallowed** | linked from the homepage, **not fetched** |
| Dossier search | `https://www.chd.lu/fr/searchfolders` | **Disallowed** | not fetched |
| XHR endpoints | `/{lang}/ajax/*` (populates homepage `.Feed__item` etc.) | **Disallowed** | not probed. `main.js` holds no static endpoint strings |
| Bills dataset (112) API | `https://data.public.lu/api/1/datasets/liste-des-projets-et-propositions-de-lois/` (old slug `la-liste-des-projets-propositions-de-lois` 301-redirects) | ok | fetched |
| 112 files | `https://download.data.public.lu/resources/la-liste-des-projets-propositions-de-lois/20240715-1438xx/112-texte-loi.{csv,xml,xlsx,zip}` (**path is timestamped; resolve via the API `resources[].url`**) | ok | fetched |
| État des travaux (121) | API slug `etat-des-travaux`; file `.../etat-des-travaux-de-la-chambre-des-deputes-du-luxembourg-121/20250410-094141/121-etat-travaux.xlsx` | ok | fetched (last update 2025-04-10, max dossier 8520) |
| Votes (109), daily | API slug `liste-des-votes-en-seances-plenieres`; CSV `.../liste-des-votes-en-seances-plenieres/{YYYYMMDD-HHMMSS}/109-votes.csv` | ok | fetched (regenerated 2026-11-19 04:30 UTC) |
| All Chambre datasets | `https://data.public.lu/api/1/organizations/chambre-des-deputes-du-grand-duche-de-luxembourg/datasets/?page_size=50` | ok | fetched (20 datasets) |

There is no public JSON API for bills on chd.lu. The only JSON is the udata
*metadata* API on data.public.lu. Use the `last_update` field and
`resources[].last_modified` to detect a refresh, then download the CSV.

## Data formats and field mapping

### Dataset 112: `112-texte-loi.csv` (primary bill list; stale since 2024-07-15)

- UTF-8 **with BOM**, comma-separated, `"`-quoted, LF line endings, **embedded
  newlines inside quoted fields** (titles, content). Use a real CSV parser.
- 4,593 rows, numbers 1129–8410. No row order guarantee (roughly ascending).
- Column `NATURE` is a constant junk column (the list of all types in brackets).
  Ignore it.
- The XML variant (Oracle BI Publisher) has the same fields:
  `/DATA_DS/DOCPA/{LAW_NUMBER, LAW_TYPE, LAW_DEPOSIT_DATE, LAW_EVACUATION_DATE, LAW_STATUS, LAW_TITLE, LAW_CONTENT, LAW_AUTHORS}`.

| Source column | lexwatch field | Notes |
|---|---|---|
| `LAW_NUMBER` | `number` (int) | 4-digit dossier number, unique |
| `LAW_TYPE` | `type` | see vocabulary. Filter `ProjetDeLoi`, `PropositionDeLoi` (+ revisions de Constitution if wanted) |
| `LAW_DEPOSIT_DATE` | `filed_on` | `DD/MM/YYYY` |
| `LAW_EVACUATION_DATE` | `closed_on` | `DD/MM/YYYY`, empty while pending (367 rows empty) |
| `LAW_STATUS` | `status` | see vocabulary |
| `LAW_TITLE` | `title` | full title, may contain `\n` |
| `LAW_CONTENT` | `documents_summary` | free-text list of the deposit documents (e.g. "Document de dépôt", "-Exposé des motifs\n-Texte…"). **Not links** |
| `LAW_AUTHORS` | `authors` (raw) | `"Monsieur Eric Thill, Ministre de la Culture"`. Several authors are comma-joined (`"Monsieur Gilles Baum, Député, Madame Taina Bofferding, Députée, …"`). For a PL, the ministry is the part after the name. Splitting is heuristic (split on `Monsieur`/`Madame`) |
| (derived) | `dossier_url` | `https://www.chd.lu/fr/dossier/{number}` (link only, do not fetch) |
| (derived) | `full_pdf_url` | `https://wdocs-pub.chd.lu/docs/compilation/docpa/pdf/{number}_Dossier_Complet.pdf` (exists at least for the 2018+ dossiers seen in 121) |

There is **no subject/objet field** separate from the title (title = "Projet de
loi portant …"). There are **no per-document links** in 112.

**`LAW_TYPE` values** (count): ProjetDeLoi 3357, ProjetDeReglementGrandDucal 487,
PropositionDeLoi 430, PropositionDeRevisionReglementCHD 91, DebatOrientation 75,
PropositionDeRevisionConstitution 56, ProjetDeRevisionDeConstitution 28,
ComptesDuServiceInterieur 28, RapportsActivites 17, DebatSurEtatNation 8,
RapportsDeCommissionDivers 5, DebatDivers 4, RapportsDeCommissionDebat 3, Autre 3,
ProprositionDeDeclarationRevisionConstitution 1 (sic, typo in source).

**`LAW_STATUS` values**: Publie 3540 (published in Journal officiel), Retire 516,
EnCommission 239, Vide 145 (empty/not applicable, e.g. débats), Transforme 42,
VoteAccepte 37, EvacueConjointement 31, VoteRefuse 19, Cree 10 (just filed),
Fusionne 10, AviseParConferencePresidents 3, EnAttenteDispenseSecondVote 1.

### Dataset 121: `121-etat-travaux.xlsx` (per-stage dates + hyperlinks; legislatures since 2018; stale since 2025-04-10)

- Sheet `ETAT_TRAVAUX` (1,156 rows, dossiers 7389–8520) and sheet `LISTE_COLONNES`
  (column dictionary). Cells are strings and datetimes. **Many cells carry Excel
  hyperlinks** (read them with openpyxl `cell.hyperlink.target`, which needs a
  non-read-only workbook).
- Title (`Relatif à`) starts with `.` (the type prefix is stripped) and uses `¯` as a
  line separator. Example: `.portant approbation ¯1° de l'Accord …`.

| Column (XLS / DB) | Field | Value / hyperlink |
|---|---|---|
| `Dossier` / dos_numero | number | link → `https://www.chd.lu/fr/dossier/{n}` |
| `Nature` / dos_nature | type | PL, PPL, PRGD, PRREG, PRCONS, DO, RAC, CSI |
| `Relatif à` / dos_intitule | title (without the type prefix) | |
| `Etat` / dos_etat | status | PUB_JO, COMM, RETIRE, `.`, VOTE_OK, TRANSFO, VOTE_KO, CREE, AVIS.CP, 2EME.VOTE |
| `EUR` | transposes EU directive | |
| `co_co` | `X` if consolidated dossier exists | link → `…/docpa/pdf/{n}_Dossier_Complet.pdf` |
| `Dépôt` / date_depot | filed_on | link → deposit document PDF |
| `RenvComm`, `1erAvisCE`, `DerAvisCE`, `RappComm`, `Comm1`, `CommLast`, `SeanPub1`, `Vote1`, `Vote2`, `DispVote`, `1erAmdtGvt`…`DerAmdtComm`, `AvisCP`, `Publication` | stage dates | most link → the corresponding PDF on wdocs-pub (this **is** the document list) |
| `Auteurs` / list_auteurs | authors, e.g. `J.Asselborn (Min. Affaires étrangères & européennes)` | link → `/fr/deputy/{id}` |
| `Comm` | commission abbrev. (e.g. `AEECA`, `FI`) | link → `/fr/commission/{id}` |
| `Rapporteur` | rapporteur | link → `/fr/deputy/{id}` |

Status crosswalk between 112 and 121: Cree↔CREE, EnCommission↔COMM,
VoteAccepte↔VOTE_OK, VoteRefuse↔VOTE_KO, Publie↔PUB_JO, Retire↔RETIRE,
Transforme↔TRANSFO, AviseParConferencePresidents↔AVIS.CP,
EnAttenteDispenseSecondVote↔2EME.VOTE, Vide↔`.`.

### Dataset 109: `109-votes.csv` (daily; plenary votes since 2021-07-13)

- **Encoding cp1252 / Latin-1, not UTF-8** (`Voté` → byte 0xE9). Header:
  `meeting_date,vote_name,vote_type,person_title,lastname,firstname,rattachement_abrv,rattachement_type,vote_result`.
- One row per deputy per vote (89k rows, 1,455 distinct votes). `meeting_date` is
  `YYYY-MM-DD HH:MM:SS` (local time).
- `vote_type`: `Premier vote constitutionnel`, `Second vote constitutionnel`, `Voté`,
  `Not Found`. `vote_result`: Oui / Non / Abstention / Pas participé.
- `vote_name` is free text. Bill references look like `PL 8535 - Logements
  abordables`, `PL 8535 - … - Vote séparé (article 44)`, `Projet de loi N°7xxx`,
  `MO 1 - PL8727` (a motion about a bill, **not** a vote on the bill). Suggested
  regex for a bill vote: `^(PL|PPL)\s?(\d{4})\b` combined with
  `vote_type == "Premier vote constitutionnel"`. Exclude names starting with
  `MO`/`Motion`.
- Latest vote in the 2026-11-19 file is **2026-07-15**, although the file is
  regenerated daily. Either there were no recorded votes since the summer recess or
  the export lags. Treat this as uncertain. Highest bill number seen: 8767.

### Date formats summary
- 112 CSV/XML: `DD/MM/YYYY`.
- 121 XLSX: native Excel datetimes (midnight).
- 109 CSV: `YYYY-MM-DD HH:MM:SS`.
- chd.lu search query params: `YYYY/MM/DD`.
- data.public.lu API: ISO-8601 (`2024-07-15T14:38:35.877000+00:00`).

## HTML parsing (selectolax)

- `selectolax==1.0.2` (current) **removed the Modest backend**:
  `from selectolax.parser import HTMLParser` raises ImportError. Use
  `from selectolax.lexbor import LexborHTMLParser`.
- It works fine on allowed chd.lu pages (Drupal, server-rendered): main content is in
  `main`. Link extraction with `main a[href]` worked on /fr/notice-legale and
  /fr/open-data.
- **Dossier page selectors were not determined**, because those pages are
  robots-disallowed. The homepage "activity feed" (`.Feed__item`) is empty in the
  server HTML and is filled by JS from `/fr/ajax/*` (disallowed).

## Politeness / rate-limit recommendation

- Daily job: 1 request to the data.public.lu API per dataset (112, 121, 109). Download
  a CSV only if `resources[].last_modified` changed. That is at most a few MB per day.
- If permission for dossier pages is ever granted: ≤1 request / 5 s, only for
  dossiers whose status changed, the descriptive UA above with a contact address,
  honour `Retry-After`, back off on 403/429 (F5 bot protection), and avoid 06:00–09:00
  CET when the open-data exports run.
- Link to PDFs on wdocs-pub.chd.lu. Do not mirror them.
- Always display attribution ("Source : Chambre des Députés").

## Risks

1. **Legal/ethical**: scraping `/dossier/`, `/search*` and `/ajax/` ignores explicit
   robots.txt rules. Get written permission first or don't do it.
2. **Freshness**: dataset 112 is 16 months stale and 121 is 7 months stale. The
   documented cadence ("tous les deux mois") is not being met. There is no
   robots-compliant source today for *new filings* within a day.
3. **Format drift**: data.public.lu file URLs are timestamped and dataset slugs
   change (112 was renamed, with a redirect). Always resolve via the API.
4. **Encoding inconsistency**: 112 is UTF-8+BOM and 109 is cp1252.
5. **Free-text fields**: authors (112) and vote names (109) need heuristic parsing.
   112 has no per-document links (121 does).
6. **Incompleteness**: the Notice légale states the site content is indicative. The
   Journal officiel (Legilux) is authoritative for published laws.
7. **Unverified alternative**: Legilux (legilux.public.lu, ELI/SPARQL) may link
   published laws to their dossier numbers. It was not investigated and would only
   cover the end of the lifecycle.

## Fixtures (`tests/fixtures/chd/`)

| File | Content |
|---|---|
| `robots.txt` | verbatim www.chd.lu/robots.txt |
| `opendata-112-dataset.json` | raw data.public.lu API response for dataset 112 (metadata + resource URLs) |
| `opendata-112-texte-loi.sample.csv` | header + 5 **raw, byte-identical records** from the 112 CSV (UTF-8 BOM kept): 8410 recent PL `Cree`, 8398 PPL `EnCommission` (multi-author), 7394 PL `Publie`, 6763 PL `Retire`, 8363 DebatOrientation `Vide` (filter-out case) |
| `opendata-121-etat-travaux.excerpt.json` | **derived** (not raw) JSON of 5 rows of the 121 XLSX with cell hyperlinks: 7389 PL PUB_JO, 7410 PPL COMM, 7424 PL COMM, 7523 PL RETIRE, 7684 PPL VOTE_KO |
| `opendata-109-votes.head.csv` | first 37 complete lines of today's 109 votes CSV (raw cp1252 bytes, via HTTP Range) |

No chd.lu dossier/listing HTML fixtures exist, because of robots.txt (see TL;DR).
