# 13F Holdings — Pipeline Design

**Status:** Implemented. Code lives in `src/etl_13f.py`, `src/edgar_client.py`, and `src/db.py`.
**Scope:** 13F-HR, 13F-HR/A, 13F-NT, 13F-NT/A since 2020. 13D and 13G are deferred.

---

## Codebase baseline

Four existing tables (`company`, `ticker`, `filing`, `financial_fact`), `filing` has a `form_type` column, restatement logic lives in `_parse_facts()` via `best` dict keyed on `(end, fp)`, and the seed universe comes from `company_tickers_exchange.json`. `src/edgar_client.py` was created as part of this phase — it is a thin `requests.Session` wrapper with a thread-safe `_RateLimiter` capped at 9 req/s and is shared by both the company and 13F pipelines.

---

## 1. Filer Universe and Seeding

**Who files 13F.** Section 13(f) of the Securities Exchange Act requires any institutional investment manager with ≥ $100 million in Section 13(f) securities (exchange-listed equities, certain options and warrants) to file a 13F within 45 days after each calendar quarter end. There are roughly 5,000–6,000 active filers at any given time.

**How to discover them.** The existing pipeline seeds from `company_tickers_exchange.json`, which only covers publicly traded companies. Investment managers rarely have publicly traded equity, so their CIKs will not appear there. The CIK universe for 13F must be discovered separately.

Proposal: scan EDGAR's quarterly full-index files for all quarters from 2020 Q1 through the most recent completed quarter. Each index file is a pipe-delimited flat file available at:

```
https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{1-4}/company.gz
```

Each line contains: `CIK | company name | form type | date filed | filename`. Filter for `form type IN ('13F-HR', '13F-HR/A', '13F-NT', '13F-NT/A')` and deduplicate by CIK. For 2020 Q1 through 2025 Q1 that is 21 index files — 21 HTTP GETs before any per-manager work begins.

An alternative is to download the bulk `submissions.zip` (~500 MB) and filter JSON entries for any filing in the `formType` set. This is a single large download rather than 21 small ones, and it also contains the entity metadata needed to populate `investment_manager` without subsequent per-manager submissions calls. The tradeoff is download size vs. request count; the right choice depends on whether the environment has bandwidth or request-count constraints.

**CIK universe overlap.** The resulting manager CIK set will not fully overlap with the existing `company` table. Most investment managers have no publicly traded ticker and are absent from `company`. Some operating companies that also manage large portfolios (e.g., Berkshire Hathaway, insurance holding companies) will have CIKs in both tables — that duplication is acceptable because each table serves a distinct purpose and the data comes from the same submissions endpoint.

**Per-manager seeding.** For each discovered manager CIK, fetch:

```
https://data.sec.gov/submissions/CIK{10-digit}.json
```

Parse the same fields as the existing `_parse_submissions()` but write into `investment_manager` instead of `company`. From the `filings.recent` array, collect all 13F-form accessions since 2020 and write them into `manager_filing`.

---

## 2. Fetch Path

**Where holdings live.** The `data.sec.gov` API endpoints (company facts, company concept, frames) are XBRL-only and contain no 13F holdings data. Holdings live exclusively in the raw filing archive:

```
https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_nodashes}/
```

**Locating the information table.** Each filing folder contains multiple documents. The cover page (primary document) is HTML; the holdings are in a separate XML attachment called the "information table." To find it reliably without hard-coding filename patterns:

1. Fetch `index.json` for the filing:
   ```
   https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodashes}/index.json
   ```
2. Iterate `directory.item[]` and locate the entry whose `type` field equals `"INFORMATION TABLE"`.
3. Fetch that document by its `name` field within the same archive path.

Filename conventions vary by filer. Common patterns include `informationtable.xml`, `infotable.xml`, and `{filer}-{date}-informationtable.xml`, but the `type` field in `index.json` is the only reliable locator.

A sample `index.json` entry for the information table:

```json
{
  "name": "informationtable.xml",
  "type": "INFORMATION TABLE",
  "size": "142835",
  "last-modified": "2024-11-13 18:22:00"
}
```

**XML structure.** The information table XML uses the SEC's 13F schema. Each holding is wrapped in an `<infoTable>` element:

```xml
<infoTable>
  <nameOfIssuer>APPLE INC</nameOfIssuer>
  <titleOfClass>COM</titleOfClass>
  <cusip>037833100</cusip>
  <value>14250438</value>           <!-- in thousands of USD -->
  <shrsOrPrnAmt>
    <sshPrnamt>82700000</sshPrnamt>
    <sshPrnamtType>SH</sshPrnamtType>
  </shrsOrPrnAmt>
  <investmentDiscretion>SOLE</investmentDiscretion>
  <votingAuthority>
    <Sole>82700000</Sole>
    <Shared>0</Shared>
    <None>0</None>
  </votingAuthority>
</infoTable>
```

**Rate-limit implications.** The SEC enforces a global 10 req/sec limit per IP, shared across all pipeline activity. The current pipeline costs approximately 2 requests per company (1 submissions + 1 company-facts). Adding 13F creates a materially different cost profile:

- Per manager CIK: 1 submissions request + (N filings × 2 archive requests) where each filing needs `index.json` + the information table XML.
- A manager active since 2020 Q1 has up to 21 quarterly filings (20 regular + amendments). That is roughly 1 + (21 × 2) = 43 requests per manager.
- At ~5,000 active managers: ~215,000 requests ≈ 6 hours at the 10 req/sec ceiling.

This is roughly 10× more expensive per entity than the company pipeline and needs careful throttling. If the bulk `submissions.zip` is used for seeding, the 5,000 individual submissions fetches (5,000 requests) can be eliminated, dropping total requests by about 2%.

13F-NT filings have no information table XML. For those accessions, the archive fetch step can be skipped entirely (just record the filing metadata).

---

## 3. Table Designs

### 3.1 `investment_manager`

Intentionally lean — stores only the manager's identity, not entity classification metadata. The pipeline's purpose is position data (who held what), so SIC codes, state of incorporation, and filer category are omitted. Populated from the name field in the submissions endpoint.

```sql
CREATE TABLE IF NOT EXISTS edgar.investment_manager (
    cik              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    loaded_at        TIMESTAMPTZ          -- NULL until all 13F filings for this CIK are ingested
);
```

**Resumability.** `loaded_at IS NOT NULL` means this manager's full filing history has been ingested. Same pattern as `company.loaded_at`. A manager is marked loaded only after all qualifying 13F accessions have been processed (holdings written, map entries attempted).

---

### 3.2 `manager_filing`

One row per 13F accession. Separate from the existing `filing` table (see Section 4 for the decision rationale).

```sql
CREATE TABLE IF NOT EXISTS edgar.manager_filing (
    accession_number  TEXT PRIMARY KEY,
    cik               TEXT NOT NULL REFERENCES edgar.investment_manager(cik),
    form_type         TEXT NOT NULL,     -- '13F-HR' | '13F-HR/A' | '13F-NT' | '13F-NT/A'
    period_of_report  DATE,             -- calendar quarter-end this filing covers (e.g. 2024-09-30)
    filing_date       DATE,             -- date accepted by EDGAR
    is_amendment      BOOLEAN NOT NULL DEFAULT FALSE,
    amends_accession  TEXT REFERENCES edgar.manager_filing(accession_number),  -- NULL for originals
    is_superseded     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS mgr_filing_cik_period_idx
    ON edgar.manager_filing(cik, period_of_report DESC);
```

**Amendment columns.** `is_amendment` distinguishes 13F-HR/A rows from originals. `amends_accession` carries the FK to the filing being amended, enabling full chain traversal. `is_superseded` is set to `TRUE` on any prior filing for the same `(cik, period_of_report)` when a newer filing (original or amendment) is ingested.

**NULL behavior.** `period_of_report` is nullable: 13F-NT filings sometimes lack a report date in the submissions array. `amends_accession` is NULL for non-amendments and for amendments where the original predates the backfill horizon.

---

### 3.3 `holding`

One row per security per 13F-HR or 13F-HR/A filing. 13F-NT filings have no holdings rows.

Scoped to position data only. The SEC 13F XML also carries `investmentDiscretion` (SOLE/DFND/OTR) and `votingAuthority` fields — these are compliance fields with no relevance to "who held what at what size" and are intentionally not stored.

