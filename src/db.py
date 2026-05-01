"""Database layer for the EDGAR pipeline.

Covers DDL creation and all upsert helpers for both pipelines. Functions are
grouped by pipeline so it is clear which tables each one touches.

Company fundamentals pipeline (etl_financial.py)
  create_schema()             create edgar schema + company / ticker / filing /
                              financial_fact tables
  get_loaded_ciks()           resumability check — which companies are done
  upsert_company()            edgar.company (one row per public company)
  upsert_tickers()            edgar.ticker (trading symbols → company)
  upsert_filings()            edgar.filing (10-K / 10-Q accessions)
  upsert_facts()              edgar.financial_fact (XBRL us-gaap metrics)
  mark_loaded()               stamp company.loaded_at after full ingest

Asset manager pipeline (etl_13f.py)
  create_13f_schema()         create investment_manager / manager_filing /
                              holding / cusip_issuer_map tables
  get_loaded_manager_ciks()   resumability check — which managers are done
  upsert_investment_manager() edgar.investment_manager (one row per 13F filer)
  upsert_manager_filings()    edgar.manager_filing (13F-HR / 13F-NT accessions)
  supersede_manager_filing()  mark prior quarter filing superseded + delete its
                              holdings when an amendment arrives
  upsert_holdings()           edgar.holding (one row per security per filing)
  upsert_cusip_mappings()     edgar.cusip_issuer_map (CUSIP → company CIK)
  mark_manager_loaded()       stamp investment_manager.loaded_at after full ingest

Shared infrastructure
  get_connection()            psycopg2 connection from .env
"""
from __future__ import annotations

import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()


# ── Company fundamentals schema (10-K / 10-Q pipeline) ───────────────────────

_DDL = """
CREATE SCHEMA IF NOT EXISTS edgar;

-- One row per SEC-registered public company seeded from the ticker universe.
-- loaded_at is NULL until the full company + facts load completes.
CREATE TABLE IF NOT EXISTS edgar.company (
    cik              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    sic_code         TEXT,
    sic_description  TEXT,
    entity_type      TEXT,
    fiscal_year_end  TEXT,           -- MMDD format, e.g. "0930"
    state_of_incorp  TEXT,
    category         TEXT,
    loaded_at        TIMESTAMPTZ     -- NULL = not yet fully ingested
);

-- Trading symbols for public companies. A single company can have multiple
-- rows (e.g. GOOG and GOOGL both reference the same CIK).
CREATE TABLE IF NOT EXISTS edgar.ticker (
    ticker    TEXT PRIMARY KEY,
    cik       TEXT NOT NULL REFERENCES edgar.company(cik),
    exchange  TEXT                   -- Nasdaq | NYSE | OTC | ... (nullable)
);
CREATE INDEX IF NOT EXISTS ticker_cik_idx ON edgar.ticker(cik);

-- 10-K and 10-Q filings since 2020. One row per accession number.
-- Scoped to FINANCIAL_FORMS only; all other form types are filtered in etl_financial.py.
CREATE TABLE IF NOT EXISTS edgar.filing (
    accession_number  TEXT PRIMARY KEY,  -- dashed format: 0000320193-24-000123
    cik               TEXT NOT NULL REFERENCES edgar.company(cik),
    form_type         TEXT NOT NULL,     -- "10-K" | "10-Q"
    period_end        DATE,              -- fiscal period end date
    filing_date       DATE               -- date accepted by EDGAR
);
CREATE INDEX IF NOT EXISTS filing_cik_period_idx ON edgar.filing(cik, period_end DESC);

-- ~45 standardized financial metrics per company per reporting period.
-- Restatements are resolved at ingest time: ON CONFLICT keeps the most
-- recently filed value for each (cik, metric, period_end, fiscal_period).
-- accession_number has no FK because facts may reference non-10-K/Q filings.
CREATE TABLE IF NOT EXISTS edgar.financial_fact (
    id                BIGSERIAL PRIMARY KEY,
    cik               TEXT NOT NULL REFERENCES edgar.company(cik),
    accession_number  TEXT,              -- source filing (no FK constraint — see above)
    metric            TEXT NOT NULL,     -- standardized name from tag_map.TAG_MAP
    xbrl_tag          TEXT,              -- raw us-gaap XBRL tag used by this filer
    value             NUMERIC,
    unit              TEXT,              -- "USD" | "USD/shares" | "shares"
    period_start      DATE,              -- NULL for instant (balance-sheet) facts
    period_end        DATE NOT NULL,
    period_type       TEXT,              -- "annual" | "quarterly"
    fiscal_year       INT,
    fiscal_period     TEXT,              -- "FY" | "Q1" | "Q2" | "Q3"
    filed_date        DATE,              -- used DESC to resolve restatements
    UNIQUE (cik, metric, period_end, fiscal_period)
);
CREATE INDEX IF NOT EXISTS fact_cik_metric_idx ON edgar.financial_fact(cik, metric, period_end DESC);
CREATE INDEX IF NOT EXISTS fact_cik_period_idx ON edgar.financial_fact(cik, period_end DESC);
"""


