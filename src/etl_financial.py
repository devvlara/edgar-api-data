"""Company fundamentals pipeline (10-K / 10-Q) — NOT for asset manager / 13F data.

Discovers every EDGAR ticker-bearing company, fetches entity metadata and XBRL
financial facts, and writes to edgar.company / ticker / filing / financial_fact.

For the institutional holdings pipeline (investment managers, 13F filings,
edgar.investment_manager / manager_filing / holding), see src/etl_13f.py.

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
from src.tag_map import TAG_MAP, UNIT_OVERRIDE

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Company pipeline — constants ──────────────────────────────────────────────

CUTOFF = date(2020, 1, 1)
# 10-K and 10-Q only — 13F forms are handled exclusively in etl_13f.py
FINANCIAL_FORMS = {"10-K", "10-Q"}


# ── Company fundamentals — parsers ────────────────────────────────────────────

def _parse_submissions(cik: str, subs: dict) -> tuple[dict, list[dict], list[dict]]:
    """Return (company_row, ticker_rows, filing_rows) from a submissions response.

    company_row  → edgar.company  (entity metadata: name, SIC, fiscal year end, etc.)
    ticker_rows  → edgar.ticker   (one row per ticker symbol; zipped from parallel arrays)
    filing_rows  → edgar.filing   (10-K and 10-Q only, period_end >= CUTOFF)

    This function is company-pipeline-specific. For the 13F equivalent that writes
    to edgar.investment_manager / manager_filing, see etl_13f._parse_manager_submissions().
    """
    company = {
        "cik":             cik,
        "name":            subs.get("name", ""),
        "sic_code":        subs.get("sic"),
        "sic_description": subs.get("sicDescription"),
        "entity_type":     subs.get("entityType"),
        "fiscal_year_end": subs.get("fiscalYearEnd"),
        "state_of_incorp": subs.get("stateOfIncorporation"),
        "category":        subs.get("category"),
    }

    tickers = [
        {"ticker": t.upper(), "cik": cik, "exchange": e or None}
        for t, e in zip(
            subs.get("tickers", []),
            subs.get("exchanges", []),
        )
    ]

    recent = subs.get("filings", {}).get("recent", {})
    accessions  = recent.get("accessionNumber", [])
    forms       = recent.get("form", [])
    periods     = recent.get("reportDate", [])
    filed_dates = recent.get("filingDate", [])

    filings = []
    for accn, form, period, filed in zip(accessions, forms, periods, filed_dates):
        if form not in FINANCIAL_FORMS:
            continue
        period_end = date.fromisoformat(period) if period else None
        if period_end is None or period_end < CUTOFF:
            continue
        filings.append({
            "accession_number": accn,
            "cik":              cik,
            "form_type":        form,
            "period_end":       period_end,
            "filing_date":      date.fromisoformat(filed) if filed else None,
        })

    return company, tickers, filings


def _parse_facts(cik: str, facts_json: dict) -> list[dict]:
    """Extract financial_fact rows from a company_facts (XBRL) response.

    Walks TAG_MAP (src/tag_map.py) for ~45 standardized metrics. For each metric
    the tag fallback list is tried in order — the first tag with qualifying data
    wins and remaining fallbacks are skipped. Observations are filtered to
    FINANCIAL_FORMS and period_end >= CUTOFF, then deduplicated by
    (period_end, fiscal_period) keeping the most recently filed value (restatement
    resolution). Investment managers do not file XBRL data — this function is only
    called from the company fundamentals pipeline.
    """
    gaap = facts_json.get("facts", {}).get("us-gaap", {})
    rows: list[dict] = []

    for metric, tags in TAG_MAP.items():
        target_unit = UNIT_OVERRIDE.get(metric, "USD")

        for tag in tags:
            tag_entry = gaap.get(tag)
            if not tag_entry:
                continue

            observations = tag_entry.get("units", {}).get(target_unit)
            if not observations:
                continue

            filtered = [
                o for o in observations
                if o.get("form") in FINANCIAL_FORMS
                and date.fromisoformat(o["end"]) >= CUTOFF
            ]
            if not filtered:
                continue

            # Per (period_end, fiscal_period) keep the most recently filed value.
            # This resolves restatements automatically.
            best: dict[tuple, dict] = {}
            for o in filtered:
                key = (o["end"], o.get("fp", ""))
                if key not in best or o["filed"] > best[key]["filed"]:
                    best[key] = o

            for o in best.values():
                fp = o.get("fp", "")
                rows.append({
                    "cik":              cik,
                    "accession_number": o.get("accn"),
                    "metric":           metric,
                    "xbrl_tag":         tag,
                    "value":            o["val"],
                    "unit":             target_unit,
                    "period_start":     date.fromisoformat(o["start"]) if o.get("start") else None,
                    "period_end":       date.fromisoformat(o["end"]),
                    "period_type":      "annual" if fp == "FY" else "quarterly",
                    "fiscal_year":      o.get("fy"),
                    "fiscal_period":    fp,
                    "filed_date":       date.fromisoformat(o["filed"]) if o.get("filed") else None,
                })

            break  # tag had data — skip remaining fallbacks for this metric

    return rows


# ── Company fundamentals — per-company pipeline ───────────────────────────────

def load_company_by_cik(client: EdgarClient, conn, cik: str) -> None:
    """Fetch and persist all data for one company CIK.

    Writes edgar.company, edgar.ticker, edgar.filing, and edgar.financial_fact
    in sequence. Marks the company loaded_at only after all four steps succeed —
    a partial failure leaves loaded_at NULL so the CIK is retried on the next run.

    A 404 on company_facts is silently swallowed: some companies (e.g. non-XBRL
    small filers) have no XBRL data. Any other HTTP error propagates so the CIK
    lands in the failed list.
    """
    subs = client.submissions(cik)
    company, tickers, filings = _parse_submissions(cik, subs)

    db.upsert_company(conn, company)
    db.upsert_tickers(conn, tickers)
    db.upsert_filings(conn, filings)

    try:
        facts_json = client.company_facts(cik)
        facts = _parse_facts(cik, facts_json)
        db.upsert_facts(conn, facts)
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
