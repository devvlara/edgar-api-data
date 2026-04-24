from __future__ import annotations

import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

_DDL = """
CREATE SCHEMA IF NOT EXISTS edgar;

CREATE TABLE IF NOT EXISTS edgar.company (
    cik              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    sic_code         TEXT,
    sic_description  TEXT,
    entity_type      TEXT,
    fiscal_year_end  TEXT,
    state_of_incorp  TEXT,
    category         TEXT,
    loaded_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS edgar.ticker (
    ticker    TEXT PRIMARY KEY,
    cik       TEXT NOT NULL REFERENCES edgar.company(cik),
    exchange  TEXT
);
CREATE INDEX IF NOT EXISTS ticker_cik_idx ON edgar.ticker(cik);

CREATE TABLE IF NOT EXISTS edgar.filing (
    accession_number  TEXT PRIMARY KEY,
    cik               TEXT NOT NULL REFERENCES edgar.company(cik),
    form_type         TEXT NOT NULL,
    period_end        DATE,
    filing_date       DATE
);
CREATE INDEX IF NOT EXISTS filing_cik_period_idx ON edgar.filing(cik, period_end DESC);

CREATE TABLE IF NOT EXISTS edgar.financial_fact (
    id                BIGSERIAL PRIMARY KEY,
    cik               TEXT NOT NULL REFERENCES edgar.company(cik),
    accession_number  TEXT,
    metric            TEXT NOT NULL,
    xbrl_tag          TEXT,
    value             NUMERIC,
    unit              TEXT,
    period_start      DATE,
    period_end        DATE NOT NULL,
    period_type       TEXT,
    fiscal_year       INT,
    fiscal_period     TEXT,
    filed_date        DATE,
    UNIQUE (cik, metric, period_end, fiscal_period)
);
CREATE INDEX IF NOT EXISTS fact_cik_metric_idx ON edgar.financial_fact(cik, metric, period_end DESC);
CREATE INDEX IF NOT EXISTS fact_cik_period_idx ON edgar.financial_fact(cik, period_end DESC);
"""


def get_connection() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )


def create_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def get_loaded_ciks(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT cik FROM edgar.company WHERE loaded_at IS NOT NULL")
        return {row[0] for row in cur.fetchall()}


def upsert_company(conn, data: dict) -> None:
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
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE edgar.company SET loaded_at = NOW() WHERE cik = %s",
            (cik,),
        )
    conn.commit()
