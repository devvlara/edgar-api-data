from __future__ import annotations

import pandas as pd


def get_all_tickers(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.ticker, c.name, t.exchange
            FROM edgar.ticker t
            JOIN edgar.company c ON c.cik = t.cik
            WHERE c.loaded_at IS NOT NULL
            ORDER BY t.ticker
        """)
        return [{"ticker": r[0], "name": r[1], "exchange": r[2] or ""} for r in cur.fetchall()]


def get_company_info(conn, ticker: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.cik, c.name, c.sic_description, c.fiscal_year_end, t.exchange
            FROM edgar.ticker t
            JOIN edgar.company c ON c.cik = t.cik
            WHERE t.ticker = %s
            LIMIT 1
        """, (ticker,))
        row = cur.fetchone()
    if not row:
        return None
    return {
        "cik":            row[0],
        "name":           row[1],
        "sic_description": row[2] or "",
        "fiscal_year_end": row[3] or "",
        "exchange":       row[4] or "",
    }


def get_facts(conn, cik: str, period_type: str, start_year: int, end_year: int) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT metric, value, unit, period_end, fiscal_year, fiscal_period
            FROM edgar.financial_fact
            WHERE cik = %s
              AND period_type = %s
              AND EXTRACT(YEAR FROM period_end) BETWEEN %s AND %s
            ORDER BY period_end
        """, (cik, period_type, start_year, end_year))
        rows = cur.fetchall()

    cols = ["metric", "value", "unit", "period_end", "fiscal_year", "fiscal_period"]
    if not rows:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(rows, columns=cols)
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["fiscal_year"] = df["fiscal_year"].fillna(df["period_end"].dt.year).astype(int)
    df["fiscal_period"] = df["fiscal_period"].fillna("").astype(str)
    return df
