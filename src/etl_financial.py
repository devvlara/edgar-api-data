"""Company fundamentals pipeline (10-K / 10-Q).

Raw dump: discovers every EDGAR ticker-bearing company, fetches entity
metadata and every XBRL fact it has ever reported (all taxonomies, all tags),
and writes it verbatim across three Postgres schemas in the "edgar" database:
    entity schema   entity / entity_address / entity_former_name / ticker
                    (submissions metadata — not specific to 10-K vs 10-Q)
    "10k" schema    filing / xbrl_tag_meta / financial_fact_raw for 10-K only
    "10q" schema    filing / xbrl_tag_meta / financial_fact_raw for 10-Q only
There is no metric mapping and no restatement collapsing — every observation
the SEC has published for a qualifying 10-K/10-Q filing is kept as its own
row, routed to the schema matching its form type.

Usage
-----
    python -m src.etl_financial

The script is fully resumable. Any company already marked loaded_at IS NOT NULL
is skipped, so you can kill and restart freely without re-processing done work.

Requirements
------------
Set these in your .env before running:
    SEC_USER_AGENT=Your Name your@email.com   (required by the SEC)
    DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD
"""
from __future__ import annotations

import logging
import os
from datetime import date

import requests
from dotenv import load_dotenv
from tqdm import tqdm

from src.edgar_client import EdgarClient
from src import db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Company pipeline — constants ──────────────────────────────────────────────

CUTOFF = date(2020, 1, 1)
# Single source of truth is db.FORM_SCHEMAS ({"10-K": "10k", "10-Q": "10q"}),
# so the set of qualifying forms and their schema routing can't drift apart.
FINANCIAL_FORMS = frozenset(db.FORM_SCHEMAS)


# ── Company fundamentals — small parsing helpers ──────────────────────────────

def _to_bool(value) -> bool | None:
    """Convert a submissions API 0/1/None flag to a real boolean, preserving NULL."""
    return None if value is None else bool(value)


def _to_date(value: str | None) -> date | None:
    """Parse a "YYYY-MM-DD" string, returning None if blank."""
    return date.fromisoformat(value) if value else None


# ── Company fundamentals — submissions parser ─────────────────────────────────

def _parse_entity(cik: str, subs: dict) -> dict:
    """Return an entity.entity row from a submissions response.

    Direct field-for-field mapping of the submissions top-level metadata.
    Addresses, former names, and filings are parsed separately below.
    """
    return {
        "cik":                                   cik,
        "name":                                  subs.get("name", ""),
        "entity_type":                           subs.get("entityType"),
        "sic_code":                              subs.get("sic"),
        "sic_description":                       subs.get("sicDescription"),
        "owner_org":                             subs.get("ownerOrg"),
        "insider_transaction_for_owner_exists":  _to_bool(subs.get("insiderTransactionForOwnerExists")),
        "insider_transaction_for_issuer_exists": _to_bool(subs.get("insiderTransactionForIssuerExists")),
        "ein":                                   subs.get("ein"),
        "lei":                                   subs.get("lei"),
        "description":                           subs.get("description") or None,
        "website":                               subs.get("website") or None,
        "investor_website":                      subs.get("investorWebsite") or None,
        "category":                              subs.get("category"),
        "fiscal_year_end":                       subs.get("fiscalYearEnd"),
        "state_of_incorporation":                subs.get("stateOfIncorporation"),
        "state_of_incorporation_description":    subs.get("stateOfIncorporationDescription"),
        "phone":                                 subs.get("phone") or None,
        "flags":                                 subs.get("flags") or None,
    }


def _parse_addresses(cik: str, subs: dict) -> list[dict]:
    """Return entity.entity_address rows (mailing + business) from a submissions response."""
    rows = []
    for addr_type, addr in (subs.get("addresses") or {}).items():
        if not addr:
            continue
        rows.append({
            "cik":                          cik,
            "address_type":                 addr_type,
            "street1":                      addr.get("street1"),
            "street2":                      addr.get("street2"),
            "city":                         addr.get("city"),
            "state_or_country":             addr.get("stateOrCountry"),
            "state_or_country_description": addr.get("stateOrCountryDescription"),
            "zip_code":                     addr.get("zipCode"),
            "is_foreign_location":          _to_bool(addr.get("isForeignLocation")),
            "foreign_state_territory":      addr.get("foreignStateTerritory"),
            "country":                      addr.get("country"),
            "country_code":                 addr.get("countryCode"),
        })
    return rows


def _parse_former_names(cik: str, subs: dict) -> list[dict]:
    """Return entity.entity_former_name rows from a submissions response."""
    return [
        {
            "cik":       cik,
            "name":      fn.get("name"),
            "date_from": fn.get("from"),
            "date_to":   fn.get("to"),
        }
        for fn in subs.get("formerNames") or []
    ]


