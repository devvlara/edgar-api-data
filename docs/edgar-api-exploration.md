# SEC EDGAR API — Exploration & Reference Guide

> A complete tour of the SEC's free, public EDGAR APIs: what endpoints exist, what data each returns, the URL structure, parameters, sample response shapes, rate limits, and how to start a project.

**Primary docs:** <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
**Data host:** <https://data.sec.gov/>
**Date of this guide:** 2026-04-19

---

## 1. Ground Rules — Read Before You Fire Any Request

Everything under `data.sec.gov` and the public EDGAR archives is **free, open, and requires no API key or OAuth**. In return, the SEC enforces a couple of hard rules. Violating them gets your IP temporarily blocked (usually ~10 minutes), returned as HTTP 403 Forbidden.

### 1.1 Required `User-Agent` header

Every single request must carry a `User-Agent` identifying you (or your app) and a contact email. The SEC rejects anonymous or browser-spoofed traffic at the edge. A missing User-Agent is the #1 cause of unexplained 403s.

Accepted format:

```
User-Agent: Sample Company Name AdminContact@samplecompany.com
```

In Python's `requests` this looks like:

```python
headers = {"User-Agent": "Dev Research devonlara13@gmail.com"}
r = requests.get(url, headers=headers)
```

### 1.2 Rate limit: 10 requests / second, per IP

The SEC's fair-access policy caps sustained traffic at **10 req/s per IP**, shared across every EDGAR host (`www.sec.gov`, `data.sec.gov`, `efts.sec.gov`). Burst over it and you'll get 403s until the cool-down expires. Build a simple throttle into any scraper.

### 1.3 Prefer bulk archives for big pulls

If you need more than a few thousand companies or several years of data, download the nightly bulk ZIPs once instead of hammering the per-company endpoints (see Section 8).

### 1.4 Data freshness

- **Submissions API** — updated within ~1 second of filing acceptance.
- **XBRL APIs (companyfacts, companyconcept, frames)** — typically under 1 minute, longer during 10-K/10-Q deadlines.
- **Bulk ZIPs** — rebuilt nightly.

---

## 2. Identifiers: CIK, Accession Number, Ticker

Every entity on EDGAR has a **Central Index Key (CIK)**, a numeric identifier. A few endpoints want CIK as a bare integer; most want it zero-padded to **10 digits** with the prefix `CIK`. Apple (CIK `320193`) becomes `CIK0000320193`.

Every filing has an **accession number**: e.g. `0000320193-24-000123`. The dashed form is used in some URLs; a dashless form (`000032019324000123`) is used in archive paths.

### 2.1 Ticker ↔ CIK mapping

Two free JSON files keep mappings current. These are just static files on `www.sec.gov`, not a REST API.

| URL | What it contains |
|---|---|
| `https://www.sec.gov/files/company_tickers.json` | ~10,000 tickers with CIK + company name. Object keyed by sequential index. |
| `https://www.sec.gov/files/company_tickers_exchange.json` | Same data plus the listing exchange (NYSE, Nasdaq, etc.) in a flat `{fields, data}` shape. |

**Sample response shape — `company_tickers.json`:**

```json
{
  "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
  "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
  "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."}
}
```

**Sample response shape — `company_tickers_exchange.json`:**

```json
{
  "fields": ["cik", "name", "ticker", "exchange"],
  "data": [
    [320193, "Apple Inc.", "AAPL", "Nasdaq"],
    [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"]
  ]
}
```

> **Gotcha:** `cik_str` is an integer, not the 10-digit zero-padded form. You'll need to pad it yourself before calling the other APIs: `f"CIK{int(cik):010d}"`.

---

## 3. Submissions API — a Company's Filing History

**Endpoint:**
```
GET https://data.sec.gov/submissions/CIK{10-digit-zero-padded}.json
```

**Example:** `https://data.sec.gov/submissions/CIK0000320193.json` (Apple)

Returns the company's identifying metadata plus its most recent ~1,000 filings. If a company has more filings than fit, older ones are split into additional files referenced under `filings.files[]`.

**Top-level keys:**

