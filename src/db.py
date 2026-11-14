"""Database layer for the EDGAR company fundamentals pipeline (etl_financial.py).

Raw dump, no metric mapping, split across three schemas in the "edgar" database:
  entity schema   entity / entity_address / entity_former_name / ticker —
                  submissions metadata, not specific to 10-K vs 10-Q.
  "10k" schema    filing / xbrl_tag_meta / financial_fact_raw for 10-K only.
  "10q" schema    filing / xbrl_tag_meta / financial_fact_raw for 10-Q only.
  Quoting note: "10k"/"10q" start with a digit, so Postgres requires them to
  be double-quoted identifiers everywhere — including in manual queries, e.g.
  SELECT * FROM "10k".filing;  (unquoted 10k.filing is a syntax error).

  create_schema()              create entity schema + "10k"/"10q" schemas
  get_loaded_ciks()            resumability check — which entities are done
  upsert_entity()              entity.entity (one row per SEC-registered entity)
  upsert_entity_addresses()    entity.entity_address (mailing + business)
  upsert_entity_former_names() entity.entity_former_name (prior legal names)
  upsert_tickers()              entity.ticker (trading symbols → entity)
  upsert_filings()              "10k"/"10q".filing (full accession metadata)
  upsert_tag_meta()             "10k"/"10q".xbrl_tag_meta (per-tag label/description)
  upsert_facts_raw()            "10k"/"10q".financial_fact_raw (every XBRL
                               observation, every taxonomy — no restatement
                               collapsing)
  mark_loaded()                stamp entity.loaded_at (and last_checked_at) after full ingest

  Refresh support (etl_refresh.py)
  get_all_entity_ciks()        every CIK in entity.entity, loaded or not
  get_known_accessions()       cik -> set of accession numbers already stored
                               in "10k".filing / "10q".filing
  clear_loaded()               set loaded_at NULL before a re-ingest, so a crash
                               mid-refresh leaves the CIK marked incomplete
  mark_checked()               stamp last_checked_at (nothing new found)

Shared infrastructure
  get_connection()            psycopg2 connection from .env
"""
from __future__ import annotations

import os

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql as pg_sql
from psycopg2.extras import execute_values

load_dotenv()


def _qualify(cur, schema: str, table: str) -> str:
    """Return a safely double-quoted "schema"."table" identifier string.

    Needed because "10k"/"10q" start with a digit and are not valid bare SQL
    identifiers — psycopg2.sql.Identifier() quotes them correctly regardless
    of what the schema name looks like.
    """
    return pg_sql.SQL("{}.{}").format(pg_sql.Identifier(schema), pg_sql.Identifier(table)).as_string(cur)


# ── Company fundamentals schema (10-K / 10-Q pipeline, raw dump) ─────────────
# Entity-level data lives once in the "entity" schema; 10-K and 10-Q filings
# and facts are physically split into "10k" and "10q" schemas so each is a
# self-contained mart (its own filing/tag_meta/fact tables) that only ever
# holds rows for that form type.

