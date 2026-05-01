# Financials Pipeline (10-K / 10-Q) — Design Reference

**Scope:** How the pipeline fetches XBRL financial facts and loads the `financial_fact` table.
**Source files:** `src/etl_financial.py`, `src/db.py`, `src/tag_map.py`

---

## Overview

After company metadata and filings are written, the pipeline fetches every XBRL fact ever reported by the company and extracts a curated set of ~45 standardized financial metrics. One row per (company, metric, reporting period) is written to `financial_fact`. Restatements are resolved automatically: when a company refiled a period, only the most recently filed value is kept.

---

## 1. Fetch Path: Company Facts API

For each CIK, the pipeline fetches:

```
https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json
```

This endpoint returns every XBRL-tagged numeric value the company has ever submitted, organized as:

```
facts → {taxonomy} → {tag} → units → {unit} → [{observations}]
```

The pipeline scopes to the `us-gaap` taxonomy only:

```python
gaap = facts_json.get("facts", {}).get("us-gaap", {})
```

Other taxonomies (`dei`, `ifrs-full`, `srt`, `invest`) are ignored. This means foreign private issuers reporting under IFRS will have empty fact rows — they file on 20-F, which is also excluded by the `FINANCIAL_FORMS` filter.

**404 handling.** Some companies are non-XBRL filers (small reporting companies, older filings). If the company-facts endpoint returns 404, the exception is caught and silently skipped — the company is still marked `loaded_at` and no facts rows are written:

```python
try:
    facts_json = client.company_facts(cik)
    facts = _parse_facts(cik, facts_json)
    db.upsert_facts(conn, facts)
except requests.exceptions.HTTPError as exc:
    if exc.response.status_code != 404:
        raise
```

Any non-404 HTTP error re-raises, causing the company to land in the `failed` list and be retried.

---

## 2. XBRL Tag Mapping: `tag_map.py`

`TAG_MAP` in `tag_map.py` defines which XBRL tags map to which standardized metric names. Each metric has an ordered list of candidate tags — the pipeline tries them from first to last and stops at the first tag that has data. This fallback chain handles the reality that companies use different (but semantically equivalent) tags across periods and filer types.

Example:

```python
"revenue": [
    "RevenueFromContractWithCustomerExcludingAssessedTax",  # tried first
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",                                 # tried last
],
```

If a company has data for `Revenues` but not the first two tags, `Revenues` is used and the last two are never checked.

**~45 metrics across three statements:**

| Statement | Sample metrics |
|---|---|
| Income statement | `revenue`, `gross_profit`, `operating_income`, `net_income`, `eps_basic`, `eps_diluted`, `shares_basic`, `shares_diluted` |
| Balance sheet | `cash`, `accounts_receivable`, `inventory`, `total_assets`, `long_term_debt`, `stockholders_equity` |
| Cash flow | `operating_cf`, `capex`, `investing_cf`, `financing_cf`, `stock_buybacks`, `dividends_paid` |

**Unit overrides.** Most metrics default to the `USD` unit bucket. Four metrics use a different unit:

```python
UNIT_OVERRIDE = {
    "eps_basic":      "USD/shares",
    "eps_diluted":    "USD/shares",
    "shares_basic":   "shares",
    "shares_diluted": "shares",
}
```

This matters because the company-facts API organizes observations by unit — looking in the wrong unit bucket returns nothing.

---

## 3. Parsing: `_parse_facts`

`etl._parse_facts(cik, facts_json)` returns a list of row dicts ready to insert into `financial_fact`.

**Per metric, the logic is:**

1. Look up the target unit from `UNIT_OVERRIDE`, defaulting to `"USD"`.
2. Walk the tag fallback list. For each tag, check if the GAAP block has that tag and if its unit bucket has observations.
3. Filter those observations to forms in `FINANCIAL_FORMS` and `period_end >= CUTOFF`.
4. Resolve restatements (see Section 4).
5. `break` — stop trying further tags. If a tag had qualifying data, it wins for this metric; remaining fallbacks are skipped.

**Field mapping from observation to row:**

| Row field | Source | Notes |
|---|---|---|
| `cik` | function argument | |
| `accession_number` | `o["accn"]` | Dashed format |
| `metric` | outer loop key | Standardized name from `TAG_MAP` |
| `xbrl_tag` | inner loop variable | The specific tag that matched |
| `value` | `o["val"]` | Raw numeric value |
| `unit` | `target_unit` | USD, USD/shares, or shares |
| `period_start` | `o.get("start")` | `None` for instant (balance-sheet) facts |
| `period_end` | `o["end"]` | Always present |
| `period_type` | derived from `fp` | `"annual"` if `fp == "FY"`, else `"quarterly"` |
| `fiscal_year` | `o.get("fy")` | As reported by filer |
| `fiscal_period` | `o.get("fp", "")` | `"FY"`, `"Q1"`, `"Q2"`, `"Q3"`, or `""` |
| `filed_date` | `o["filed"]` | Used for restatement resolution |

