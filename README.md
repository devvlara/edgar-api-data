# EDGAR Financial Data Pipeline

Pulls company submissions, XBRL financial facts, and institutional 13F holdings from the SEC's free, public [EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) and loads them into a PostgreSQL database. The pipeline is fully resumable — any company or investment manager already marked `loaded_at IS NOT NULL` is skipped on restart.

## What's in this repo

```
edgar/
├── README.md
├── requirements.txt
├── .env                          ← local secrets (gitignored)
├── src/
│   ├── db.py                     ← DDL + upsert helpers (company + 13F tables)
│   ├── edgar_client.py           ← rate-limited HTTP client (shared by both pipelines)
│   ├── etl_financial.py          ← company fundamentals ETL entry point
│   ├── etl_13f.py                ← 13F holdings ETL entry point
│   ├── queries.py                ← query functions (tickers, company info, facts)
│   └── tag_map.py                ← XBRL tag → standardized metric mapping
├── edgar_docs/                   ← SEC EDGAR API reference (upstream docs)
│   ├── edgar-api-data-dictionary.md
│   ├── edgar-api-data-dictionary.xlsx
│   ├── edgar-api-erd.md
│   └── edgar-api-erd.mermaid
└── my-docs/                      ← project schema docs
    ├── edgar-schema-erd.mermaid        ← PostgreSQL ERD for the edgar schema
    ├── company-data-design.md          ← company / ticker / filing pipeline design
    ├── company-financials-design.md    ← 10-K / 10-Q XBRL facts pipeline design
    └── institutional-13f-design.md     ← 13F institutional holdings pipeline design
```

## Database schema

Seven tables in the `edgar` PostgreSQL schema, split across two pipelines:

**Company fundamentals (10-K / 10-Q)**

| Table | Key | Description |
|---|---|---|
| `company` | `cik` | One row per SEC filer; `loaded_at` tracks ETL completion |
| `ticker` | `ticker` | Trading symbols with exchange; FK → `company` |
| `filing` | `accession_number` | 10-K / 10-Q filings since 2020; FK → `company` |
| `financial_fact` | `id` (BIGSERIAL) | ~45 standardized metrics per company per period; unique on `(cik, metric, period_end, fiscal_period)` |

**Institutional holdings (13F)**

| Table | Key | Description |
|---|---|---|
| `investment_manager` | `cik` | One row per 13F filer; `loaded_at` tracks ETL completion |
| `manager_filing` | `accession_number` | 13F-HR / 13F-NT filings and amendments since 2020; FK → `investment_manager` |
| `holding` | `id` (BIGSERIAL) | One row per security per 13F filing; FK → `manager_filing` |
| `cusip_issuer_map` | `cusip` | Maps CUSIP identifiers to company CIKs; nullable FK → `company` |

See [my-docs/edgar-schema-erd.mermaid](my-docs/edgar-schema-erd.mermaid) for the full ERD. Pipeline design docs:
- [company-data-design.md](my-docs/company-data-design.md) — company / ticker / filing ingestion
- [company-financials-design.md](my-docs/company-financials-design.md) — 10-K / 10-Q XBRL facts ingestion
- [institutional-13f-design.md](my-docs/institutional-13f-design.md) — 13F institutional holdings ingestion

## What is a CUSIP?

A **CUSIP** (Committee on Uniform Securities Identification Procedures) is a 9-character alphanumeric identifier assigned to every publicly traded security in North America. The first 6 characters identify the issuer, the next 2 identify the specific security (share class, bond series, etc.), and the final character is a check digit.

CUSIPs appear in 13F filings as the primary key for each reported holding. They are separate from — but can be mapped to — the CIK identifiers used throughout the rest of EDGAR. The `cusip_issuer_map` table maintains that bridge: for each CUSIP seen in a 13F holding, it records the corresponding `company.cik` (when resolved) alongside a `source` column tracking how the mapping was obtained (name match, manual, or third-party).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
SEC_USER_AGENT=Your Name your@email.com
DB_HOST=localhost
DB_PORT=5432
DB_NAME=your_db
DB_USER=your_user
DB_PASSWORD=your_password
```

`SEC_USER_AGENT` is required by the SEC. Requests without it will be blocked.

## Running the ETL

Both pipelines are independent and fully resumable. Run them in either order; any CIK already marked `loaded_at IS NOT NULL` is skipped automatically.

**Company fundamentals (10-K / 10-Q)**

```bash
python -m src.etl_financial
```

1. Fetches the full EDGAR ticker universe (~10 k+ companies)
2. Skips any CIK already marked loaded
3. For each remaining company: upserts company metadata, tickers, 10-K/10-Q filings, and financial facts
4. Resolves restatements by keeping the most recently filed value per `(period_end, fiscal_period)`

**Institutional holdings (13F)**

```bash
python -m src.etl_13f
```

1. Scans EDGAR quarterly full-index files (2020 Q1 → most recent completed quarter) to collect all 13F filer CIKs
2. Skips any manager CIK already marked loaded
3. For each remaining manager: upserts entity metadata and filing records, then fetches and parses the information table XML for every 13F-HR / 13F-HR/A filing
4. Handles amendments by superseding the prior filing's holdings for the same quarter
5. Attempts CUSIP → CIK resolution via issuer name matching against the `company` table

## Metrics tracked

`tag_map.py` maps ~45 standardized metric names to their XBRL tag fallback chains across three financial statements:

- **Income statement** — revenue, gross profit, operating income, net income, EPS, share counts, etc.
- **Balance sheet** — cash, receivables, inventory, total assets, debt, equity, etc.
- **Cash flow** — operating/investing/financing cash flows, capex, buybacks, dividends, etc.

## EDGAR API limits

The SEC requires a descriptive `User-Agent` header and enforces a limit of **10 requests per second** per IP. The `SEC_USER_AGENT` env var is passed with every request.

The 13F pipeline is more request-intensive than the fundamentals pipeline: each manager filing requires two archive fetches (directory index + information table XML) on top of the initial submissions fetch, compared to two requests per company for fundamentals. Both pipelines share the same rate limit budget.

## References

- [EDGAR API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Accessing EDGAR data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- [SEC Developer Resources](https://www.sec.gov/about/developer-resources)
