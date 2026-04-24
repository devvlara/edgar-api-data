# EDGAR Financial Data Pipeline

Pulls company submissions and XBRL financial facts from the SEC's free, public [EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) and loads them into a PostgreSQL database. The pipeline is fully resumable — any company already marked `loaded_at IS NOT NULL` is skipped on restart.

## What's in this repo

```
edgar/
├── README.md
├── requirements.txt
├── .env                          ← local secrets (gitignored)
├── src/
│   ├── db.py                     ← DDL + upsert helpers
│   ├── etl.py                    ← bulk ETL entry point
│   ├── queries.py                ← query functions (tickers, company info, facts)
│   └── tag_map.py                ← XBRL tag → standardized metric mapping
├── edgar_docs/                   ← SEC EDGAR API reference (upstream docs)
│   ├── edgar-api-data-dictionary.md
│   ├── edgar-api-data-dictionary.xlsx
│   ├── edgar-api-erd.md
│   └── edgar-api-erd.mermaid
└── my-docs/                      ← project schema docs
    └── edgar-schema-erd.mermaid  ← PostgreSQL ERD for the edgar schema
```

## Database schema

Four tables in the `edgar` PostgreSQL schema:

| Table | Key | Description |
|---|---|---|
| `company` | `cik` | One row per SEC filer; `loaded_at` tracks ETL completion |
| `ticker` | `ticker` | Trading symbols with exchange; FK → `company` |
| `filing` | `accession_number` | 10-K / 10-Q filings since 2020; FK → `company` |
| `financial_fact` | `id` (BIGSERIAL) | ~45 standardized metrics per company per period; unique on `(cik, metric, period_end, fiscal_period)` |

See [my-docs/edgar-schema-erd.mermaid](my-docs/edgar-schema-erd.mermaid) for the full ERD.

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

```bash
python -m src.etl
```

The script:
1. Fetches the full EDGAR ticker universe (~10 k+ companies)
2. Skips any CIK already marked loaded
3. For each remaining company: upserts company metadata, tickers, 10-K/10-Q filings, and financial facts
4. Resolves restatements by keeping the most recently filed value per `(period_end, fiscal_period)`

## Metrics tracked

`tag_map.py` maps ~45 standardized metric names to their XBRL tag fallback chains across three financial statements:

- **Income statement** — revenue, gross profit, operating income, net income, EPS, share counts, etc.
- **Balance sheet** — cash, receivables, inventory, total assets, debt, equity, etc.
- **Cash flow** — operating/investing/financing cash flows, capex, buybacks, dividends, etc.

## EDGAR API limits

The SEC requires a descriptive `User-Agent` header and enforces a limit of **10 requests per second** per IP. The `SEC_USER_AGENT` env var is passed with every request.

## References

- [EDGAR API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Accessing EDGAR data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- [SEC Developer Resources](https://www.sec.gov/about/developer-resources)