| Key | Meaning |
|---|---|
| `cik` | CIK as a string (not zero-padded). |
| `name`, `sic`, `sicDescription`, `tickers`, `exchanges` | Company identity + classification. |
| `ein`, `category`, `fiscalYearEnd` | Tax ID, filer category (e.g. "Large accelerated filer"), fiscal year end as `MMDD`. |
| `addresses` | Mailing and business addresses. |
| `formerNames` | Historical names with date ranges. |
| `filings.recent` | Parallel arrays of recent filings (see below). |
| `filings.files` | Pointers to overflow JSONs for older filings. |

**`filings.recent` is a columnar object** — every key is an array, and index `i` across arrays describes one filing:

```json
{
  "accessionNumber":   ["0000320193-24-000123", "..."],
  "filingDate":        ["2024-11-01", "..."],
  "reportDate":        ["2024-09-28", "..."],
  "acceptanceDateTime":["2024-11-01T16:30:00.000Z", "..."],
  "form":              ["10-K", "..."],
  "primaryDocument":   ["aapl-20240928.htm", "..."],
  "primaryDocDescription":["10-K", "..."],
  "items":             ["", "..."],
  "size":              [12345678, 0],
  "isXBRL":            [1, 0],
  "isInlineXBRL":      [1, 0]
}
```

**Build a filing URL** from an accession number + the primary document name:

```
https://www.sec.gov/Archives/edgar/data/{CIK_no_leading_zeros}/{accession_no_dashes}/{primaryDocument}
```

e.g. `https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm`

---

## 4. Company Facts API — Every XBRL Fact a Company Ever Filed

**Endpoint:**
```
GET https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json
```

**Example:** `https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json`

Returns every numeric fact the company has ever tagged in XBRL, grouped by taxonomy (e.g. `us-gaap`, `dei`, `ifrs-full`, `srt`) and tag (e.g. `Revenues`, `Assets`, `NetIncomeLoss`). This is the workhorse endpoint for financial analysis.

**Response shape:**

```json
{
  "cik": 320193,
  "entityName": "Apple Inc.",
  "facts": {
    "dei": {
      "EntityCommonStockSharesOutstanding": { "label": "...", "description": "...", "units": {"shares": [ /* array of observations */ ]} }
    },
    "us-gaap": {
      "Assets": {
        "label": "Assets",
        "description": "Sum of the carrying amounts as of the balance sheet date of all assets...",
        "units": {
          "USD": [
            {
              "start": "2022-09-25", "end": "2023-09-30",
              "val": 352583000000,
              "accn": "0000320193-23-000106",
              "fy": 2023, "fp": "FY",
              "form": "10-K", "filed": "2023-11-03",
              "frame": "CY2023Q3I"
            }
          ]
        }
      },
      "Revenues": { "units": {"USD": [/* ... */]} },
      "NetIncomeLoss": { "units": {"USD": [/* ... */]} }
    }
  }
}
```

**Per-fact fields to know:**
- `start` / `end` — the reporting period (flow facts like revenue) or just `end` (instantaneous facts like assets).
- `val` — the numeric value in the stated unit.
- `accn`, `form`, `filed` — source filing accession, form type, filing date.
- `fy`, `fp` — fiscal year + period (`FY`, `Q1`, `Q2`, `Q3`).
- `frame` — the standardized frame bucket (e.g. `CY2023Q4`, `CY2023Q3I`); **absent if the fact doesn't align to a calendar frame**. This matters when comparing across companies.

> **Gotcha:** A single fact can appear many times across filings (original 10-Q, restatement in 10-K, etc.). Sort by `filed` descending and dedupe on `(end, fy, fp)` to get the latest value.

---

## 5. Company Concept API — One Tag for One Company

**Endpoint:**
```
GET https://data.sec.gov/api/xbrl/companyconcept/CIK{10-digit}/{taxonomy}/{tag}.json
```

**Example:** `https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/AccountsPayableCurrent.json`

Narrower than Company Facts: returns the full time-series for a single `(CIK, taxonomy, tag)`. Useful when you only want one line item and don't want to download a ~5 MB companyfacts JSON.

**Response shape:** the same structure as a single tag inside `companyfacts.facts[taxonomy][tag]`:

```json
{
  "cik": 320193,
  "taxonomy": "us-gaap",
  "tag": "AccountsPayableCurrent",
  "label": "Accounts Payable, Current",
  "description": "Carrying value as of the balance sheet date of liabilities incurred...",
  "entityName": "Apple Inc.",
  "units": {
    "USD": [
      {"end": "2023-09-30", "val": 62611000000, "accn": "0000320193-23-000106", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "frame": "CY2023Q3I"}
    ]
  }
}
```

**Common taxonomies you'll see in the path:**
- `us-gaap` — US GAAP reporters.
- `ifrs-full` — foreign private issuers using IFRS.
- `dei` — Document and Entity Information (filer metadata, shares outstanding, etc.).
- `srt` — SEC Reporting Taxonomy (schedules, e.g. segment reporting).

---

## 6. Frames API — Cross-Company Snapshot for One Tag

**Endpoint:**
```
GET https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json
```

**Example:** `https://data.sec.gov/api/xbrl/frames/us-gaap/AccountsPayableCurrent/USD/CY2019Q1I.json`

This is the inverse of Company Concept: pick one `(taxonomy, tag, unit, period)` and you get **every reporting entity**'s value that most closely aligns with that calendar frame. Perfect for cross-sectional analysis (rank all companies by some metric at a given quarter).

**Period format:**
- `CY{YYYY}Q{1-4}` — a calendar quarter (flow/duration facts like revenue).
- `CY{YYYY}Q{1-4}I` — instantaneous at end of quarter (balance-sheet items like assets, AP).
- `CY{YYYY}` — full calendar year (annual flows).

**Response shape:**

```json
{
  "taxonomy": "us-gaap",
  "tag": "AccountsPayableCurrent",
  "ccp": "CY2019Q1I",
  "uom": "USD",
  "label": "Accounts Payable, Current",
  "description": "...",
  "pts": 3410,
  "data": [
    {"accn": "0000320193-19-000066", "cik": 320193, "entityName": "Apple Inc.", "loc": "US-CA", "end": "2019-03-30", "val": 30443000000},
    {"accn": "0000789019-19-000006", "cik": 789019, "entityName": "MICROSOFT CORP", "loc": "US-WA", "end": "2019-03-31", "val": 8544000000}
  ]
}
```

- `pts` is the number of data points returned (one per reporting company).
- `loc` uses ISO 3166-2 codes.

> **Gotcha:** Not every tag is defined for every period. Common tags (`Revenues`, `Assets`, `NetIncomeLoss`) are well populated; obscure tags may return 404.

---

## 7. Full-Text Search API (EFTS)

Everything under `data.sec.gov` deals with structured metadata and XBRL numbers — but the actual text of filings is searchable via a separate, undocumented-but-public API that powers the EDGAR Full-Text Search UI.

**Endpoint:**
```
GET https://efts.sec.gov/LATEST/search-index?q={query}&forms={form}&dateRange=custom&startdt=YYYY-MM-DD&enddt=YYYY-MM-DD
```

**Key parameters:**

| Param | Purpose |
|---|---|
| `q` | Query string. Supports quoted phrases, Boolean `OR`/`NOT`, and `*` wildcards. |
| `forms` | Comma-separated form types (`10-K`, `8-K`, `DEF 14A`, `4`, etc.). |
| `dateRange=custom` + `startdt` + `enddt` | Date range filter (ISO dates). |
| `ciks` | Comma-separated CIKs to restrict the search. |
| `from` | Pagination offset (0-based). |
| `size` | Page size, max **100**; default 10. |

**Example** — every 10-K mentioning "supply chain disruption" in 2023:
```
https://efts.sec.gov/LATEST/search-index?q=%22supply+chain+disruption%22&forms=10-K&dateRange=custom&startdt=2023-01-01&enddt=2023-12-31
```

**Response shape (abridged):**

```json
{
  "hits": {
    "total": {"value": 2471, "relation": "eq"},
    "hits": [
      {
        "_id": "0000320193-23-000106:aapl-20230930.htm",
        "_source": {
          "ciks": ["0000320193"],
          "display_names": ["Apple Inc.  (AAPL)  (CIK 0000320193)"],
          "form": "10-K",
          "file_date": "2023-11-03",
          "adsh": "0000320193-23-000106"
        }
      }
    ]
  }
}
```

> **Gotcha:** The full-text index only covers filings from **2001 forward**. Older filings are still available on EDGAR but aren't indexed.

