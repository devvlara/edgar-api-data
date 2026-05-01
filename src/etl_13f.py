"""13F holdings ETL: fetch every institutional manager's quarterly holdings
and load them into the edgar.investment_manager / manager_filing / holding tables.

Usage
-----
    python -m src.etl_13f

The script is fully resumable. Any manager already marked loaded_at IS NOT NULL
is skipped. Kill and restart freely.

Pipeline overview
-----------------
1. Scan EDGAR quarterly full-index files (2020 Q1 → most recent completed quarter)
   to build the universe of 13F filer CIKs.
2. Fetch submissions for each new manager → populate investment_manager +
   manager_filing rows.
3. For each 13F-HR / 13F-HR/A filing (chronological order):
   a. Fetch index.json from the archive to locate the information table XML.
   b. Parse holdings from the XML.
   c. Supersede prior filings for the same (cik, period_of_report).
   d. Write holdings rows.
   e. Attempt CUSIP → CIK resolution via issuer name matching.
4. Mark manager loaded_at.
"""
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation

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

CUTOFF           = date(2020, 1, 1)
_13F_HR_FORMS    = frozenset({"13F-HR", "13F-HR/A"})
_13F_ALL_FORMS   = frozenset({"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"})


# ── Asset manager — quarter helpers ──────────────────────────────────────────

def _completed_quarters(start_year: int = 2020, start_quarter: int = 1) -> list[tuple[int, int]]:
    """Return all completed calendar quarters from start through yesterday's quarter."""
    today = date.today()
    current_q = (today.month - 1) // 3 + 1
    end_year, end_quarter = today.year, current_q - 1
    if end_quarter == 0:
        end_year  -= 1
        end_quarter = 4

    quarters: list[tuple[int, int]] = []
    y, q = start_year, start_quarter
    while (y, q) <= (end_year, end_quarter):
        quarters.append((y, q))
        q += 1
        if q > 4:
            q, y = 1, y + 1
    return quarters


# ── Asset manager — submissions parser ───────────────────────────────────────

def _parse_manager_submissions(
    cik: str, subs: dict
) -> tuple[dict, list[dict]]:
    """Return (manager_row, filing_rows) from a submissions response."""
    manager = {
        "cik":  cik,
        "name": subs.get("name", ""),
    }

    recent      = subs.get("filings", {}).get("recent", {})
    accessions  = recent.get("accessionNumber", [])
    forms       = recent.get("form", [])
    periods     = recent.get("reportDate", [])
    filed_dates = recent.get("filingDate", [])

    filings: list[dict] = []
    for accn, form, period, filed in zip(accessions, forms, periods, filed_dates):
        if form not in _13F_ALL_FORMS:
            continue
        period_date = None
        if period:
            try:
                period_date = date.fromisoformat(period)
            except ValueError:
                pass
        if period_date and period_date < CUTOFF:
            continue
        filing_date = None
        if filed:
            try:
                filing_date = date.fromisoformat(filed)
            except ValueError:
                pass
        filings.append({
            "accession_number": accn,
            "cik":              cik,
            "form_type":        form,
            "period_of_report": period_date,
            "filing_date":      filing_date,
            "is_amendment":     form.endswith("/A"),
        })

    return manager, filings


# ── Asset manager — information table XML parser ──────────────────────────────

def _strip_ns(tag: str) -> str:
    """Strip the XML namespace prefix from a tag name (e.g. '{http://...}cusip' → 'cusip').

    The 13F information table XML namespace varies by filer and filing year,
    so namespace-aware lookups are unreliable. Stripping the prefix and matching
    on the local name alone is the only portable approach.
    """
    return tag.split("}")[-1] if "}" in tag else tag


def _to_int(text: str | None) -> int | None:
    """Parse an integer from a string, returning None if blank or non-numeric.

    Strips commas to handle values like "82,700,000" as reported in 13F XML.
    """
    if not text:
        return None
    try:
        return int(text.strip().replace(",", ""))
    except ValueError:
        return None


def _to_decimal(text: str | None) -> Decimal | None:
    """Parse a Decimal from a string, returning None if blank or non-numeric.

    Used for shares_or_principal_amount, which can be a large bond principal
    that exceeds int range and requires exact decimal representation.
    """
    if not text:
        return None
    try:
        return Decimal(text.strip().replace(",", ""))
    except InvalidOperation:
        return None