```sql
CREATE TABLE IF NOT EXISTS edgar.holding (
    id                          BIGSERIAL PRIMARY KEY,
    accession_number            TEXT NOT NULL REFERENCES edgar.manager_filing(accession_number),
    cusip                       TEXT NOT NULL,
    ticker                      TEXT,                    -- resolved at ingest via CUSIP → CIK → ticker; NULL if unresolved
    name_of_issuer              TEXT,
    title_of_class              TEXT,                    -- e.g. "COM" distinguishes common from warrants/preferred
    value_usd                   BIGINT,                  -- fair market value in THOUSANDS of USD (as reported)
    shares_or_principal_amount  NUMERIC,                 -- NUMERIC to handle large bond principal amounts
    shares_or_principal_type    TEXT                     -- 'SH' (shares) | 'PRN' (principal amount)
);

CREATE INDEX IF NOT EXISTS holding_accession_idx ON edgar.holding(accession_number);
CREATE INDEX IF NOT EXISTS holding_cusip_idx     ON edgar.holding(cusip);
```

**PK strategy.** BIGSERIAL surrogate key only, matching `financial_fact`. No composite UNIQUE constraint is enforced — `title_of_class` is nullable, and PostgreSQL UNIQUE constraints treat NULLs as distinct, so the constraint would allow duplicate rows when this column is NULL. Re-ingest safety is achieved instead via delete-before-insert in `db.upsert_holdings`: all existing rows for the accession are deleted before the new batch is inserted (see Section 5).

**`value_usd` unit.** Stored as reported: thousands of USD. 13F-HR instructions explicitly require this unit. All downstream queries must divide by 1,000 to get full dollars.

**NULL behavior.** `name_of_issuer` and `title_of_class` are nullable because filers occasionally omit them (particularly for certain option positions).

---

### 3.4 `cusip_issuer_map`

Maps a CUSIP to a CIK in the existing `company` table. Enables joining 13F holdings back to financial fundamentals.

```sql
CREATE TABLE IF NOT EXISTS edgar.cusip_issuer_map (
    cusip      TEXT PRIMARY KEY,
    cik        TEXT REFERENCES edgar.company(cik),   -- NULL when unresolved
    source     TEXT NOT NULL,    -- 'name_match' | 'manual' | 'third_party'
    mapped_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Unresolved CUSIPs.** Rows are inserted for every CUSIP encountered during holding ingest, even when no CIK match is found. `cik` is left NULL for unresolved entries. This allows a later resolution pass to UPDATE the row rather than INSERT, and it gives a clear audit of which CUSIPs have never been resolved.

**Source column.** Tracks how each mapping was obtained:
- `name_match` — resolved by fuzzy-matching `holding.name_of_issuer` against `company.name`
- `manual` — curated override
- `third_party` — sourced from an external service (e.g., OpenFIGI)

**Resolution approach.** Name matching is the only zero-cost option available without a third-party data subscription. Exact match on normalized names (uppercase, punctuation stripped) will cover common cases. Fuzzy matching (edit distance, token sort) can cover abbreviations ("APPLE INC" → "Apple Inc."). OpenFIGI offers a free API that maps CUSIP → FIGI → ticker, from which CIK can be looked up via the ticker map — worth considering as a follow-up.

---

## 4. Filing Table: Extend vs. Separate

**Decision: add a separate `manager_filing` table (Option B).**

The existing `filing` table has:
```sql
cik TEXT NOT NULL REFERENCES edgar.company(cik)
```

This FK is a hard constraint. Most 13F managers are not in `company` (they have no ticker), so 13F accessions cannot be inserted into `filing` without either (a) removing the FK constraint, (b) making `cik` nullable and adding a second FK column, or (c) populating `company` with every manager — which conflates two logically distinct entity types.

Additional reasons to keep them separate:

- `manager_filing` needs columns that have no equivalent on `filing`: `period_of_report` (the quarter covered, distinct from a fiscal period end), `is_amendment`, `amends_accession`, and `is_superseded`. Adding these as nullable columns to `filing` pollutes the table for all existing rows.
- Existing queries in `queries.py` join `filing` to `company` and assume company-type semantics. A UNION approach keeps those queries unchanged.
- The separation makes the amendment supersedence rule simpler: `is_superseded` is a clean boolean on `manager_filing` with no risk of collision against 10-K/10-Q restatement logic.

**Amendment handling in the filing table.** `manager_filing.is_superseded = TRUE` is set on the prior row for the same `(cik, period_of_report)` each time a later accession for that period is ingested. `holding` rows for the superseded accession are deleted and replaced with rows under the new accession. See Section 5 for the full rule.

---

## 5. Amendment / Supersedence Rule for 13F-HR/A

**Natural reporting key:** `(cik, period_of_report)`. An amendment (13F-HR/A) replaces the entire holdings schedule for its reporting quarter — there is no partial amendment concept in 13F.

**Proposed rule:**

1. When a 13F-HR or 13F-HR/A is ingested for `(cik, period_of_report)`:
   - Look for any existing `manager_filing` rows for the same `(cik, period_of_report)` where `is_superseded = FALSE`.
   - For each such row, set `is_superseded = TRUE` and delete all `holding` rows tied to that accession.
2. Insert the new `manager_filing` row (`is_superseded = FALSE`).
3. Insert new `holding` rows under the new accession.

**Parallel to `financial_fact`.** The existing pipeline keeps only the most recently filed value per `(period_end, fiscal_period)` via `ON CONFLICT DO UPDATE`. The 13F rule is conceptually the same — latest filing wins — but operates at the level of an entire holdings set rather than individual metric values. The difference is that `financial_fact` preserves the prior row with an updated value, while 13F holdings must be completely replaced. Storing holdings under the amendment's accession number (rather than updating in place) means the `manager_filing` audit trail is preserved even though the old holdings are deleted.

**Query pattern for current holdings:**

```sql
SELECT h.*
FROM   edgar.holding h
JOIN   edgar.manager_filing mf ON mf.accession_number = h.accession_number
WHERE  mf.cik             = :manager_cik
  AND  mf.period_of_report = :quarter_end
  AND  mf.is_superseded   = FALSE;