# ── Asset manager schema (13F pipeline) ──────────────────────────────────────

_DDL_13F = """
-- One row per institutional investment manager that has filed a 13F since 2020.
-- Parallel to edgar.company but keyed on manager CIKs discovered via the
-- quarterly full-index rather than the ticker exchange list.
-- Intentionally lean: only identity fields are stored — the pipeline's purpose
-- is position data (who held what), not entity classification metadata.
-- loaded_at is NULL until all qualifying 13F filings are ingested.
CREATE TABLE IF NOT EXISTS edgar.investment_manager (
    cik              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    loaded_at        TIMESTAMPTZ     -- NULL = not yet fully ingested
);

-- One row per 13F accession (original and amendments).
-- Parallel to edgar.filing but FK'd to investment_manager, not company.
-- is_superseded = TRUE when a later filing covers the same (cik, period_of_report).
CREATE TABLE IF NOT EXISTS edgar.manager_filing (
    accession_number  TEXT PRIMARY KEY,
    cik               TEXT NOT NULL REFERENCES edgar.investment_manager(cik),
    form_type         TEXT NOT NULL,     -- "13F-HR" | "13F-HR/A" | "13F-NT" | "13F-NT/A"
    period_of_report  DATE,              -- calendar quarter-end covered (nullable for NT)
    filing_date       DATE,
    is_amendment      BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE for /A variants
    amends_accession  TEXT REFERENCES edgar.manager_filing(accession_number),  -- NULL for originals
    is_superseded     BOOLEAN NOT NULL DEFAULT FALSE   -- TRUE once a later filing takes over
);
CREATE INDEX IF NOT EXISTS mgr_filing_cik_period_idx
    ON edgar.manager_filing(cik, period_of_report DESC);

-- One row per security per 13F-HR / 13F-HR/A filing.
-- 13F-NT filings have no holding rows (they carry no holdings schedule).
-- value_usd is stored in thousands of USD as reported per SEC 13F instructions.
-- Scoped to position data only: investment_discretion and voting_auth_* columns
-- are intentionally omitted — they are compliance fields, not position data.
-- No UNIQUE constraint: delete-before-insert in upsert_holdings() handles
-- re-ingest idempotency (a nullable composite key is unreliable in Postgres).
CREATE TABLE IF NOT EXISTS edgar.holding (
    id                          BIGSERIAL PRIMARY KEY,
    accession_number            TEXT NOT NULL REFERENCES edgar.manager_filing(accession_number),
    cusip                       TEXT NOT NULL,           -- 9-character CUSIP
    ticker                      TEXT,                    -- resolved at ingest via cusip_issuer_map; NULL if unresolved
    name_of_issuer              TEXT,                    -- as reported in the XML
    title_of_class              TEXT,                    -- e.g. "COM" for common stock
    value_usd                   BIGINT,                  -- fair market value in THOUSANDS of USD
    shares_or_principal_amount  NUMERIC,                 -- NUMERIC for large bond principals
    shares_or_principal_type    TEXT                     -- "SH" (shares) | "PRN" (principal)
);
CREATE INDEX IF NOT EXISTS holding_accession_idx ON edgar.holding(accession_number);
CREATE INDEX IF NOT EXISTS holding_cusip_idx     ON edgar.holding(cusip);

-- Maps each CUSIP seen in a holding to the matching company CIK.
-- cik is NULL for CUSIPs that could not be resolved via name matching.
-- source tracks how the mapping was obtained so manual entries are never
-- overwritten by automated re-runs.
CREATE TABLE IF NOT EXISTS edgar.cusip_issuer_map (
    cusip      TEXT PRIMARY KEY,
    cik        TEXT REFERENCES edgar.company(cik),  -- NULL if unresolved
    source     TEXT NOT NULL,    -- "name_match" | "manual" | "third_party"
    mapped_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


# ── Shared infrastructure ─────────────────────────────────────────────────────

def get_connection() -> psycopg2.extensions.connection:
    """Open a psycopg2 connection using DB_* env vars from .env."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )


# ── Company fundamentals pipeline (etl_financial.py) ───────────────────────────────────

def create_schema(conn) -> None:
    """Create the edgar schema and company fundamentals tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def get_loaded_ciks(conn) -> set[str]:
    """Return the set of company CIKs that have already been fully ingested.

    Used at startup to skip completed work and make the company pipeline
    resumable. A CIK is considered loaded only after both submissions and
    facts have been written (loaded_at is set at the very end of
    load_company_by_cik).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT cik FROM edgar.company WHERE loaded_at IS NOT NULL")
        return {row[0] for row in cur.fetchall()}


def upsert_company(conn, data: dict) -> None:
    """Insert or update one row in edgar.company.

    ON CONFLICT DO UPDATE overwrites all metadata columns except loaded_at —
    that column is only written by mark_loaded() after the full pipeline
    completes, so a partial run doesn't accidentally stamp a company as done.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO edgar.company
                (cik, name, sic_code, sic_description, entity_type,
                 fiscal_year_end, state_of_incorp, category)
            VALUES
                (%(cik)s, %(name)s, %(sic_code)s, %(sic_description)s, %(entity_type)s,
                 %(fiscal_year_end)s, %(state_of_incorp)s, %(category)s)
            ON CONFLICT (cik) DO UPDATE SET
                name            = EXCLUDED.name,
                sic_code        = EXCLUDED.sic_code,
                sic_description = EXCLUDED.sic_description,
                entity_type     = EXCLUDED.entity_type,
                fiscal_year_end = EXCLUDED.fiscal_year_end,
                state_of_incorp = EXCLUDED.state_of_incorp,
                category        = EXCLUDED.category
            """,
            data,
        )
    conn.commit()


def upsert_tickers(conn, rows: list[dict]) -> None:
    """Insert or update ticker symbols for a company.

    ON CONFLICT DO UPDATE handles ticker reassignments (a symbol moving to a
    different issuer). A company with no tickers passes an empty list and
    returns immediately.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO edgar.ticker (ticker, cik, exchange)
            VALUES %s
            ON CONFLICT (ticker) DO UPDATE SET
                cik      = EXCLUDED.cik,
                exchange = EXCLUDED.exchange
            """,
            [(r["ticker"], r["cik"], r["exchange"]) for r in rows],
        )
    conn.commit()