def _parse_information_table(xml_bytes: bytes, accession_number: str) -> list[dict]:
    """Parse a 13F information table XML into holding dicts."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.warning("XML parse error for %s: %s", accession_number, exc)
        return []

    holdings: list[dict] = []
    for elem in root.iter():
        if _strip_ns(elem.tag) != "infoTable":
            continue

        row: dict = {"accession_number": accession_number}
        for child in elem:
            tag  = _strip_ns(child.tag)
            text = (child.text or "").strip() or None

            if tag == "nameOfIssuer":
                row["name_of_issuer"] = text
            elif tag == "titleOfClass":
                row["title_of_class"] = text
            elif tag == "cusip":
                row["cusip"] = text
            elif tag == "value":
                row["value_usd"] = _to_int(text)
            elif tag == "shrsOrPrnAmt":
                for sub in child:
                    st = _strip_ns(sub.tag)
                    sv = (sub.text or "").strip() or None
                    if st == "sshPrnamt":
                        row["shares_or_principal_amount"] = _to_decimal(sv)
                    elif st == "sshPrnamtType":
                        row["shares_or_principal_type"] = sv

        if row.get("cusip"):
            holdings.append(row)

    return holdings


def _fetch_information_table(
    client: EdgarClient, cik: str, accession: str
) -> bytes | None:
    """Locate and fetch the information table XML from the filing archive.

    Returns the raw bytes, or None if the document cannot be found.
    """
    try:
        index = client.filing_index(cik, accession)
    except requests.exceptions.HTTPError as exc:
        log.warning("index.json fetch failed %s: %s", accession, exc)
        return None

    for item in index.get("directory", {}).get("item", []):
        if item.get("type", "").upper() == "INFORMATION TABLE":
            filename = item.get("name")
            if not filename:
                continue
            try:
                return client.filing_document(cik, accession, filename)
            except requests.exceptions.HTTPError as exc:
                log.warning("document fetch failed %s/%s: %s", accession, filename, exc)
                return None

    return None


# ── Asset manager — CUSIP resolution ─────────────────────────────────────────

def _build_company_name_index(conn) -> dict[str, str]:
    """Return {normalized_name: cik} for all companies in edgar.company."""
    with conn.cursor() as cur:
        cur.execute("SELECT cik, name FROM edgar.company WHERE name IS NOT NULL")
        return {
            re.sub(r"[^A-Z0-9]", "", row[1].upper()): row[0]
            for row in cur.fetchall()
        }


def _build_cik_ticker_index(conn) -> dict[str, str]:
    """Return {cik: ticker} using the first ticker alphabetically per CIK.

    Used to annotate holding rows with a resolved ticker at ingest time so
    downstream queries don't need to join through cusip_issuer_map → ticker.
    When a CIK has multiple tickers (e.g. GOOG / GOOGL), the alphabetically
    first symbol is used.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (cik) cik, ticker FROM edgar.ticker ORDER BY cik, ticker"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _resolve_cusips(
    holdings: list[dict], name_index: dict[str, str]
) -> list[dict]:
    """Return cusip_issuer_map rows for every unique CUSIP in the holdings list."""
    seen: dict[str, dict] = {}
    for h in holdings:
        cusip = h.get("cusip")
        if not cusip or cusip in seen:
            continue
        normalized = re.sub(r"[^A-Z0-9]", "", (h.get("name_of_issuer") or "").upper())
        cik = name_index.get(normalized)
        seen[cusip] = {
            "cusip":  cusip,
            "cik":    cik,
            "source": "name_match",
        }
    return list(seen.values())


# ── Asset manager — per-manager pipeline ─────────────────────────────────────