---

## 8. Bulk Data Archives

For heavy workloads — populating a database, backtesting, doing anything over all ~15,000 filers — use the nightly ZIPs instead of crawling the per-company endpoints.

| File | Contents | URL |
|---|---|---|
| `submissions.zip` | One JSON per filer, same schema as the Submissions API. | `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip` |
| `companyfacts.zip` | One JSON per filer, same schema as the Company Facts API. | `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip` |

Both are rebuilt every night. The per-company JSONs inside are named `CIK{10-digit}.json`.

### 8.1 Other structured datasets on SEC.gov

- **Financial Statement Data Sets** — quarterly CSVs covering the numeric facts from ~every 10-K/10-Q. Nice tidy schema for SQL loading. `https://www.sec.gov/dera/data/financial-statement-data-sets`
- **Mutual Fund Prospectus Risk/Return Data Sets** — similar but for fund filings.
- **Financial Statement and Notes Data Sets** — adds note-level data (larger, more complex).
- **Form 13F-HR Information Tables** — institutional holdings, quarterly.
- **EDGAR Log File Data Sets** — anonymized web access logs (research-only, rarely needed for analytics).

---

## 9. EDGAR Archive URL Conventions (Raw Filings)

When you want to pull the actual filing document (not metadata), the paths under `www.sec.gov/Archives/edgar/data/` are predictable.

**Filing directory (index):**
```
https://www.sec.gov/Archives/edgar/data/{cik_no_padding}/{accession_no_dashes}/
```

The directory listing itself is available as JSON:
```
https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/index.json
```

**Primary HTML document:** combine the directory URL with `primaryDocument` from the Submissions API.

**XBRL Financial Report viewer** (rendered):
```
https://www.sec.gov/cgi-bin/viewer?action=view&cik={cik}&accession_number={accession-dashed}
```

**Filing summary (XML):** every post-2010 filing has `FilingSummary.xml` listing the Reports (R1.htm, R2.htm, …), which correspond to rendered financial statement tables.

---

## 10. Common XBRL Tags to Start With

A big part of EDGAR is just knowing *which tag you want*. Here's a starter kit for us-gaap filers:

### Income statement (flows — use `CY{YYYY}Q{1-4}` or `CY{YYYY}` frames)
- `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`
- `CostOfRevenue`, `GrossProfit`
- `OperatingIncomeLoss`
- `NetIncomeLoss`
- `EarningsPerShareBasic`, `EarningsPerShareDiluted`

### Balance sheet (instants — use `CY{YYYY}Q{1-4}I` frames)
- `Assets`, `Liabilities`, `StockholdersEquity`
- `CashAndCashEquivalentsAtCarryingValue`
- `AccountsPayableCurrent`, `AccountsReceivableNetCurrent`
- `LongTermDebt`, `LongTermDebtNoncurrent`

### Cash flow
- `NetCashProvidedByUsedInOperatingActivities`
- `NetCashProvidedByUsedInInvestingActivities`
- `NetCashProvidedByUsedInFinancingActivities`
- `PaymentsToAcquirePropertyPlantAndEquipment` (capex)

### Entity / DEI
- `dei:EntityCommonStockSharesOutstanding`
- `dei:DocumentFiscalYearFocus`, `dei:DocumentFiscalPeriodFocus`

> **Gotcha:** Company tag choice drifts over time. Apple may have used `SalesRevenueNet` in 2015 and `RevenueFromContractWithCustomerExcludingAssessedTax` after ASC 606. For time series, query multiple aliases and coalesce.

---

## 11. Minimal Python Starter

```python
import requests
import time

HEADERS = {"User-Agent": "Dev Research devonlara13@gmail.com"}
BASE = "https://data.sec.gov"

def get(url):
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    time.sleep(0.11)   # stay under 10 req/s
    return r.json()

# 1. resolve AAPL -> CIK
tickers = get("https://www.sec.gov/files/company_tickers.json")
aapl = next(v for v in tickers.values() if v["ticker"] == "AAPL")
cik10 = f"CIK{int(aapl['cik_str']):010d}"

# 2. filing history
subs = get(f"{BASE}/submissions/{cik10}.json")
recent = subs["filings"]["recent"]
print(f"Latest form: {recent['form'][0]} filed {recent['filingDate'][0]}")

# 3. one tag across all time
rev = get(f"{BASE}/api/xbrl/companyconcept/{cik10}/us-gaap/Revenues.json")
for obs in rev["units"]["USD"][-5:]:
    print(obs["end"], obs["val"], obs["form"])

# 4. cross-sectional snapshot
frame = get(f"{BASE}/api/xbrl/frames/us-gaap/Assets/USD/CY2023Q4I.json")
print(f"{frame['pts']} companies reported Assets in CY2023Q4I")
```