_DDL_ENTITY = """
CREATE SCHEMA IF NOT EXISTS entity;

-- One row per SEC-registered entity. Direct mapping of the submissions
-- top-level fields (excluding addresses/formerNames/filings — own tables).
-- loaded_at is NULL until the full entity + facts load completes.
CREATE TABLE IF NOT EXISTS entity.entity (
    cik                                   TEXT PRIMARY KEY,
    name                                  TEXT NOT NULL,
    entity_type                           TEXT,     -- "operating" | "investment_company" | ...
    sic_code                              TEXT,
    sic_description                       TEXT,
    owner_org                             TEXT,
    insider_transaction_for_owner_exists  BOOLEAN,
    insider_transaction_for_issuer_exists BOOLEAN,
    ein                                   TEXT,
    lei                                   TEXT,
    description                           TEXT,
    website                               TEXT,
    investor_website                      TEXT,
    category                              TEXT,     -- "Large accelerated filer" | ...
    fiscal_year_end                       TEXT,     -- MMDD
    state_of_incorporation                TEXT,
    state_of_incorporation_description    TEXT,
    phone                                 TEXT,
    flags                                 TEXT,
    loaded_at                             TIMESTAMPTZ,  -- NULL = not yet fully ingested; else last time facts were (re)loaded
    last_checked_at                       TIMESTAMPTZ   -- last time etl_refresh compared this entity to its submissions (changed or not)
);
-- Added after the first release; CREATE TABLE IF NOT EXISTS won't add it to an
-- existing table, so apply it explicitly (no-op once present).
ALTER TABLE entity.entity ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;

-- Mailing + business address, one row per address type per entity.
CREATE TABLE IF NOT EXISTS entity.entity_address (
    id                            BIGSERIAL PRIMARY KEY,
    cik                           TEXT NOT NULL REFERENCES entity.entity(cik),
    address_type                  TEXT NOT NULL,   -- "mailing" | "business"
    street1                       TEXT,
    street2                       TEXT,
    city                          TEXT,
    state_or_country              TEXT,
    state_or_country_description  TEXT,
    zip_code                      TEXT,
    is_foreign_location           BOOLEAN,
    foreign_state_territory       TEXT,
    country                       TEXT,
    country_code                  TEXT,
    UNIQUE (cik, address_type)
);

-- Prior legal names. No UNIQUE constraint — date_from/date_to can be NULL
-- and NULLs are never equal in Postgres, so a natural-key UNIQUE constraint
-- would not reliably prevent duplicates; re-ingest safety is handled instead
-- by delete-before-insert in upsert_entity_former_names().
CREATE TABLE IF NOT EXISTS entity.entity_former_name (
    id         BIGSERIAL PRIMARY KEY,
    cik        TEXT NOT NULL REFERENCES entity.entity(cik),
    name       TEXT NOT NULL,
    date_from  TIMESTAMPTZ,
    date_to    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS former_name_cik_idx ON entity.entity_former_name(cik);

-- Trading symbols. A single entity can have multiple rows (e.g. GOOG/GOOGL).
CREATE TABLE IF NOT EXISTS entity.ticker (
    ticker    TEXT PRIMARY KEY,
    cik       TEXT NOT NULL REFERENCES entity.entity(cik),
    exchange  TEXT                   -- Nasdaq | NYSE | OTC | ... (nullable)
);
CREATE INDEX IF NOT EXISTS ticker_cik_idx ON entity.ticker(cik);
"""

# Applied once per form-type schema ("10k", "10q"). Identical table shapes;
# each schema only ever receives rows for its own form type — the ETL splits
# filings and facts by form_type/form before calling the upsert functions.
_DDL_FORM_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS {schema};

-- One row per accession number, carrying the full filings.recent field set
-- (not just accession/form/period/date). Scoped by form_type in etl_financial.py.
CREATE TABLE IF NOT EXISTS {schema}.filing (
    accession_number         TEXT PRIMARY KEY,  -- dashed format: 0000320193-24-000123
    cik                      TEXT NOT NULL REFERENCES entity.entity(cik),
    form_type                TEXT NOT NULL,     -- "10-K" | "10-Q"
    filing_date              DATE,
    report_date              DATE,              -- fiscal period end covered
    acceptance_date_time     TIMESTAMPTZ,
    act                      TEXT,
    file_number              TEXT,
    film_number              TEXT,
    items                    TEXT,
    core_type                TEXT,
    size_bytes               BIGINT,
    is_xbrl                  BOOLEAN,
    is_inline_xbrl           BOOLEAN,
    is_xbrl_numeric          BOOLEAN,
    primary_document         TEXT,
    primary_doc_description  TEXT
);
CREATE INDEX IF NOT EXISTS filing_cik_period_idx ON {schema}.filing(cik, report_date DESC);

-- Per-(taxonomy, tag) metadata, scoped to tags actually seen in this form
-- type. Duplicated across "10k"/"10q" (rather than shared) so each schema is
-- fully self-contained for querying facts + tag metadata with no cross-schema join.
CREATE TABLE IF NOT EXISTS {schema}.xbrl_tag_meta (
    taxonomy     TEXT NOT NULL,   -- "us-gaap" | "dei" | "srt" | ...
    tag          TEXT NOT NULL,
    label        TEXT,
    description  TEXT,
    PRIMARY KEY (taxonomy, tag)
);