def load_manager_by_cik(
    client: EdgarClient,
    conn,
    cik: str,
    name_index: dict[str, str],
    cik_ticker_index: dict[str, str],
) -> None:
    """Fetch and persist all data for one investment manager CIK.

    Writes edgar.investment_manager, edgar.manager_filing, edgar.holding, and
    edgar.cusip_issuer_map. Holdings-bearing filings (13F-HR / 13F-HR/A) are
    processed chronologically so each amendment arrives after the original it
    supersedes — supersede_manager_filing() marks the prior filing and deletes
    its holdings before the new batch is written.

    Marks the manager loaded_at only after all filings are processed. A partial
    failure leaves loaded_at NULL so the CIK is retried on the next run.

    Both name_index and cik_ticker_index must be built once per run and passed in
    — building them inside this function would query the DB once per manager.
    """
    subs = client.submissions(cik)
    manager, filings = _parse_manager_submissions(cik, subs)

    db.upsert_investment_manager(conn, manager)
    db.upsert_manager_filings(conn, filings)

    # Process holdings-bearing filings in chronological order so amendments
    # always arrive after the originals they supersede.
    qualifying = sorted(
        [f for f in filings if f["form_type"] in _13F_HR_FORMS],
        key=lambda f: f["filing_date"] or date.min,
    )

    for filing in qualifying:
        accn = filing["accession_number"]
        xml_bytes = _fetch_information_table(client, cik, accn)
        if xml_bytes is None:
            log.warning("No information table found for %s — skipping holdings", accn)
            continue

        holdings = _parse_information_table(xml_bytes, accn)
        if not holdings:
            log.warning("Zero holdings parsed from %s", accn)
            continue

        period = filing.get("period_of_report")
        if period:
            db.supersede_manager_filing(conn, cik, period, accn)

        # Resolve CUSIPs first so we can annotate holdings with ticker before writing.
        cusip_rows = _resolve_cusips(holdings, name_index)
        db.upsert_cusip_mappings(conn, cusip_rows)

        cusip_to_ticker = {
            r["cusip"]: cik_ticker_index.get(r["cik"])
            for r in cusip_rows if r.get("cik")
        }
        for h in holdings:
            h["ticker"] = cusip_to_ticker.get(h.get("cusip", ""))

        db.upsert_holdings(conn, holdings)

    db.mark_manager_loaded(conn, cik)


# ── Asset manager — bulk loader entry point ───────────────────────────────────

def run_13f_load() -> None:
    """Seed the manager universe from quarterly full-index files and load all managers.

    Step 1: Scans EDGAR quarterly full-index company.gz files for every quarter
    from 2020 Q1 through the most recent completed quarter, collecting every
    unique CIK that has filed a 13F-HR, 13F-HR/A, 13F-NT, or 13F-NT/A.

    Step 2: Skips CIKs already marked loaded_at IS NOT NULL.

    Step 3: Builds a normalized company-name → CIK index once for CUSIP resolution.

    Step 4: Calls load_manager_by_cik() for each remaining CIK. Failed CIKs are
    logged and will be retried on the next run.
    """
    user_agent = os.getenv("SEC_USER_AGENT")
    if not user_agent:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. Add it to your .env:\n"
            "  SEC_USER_AGENT=Your Name your@email.com"
        )

    client = EdgarClient(user_agent=user_agent)
    conn   = db.get_connection()
    db.create_schema(conn)
    db.create_13f_schema(conn)

    # ── Step 1: collect manager CIK universe from quarterly full-index files ──
    log.info("Scanning quarterly full-index files for 13F filer CIKs...")
    quarters = _completed_quarters()
    all_ciks: dict[str, None] = {}
    for year, quarter in tqdm(quarters, desc="Quarters", unit="qtr"):
        try:
            for row in client.quarterly_index_13f(year, quarter):
                all_ciks[row["cik"]] = None
        except Exception as exc:
            log.warning("Failed to fetch index for %d Q%d: %s", year, quarter, exc)

    log.info("Found %d unique 13F filer CIKs across %d quarters", len(all_ciks), len(quarters))

    # ── Step 2: skip already-loaded managers ─────────────────────────────────
    loaded    = db.get_loaded_manager_ciks(conn)
    remaining = [c for c in all_ciks if c not in loaded]

    log.info(
        "%d managers total | %d already loaded | %d to process",
        len(all_ciks), len(loaded), len(remaining),
    )

    # ── Step 3: build indexes once for CUSIP and ticker resolution ───────────
    log.info("Building company name index for CUSIP resolution...")
    name_index = _build_company_name_index(conn)
    log.info("Name index: %d entries", len(name_index))
    cik_ticker_index = _build_cik_ticker_index(conn)
    log.info("Ticker index: %d entries", len(cik_ticker_index))

    # ── Step 4: load each manager ─────────────────────────────────────────────
    failed: list[str] = []
    for cik in tqdm(remaining, desc="Managers", unit="mgr"):
        try:
            load_manager_by_cik(client, conn, cik, name_index, cik_ticker_index)
        except Exception as exc:
            log.warning("FAILED %s: %s", cik, exc)
            failed.append(cik)

    log.info(
        "Done. %d loaded, %d failed.",
        len(remaining) - len(failed), len(failed),
    )
    if failed:
        log.info("Failed CIKs: %s", failed)

    conn.close()


if __name__ == "__main__":
    run_13f_load()