def _parse_tickers(cik: str, subs: dict) -> list[dict]:
    """Return entity.ticker rows zipped from the parallel tickers/exchanges arrays."""
    return [
        {"ticker": t.upper(), "cik": cik, "exchange": e or None}
        for t, e in zip(subs.get("tickers", []), subs.get("exchanges", []))
    ]


def _parse_filings(cik: str, subs: dict) -> list[dict]:
    """Return "10k"/"10q".filing rows (10-K / 10-Q only, report_date >= CUTOFF).

    Carries the full filings.recent field set. Missing optional arrays are
    defaulted to a same-length list of None rather than [], because zipping
    against a genuinely empty list (a key the filer's history never uses)
    would otherwise silently drop every filing via short-circuiting.
    """
    recent = subs.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])
    n = len(accessions)

    def field(name: str) -> list:
        values = recent.get(name)
        return values if values is not None else [None] * n

    forms                 = field("form")
    filing_dates          = field("filingDate")
    report_dates          = field("reportDate")
    acceptance_times      = field("acceptanceDateTime")
    acts                  = field("act")
    file_numbers          = field("fileNumber")
    film_numbers          = field("filmNumber")
    items_list            = field("items")
    core_types            = field("core_type")
    sizes                 = field("size")
    is_xbrl_list          = field("isXBRL")
    is_inline_xbrl_list   = field("isInlineXBRL")
    is_xbrl_numeric_list  = field("isXBRLNumeric")
    primary_documents      = field("primaryDocument")
    primary_doc_descs      = field("primaryDocDescription")

    filings = []
    for i in range(n):
        form = forms[i]
        if form not in FINANCIAL_FORMS:
            continue
        report_date = _to_date(report_dates[i])
        if report_date is None or report_date < CUTOFF:
            continue
        filings.append({
            "accession_number":        accessions[i],
            "cik":                     cik,
            "form_type":               form,
            "filing_date":             _to_date(filing_dates[i]),
            "report_date":             report_date,
            "acceptance_date_time":    acceptance_times[i],
            "act":                     acts[i],
            "file_number":             file_numbers[i],
            "film_number":             film_numbers[i],
            "items":                   items_list[i],
            "core_type":               core_types[i],
            "size_bytes":              sizes[i],
            "is_xbrl":                 _to_bool(is_xbrl_list[i]),
            "is_inline_xbrl":          _to_bool(is_inline_xbrl_list[i]),
            "is_xbrl_numeric":         _to_bool(is_xbrl_numeric_list[i]),
            "primary_document":        primary_documents[i],
            "primary_doc_description": primary_doc_descs[i],
        })

    return filings


# ── Company fundamentals — XBRL facts parser (raw, all taxonomies) ──────────

def _parse_facts(cik: str, facts_json: dict) -> tuple[dict[str, list[dict]], dict[tuple[str, str], dict]]:
    """Return (fact_rows_by_form, tag_lookup) from a company_facts (XBRL) response.

    fact_rows_by_form buckets every observation by its own reported form
    ("10-K" or "10-Q") so the caller can route each bucket to the matching
    "10k"/"10q" schema. tag_lookup maps (taxonomy, tag) -> {label,
    description} for every tag encountered, regardless of form — used by
    _tag_meta_for() to build each schema's own scoped tag_meta rows.

    Walks every taxonomy (us-gaap, dei, srt, ...) and every tag the filer has
    ever used — not just the curated TAG_MAP metrics. Observations are
    filtered to FINANCIAL_FORMS and period_end >= CUTOFF but are otherwise
    kept as-is: no per-(period, fiscal_period) restatement collapsing, so a
    tag with three restated values for the same quarter yields three rows.
    Investment managers do not file XBRL data — this function is only called
    from the company fundamentals pipeline.
    """
    facts = facts_json.get("facts", {})
    fact_rows_by_form: dict[str, list[dict]] = {form: [] for form in FINANCIAL_FORMS}
    tag_lookup: dict[tuple[str, str], dict] = {}

    for taxonomy, tags in facts.items():
        for tag, entry in tags.items():
            tag_lookup[(taxonomy, tag)] = {
                "label":       entry.get("label"),
                "description": entry.get("description"),
            }

            for unit, observations in entry.get("units", {}).items():
                for o in observations:
                    form = o.get("form")
                    if form not in FINANCIAL_FORMS:
                        continue
                    period_end = _to_date(o.get("end"))
                    if period_end is None or period_end < CUTOFF:
                        continue

                    fact_rows_by_form[form].append({
                        "cik":              cik,
                        "taxonomy":         taxonomy,
                        "tag":              tag,
                        "unit":             unit,
                        "value":            o.get("val"),
                        "period_start":     _to_date(o.get("start")),
                        "period_end":       period_end,
                        "accession_number": o.get("accn"),
                        "fiscal_year":      o.get("fy"),
                        "fiscal_period":    o.get("fp"),
                        "form":             form,
                        "filed_date":       _to_date(o.get("filed")),
                        "frame":            o.get("frame"),
                    })

    return fact_rows_by_form, tag_lookup