-- Every XBRL observation reported under a filing of this form type, across
-- every taxonomy and tag the filer used — no metric mapping, no restatement
-- collapsing. No UNIQUE constraint: period_start is nullable for instant
-- facts, and Postgres treats NULLs as always distinct, so a natural-key
-- UNIQUE constraint would not reliably prevent duplicates; re-ingest safety
-- is handled instead by delete-before-insert in upsert_facts_raw().
CREATE TABLE IF NOT EXISTS {schema}.financial_fact_raw (
    id                BIGSERIAL PRIMARY KEY,
    cik               TEXT NOT NULL REFERENCES entity.entity(cik),
    taxonomy          TEXT NOT NULL,
    tag               TEXT NOT NULL,
    unit              TEXT NOT NULL,     -- "USD" | "USD/shares" | "shares" | ...
    value             NUMERIC,
    period_start      DATE,              -- NULL for instant (balance-sheet) facts
    period_end        DATE NOT NULL,
    accession_number  TEXT,              -- source filing; no FK — facts can cite non-10-K/Q accns
    fiscal_year       INT,
    fiscal_period     TEXT,              -- "FY" | "Q1" | "Q2" | "Q3"
    form              TEXT,              -- as reported on the observation itself
    filed_date        DATE,
    frame             TEXT               -- SEC's standardized period label, e.g. "CY2023Q4I"
);
CREATE INDEX IF NOT EXISTS fact_raw_cik_tag_idx   ON {schema}.financial_fact_raw(cik, taxonomy, tag, period_end DESC);
CREATE INDEX IF NOT EXISTS fact_raw_accession_idx ON {schema}.financial_fact_raw(accession_number);
"""

# schema name → the Python-side identifier passed to upsert_filings() /
# upsert_tag_meta() / upsert_facts_raw(). "10k"/"10q" are quoted internally
# by _qualify(); index names only need to be unique within their own schema,
# so both form schemas can reuse the same index names without colliding.
FORM_SCHEMAS = {"10-K": "10k", "10-Q": "10q"}


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
    """Create the entity schema plus the "10k" and "10q" form-type schemas.

    Order matters: the entity schema must exist first because both form
    schemas' filing/financial_fact_raw tables carry a FK to entity.entity.
    """
    with conn.cursor() as cur:
        cur.execute(_DDL_ENTITY)
        cur.execute(_DDL_FORM_SCHEMA.format(schema='"10k"'))
        cur.execute(_DDL_FORM_SCHEMA.format(schema='"10q"'))
    conn.commit()


def get_loaded_ciks(conn) -> set[str]:
    """Return the set of entity CIKs that have already been fully ingested.

    Used at startup to skip completed work and make the company pipeline
    resumable. A CIK is considered loaded only after both submissions and
    facts have been written (loaded_at is set at the very end of
    load_company_by_cik).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT cik FROM entity.entity WHERE loaded_at IS NOT NULL")
        return {row[0] for row in cur.fetchall()}


def upsert_entity(conn, data: dict) -> None:
    """Insert or update one row in entity.entity.

    ON CONFLICT DO UPDATE overwrites all metadata columns except loaded_at —
    that column is only written by mark_loaded() after the full pipeline
    completes, so a partial run doesn't accidentally stamp an entity as done.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO entity.entity
                (cik, name, entity_type, sic_code, sic_description, owner_org,
                 insider_transaction_for_owner_exists, insider_transaction_for_issuer_exists,
                 ein, lei, description, website, investor_website, category,
                 fiscal_year_end, state_of_incorporation, state_of_incorporation_description,
                 phone, flags)
            VALUES
                (%(cik)s, %(name)s, %(entity_type)s, %(sic_code)s, %(sic_description)s, %(owner_org)s,
                 %(insider_transaction_for_owner_exists)s, %(insider_transaction_for_issuer_exists)s,
                 %(ein)s, %(lei)s, %(description)s, %(website)s, %(investor_website)s, %(category)s,
                 %(fiscal_year_end)s, %(state_of_incorporation)s, %(state_of_incorporation_description)s,
                 %(phone)s, %(flags)s)
            ON CONFLICT (cik) DO UPDATE SET
                name                                  = EXCLUDED.name,
                entity_type                           = EXCLUDED.entity_type,
                sic_code                               = EXCLUDED.sic_code,
                sic_description                        = EXCLUDED.sic_description,
                owner_org                              = EXCLUDED.owner_org,
                insider_transaction_for_owner_exists   = EXCLUDED.insider_transaction_for_owner_exists,
                insider_transaction_for_issuer_exists  = EXCLUDED.insider_transaction_for_issuer_exists,
                ein                                     = EXCLUDED.ein,
                lei                                     = EXCLUDED.lei,
                description                             = EXCLUDED.description,
                website                                 = EXCLUDED.website,
                investor_website                        = EXCLUDED.investor_website,
                category                                = EXCLUDED.category,
                fiscal_year_end                         = EXCLUDED.fiscal_year_end,
                state_of_incorporation                  = EXCLUDED.state_of_incorporation,
                state_of_incorporation_description      = EXCLUDED.state_of_incorporation_description,
                phone                                   = EXCLUDED.phone,
                flags                                   = EXCLUDED.flags
            """,
            data,
        )
    conn.commit()