def upsert_filings(conn, rows: list[dict]) -> None:
    """Insert 10-K / 10-Q filing records. Scoped to FINANCIAL_FORMS in etl_financial.py.

    ON CONFLICT DO NOTHING — filings are immutable once accepted by EDGAR, so
    there is nothing to update if the accession already exists.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO edgar.filing (accession_number, cik, form_type, period_end, filing_date)
            VALUES %s
            ON CONFLICT (accession_number) DO NOTHING
            """,
            [
                (r["accession_number"], r["cik"], r["form_type"], r["period_end"], r["filing_date"])
                for r in rows
            ],
        )
    conn.commit()


def upsert_facts(conn, rows: list[dict]) -> None:
    """Insert or update XBRL financial fact rows for a company.

    ON CONFLICT on the natural key (cik, metric, period_end, fiscal_period)
    overwrites all value columns — this is how restatements are resolved:
    the most recently filed value for a given period wins on each run.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO edgar.financial_fact
                (cik, accession_number, metric, xbrl_tag, value, unit,
                 period_start, period_end, period_type, fiscal_year, fiscal_period, filed_date)
            VALUES %s
            ON CONFLICT (cik, metric, period_end, fiscal_period) DO UPDATE SET
                accession_number = EXCLUDED.accession_number,
                xbrl_tag         = EXCLUDED.xbrl_tag,
                value            = EXCLUDED.value,
                unit             = EXCLUDED.unit,
                period_start     = EXCLUDED.period_start,
                period_type      = EXCLUDED.period_type,
                fiscal_year      = EXCLUDED.fiscal_year,
                filed_date       = EXCLUDED.filed_date
            """,
            [
                (
                    r["cik"], r["accession_number"], r["metric"], r["xbrl_tag"],
                    r["value"], r["unit"], r["period_start"], r["period_end"],
                    r["period_type"], r["fiscal_year"], r["fiscal_period"], r["filed_date"],
                )
                for r in rows
            ],
        )
    conn.commit()


def mark_loaded(conn, cik: str) -> None:
    """Stamp company.loaded_at = NOW() to mark this CIK as fully ingested.

    Called at the very end of load_company_by_cik() — after both submissions
    and facts have been written without error. This is the checkpoint that
    prevents the company from being reprocessed on a subsequent run.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE edgar.company SET loaded_at = NOW() WHERE cik = %s",
            (cik,),
        )
    conn.commit()


# ── Asset manager pipeline (etl_13f.py) ──────────────────────────────────────

def create_13f_schema(conn) -> None:
    """Create the 13F tables if they don't exist.

    Must be called after create_schema() because cusip_issuer_map has a FK
    to edgar.company which is created by that function.
    """
    with conn.cursor() as cur:
        cur.execute(_DDL_13F)
    conn.commit()


def get_loaded_manager_ciks(conn) -> set[str]:
    """Return the set of manager CIKs that have already been fully ingested.

    Mirrors get_loaded_ciks() for the asset manager pipeline. A manager is
    considered loaded only after all qualifying 13F filings have been
    processed and holdings written.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cik FROM edgar.investment_manager WHERE loaded_at IS NOT NULL"
        )
        return {row[0] for row in cur.fetchall()}


def upsert_investment_manager(conn, data: dict) -> None:
    """Insert or update one row in edgar.investment_manager.

    Only stores cik and name — entity classification metadata (SIC, state of
    incorporation, category) is intentionally omitted. loaded_at is excluded
    from the update set; it is only written by mark_manager_loaded().
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO edgar.investment_manager (cik, name)
            VALUES (%(cik)s, %(name)s)
            ON CONFLICT (cik) DO UPDATE SET
                name = EXCLUDED.name
            """,
            data,
        )
    conn.commit()


def upsert_manager_filings(conn, rows: list[dict]) -> None:
    """Insert 13F filing records (all form types including NT).

    ON CONFLICT DO NOTHING — accessions are immutable, same as upsert_filings().
    is_superseded and amends_accession are managed separately by
    supersede_manager_filing() and are not set here.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO edgar.manager_filing
                (accession_number, cik, form_type, period_of_report,
                 filing_date, is_amendment)
            VALUES %s
            ON CONFLICT (accession_number) DO NOTHING
            """,
            [
                (
                    r["accession_number"], r["cik"], r["form_type"],
                    r.get("period_of_report"), r.get("filing_date"),
                    r.get("is_amendment", False),
                )
                for r in rows
            ],
        )
    conn.commit()