def _tag_meta_for(fact_rows: list[dict], tag_lookup: dict[tuple[str, str], dict]) -> list[dict]:
    """Return tag_meta rows for the distinct (taxonomy, tag) pairs in fact_rows.

    Used to scope each form schema's xbrl_tag_meta table to only the tags
    actually seen in that form type, per the self-contained-schema design.
    """
    seen: dict[tuple[str, str], dict] = {}
    for r in fact_rows:
        key = (r["taxonomy"], r["tag"])
        if key not in seen:
            meta = tag_lookup.get(key, {})
            seen[key] = {
                "taxonomy":    key[0],
                "tag":         key[1],
                "label":       meta.get("label"),
                "description": meta.get("description"),
            }
    return list(seen.values())


# ── Company fundamentals — per-company pipeline ───────────────────────────────

def load_company_by_cik(client: EdgarClient, conn, cik: str, subs: dict | None = None) -> None:
    """Fetch and persist all raw data for one company CIK.

    Writes entity.entity, entity_address, entity_former_name, ticker, then
    splits filings and facts by form type and writes each half into its
    matching "10k"/"10q" schema (db.FORM_SCHEMAS). Marks the entity
    loaded_at only after every step succeeds — a partial failure leaves
    loaded_at NULL so the CIK is retried on the next run.

    A 404 on company_facts is silently swallowed: some companies (e.g.
    non-XBRL small filers) have no XBRL data. Any other HTTP error propagates
    so the CIK lands in the failed list.

    ``subs`` is an already-fetched submissions response; the refresh job passes
    it to avoid fetching the same document twice. If omitted it is fetched here.
    """
    if subs is None:
        subs = client.submissions(cik)

    db.upsert_entity(conn, _parse_entity(cik, subs))
    db.upsert_entity_addresses(conn, _parse_addresses(cik, subs))
    db.upsert_entity_former_names(conn, cik, _parse_former_names(cik, subs))
    db.upsert_tickers(conn, _parse_tickers(cik, subs))

    filings = _parse_filings(cik, subs)
    filings_by_form: dict[str, list[dict]] = {form: [] for form in FINANCIAL_FORMS}
    for f in filings:
        filings_by_form[f["form_type"]].append(f)
    for form, schema in db.FORM_SCHEMAS.items():
        db.upsert_filings(conn, schema, filings_by_form[form])

    try:
        facts_json = client.company_facts(cik)
        fact_rows_by_form, tag_lookup = _parse_facts(cik, facts_json)
        for form, schema in db.FORM_SCHEMAS.items():
            fact_rows = fact_rows_by_form[form]
            db.upsert_tag_meta(conn, schema, _tag_meta_for(fact_rows, tag_lookup))
            db.upsert_facts_raw(conn, schema, cik, fact_rows)
    except requests.exceptions.HTTPError as exc:
        if exc.response.status_code != 404:
            raise

    db.mark_loaded(conn, cik)


# ── Company fundamentals — bulk loader entry point ────────────────────────────

def run_bulk_load() -> None:
    """Seed the company universe from EDGAR's ticker list and load all companies.

    Seeds from company_tickers_exchange.json (~10 k tickers), deduplicates to
    unique CIKs, skips any CIK already marked loaded_at IS NOT NULL, then calls
    load_company_by_cik() for each remaining CIK. Failed CIKs are logged and
    will be retried on the next run.
    """
    user_agent = os.getenv("SEC_USER_AGENT")
    if not user_agent:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. Add it to your .env:\n"
            "  SEC_USER_AGENT=Your Name your@email.com"
        )

    client = EdgarClient(user_agent=user_agent)
    conn = db.get_connection()
    db.create_schema(conn)

    log.info("Fetching full ticker universe from EDGAR...")
    raw = client.company_tickers_exchange()
    fields = raw["fields"]  # ["cik", "name", "ticker", "exchange"]
    cik_idx = fields.index("cik")

    # Deduplicate: same CIK appears once per ticker (e.g. GOOG + GOOGL)
    seen: dict[str, None] = {}
    for row in raw["data"]:
        seen[str(row[cik_idx]).zfill(10)] = None
    all_ciks = list(seen)

    loaded = db.get_loaded_ciks(conn)
    remaining = [c for c in all_ciks if c not in loaded]

    log.info(
        f"{len(all_ciks):,} companies total | "
        f"{len(loaded):,} already loaded | "
        f"{len(remaining):,} to process"
    )

    failed: list[str] = []

    for cik in tqdm(remaining, unit="co", desc="Loading"):
        try:
            load_company_by_cik(client, conn, cik)
        except Exception as exc:
            log.warning(f"FAILED {cik}: {exc}")
            failed.append(cik)

    log.info(
        f"Done. {len(remaining) - len(failed):,} loaded, {len(failed):,} failed."
    )
    if failed:
        log.info(f"Failed CIKs: {failed}")

    conn.close()


if __name__ == "__main__":
    run_bulk_load()