def upsert_entity_addresses(conn, rows: list[dict]) -> None:
    """Insert or update mailing/business addresses for an entity.

    ON CONFLICT (cik, address_type) DO UPDATE — address_type is always one of
    a controlled, non-null set ("mailing" | "business"), so the composite key
    is reliable for upsert (unlike entity_former_name, see that function).
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO entity.entity_address
                (cik, address_type, street1, street2, city, state_or_country,
                 state_or_country_description, zip_code, is_foreign_location,
                 foreign_state_territory, country, country_code)
            VALUES %s
            ON CONFLICT (cik, address_type) DO UPDATE SET
                street1                       = EXCLUDED.street1,
                street2                       = EXCLUDED.street2,
                city                          = EXCLUDED.city,
                state_or_country              = EXCLUDED.state_or_country,
                state_or_country_description  = EXCLUDED.state_or_country_description,
                zip_code                      = EXCLUDED.zip_code,
                is_foreign_location           = EXCLUDED.is_foreign_location,
                foreign_state_territory       = EXCLUDED.foreign_state_territory,
                country                       = EXCLUDED.country,
                country_code                  = EXCLUDED.country_code
            """,
            [
                (
                    r["cik"], r["address_type"], r.get("street1"), r.get("street2"),
                    r.get("city"), r.get("state_or_country"), r.get("state_or_country_description"),
                    r.get("zip_code"), r.get("is_foreign_location"), r.get("foreign_state_territory"),
                    r.get("country"), r.get("country_code"),
                )
                for r in rows
            ],
        )
    conn.commit()


def upsert_entity_former_names(conn, cik: str, rows: list[dict]) -> None:
    """Replace all former-name rows for one entity.

    Uses delete-before-insert rather than ON CONFLICT because date_from can be
    NULL and PostgreSQL treats NULLs as always distinct, making a natural-key
    UNIQUE constraint unreliable here.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM entity.entity_former_name WHERE cik = %s", (cik,))
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO entity.entity_former_name (cik, name, date_from, date_to)
                VALUES %s
                """,
                [(r["cik"], r["name"], r.get("date_from"), r.get("date_to")) for r in rows],
            )
    conn.commit()


def upsert_tickers(conn, rows: list[dict]) -> None:
    """Insert or update ticker symbols for an entity.

    ON CONFLICT DO UPDATE handles ticker reassignments (a symbol moving to a
    different issuer). An entity with no tickers passes an empty list and
    returns immediately.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO entity.ticker (ticker, cik, exchange)
            VALUES %s
            ON CONFLICT (ticker) DO UPDATE SET
                cik      = EXCLUDED.cik,
                exchange = EXCLUDED.exchange
            """,
            [(r["ticker"], r["cik"], r["exchange"]) for r in rows],
        )
    conn.commit()


def upsert_filings(conn, schema: str, rows: list[dict]) -> None:
    """Insert filing records into the "10k" or "10q" schema's filing table.

    schema is "10k" or "10q" (see FORM_SCHEMAS) — the caller has already
    split rows by form_type before calling this. ON CONFLICT DO NOTHING —
    filings are immutable once accepted by EDGAR, so there is nothing to
    update if the accession already exists.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""
            INSERT INTO {_qualify(cur, schema, 'filing')}
                (accession_number, cik, form_type, filing_date, report_date,
                 acceptance_date_time, act, file_number, film_number, items,
                 core_type, size_bytes, is_xbrl, is_inline_xbrl, is_xbrl_numeric,
                 primary_document, primary_doc_description)
            VALUES %s
            ON CONFLICT (accession_number) DO NOTHING
            """,
            [
                (
                    r["accession_number"], r["cik"], r["form_type"], r["filing_date"], r["report_date"],
                    r.get("acceptance_date_time"), r.get("act"), r.get("file_number"), r.get("film_number"),
                    r.get("items"), r.get("core_type"), r.get("size_bytes"), r.get("is_xbrl"),
                    r.get("is_inline_xbrl"), r.get("is_xbrl_numeric"), r.get("primary_document"),
                    r.get("primary_doc_description"),
                )
                for r in rows
            ],
        )
    conn.commit()