def supersede_manager_filing(
    conn, cik: str, period_of_report, current_accn: str
) -> None:
    """Mark prior filings for (cik, period_of_report) as superseded and
    delete their holdings — called when a 13F-HR/A amendment is ingested.

    The two steps run in one transaction:
    1. UPDATE manager_filing SET is_superseded = TRUE for all prior filings
       covering the same quarter (excluding current_accn itself).
    2. DELETE holding rows belonging to those superseded filings.

    After this call, upsert_holdings() inserts the current amendment's
    holdings, leaving the table with only one active set per quarter.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE edgar.manager_filing
               SET is_superseded = TRUE
             WHERE cik              = %s
               AND period_of_report = %s
               AND accession_number != %s
               AND is_superseded    = FALSE
            """,
            (cik, period_of_report, current_accn),
        )
        cur.execute(
            """
            DELETE FROM edgar.holding h
            USING edgar.manager_filing mf
            WHERE h.accession_number  = mf.accession_number
              AND mf.cik              = %s
              AND mf.period_of_report = %s
              AND mf.is_superseded    = TRUE
              AND mf.accession_number != %s
            """,
            (cik, period_of_report, current_accn),
        )
    conn.commit()


def upsert_holdings(conn, rows: list[dict]) -> None:
    """Replace all holdings for one accession number.

    Uses delete-before-insert rather than ON CONFLICT because title_of_class
    is nullable and a UNIQUE constraint on nullable columns is unreliable in
    PostgreSQL — NULLs are always considered distinct.

    All rows must share the same accession_number (called once per filing).
    """
    if not rows:
        return
    accession = rows[0]["accession_number"]
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM edgar.holding WHERE accession_number = %s", (accession,)
        )
        execute_values(
            cur,
            """
            INSERT INTO edgar.holding
                (accession_number, cusip, ticker, name_of_issuer, title_of_class,
                 value_usd, shares_or_principal_amount, shares_or_principal_type)
            VALUES %s
            """,
            [
                (
                    r["accession_number"],
                    r.get("cusip"),
                    r.get("ticker"),
                    r.get("name_of_issuer"),
                    r.get("title_of_class"),
                    r.get("value_usd"),
                    r.get("shares_or_principal_amount"),
                    r.get("shares_or_principal_type"),
                )
                for r in rows
            ],
        )
    conn.commit()


def upsert_cusip_mappings(conn, rows: list[dict]) -> None:
    """Insert CUSIP → company CIK mappings resolved during 13F ingest.

    ON CONFLICT upgrades a prior NULL cik to a resolved value if a later run
    finds a name match, but never overwrites a 'manual' entry — those are
    curated overrides that automated matching should not touch.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO edgar.cusip_issuer_map (cusip, cik, source, mapped_at)
            VALUES %s
            ON CONFLICT (cusip) DO UPDATE SET
                cik       = COALESCE(EXCLUDED.cik, edgar.cusip_issuer_map.cik),
                source    = EXCLUDED.source,
                mapped_at = NOW()
            WHERE edgar.cusip_issuer_map.source != 'manual'
            """,
            [(r["cusip"], r.get("cik"), r["source"]) for r in rows],
        )
    conn.commit()


def mark_manager_loaded(conn, cik: str) -> None:
    """Stamp investment_manager.loaded_at = NOW() to mark this manager as done.

    Mirrors mark_loaded() for the 13F pipeline. Called at the very end of
    load_manager_by_cik() after all holdings and CUSIP mappings are written.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE edgar.investment_manager SET loaded_at = NOW() WHERE cik = %s",
            (cik,),
        )
    conn.commit()
