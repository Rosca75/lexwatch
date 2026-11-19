"""data.public.lu adapters for datasets 112 (bill list) and 121 (Etat des travaux).

Both resolve their file through the udata API (``GET {api}datasets/{id-or-slug}/``), pick the
main resource of the expected format with the highest ``last_modified``, and download it via
the stable alias ``https://data.public.lu/fr/datasets/r/{resource_id}`` (302 to the current
timestamped file). When the resource id and checksum equal those of the last successful run
(``known_state``, set by the pipeline from the ``source_state`` table) the download is
skipped and ``fetch`` returns ``[]`` with ``unchanged = True``.

Parsing choices are documented in ``docs/ingestion.md``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from lexwatch.http import polite_get
from lexwatch.sources.base import (
    STATUS_ADOPTED,
    STATUS_DEPOSITED,
    STATUS_IN_COMMITTEE,
    STATUS_MERGED,
    STATUS_OTHER,
    STATUS_PUBLISHED,
    STATUS_REJECTED,
    STATUS_SECOND_VOTE_PENDING,
    STATUS_TRANSFORMED,
    STATUS_WITHDRAWN,
    TYPE_PROJET,
    TYPE_PROPOSITION,
    BillRecord,
    ResourceState,
    SourceError,
    clean_text,
    dossier_url,
    normalize_number,
)

log = logging.getLogger(__name__)

SOURCE_112 = "datapublic-112"
SOURCE_121 = "datapublic-121"

DEFAULT_API_URL = "https://data.public.lu/api/1/"

# Dataset references, tried in order (id first, slug as fallback; slugs have changed before).
DATASET_112 = ("63c6884fcfa8113ec4793d13", "liste-des-projets-et-propositions-de-lois")
DATASET_121 = ("67f775f1adf67ff4c46f1b37", "etat-des-travaux")

# --- dataset 112 -----------------------------------------------------------------------------

# LAW_TYPE values kept (bills only) and their normalized type.
KEPT_112_TYPES: dict[str, str] = {
    "ProjetDeLoi": TYPE_PROJET,
    "PropositionDeLoi": TYPE_PROPOSITION,
}
# LAW_TYPE values seen on 2026-11-19 and deliberately excluded (not bills, or constitutional /
# rules-of-procedure revisions). Any other value is excluded too and counted in ``stats``.
EXCLUDED_112_TYPES = (
    "ProjetDeReglementGrandDucal",
    "PropositionDeRevisionReglementCHD",
    "DebatOrientation",
    "PropositionDeRevisionConstitution",
    "ProjetDeRevisionDeConstitution",
    "ComptesDuServiceInterieur",
    "RapportsActivites",
    "DebatSurEtatNation",
    "RapportsDeCommissionDivers",
    "DebatDivers",
    "RapportsDeCommissionDebat",
    "Autre",
    "ProprositionDeDeclarationRevisionConstitution",  # sic, upstream typo
)
# LAW_STATUS -> normalized status. ``None`` = "no status" (never overwrites a known status).
STATUS_112: dict[str, str | None] = {
    "Cree": STATUS_DEPOSITED,
    "EnCommission": STATUS_IN_COMMITTEE,
    "AviseParConferencePresidents": STATUS_OTHER,
    "EnAttenteDispenseSecondVote": STATUS_SECOND_VOTE_PENDING,
    "VoteAccepte": STATUS_ADOPTED,
    "VoteRefuse": STATUS_REJECTED,
    "Publie": STATUS_PUBLISHED,
    "Retire": STATUS_WITHDRAWN,
    "Transforme": STATUS_TRANSFORMED,
    "Fusionne": STATUS_MERGED,
    "EvacueConjointement": STATUS_MERGED,
    "Vide": None,
}
REQUIRED_112_COLUMNS = (
    "LAW_NUMBER",
    "LAW_TYPE",
    "LAW_DEPOSIT_DATE",
    "LAW_EVACUATION_DATE",
    "LAW_STATUS",
    "LAW_TITLE",
    "LAW_CONTENT",
    "LAW_AUTHORS",
)

# --- dataset 121 -----------------------------------------------------------------------------

SHEET_121 = "ETAT_TRAVAUX"
KEPT_121_NATURES: dict[str, str] = {"PL": TYPE_PROJET, "PPL": TYPE_PROPOSITION}
# Etat -> normalized status (crosswalk with 112 in docs/research/datapublic.md).
STATUS_121: dict[str, str | None] = {
    "CREE": STATUS_DEPOSITED,
    "COMM": STATUS_IN_COMMITTEE,
    "AVIS.CP": STATUS_OTHER,
    "2EME.VOTE": STATUS_SECOND_VOTE_PENDING,
    "VOTE_OK": STATUS_ADOPTED,
    "VOTE_KO": STATUS_REJECTED,
    "PUB_JO": STATUS_PUBLISHED,
    "RETIRE": STATUS_WITHDRAWN,
    "TRANSFO": STATUS_TRANSFORMED,
    ".": None,
}
# Procedure-step date columns (in procedure order) -> label of the linked document.
# Dates go to extra["dates"] (keyed by column); linked cells to extra["documents"].
DATE_COLUMNS_121: dict[str, str] = {
    "Dépôt": "Dépôt",
    "Recev?": "Recevabilité",
    "RenvComm": "Renvoi en commission",
    "1erAvisCE": "Premier avis du Conseil d'État",
    "DerAvisCE": "Dernier avis du Conseil d'État",
    "1erRapp": "Nomination du premier rapporteur",
    "DerRapp": "Nomination du dernier rapporteur",
    "RappCommPrev": "Rapport de commission (prévisionnel)",
    "RappComm": "Rapport de commission",
    "Comm1": "Première réunion de commission",
    "CommLast": "Dernière réunion de commission",
    "SeanPub1": "Première séance publique",
    "Vote1": "Premier vote",
    "Vote2": "Second vote",
    "DispVote": "Dispense du second vote",
    "1erAmdtGvt": "Premier amendement gouvernemental",
    "DerAmdtGvt": "Dernier amendement gouvernemental",
    "1erAmdtComm": "Premier amendement de commission",
    "DerAmdtComm": "Dernier amendement de commission",
    "AvisCP": "Avis de la Conférence des Présidents",
    "Publication": "Publication au Journal officiel",
}
# Every column the parser reads: a missing one is a structure change (SourceError), never a
# silent loss of data.
REQUIRED_121_COLUMNS = (
    "Dossier",
    "Nature",
    "Relatif à",
    "Etat",
    "EUR",
    "co_co",
    *DATE_COLUMNS_121,
    "Auteurs",
    "Comm",
    "Rapporteur",
    "cmt_commission",
)
# closing_date for a 121 row, by normalized status: first non-empty column wins. 112's
# LAW_EVACUATION_DATE equals DispVote for adopted/published bills, so DispVote comes first.
CLOSING_COLUMNS_121: dict[str, tuple[str, ...]] = {
    STATUS_PUBLISHED: ("DispVote", "Vote2", "Publication", "Vote1"),
    STATUS_ADOPTED: ("DispVote", "Vote2", "Vote1"),
    STATUS_REJECTED: ("Vote2", "Vote1"),
}
FULL_DOSSIER_PDF = (
    "https://wdocs-pub.chd.lu/docs/compilation/docpa/pdf/{number}_Dossier_Complet.pdf"
)


# --- shared helpers --------------------------------------------------------------------------


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_dmy(value: str | None) -> date | None:
    """``DD/MM/YYYY`` -> date; empty -> None; anything else raises ValueError."""
    value = (value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, "%d/%m/%Y").date()


def _strip_quote(value: str | None) -> str:
    """Strip whitespace and one leading Excel text-escape quote."""
    value = (value or "").strip()
    return value[1:].lstrip() if value.startswith("'") else value


def site_root(api_url: str) -> str:
    parts = urlsplit(api_url)
    return f"{parts.scheme}://{parts.netloc}"


class UdataResourceAdapter:
    """Base adapter: resolve a data.public.lu resource, skip it when unchanged, parse it."""

    name = "udata"
    dataset_refs: tuple[str, ...] = ()
    resource_format = "csv"
    title_prefix: str | None = None

    def __init__(self, api_url: str = DEFAULT_API_URL, request_delay_seconds: float = 1.0) -> None:
        self.api_url = api_url if api_url.endswith("/") else api_url + "/"
        self.request_delay_seconds = request_delay_seconds
        self.known_state: ResourceState | None = None
        self.state: ResourceState | None = None
        self.unchanged = False
        self.stats: dict[str, Any] = {}

    # -- resolution ---------------------------------------------------------------------------

    def _get(self, client: httpx.Client, url: str) -> httpx.Response:
        return polite_get(client, url, delay_seconds=self.request_delay_seconds)

    def resolve(self, client: httpx.Client) -> dict[str, Any]:
        """Return the udata resource dict of the file to download."""
        errors: list[str] = []
        dataset: dict[str, Any] | None = None
        for ref in self.dataset_refs:
            try:
                payload = self._get(client, f"{self.api_url}datasets/{ref}/").json()
            except SourceError as exc:
                errors.append(str(exc))
                continue
            except ValueError as exc:
                errors.append(f"dataset {ref}: invalid JSON ({exc})")
                continue
            if not isinstance(payload, dict):
                raise SourceError(
                    f"{self.name}: dataset {ref}: API JSON is a {type(payload).__name__}, "
                    "expected an object"
                )
            dataset = payload
            break
        if dataset is None:
            raise SourceError(f"{self.name}: dataset not found: " + "; ".join(errors))
        resources = dataset.get("resources")
        if not isinstance(resources, list):
            raise SourceError(f"{self.name}: dataset has no resources list")
        candidates = [
            r
            for r in resources
            if isinstance(r, dict)
            and r.get("type", "main") == "main"
            and str(r.get("format", "")).lower() == self.resource_format
            and r.get("id")
        ]
        if self.title_prefix:
            preferred = [
                r for r in candidates if str(r.get("title", "")).startswith(self.title_prefix)
            ]
            candidates = preferred or candidates
        if not candidates:
            raise SourceError(f"{self.name}: no main {self.resource_format} resource in dataset")

        def key(r: dict[str, Any]) -> tuple[datetime, datetime]:
            floor = datetime.min.replace(tzinfo=UTC)
            return (
                parse_iso_datetime(r.get("last_modified")) or floor,
                parse_iso_datetime(r.get("created_at")) or floor,
            )

        return max(candidates, key=key)

    @staticmethod
    def resource_state(resource: dict[str, Any]) -> ResourceState:
        checksum = resource.get("checksum") or {}
        value = checksum.get("value") if isinstance(checksum, dict) else None
        algo = (checksum.get("type") or "sha1") if isinstance(checksum, dict) else "sha1"
        return ResourceState(
            resource_id=str(resource["id"]),
            last_modified=parse_iso_datetime(resource.get("last_modified")),
            checksum=f"{algo}:{value}" if value else None,
        )

    def download(self, client: httpx.Client, state: ResourceState) -> bytes:
        url = f"{site_root(self.api_url)}/fr/datasets/r/{state.resource_id}"
        content = self._get(client, url).content
        if state.checksum:
            algo, _, expected = state.checksum.partition(":")
            if algo in {"sha1", "md5", "sha256"}:
                actual = hashlib.new(algo, content).hexdigest()
                if actual != expected.lower():
                    # Truncated or corrupted download, or a file replaced between the API
                    # call and the download: never parse it; the next run retries.
                    raise SourceError(
                        f"{self.name}: checksum mismatch for resource {state.resource_id} "
                        f"({algo} expected {expected}, got {actual}, {len(content)} bytes)"
                    )
        return content

    def fetch(self, client: httpx.Client) -> list[BillRecord]:
        self.state, self.unchanged, self.stats = None, False, {}
        resource = self.resolve(client)
        self.state = self.resource_state(resource)
        if self.state.same_content(self.known_state):
            self.unchanged = True
            return []
        content = self.download(client, self.state)
        return self.parse(content, self.state)

    def parse(self, content: bytes, state: ResourceState) -> list[BillRecord]:
        raise NotImplementedError


# --- dataset 112 -----------------------------------------------------------------------------


def parse_112_csv(
    content: bytes, updated_at: datetime | None, stats: dict[str, Any] | None = None
) -> list[BillRecord]:
    """Parse ``112-texte-loi.csv`` (UTF-8 with BOM, multiline quoted cells)."""
    stats = stats if stats is not None else {}
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError(f"{SOURCE_112}: CSV is not UTF-8 ({exc})") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in REQUIRED_112_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise SourceError(f"{SOURCE_112}: CSV header changed, missing columns {missing}")
    excluded: Counter[str] = Counter()
    unknown_status: Counter[str] = Counter()
    records: list[BillRecord] = []
    bad_rows = 0
    rows = 0
    try:
        for row in reader:
            rows += 1
            if None in row or any(row.get(c) is None for c in REQUIRED_112_COLUMNS):
                # fewer (or more) cells than the header: a truncated or malformed file
                raise SourceError(
                    f"{SOURCE_112}: row {rows} has {len(row) - (None in row)} cells instead of "
                    f"{len(reader.fieldnames or [])} (truncated or malformed CSV)"
                )
            raw_type = (row.get("LAW_TYPE") or "").strip()
            bill_type = KEPT_112_TYPES.get(raw_type)
            if bill_type is None:
                excluded[raw_type or "(empty)"] += 1
                continue
            number = normalize_number(row.get("LAW_NUMBER"))
            if number is None:
                bad_rows += 1
                continue
            raw_status = (row.get("LAW_STATUS") or "").strip()
            if raw_status in STATUS_112:
                status = STATUS_112[raw_status]
            else:
                status = STATUS_OTHER
                unknown_status[raw_status] += 1
            try:
                filing = parse_dmy(row.get("LAW_DEPOSIT_DATE"))
                closing = parse_dmy(row.get("LAW_EVACUATION_DATE"))
            except ValueError:
                bad_rows += 1
                log.warning("ingest.bad_date", extra={"source": SOURCE_112, "number": number})
                filing = closing = None
            extra: dict[str, Any] = {"raw_status": raw_status, "raw_type": raw_type}
            content_list = clean_text(_strip_quote(row.get("LAW_CONTENT")))
            if content_list:
                extra["documents_list"] = content_list
            records.append(
                BillRecord(
                    number=number,
                    type=bill_type,
                    title=clean_text(_strip_quote(row.get("LAW_TITLE"))),
                    status=status,
                    filing_date=filing,
                    closing_date=closing,
                    author=clean_text(_strip_quote(row.get("LAW_AUTHORS"))) or None,
                    dossier_url=dossier_url(number),
                    summary=None,
                    source=SOURCE_112,
                    source_updated_at=updated_at,
                    extra=extra,
                )
            )
    except csv.Error as exc:
        raise SourceError(f"{SOURCE_112}: CSV parse error: {exc}") from exc
    if rows and not records:
        raise SourceError(f"{SOURCE_112}: no bill rows among {rows} rows")
    stats.update(
        rows=rows,
        kept=len(records),
        excluded_types=dict(sorted(excluded.items())),
        unknown_statuses=dict(unknown_status),
        bad_rows=bad_rows,
    )
    return records


class Dataset112Adapter(UdataResourceAdapter):
    name = SOURCE_112
    dataset_refs = DATASET_112
    resource_format = "csv"
    title_prefix = "112-texte-loi"

    def parse(self, content: bytes, state: ResourceState) -> list[BillRecord]:
        return parse_112_csv(content, state.last_modified, self.stats)


# --- dataset 121 -----------------------------------------------------------------------------


def _cell_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        for fmt in ("%d.%m.%y", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def _cell_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _split_pipe(value: Any) -> list[str]:
    return [p.strip() for p in _cell_text(value).split("|") if p.strip()]


def title_from_121(value: Any, bill_type: str) -> str:
    """``.portant approbation ¯1° de ...`` -> ``Projet de loi portant approbation\\n1° de ...``."""
    text = clean_text(_cell_text(value).replace("¯", "\n").lstrip(". \n"))
    if not text:
        return ""
    prefix = "Projet de loi" if bill_type == TYPE_PROJET else "Proposition de loi"
    if text.lower().startswith(("projet de loi", "proposition de loi")):
        return text
    return f"{prefix} {text}"


def parse_121_xlsx(
    content: bytes, updated_at: datetime | None, stats: dict[str, Any] | None = None
) -> list[BillRecord]:
    """Parse ``121-etat-travaux.xlsx``. Not read-only: hyperlinks are needed."""
    import openpyxl

    stats = stats if stats is not None else {}
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    except Exception as exc:  # zipfile/openpyxl raise many types on a corrupt file
        raise SourceError(f"{SOURCE_121}: cannot open XLSX: {exc!r}") from exc
    if SHEET_121 not in workbook.sheetnames:
        raise SourceError(f"{SOURCE_121}: sheet {SHEET_121!r} missing ({workbook.sheetnames})")
    sheet = workbook[SHEET_121]
    rows = sheet.iter_rows()
    try:
        header_cells = next(rows)
    except StopIteration as exc:
        raise SourceError(f"{SOURCE_121}: empty sheet") from exc
    columns: dict[str, int] = {}
    for idx, cell in enumerate(header_cells):
        name = _cell_text(cell.value)
        if name.startswith("Recev"):  # header lost a character upstream ("Recev?")
            name = "Recev?"
        if name:
            columns.setdefault(name, idx)
    missing = [c for c in REQUIRED_121_COLUMNS if c not in columns]
    if missing:
        raise SourceError(f"{SOURCE_121}: header changed, missing columns {missing}")

    excluded: Counter[str] = Counter()
    unknown_status: Counter[str] = Counter()
    records: list[BillRecord] = []
    total = 0
    for row in rows:

        def cell(name: str, row: tuple = row) -> Any:
            idx = columns.get(name)
            return row[idx] if idx is not None and idx < len(row) else None

        def value(name: str) -> Any:
            c = cell(name)
            return c.value if c is not None else None

        def link(name: str) -> str | None:
            c = cell(name)
            hyperlink = getattr(c, "hyperlink", None) if c is not None else None
            target = getattr(hyperlink, "target", None) if hyperlink is not None else None
            return str(target).strip() if target else None

        number = normalize_number(value("Dossier"))
        if number is None:
            continue
        total += 1
        nature = _cell_text(value("Nature"))
        bill_type = KEPT_121_NATURES.get(nature)
        if bill_type is None:
            excluded[nature or "(empty)"] += 1
            continue
        raw_status = _cell_text(value("Etat"))
        if raw_status in STATUS_121:
            status = STATUS_121[raw_status]
        elif not raw_status:
            status = None
        else:
            status = STATUS_OTHER
            unknown_status[raw_status] += 1

        dates: dict[str, str] = {}
        documents: list[dict[str, str]] = []
        for col, label in DATE_COLUMNS_121.items():
            if col not in columns:
                continue
            day = _cell_date(value(col))
            url = link(col)
            if day:
                dates[col] = day.isoformat()
            if url:
                documents.append(
                    {"label": label, "date": day.isoformat() if day else None, "url": url}
                )
        closing = None
        for col in CLOSING_COLUMNS_121.get(status or "", ()):
            if col in columns and (closing := _cell_date(value(col))):
                break

        authors = _split_pipe(value("Auteurs"))
        extra: dict[str, Any] = {"raw_status": raw_status, "raw_type": nature}
        if dates:
            extra["dates"] = dates
        if documents:
            extra["documents"] = documents
        if committee := _split_pipe(value("Comm")):
            extra["committee"] = committee
        if committee_name := _cell_text(value("cmt_commission")):
            extra["committee_name"] = committee_name
        if committee_url := link("Comm"):
            extra["committee_url"] = committee_url
        if rapporteur := _split_pipe(value("Rapporteur")):
            extra["rapporteur"] = rapporteur
        if rapporteur_url := link("Rapporteur"):
            extra["rapporteur_url"] = rapporteur_url
        if author_url := link("Auteurs"):
            extra["author_url"] = author_url
        full_pdf = link("co_co")
        if not full_pdf and _cell_text(value("co_co")).upper() == "X":
            full_pdf = FULL_DOSSIER_PDF.format(number=number)
        if full_pdf:
            extra["dossier_pdf_url"] = full_pdf
        if _cell_text(value("EUR")).upper() == "OUI":
            extra["eu_transposition"] = True

        records.append(
            BillRecord(
                number=number,
                type=bill_type,
                title=title_from_121(value("Relatif à"), bill_type),
                status=status,
                filing_date=_cell_date(value("Dépôt")),
                closing_date=closing,
                author="; ".join(authors) or None,
                dossier_url=link("Dossier") or dossier_url(number),
                summary=None,
                source=SOURCE_121,
                source_updated_at=updated_at,
                extra=extra,
            )
        )
    if total and not records:
        raise SourceError(f"{SOURCE_121}: no PL/PPL rows among {total} rows")
    stats.update(
        rows=total,
        kept=len(records),
        excluded_types=dict(sorted(excluded.items())),
        unknown_statuses=dict(unknown_status),
    )
    return records


class Dataset121Adapter(UdataResourceAdapter):
    name = SOURCE_121
    dataset_refs = DATASET_121
    resource_format = "xlsx"
    title_prefix = "121-etat-travaux"

    def parse(self, content: bytes, state: ResourceState) -> list[BillRecord]:
        return parse_121_xlsx(content, state.last_modified, self.stats)