def upsert_tag_meta(conn, schema: str, rows: list[dict]) -> None:
    """Insert or update per-(taxonomy, tag) label/description metadata.

    schema is "10k" or "10q" — each form schema keeps its own tag_meta table,
    scoped to only the tags actually seen in that form type. ON CONFLICT DO
    UPDATE keeps labels fresh if the SEC revises one; the table is small (one
    row per unique tag ever seen, not per observation) so refreshing on every
    run is negligible cost.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""
            INSERT INTO {_qualify(cur, schema, 'xbrl_tag_meta')} (taxonomy, tag, label, description)
            VALUES %s
            ON CONFLICT (taxonomy, tag) DO UPDATE SET
                label       = EXCLUDED.label,
                description = EXCLUDED.description
            """,
            [(r["taxonomy"], r["tag"], r.get("label"), r.get("description")) for r in rows],
        )
    conn.commit()


def upsert_facts_raw(conn, schema: str, cik: str, rows: list[dict]) -> None:
    """Replace one entity's raw XBRL fact rows in the "10k" or "10q" schema.

    schema is "10k" or "10q" — the caller has already split fact rows by the
    observation's reported form before calling this, once per schema.

    Uses delete-before-insert rather than ON CONFLICT: period_start is NULL
    for instant facts, and PostgreSQL treats NULLs as always distinct, so a
    natural-key UNIQUE constraint would not reliably prevent duplicates on a
    retried run. Facts are only reprocessed when a prior run left loaded_at
    NULL, so deleting the entity's existing rows before writing is safe.
    """
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_qualify(cur, schema, 'financial_fact_raw')} WHERE cik = %s", (cik,))
        if rows:
            execute_values(
                cur,
                f"""
                INSERT INTO {_qualify(cur, schema, 'financial_fact_raw')}
                    (cik, taxonomy, tag, unit, value, period_start, period_end,
                     accession_number, fiscal_year, fiscal_period, form, filed_date, frame)
                VALUES %s
                """,
                [
                    (
                        r["cik"], r["taxonomy"], r["tag"], r["unit"], r.get("value"),
                        r.get("period_start"), r["period_end"], r.get("accession_number"),
                        r.get("fiscal_year"), r.get("fiscal_period"), r.get("form"),
                        r.get("filed_date"), r.get("frame"),
                    )
                    for r in rows
                ],
            )
    conn.commit()


def get_all_entity_ciks(conn) -> list[str]:
    """Return every CIK in entity.entity (loaded or not), sorted."""
    with conn.cursor() as cur:
        cur.execute("SELECT cik FROM entity.entity ORDER BY cik")
        return [row[0] for row in cur.fetchall()]


def get_known_accessions(conn) -> dict[str, set[str]]:
    """Return {cik: {accession_number, ...}} for every stored 10-K and 10-Q filing.

    Lets the refresh job tell whether a company's current submissions list
    contains a filing the database has not seen, using one query per schema
    instead of one per company.
    """
    known: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        for schema in FORM_SCHEMAS.values():
            cur.execute(f"SELECT cik, accession_number FROM {_qualify(cur, schema, 'filing')}")
            for cik, accession in cur.fetchall():
                known.setdefault(cik, set()).add(accession)
    return known


def clear_loaded(conn, cik: str) -> None:
    """Set loaded_at NULL so a re-ingest that fails part-way is retried.

    load_company_by_cik() writes filings before facts, so a failure between the
    two would leave new filings recorded but their facts missing. Clearing
    loaded_at first marks the CIK incomplete until mark_loaded() runs again.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE entity.entity SET loaded_at = NULL WHERE cik = %s", (cik,))
    conn.commit()


def mark_checked(conn, cik: str) -> None:
    """Stamp last_checked_at = NOW() without touching loaded_at."""
    with conn.cursor() as cur:
        cur.execute("UPDATE entity.entity SET last_checked_at = NOW() WHERE cik = %s", (cik,))
    conn.commit()


def mark_loaded(conn, cik: str) -> None:
    """Stamp entity.loaded_at = NOW() to mark this CIK as fully ingested.

    Called at the very end of load_company_by_cik() — after both submissions
    and facts have been written without error. This is the checkpoint that
    prevents the entity from being reprocessed on a subsequent run.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE entity.entity SET loaded_at = NOW(), last_checked_at = NOW() WHERE cik = %s",
            (cik,),
        )
    conn.commit()