---

## 4. Restatement Resolution

The company-facts API can return multiple observations for the same reporting period — the original filing plus one or more amendments or corrections. The pipeline keeps only the most recently filed value per `(period_end, fiscal_period)`:

```python
best: dict[tuple, dict] = {}
for o in filtered:
    key = (o["end"], o.get("fp", ""))
    if key not in best or o["filed"] > best[key]["filed"]:
        best[key] = o
```

This happens entirely in memory before any database writes. Only the winning observation per key enters the `rows` list. There is no history kept — restated values are silently discarded.

The database upsert reinforces this: if the pipeline runs twice (e.g., after a restart mid-run), the `ON CONFLICT DO UPDATE` ensures the database row is overwritten with the most recently parsed value rather than keeping a stale duplicate.

---

## 5. Table Design

### `financial_fact`

```sql
CREATE TABLE IF NOT EXISTS edgar.financial_fact (
    id                BIGSERIAL PRIMARY KEY,
    cik               TEXT NOT NULL REFERENCES edgar.company(cik),
    accession_number  TEXT,                          -- source filing; no FK constraint
    metric            TEXT NOT NULL,                 -- standardized name from TAG_MAP
    xbrl_tag          TEXT,                          -- raw XBRL tag from filer
    value             NUMERIC,
    unit              TEXT,                          -- USD | USD/shares | shares
    period_start      DATE,                          -- NULL for instant (balance-sheet) facts
    period_end        DATE NOT NULL,
    period_type       TEXT,                          -- "annual" | "quarterly"
    fiscal_year       INT,
    fiscal_period     TEXT,                          -- "FY" | "Q1" | "Q2" | "Q3" | ""
    filed_date        DATE,
    UNIQUE (cik, metric, period_end, fiscal_period)
);
CREATE INDEX IF NOT EXISTS fact_cik_metric_idx
    ON edgar.financial_fact(cik, metric, period_end DESC);
CREATE INDEX IF NOT EXISTS fact_cik_period_idx
    ON edgar.financial_fact(cik, period_end DESC);
```

**PK.** BIGSERIAL surrogate. The natural uniqueness constraint is on `(cik, metric, period_end, fiscal_period)` — this is what the upsert targets, not the surrogate key.

**`accession_number` has no FK constraint.** Unlike `filing`, the fact table's accession column is intentionally left as a plain `TEXT` with no `REFERENCES edgar.filing(accession_number)`. This is because the company-facts API returns facts from all form types (including 8-K amendments and others), so the accession number on a fact may not correspond to a row in `filing` (which only stores 10-K/10-Q).

**Upsert behavior.** `ON CONFLICT (cik, metric, period_end, fiscal_period) DO UPDATE` overwrites all seven non-key columns. The update payload intentionally excludes the surrogate `id` — Postgres keeps the original BIGSERIAL value on conflict, so row identity is stable across re-ingestion.

---

## 6. Indexes

Two covering indexes are defined:

| Index | Columns | Primary use |
|---|---|---|
| `fact_cik_metric_idx` | `(cik, metric, period_end DESC)` | Time-series for one metric on one company |
| `fact_cik_period_idx` | `(cik, period_end DESC)` | All metrics for one company at a point in time |

`queries.get_facts()` filters on `(cik, period_type, period_end YEAR BETWEEN ...)` — it uses `fact_cik_period_idx` for the CIK scan and applies `period_type` as a filter after.

---

## 7. Known Gaps

- **`us-gaap` only.** The pipeline ignores `ifrs-full` and other taxonomies, so foreign private issuers (20-F filers) will always have zero facts. If IFRS coverage is added later, `_parse_facts` needs a second taxonomy pass and `TAG_MAP` needs IFRS equivalents.

- **Only the first matching tag is used per metric.** If a company switches XBRL tags mid-history (e.g., uses `Revenues` for older filings and `RevenueFromContractWithCustomerExcludingAssessedTax` for newer), `break` after the first match means only one tag's observations make it into the database for that metric. The earlier observations from the other tag are silently dropped. In practice this is usually fine because the fallback list is ordered with the most-common modern tags first, but it can produce gaps in older periods.

- **`fiscal_period` defaults to `""`** when the API observation has no `fp` field. This becomes part of the uniqueness key, so a fact with `fp=""` and a fact with `fp="FY"` for the same period-end are treated as distinct rows rather than deduped.