---

## 12. Endpoint Cheat Sheet

| # | Endpoint | Purpose | Shape |
|---|---|---|---|
| 1 | `www.sec.gov/files/company_tickers.json` | Ticker → CIK map | `{index: {cik_str, ticker, title}}` |
| 2 | `www.sec.gov/files/company_tickers_exchange.json` | + exchange | `{fields, data[]}` |
| 3 | `data.sec.gov/submissions/CIK##########.json` | One company's filings | Entity metadata + columnar `filings.recent` |
| 4 | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | All XBRL facts for a company | `{facts: {taxonomy: {tag: {units: {unit: [obs]}}}}}` |
| 5 | `data.sec.gov/api/xbrl/companyconcept/CIK##########/{tax}/{tag}.json` | One tag, one company, all time | Single-tag subset of #4 |
| 6 | `data.sec.gov/api/xbrl/frames/{tax}/{tag}/{unit}/{period}.json` | One tag, all companies, one period | `{data: [{cik, entityName, val}]}` |
| 7 | `efts.sec.gov/LATEST/search-index?q=...` | Full-text search | Elasticsearch-style `{hits}` |
| 8 | `www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip` | Bulk submissions | ZIP of per-CIK JSONs |
| 9 | `www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip` | Bulk facts | ZIP of per-CIK JSONs |
| 10 | `www.sec.gov/Archives/edgar/data/{cik}/{accn}/` | Raw filing documents | HTML directory + JSON index |

---

## 13. Project Ideas Worth Exploring

Now that you've seen the surface area, here are directions this dataset points toward. Pick one for a first sprint:

1. **Fundamental screener** — use `frames` to rank all companies on Revenue, Assets, ROE, leverage ratios for a chosen quarter. Output: sortable table / dashboard.
2. **Growth tracker** — pull `companyfacts` for a ticker list and compute YoY / QoQ deltas on key tags; watch for surprises.
3. **Restatement detector** — compare each quarter's originally filed value vs. the latest value for the same `(end, fy, fp)`; flag large revisions.
4. **8-K event feed** — poll Submissions for new 8-Ks and classify by `items` codes (e.g. 5.02 for executive changes, 2.02 for earnings).
5. **Insider activity tracker** — pull Form 3/4/5 filings and reconstruct net buying/selling per company per month.
6. **10-K language diff** — use full-text search + archive URLs to diff a company's risk factors vs. last year's 10-K.
7. **Peer-group benchmarking** — from SIC code in Submissions, group companies and compute percentile rankings on any fact.
8. **Macro roll-up** — use `frames` across many tags to roll up aggregate revenue, capex, or employment across the entire S&P 1500.

---

## 14. Known Limitations

- Live API calls from this current environment are blocked (the SEC domains are not on the allowlist of this sandbox). All responses illustrated here are shape-accurate per the official SEC docs; to actually fetch data, run the Python starter in Section 11 on your own machine.
- XBRL quality varies. Small filers, foreign private issuers, and historical filings have gaps, tag drift, and custom extensions that won't roll up cleanly.
- Not every form type is XBRL-tagged. Amendments, 8-Ks, proxy statements mostly aren't.
- There is no "search by company" metadata endpoint — you resolve companies via the ticker JSON or by knowing their CIK.

---

## 15. Sources

- [SEC EDGAR APIs (official)](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- [EDGAR API overview PDF](https://www.sec.gov/files/edgar/filer-information/api-overview.pdf)
- [SEC Developer Resources](https://www.sec.gov/about/developer-resources)
- [EDGAR Full Text Search](https://www.sec.gov/edgar/search/)
- [New Rate Control Limits announcement](https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits)
- [data.sec.gov host](https://data.sec.gov/)