```

**Re-ingest safety.** If the same accession is re-ingested (e.g., a pipeline restart), `db.upsert_holdings` deletes all existing `holding` rows for that accession before inserting the new batch. This is safe because an accession's XML contents are immutable once accepted by EDGAR; only a new accession (amendment) changes the data.

---

## 6. Implementation Decisions

The following questions were open during design. Each is closed with the decision made during implementation.

- **`edgar_client.py`** — Created as `src/edgar_client.py`: a thin `requests.Session` wrapper with a thread-safe `_RateLimiter` at 9 req/s. One instance is shared across both pipelines via the `EdgarClient` class.

- **Seeding strategy** — Quarterly full-index files chosen. `EdgarClient.quarterly_index_13f(year, quarter)` fetches and parses one `company.gz` per quarter (25 GETs for 2020 Q1 – 2026 Q1). Avoids the 500 MB bulk download.

- **`value_usd` unit** — Stored as-reported in thousands of USD, matching the SEC 13F filing instruction. Downstream queries must divide by 1,000 for full-dollar amounts. The column name and DDL comment make this explicit.

- **`holding` uniqueness constraint** — Dropped entirely. `title_of_class` is nullable, and a UNIQUE constraint on nullable columns is unreliable in PostgreSQL (NULLs are always distinct). Re-ingest safety is handled by delete-before-insert in `db.upsert_holdings`: all rows for the accession are deleted before the new batch is written.

- **CUSIP resolution timing** — Eager, during ingest. `_resolve_cusips()` normalizes issuer names (uppercase, strip non-alphanumeric) and looks them up against an in-memory index built from `edgar.company` once per run. Unresolved CUSIPs are inserted with `cik = NULL`; re-runs upgrade NULL to a resolved value if a match is found.

- **13F-NT tracking** — NT filings are recorded in `manager_filing` (giving a full audit trail of which managers were exempt and when) but no archive fetch is attempted and no `holding` rows are written. The ETL filters on `_13F_HR_FORMS = {"13F-HR", "13F-HR/A"}` for the holdings step.

- **Managers that are also operating companies** — Duplication is accepted. A CIK like Berkshire Hathaway's exists in both `company` and `investment_manager`; each table serves a distinct purpose and both are populated from the same submissions endpoint.
