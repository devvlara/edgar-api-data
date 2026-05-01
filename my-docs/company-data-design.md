# Company Data Pipeline — Design Reference

**Scope:** How the pipeline discovers companies, fetches entity metadata, and loads the `company`, `ticker`, and `filing` tables.
**Source files:** `src/etl_financial.py`, `src/db.py`

---

## Overview

The company data pipeline runs as the first phase of every `load_company_by_cik` call. It seeds the universe of companies from a single EDGAR index endpoint, then fetches per-company entity metadata and filing history from the submissions API. Three tables are written: `company`, `ticker`, and `filing`. Financial facts are a separate fetch that happens after this phase — see `financials-design.md`.

---

## 1. Seed: Building the CIK Universe

The pipeline starts by fetching the full EDGAR ticker universe:

```
https://www.sec.gov/files/company_tickers_exchange.json
```

This returns a flat `{fields, data}` structure with ~10,000 rows, one per ticker. Because a single company can have multiple share classes (e.g., Google has both `GOOG` and `GOOGL`), the same CIK appears more than once. The pipeline deduplicates by CIK before doing any per-company work:

```python
seen: dict[str, None] = {}
for row in raw["data"]:
    seen[str(row[cik_idx]).zfill(10)] = None
all_ciks = list(seen)
```

CIKs are zero-padded to 10 digits at this step — that is the format used in all subsequent API URLs.

**Resumability check.** Before the main loop starts, the pipeline queries `edgar.company WHERE loaded_at IS NOT NULL` and removes those CIKs from the work queue. Any CIK that completed a prior run is skipped entirely.

**Universe boundary.** This seed only covers companies with at least one trading ticker. Private companies, foreign private issuers that haven't listed on a US exchange, and investment managers (13F filers) are not in this universe.

---

## 2. Fetch Path: Submissions API

For each remaining CIK, the pipeline fetches:

```
https://data.sec.gov/submissions/CIK{10-digit}.json
```

This single endpoint returns three categories of data used by this pipeline:

| Data category | JSON path | Written to |
|---|---|---|
| Entity metadata | Top-level fields (`name`, `sic`, `entityType`, etc.) | `company` |
| Ticker symbols + exchanges | `tickers[]` and `exchanges[]` parallel arrays | `ticker` |
| Filing history | `filings.recent.*` columnar arrays | `filing` |

**Overflow files.** The `filings.recent` array holds the most recent ~1,000 filings. Older filings for large companies spill into separate JSON files listed under `filings.files[]`. The current pipeline does not fetch those overflow files — it only reads `filings.recent`. For the 2020 cutoff, most companies' qualifying filings fall within the recent window, but prolific filers with decades of history could theoretically have 2020-era filings pushed into an overflow file.

---

## 3. Parsing: `_parse_submissions`

`etl._parse_submissions(cik, subs)` returns a tuple of `(company_row, ticker_rows, filing_rows)`.

**Company row.** Eight fields pulled from top-level keys of the submissions response:

```python
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
```

All fields except `cik` and `name` use `.get()` with no default, so they land as `None` when absent. `name` defaults to `""` rather than `None` to satisfy the `NOT NULL` column constraint.

**Ticker rows.** Built by zipping the parallel `tickers` and `exchanges` arrays. Ticker symbols are forced to uppercase. Exchange is coerced to `None` (rather than empty string) when falsy:

```python
tickers = [
    {"ticker": t.upper(), "cik": cik, "exchange": e or None}
    for t, e in zip(subs.get("tickers", []), subs.get("exchanges", []))
]
```

A company with no tickers produces an empty list — `upsert_tickers` returns early in that case.

**Filing rows.** Iterated from the four parallel columnar arrays in `filings.recent`:

- `accessionNumber` — dashed format, used as primary key
- `form` — kept only if in `FINANCIAL_FORMS = {"10-K", "10-Q"}`
- `reportDate` — parsed to `date`; row is dropped if empty or before `CUTOFF = date(2020, 1, 1)`
- `filingDate` — parsed to `date`, nullable

Two filters are applied in sequence: form type must be 10-K or 10-Q, and `period_end` must be on or after 2020-01-01. Any row that fails either filter is silently skipped.

---

## 4. Table Designs

### `company`

```sql
CREATE TABLE IF NOT EXISTS edgar.company (
    cik              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    sic_code         TEXT,
    sic_description  TEXT,
    entity_type      TEXT,
    fiscal_year_end  TEXT,         -- MMDD format, e.g. "0930" for September 30
    state_of_incorp  TEXT,
    category         TEXT,
    loaded_at        TIMESTAMPTZ   -- NULL until full load (company + facts) completes
);
```

**Upsert behavior.** `ON CONFLICT (cik) DO UPDATE` overwrites all seven metadata columns. `loaded_at` is intentionally excluded from the update — it is only written by `db.mark_loaded()` at the very end of `load_company_by_cik`, after facts are also done. This means a partially completed company (metadata written, facts failed) stays with `loaded_at IS NULL` and will be retried on the next run.

### `ticker`

```sql
CREATE TABLE IF NOT EXISTS edgar.ticker (
    ticker    TEXT PRIMARY KEY,
    cik       TEXT NOT NULL REFERENCES edgar.company(cik),
    exchange  TEXT
);
CREATE INDEX IF NOT EXISTS ticker_cik_idx ON edgar.ticker(cik);
```

**Upsert behavior.** `ON CONFLICT (ticker) DO UPDATE` overwrites `cik` and `exchange`. This handles ticker reassignments (e.g., a symbol transferred to a different issuer).

### `filing`

```sql
CREATE TABLE IF NOT EXISTS edgar.filing (
    accession_number  TEXT PRIMARY KEY,
    cik               TEXT NOT NULL REFERENCES edgar.company(cik),
    form_type         TEXT NOT NULL,
    period_end        DATE,
    filing_date       DATE
);
CREATE INDEX IF NOT EXISTS filing_cik_period_idx ON edgar.filing(cik, period_end DESC);
```

**Upsert behavior.** `ON CONFLICT (accession_number) DO NOTHING`. Unlike `company` and `ticker`, filings are immutable once accepted by EDGAR — the accession number is the SEC's permanent record key. There is nothing to update, so conflicts are silently ignored rather than overwriting.

**Scope.** Only 10-K and 10-Q filings since 2020-01-01 are written here. All other form types in the submissions response are filtered out in `_parse_submissions`.

---

## 5. Resumability

`loaded_at` is the single checkpoint for the entire per-company pipeline. It is set to `NOW()` only after `upsert_company`, `upsert_tickers`, `upsert_filings`, and `upsert_facts` have all completed without error. If any step raises an exception, the exception propagates to `run_bulk_load`, is caught and logged as a warning, and the CIK is added to a `failed` list. On the next run, that CIK is in the `remaining` set again and retried from the beginning.

This means company metadata and filing rows may be written to the database even for CIKs that ultimately appear in the failed list — those rows are upserted again on retry, which is harmless.

---

## 6. Known Gaps

- **Overflow filing history not fetched.** `filings.files[]` overflow pages are ignored. Companies with very large filing histories could be missing older 2020-era 10-K/10-Q rows.

- **No company-level error distinction.** All exceptions per CIK are caught the same way — a transient network error, a rate-limit 403, and a genuine malformed response all land in the `failed` list with a `log.warning`. There is no retry-with-backoff or error categorization.
