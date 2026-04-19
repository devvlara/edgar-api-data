# SEC EDGAR API — Data Dictionary

> Field-level reference for the 10 public EDGAR endpoints. Companion to `edgar-api-exploration.md`.
> Each section lists every field the endpoint returns (or accepts as a parameter), its type, whether it's required, what its value means, a concrete example, and a notes column for gotchas.

**Date:** 2026-04-19  •  **Primary docs:** <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>

---

## Contents

1. [01_Ticker_Map — company_tickers.json](#01-ticker-map)
2. [02_Ticker_Exchange — company_tickers_exchange.json](#02-ticker-exchange)
3. [03_Submissions — submissions/CIK##########.json](#03-submissions)
4. [04_CompanyFacts — api/xbrl/companyfacts/CIK#####.json](#04-companyfacts)
5. [05_CompanyConcept — api/xbrl/companyconcept/.../tag.json](#05-companyconcept)
6. [06_Frames — api/xbrl/frames/.../period.json](#06-frames)
7. [07_FullTextSearch — efts.sec.gov/LATEST/search-index](#07-fulltextsearch)
8. [08_Bulk_Submissions — submissions.zip](#08-bulk-submissions)
9. [09_Bulk_CompanyFacts — companyfacts.zip](#09-bulk-companyfacts)
10. [10_Archive_Filings — Archives/edgar/data/{cik}/{accn}/](#10-archive-filings)
11. [Appendix — Reference Values](#appendix--reference-values)

---

## 01_Ticker_Map

**Endpoint:** `company_tickers.json`

**URL pattern:** `https://www.sec.gov/files/company_tickers.json`

**Purpose.** Ticker → CIK → company name mapping for ~10,000 filers. Use this to resolve a ticker into a CIK before calling any other endpoint.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `{index}` | `object` | req | Top-level object keyed by stringified integer index ("0", "1", ...). The key itself carries no meaning — just iterate values(). | `{"0": {...}, "1": {...}}` | Not stable across file updates. Don't rely on the index as an ID. |
| `{index}.cik_str` | `integer` | req | The Central Index Key (CIK) of the registrant. | `320193` | Integer, NOT zero-padded. Pad to 10 digits with 'CIK' prefix for other endpoints: f"CIK{int(c):010d}". |
| `{index}.ticker` | `string` | req | Primary ticker symbol as registered with the SEC. | `"AAPL"` | Always uppercase. A company with multiple share classes may appear as multiple entries (e.g. GOOG + GOOGL). |
| `{index}.title` | `string` | req | EDGAR conformed company name — the official uppercase form used in EDGAR. | `"Apple Inc."` | May differ slightly from the marketing/legal name (e.g. "MICROSOFT CORP" vs "Microsoft Corporation"). |

---

## 02_Ticker_Exchange

**Endpoint:** `company_tickers_exchange.json`

**URL pattern:** `https://www.sec.gov/files/company_tickers_exchange.json`

**Purpose.** Same as #1 but includes listing exchange (Nasdaq/NYSE/OTC). Flat {fields,data} shape instead of keyed object.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `fields` | `array<string>` | req | Column name array defining the schema of rows in `data`. | `["cik","name","ticker","exchange"]` | Always 4 columns in this order at time of writing. Treat as authoritative in case order changes. |
| `data` | `array<array>` | req | Row-major array of company records. Each inner array has the same length as `fields`. | `[[320193,"Apple Inc.","AAPL","Nasdaq"], ...]` | Use pandas: pd.DataFrame(data, columns=fields). |
| `data[].cik` | `integer` | req | CIK of the registrant. | `320193` | Not zero-padded. |
| `data[].name` | `string` | req | EDGAR conformed company name. | `"Apple Inc."` |  |
| `data[].ticker` | `string` | req | Primary ticker symbol. | `"AAPL"` | May be null for some SEC-registered entities without a trading symbol. |
| `data[].exchange` | `string\|null` | opt | Listing exchange. | `"Nasdaq"` | Common values: Nasdaq, NYSE, NYSE Arca, NYSE American, CBOE, OTC. Can be null for unlisted / delisted entities. |

---

## 03_Submissions

**Endpoint:** `submissions/CIK##########.json`

**URL pattern:** `https://data.sec.gov/submissions/CIK{10-digit}.json`

**Purpose.** One company's complete filing history + entity metadata (SIC, addresses, tickers, former names). Paginated via overflow files.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `cik` | `string` | req | CIK of the entity as a string (NOT zero-padded at this level). | `"320193"` | Contrast with the URL, which requires 10-digit padding. |
| `entityType` | `string` | req | EDGAR entity classification. | `"operating"` | Common values: operating, investment, agent, large accelerated filer, etc. |
| `sic` | `string` | opt | Standard Industrial Classification code (4 digits, as a string). | `"3571"` | Primary industry. Useful for peer-group construction. |
| `sicDescription` | `string` | opt | Human-readable SIC description. | `"ELECTRONIC COMPUTERS"` |  |
| `ownerOrg` | `string` | opt | Internal EDGAR organizational owner of the filer record. | `"06 Technology"` | Mostly useful for SEC staff. |
| `insiderTransactionForOwnerExists` | `integer (0/1)` | req | 1 if any Form 3/4/5 has been filed with this entity as the owner. | `1` | Boolean-as-int. |
| `insiderTransactionForIssuerExists` | `integer (0/1)` | req | 1 if any Form 3/4/5 has been filed with this entity as the issuer. | `1` | Boolean-as-int. |
| `name` | `string` | req | Current entity name. | `"Apple Inc."` |  |
| `tickers` | `array<string>` | req | List of ticker symbols currently associated with the filer. | `["AAPL"]` | Empty array for non-publicly-traded filers. |
| `exchanges` | `array<string>` | req | Parallel to `tickers` — listing exchange for each. | `["Nasdaq"]` |  |
| `ein` | `string` | opt | Employer Identification Number (tax ID). | `"942404110"` | Digits only, no hyphens. |
| `description` | `string` | opt | Free-text description of the filer. | `""` | Often empty. |
| `website` | `string` | opt | Corporate website URL. | `""` | Often empty. |
| `investorWebsite` | `string` | opt | Investor relations URL. | `""` | Often empty. |
| `category` | `string` | opt | Filer category for SEC reporting purposes. | `"Large accelerated filer"` | Drives some form-applicability rules (e.g. 10-K deadline). |
| `fiscalYearEnd` | `string (MMDD)` | opt | Fiscal year end date as a 4-character MMDD string. | `"0930"` | Not ISO date; MMDD only. Apple = 0930. |
| `stateOfIncorporation` | `string` | opt | 2-letter state/territory code of incorporation. | `"CA"` | US states only; foreign private issuers may show a country code. |
| `stateOfIncorporationDescription` | `string` | opt | Human-readable form of the incorporation state. | `"CA"` | Often duplicates the code. |
| `addresses.mailing` | `object` | req | Mailing address block. | `{street1, city, stateOrCountry, zipCode, ...}` |  |
| `addresses.business` | `object` | req | Principal business address block. | `{street1, city, stateOrCountry, zipCode, ...}` | Often same as mailing. |
| `addresses.*.street1` | `string` | opt | Primary street line of the address. | `"One Apple Park Way"` |  |
| `addresses.*.street2` | `string` | opt | Secondary street line (suite, floor, etc.). | `null` | Often null. |
| `addresses.*.city` | `string` | opt | City. | `"Cupertino"` |  |
| `addresses.*.stateOrCountry` | `string` | opt | 2-letter US state OR ISO country code for foreign addresses. | `"CA"` | You'll see things like "X0" for unknown — handle defensively. |
| `addresses.*.zipCode` | `string` | opt | Postal code. | `"95014"` |  |
| `addresses.*.stateOrCountryDescription` | `string` | opt | Human-readable form of `stateOrCountry`. | `"CA"` |  |
| `phone` | `string` | opt | Main phone number. | `"(408) 996-1010"` |  |
| `flags` | `string` | opt | EDGAR internal flags string. | `""` | Typically empty; SEC-internal. |
| `formerNames` | `array<object>` | req | History of prior legal/conformed names. | `[{"name":"APPLE COMPUTER INC","from":"1980-01-01T00:00:00.000Z","to":"2007-01-09T00:00:00.000Z"}]` | Empty array if no renames. |
| `formerNames[].name` | `string` | req | Prior name. | `"APPLE COMPUTER INC"` |  |
| `formerNames[].from` | `string (ISO datetime)` | req | When the name became effective. | `"1980-01-01T00:00:00.000Z"` | Midnight UTC. |
| `formerNames[].to` | `string (ISO datetime)` | req | When the name was replaced. | `"2007-01-09T00:00:00.000Z"` |  |
| `filings` | `object` | req | Container for filing history. | `{recent:{...}, files:[...]}` |  |
| `filings.recent` | `object` | req | Most recent ~1,000 filings, stored as parallel columnar arrays (one key per column, aligned by index). | `{form:[...], filingDate:[...], ...}` | Columnar layout — to get filing i, read every array at index i. |
| `filings.recent.accessionNumber` | `array<string>` | req | Accession number in dashed format. | `["0000320193-24-000123", ...]` | Strip dashes for archive URL path construction. |
| `filings.recent.filingDate` | `array<string (YYYY-MM-DD)>` | req | Date the filing was accepted by EDGAR. | `["2024-11-01", ...]` | Not the report period — see reportDate. |
| `filings.recent.reportDate` | `array<string (YYYY-MM-DD)>` | req | Period of report (fiscal period end covered by the filing). | `["2024-09-28", ...]` | Can be empty string for non-periodic filings (e.g. 8-K event filings). |
| `filings.recent.acceptanceDateTime` | `array<string (ISO datetime)>` | req | Exact acceptance timestamp, UTC. | `["2024-11-01T16:30:00.000Z", ...]` |  |
| `filings.recent.act` | `array<string>` | opt | Which Securities Act the filing is made under. | `["34", ...]` | "33" = 1933 Act (offerings), "34" = 1934 Act (reporting). |
| `filings.recent.form` | `array<string>` | req | Form type. | `["10-K", "10-Q", "8-K", "4", ...]` | Amendments have /A suffix (e.g. 10-K/A). |
| `filings.recent.fileNumber` | `array<string>` | opt | SEC file number assigned to the filer for this form type. | `["001-36743", ...]` |  |
| `filings.recent.filmNumber` | `array<string>` | opt | EDGAR film identifier (internal). | `["241420000", ...]` |  |
| `filings.recent.items` | `array<string>` | opt | For 8-Ks: comma-separated item codes (e.g. "2.02,9.01"). Empty for other forms. | `["2.02,9.01", "", ...]` | Item codes classify 8-K triggers — 5.02 = exec departure, 2.02 = earnings, etc. |
| `filings.recent.size` | `array<integer>` | req | Total size of the filing package in bytes. | `[12345678, 0, ...]` | Can be 0 for rare filings. |
| `filings.recent.isXBRL` | `array<integer (0/1)>` | req | 1 if the filing has any XBRL data attached. | `[1, 0, ...]` |  |
| `filings.recent.isInlineXBRL` | `array<integer (0/1)>` | req | 1 if the filing uses Inline XBRL (iXBRL). | `[1, 0, ...]` | Modern filings are mostly iXBRL. |
| `filings.recent.primaryDocument` | `array<string>` | req | Filename of the primary document within the filing. | `["aapl-20240928.htm", ...]` | Combine with accession path to build a direct URL to the filing. |
| `filings.recent.primaryDocDescription` | `array<string>` | opt | Human description of the primary document. | `["10-K", ...]` |  |
| `filings.files` | `array<object>` | req | Pointers to additional JSON files holding older filings when the company exceeds the ~1,000-filing buffer in `recent`. | `[{"name":"CIK0000320193-submissions-001.json","filingCount":1000,"filingFrom":"1994-01-01","filingTo":"2014-02-14"}]` | Fetch from same /submissions/ path; empty for small filers. |
| `filings.files[].name` | `string` | req | Filename of the overflow JSON. | `"CIK0000320193-submissions-001.json"` | GET https://data.sec.gov/submissions/{name}. |
| `filings.files[].filingCount` | `integer` | req | Number of filings contained in that overflow file. | `1000` |  |
| `filings.files[].filingFrom` | `string (YYYY-MM-DD)` | req | Oldest filing date covered by the overflow file. | `"1994-01-01"` |  |
| `filings.files[].filingTo` | `string (YYYY-MM-DD)` | req | Newest filing date covered by the overflow file. | `"2014-02-14"` |  |

---

## 04_CompanyFacts

**Endpoint:** `api/xbrl/companyfacts/CIK#####.json`

**URL pattern:** `https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json`

**Purpose.** Every XBRL-tagged numeric fact a company has ever filed, grouped by taxonomy → tag → unit. Workhorse for fundamentals.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `cik` | `integer` | req | CIK of the entity. | `320193` | Not zero-padded. |
| `entityName` | `string` | req | Company name as known to EDGAR at time of last filing. | `"Apple Inc."` |  |
| `facts` | `object` | req | Container keyed by taxonomy. | `{us-gaap:{...}, dei:{...}}` | Common taxonomies: us-gaap, dei, ifrs-full, srt. |
| `facts.{taxonomy}` | `object` | req | All tags reported under that taxonomy. | `{Assets:{...}, Revenues:{...}}` | Iterate .items() to walk tags. |
| `facts.{taxonomy}.{tag}` | `object` | req | Definition + data for one tag. | `{label:..., description:..., units:{...}}` |  |
| `facts.{taxonomy}.{tag}.label` | `string` | req | Human-readable label for the tag. | `"Assets"` | From the taxonomy label linkbase. |
| `facts.{taxonomy}.{tag}.description` | `string` | req | Full narrative definition of what the tag represents. | `"Sum of the carrying amounts as of the balance sheet date of all assets..."` | Useful for picking the right tag. |
| `facts.{taxonomy}.{tag}.units` | `object` | req | Container keyed by unit of measure. | `{USD:[...], shares:[...]}` | Common units: USD, USD/shares, shares, pure. |
| `facts.{taxonomy}.{tag}.units.{unit}` | `array<object>` | req | Array of observations (facts) in that unit. | `[{end:..., val:..., accn:...}, ...]` | Multiple entries for the same period are normal (original + restatement). |
| `....units.{unit}[].start` | `string (YYYY-MM-DD)` | opt | Start of the reporting period. Present for duration/flow facts (e.g. Revenues); absent for instantaneous facts. | `"2022-09-25"` | Missing on balance-sheet items — only `end` is meaningful there. |
| `....units.{unit}[].end` | `string (YYYY-MM-DD)` | req | End of the reporting period (or the instant date for balance-sheet facts). | `"2023-09-30"` | The one date you can count on. |
| `....units.{unit}[].val` | `number` | req | Numeric value in the stated unit. | `352583000000` | JSON number — can be very large; handle as int/float carefully. |
| `....units.{unit}[].accn` | `string` | req | Accession number of the filing that reported this value. | `"0000320193-23-000106"` | Dashed format. Join back to Submissions or Archives. |
| `....units.{unit}[].fy` | `integer` | req | Fiscal year as reported by the filer. | `2023` |  |
| `....units.{unit}[].fp` | `string` | req | Fiscal period. | `"FY"` | Values: FY (full year), Q1, Q2, Q3. |
| `....units.{unit}[].form` | `string` | req | Form type of the source filing. | `"10-K"` | Same values as Submissions `form`. |
| `....units.{unit}[].filed` | `string (YYYY-MM-DD)` | req | Date the source filing was filed with the SEC. | `"2023-11-03"` | Use this to dedupe: keep the latest `filed` for a given (end, fy, fp). |
| `....units.{unit}[].frame` | `string` | opt | Standardized calendrical frame bucket. | `"CY2023Q3I"` | Absent if the fact doesn't align to a standard calendar frame. Matters for cross-company comparison. |

---

## 05_CompanyConcept

**Endpoint:** `api/xbrl/companyconcept/.../tag.json`

**URL pattern:** `https://data.sec.gov/api/xbrl/companyconcept/CIK{10}/{taxonomy}/{tag}.json`

**Purpose.** Narrower than Company Facts: full time-series for ONE (company, taxonomy, tag). Use when you only want one line item.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `cik` | `integer` | req | CIK of the entity. | `320193` |  |
| `taxonomy` | `string` | req | Taxonomy from the URL. | `"us-gaap"` |  |
| `tag` | `string` | req | Tag from the URL. | `"AccountsPayableCurrent"` |  |
| `label` | `string` | req | Human-readable label for the tag. | `"Accounts Payable, Current"` |  |
| `description` | `string` | req | Full narrative definition. | `"Carrying value as of the balance sheet date..."` |  |
| `entityName` | `string` | req | Company name. | `"Apple Inc."` |  |
| `units` | `object` | req | Container keyed by unit of measure. | `{USD:[...]}` |  |
| `units.{unit}` | `array<object>` | req | Array of observations. | `[{end:..., val:..., accn:...}, ...]` |  |
| `....units.{unit}[].start` | `string (YYYY-MM-DD)` | opt | Start of the reporting period. Present for duration/flow facts (e.g. Revenues); absent for instantaneous facts. | `"2022-09-25"` | Missing on balance-sheet items — only `end` is meaningful there. |
| `....units.{unit}[].end` | `string (YYYY-MM-DD)` | req | End of the reporting period (or the instant date for balance-sheet facts). | `"2023-09-30"` | The one date you can count on. |
| `....units.{unit}[].val` | `number` | req | Numeric value in the stated unit. | `352583000000` | JSON number — can be very large; handle as int/float carefully. |
| `....units.{unit}[].accn` | `string` | req | Accession number of the filing that reported this value. | `"0000320193-23-000106"` | Dashed format. Join back to Submissions or Archives. |
| `....units.{unit}[].fy` | `integer` | req | Fiscal year as reported by the filer. | `2023` |  |
| `....units.{unit}[].fp` | `string` | req | Fiscal period. | `"FY"` | Values: FY (full year), Q1, Q2, Q3. |
| `....units.{unit}[].form` | `string` | req | Form type of the source filing. | `"10-K"` | Same values as Submissions `form`. |
| `....units.{unit}[].filed` | `string (YYYY-MM-DD)` | req | Date the source filing was filed with the SEC. | `"2023-11-03"` | Use this to dedupe: keep the latest `filed` for a given (end, fy, fp). |
| `....units.{unit}[].frame` | `string` | opt | Standardized calendrical frame bucket. | `"CY2023Q3I"` | Absent if the fact doesn't align to a standard calendar frame. Matters for cross-company comparison. |

---

## 06_Frames

**Endpoint:** `api/xbrl/frames/.../period.json`

**URL pattern:** `https://data.sec.gov/api/xbrl/frames/{tax}/{tag}/{unit}/{period}.json`

**Purpose.** Inverse of Company Concept: ONE tag, ALL companies, ONE period. Perfect for cross-sectional snapshots / screens.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `taxonomy` | `string` | req | Taxonomy from the URL. | `"us-gaap"` |  |
| `tag` | `string` | req | Tag from the URL. | `"AccountsPayableCurrent"` |  |
| `ccp` | `string` | req | Calendrical period (frame) queried. Format: CYyyyy, CYyyyyQn, or CYyyyyQnI. | `"CY2019Q1I"` | I suffix = instantaneous (balance-sheet). No suffix = duration (flow). |
| `uom` | `string` | req | Unit of measure from the URL. | `"USD"` |  |
| `label` | `string` | req | Human-readable label for the tag. | `"Accounts Payable, Current"` |  |
| `description` | `string` | req | Full narrative definition. | `"Carrying value..."` |  |
| `pts` | `integer` | req | Number of data points (companies) in the response. | `3410` | Equal to len(data). |
| `data` | `array<object>` | req | One entry per reporting company. | `[{...}, {...}]` |  |
| `data[].accn` | `string` | req | Accession number of the source filing. | `"0000320193-19-000066"` |  |
| `data[].cik` | `integer` | req | CIK of the reporting company. | `320193` |  |
| `data[].entityName` | `string` | req | Company name. | `"Apple Inc."` |  |
| `data[].loc` | `string` | opt | ISO 3166-2 location of the entity's principal office. | `"US-CA"` | Can be missing for foreign private issuers. |
| `data[].start` | `string (YYYY-MM-DD)` | opt | Period start (duration facts only). | `"2019-01-01"` | Absent for instantaneous (I) frames. |
| `data[].end` | `string (YYYY-MM-DD)` | req | Period end / instant date. | `"2019-03-30"` |  |
| `data[].val` | `number` | req | Numeric value in the stated `uom`. | `30443000000` |  |

---

## 07_FullTextSearch

**Endpoint:** `efts.sec.gov/LATEST/search-index`

**URL pattern:** `https://efts.sec.gov/LATEST/search-index?q={...}`

**Purpose.** Full-text search across every EDGAR filing since 2001. Elasticsearch-style response. Supports quoted phrases, Booleans, wildcards.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `q` | `query param: string` | req | Search query. Supports quoted phrases ("cyber incident"), Boolean operators (OR, NOT), and * wildcards. | `"supply chain disruption"` | URL-encode the query string. |
| `forms` | `query param: string` | opt | Comma-separated list of form types to filter by. | `"10-K,10-Q"` |  |
| `dateRange` | `query param: string` | opt | Set to "custom" to enable custom date filtering via startdt/enddt. | `"custom"` |  |
| `startdt` | `query param: YYYY-MM-DD` | opt | Start of custom date range (inclusive). | `"2023-01-01"` | Requires dateRange=custom. |
| `enddt` | `query param: YYYY-MM-DD` | opt | End of custom date range (inclusive). | `"2023-12-31"` | Requires dateRange=custom. |
| `ciks` | `query param: string` | opt | Comma-separated CIKs to restrict the search to specific filers. | `"0000320193"` | Zero-padded form. |
| `from` | `query param: integer` | opt | Pagination offset (0-based). | `100` | Advance by `size` to page through results. |
| `size` | `query param: integer` | opt | Page size. Default 10, max 100. | `100` | Requests beyond 100 are capped. |
| `hits` | `object` | req | Results container. | `{total:{...}, hits:[...]}` | Elasticsearch-style envelope. |
| `hits.total` | `object` | req | Total match count metadata. | `{value:2471, relation:"eq"}` |  |
| `hits.total.value` | `integer` | req | Approximate total matches. | `2471` |  |
| `hits.total.relation` | `string` | req | "eq" = exact count; "gte" = capped (only "value" or more). | `"eq"` | "gte" usually means >10,000 hits; narrow your query. |
| `hits.hits` | `array<object>` | req | Array of up to `size` result records. | `[{...}, {...}]` |  |
| `hits.hits[]._id` | `string` | req | Composite ID: accession:primary-document. | `"0000320193-23-000106:aapl-20230930.htm"` |  |
| `hits.hits[]._score` | `number` | opt | Relevance score. | `8.42` | Higher = more relevant to q. |
| `hits.hits[]._source` | `object` | req | Document source fields. | `{ciks:[...], form:..., file_date:..., adsh:...}` |  |
| `hits.hits[]._source.ciks` | `array<string>` | req | CIK(s) associated with the filing (zero-padded strings). | `["0000320193"]` | Multiple for co-registrants. |
| `hits.hits[]._source.display_names` | `array<string>` | req | Formatted display strings for UI. | `["Apple Inc.  (AAPL)  (CIK 0000320193)"]` |  |
| `hits.hits[]._source.form` | `string` | req | Form type of the matched filing. | `"10-K"` |  |
| `hits.hits[]._source.root_form` | `string` | opt | Root form type when the matched doc is an exhibit. | `"10-K"` |  |
| `hits.hits[]._source.file_date` | `string (YYYY-MM-DD)` | req | Filing date. | `"2023-11-03"` |  |
| `hits.hits[]._source.adsh` | `string` | req | Accession number (dashed). | `"0000320193-23-000106"` |  |
| `hits.hits[]._source.file_type` | `string` | opt | Type label for the specific document that matched. | `"10-K"` |  |
| `hits.hits[]._source.inc_states` | `array<string>` | opt | State(s) of incorporation for the filer(s). | `["CA"]` |  |
| `hits.hits[]._source.biz_states` | `array<string>` | opt | Principal business state(s) for the filer(s). | `["CA"]` |  |
| `hits.hits[]._source.sics` | `array<string>` | opt | SIC code(s) of the filer(s). | `["3571"]` |  |

---

## 08_Bulk_Submissions

**Endpoint:** `submissions.zip`

**URL pattern:** `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`

**Purpose.** Nightly ZIP of every filer's submissions JSON. Use instead of looping the per-company endpoint for large pulls.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `submissions.zip` | `ZIP archive` | req | Container holding one JSON per filer. Rebuilt nightly. | `~500 MB zipped` | Stream-unzip rather than extracting everything at once. |
| `CIK{10-digit}.json` | `JSON file` | req | Per-filer file with the SAME schema as the Submissions API (see sheet 03). | `CIK0000320193.json` | The in-file `filings.recent` + `filings.files` combine to give ALL filings; overflow files are also included in the ZIP. |
| `<entries under each JSON>` | `` |  | See sheet 03_Submissions for the full field-level dictionary. | `` | Only difference vs the live API: this is a nightly snapshot, so may lag by up to 24 hours. |

---

## 09_Bulk_CompanyFacts

**Endpoint:** `companyfacts.zip`

**URL pattern:** `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`

**Purpose.** Nightly ZIP of every filer's companyfacts JSON (~15k files).

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `companyfacts.zip` | `ZIP archive` | req | Container holding one JSON per filer with all XBRL facts ever reported. Rebuilt nightly. | `~10 GB zipped (~40 GB unzipped)` | Large. Plan for streaming ingestion. |
| `CIK{10-digit}.json` | `JSON file` | req | Per-filer file with the SAME schema as the Company Facts API (see sheet 04). | `CIK0000320193.json` |  |
| `<entries under each JSON>` | `` |  | See sheet 04_CompanyFacts for the full field-level dictionary. | `` | Only non-XBRL filers are absent. |

---

## 10_Archive_Filings

**Endpoint:** `Archives/edgar/data/{cik}/{accn}/`

**URL pattern:** `https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accession_nodashes}/`

**Purpose.** Raw filing documents (HTML, XBRL, exhibits). Directory listing at index.json. Used after resolving accession numbers from Submissions.

| Field Path | Data Type | Req/Opt | Description | Example | Notes / Gotchas |
|---|---|---|---|---|---|
| `Path pattern` | `URL path` | req | /Archives/edgar/data/{cik_unpadded}/{accession_no_dashes}/ | `/Archives/edgar/data/320193/000032019324000123/` | cik is NOT zero-padded here; accession has dashes STRIPPED. |
| `index.json` | `JSON file` | req | Machine-readable directory listing available under every filing folder. | `.../000032019324000123/index.json` | Preferred over scraping the HTML directory page. |
| `directory` | `object` | req | Root wrapper for index.json. | `{name:..., parent-dir:..., item:[...]}` |  |
| `directory.name` | `string` | req | Accession number (dashless) = folder name. | `"000032019324000123"` |  |
| `directory.parent-dir` | `string` | req | Parent archive path for navigation. | `"/Archives/edgar/data/320193/"` |  |
| `directory.item` | `array<object>` | req | One entry per file/subdirectory in the folder. | `[{...}, {...}]` |  |
| `directory.item[].name` | `string` | req | Filename as stored. | `"aapl-20240928.htm"` |  |
| `directory.item[].type` | `string` | opt | File type label. | `"10-K"` | Human-readable, not MIME type. |
| `directory.item[].size` | `string` | req | Size in bytes (as a string, not number). | `"1234567"` | Cast to int if doing math. |
| `directory.item[].last-modified` | `string (YYYY-MM-DD HH:MM:SS)` | req | File modification timestamp (ET). | `"2024-11-01 16:30:00"` |  |
| `<filing contents>` | `various` | req | Files in the folder include: primary doc (htm), exhibits (htm/pdf/xml), XBRL instance (xml), XBRL linkbases, R{n}.htm rendered reports, Financial_Report.xlsx, FilingSummary.xml. | `R1.htm, R2.htm, ...` | FilingSummary.xml maps R-reports to the corresponding statement (cover page, income, balance sheet, etc.). |

---

## Appendix — Reference Values

Enumerations and code lists that recur across multiple endpoints.

### XBRL Taxonomies

| Code | Name | When you see it |
|---|---|---|
| us-gaap | US GAAP Financial Reporting Taxonomy | Domestic US filers reporting under US GAAP (most 10-K/10-Q). |
| ifrs-full | IFRS Taxonomy | Foreign private issuers reporting under IFRS (20-F, some 6-K). |
| dei | Document & Entity Information | Entity metadata (shares outstanding, fiscal period) across all filers. |
| srt | SEC Reporting Taxonomy | Schedules and supplemental disclosures (e.g., segment reporting). |
| invest | Investment Taxonomy | Investment company filings (N-CSR, N-Q, etc.). |

### Common Units of Measure (uom)

| Code | Meaning |
|---|---|
| USD | US Dollars |
| USD/shares | Per-share USD amount (EPS, dividends per share) |
| shares | Share count |
| pure | Dimensionless ratio (e.g., tax rate) |
| Y | Years |

### Frame Period Formats (ccp)

| Format | Example | Meaning |
|---|---|---|
| CY{YYYY} | CY2023 | Calendar year, duration (flows over the full year). |
| CY{YYYY}Q{1-4} | CY2023Q4 | Calendar quarter, duration (flows over the quarter). |
| CY{YYYY}Q{1-4}I | CY2023Q4I | Calendar quarter instant (balance as of quarter-end). |

### Fiscal Period (fp)

| Value | Meaning |
|---|---|
| FY | Full fiscal year |
| Q1 | First fiscal quarter |
| Q2 | Second fiscal quarter |
| Q3 | Third fiscal quarter |

### Common Form Types

| Form | What it is |
|---|---|
| 10-K | Annual report (US domestic) |
| 10-Q | Quarterly report (US domestic) |
| 20-F | Annual report (foreign private issuer) |
| 8-K | Current report — material events; items classify the trigger |
| DEF 14A | Definitive proxy statement |
| S-1 | Registration statement for IPO |
| 3, 4, 5 | Insider beneficial ownership (initial, change, annual) |
| 13F-HR | Institutional investment manager holdings (quarterly) |
| SC 13D/G | Beneficial ownership >5% |
| 6-K | Foreign issuer current report |

### Common 8-K Item Codes

| Item | Meaning |
|---|---|
| 1.01 | Entry into a material definitive agreement |
| 2.01 | Completion of acquisition or disposition of assets |
| 2.02 | Results of operations and financial condition (earnings) |
| 5.02 | Departure/appointment of directors or principal officers |
| 7.01 | Regulation FD disclosure |
| 8.01 | Other events |
| 9.01 | Financial statements and exhibits |

### HTTP Status Gotchas

| Status | Typical cause | Remedy |
|---|---|---|
| 403 Forbidden | Missing or invalid User-Agent header. | Send User-Agent: "Name email@domain" format. |
| 403 Forbidden | Exceeded 10 req/s rate limit. | Back off ~10 minutes, throttle to 9 req/s. |
| 404 Not Found | Bad CIK padding (not 10 digits). | Pad to 10 digits with leading zeros, prefix with 'CIK'. |
| 404 Not Found | Tag or period has no data (Frames API). | Try a different period; check facts availability in companyfacts first. |
| 429 Too Many Requests | Rare; same semantics as the 403 rate-limit response. | Throttle. |

---

## Notes on Usage

- **Req/Opt** means "reliably present in responses" (req) vs "may be absent / null" (opt).
- Examples are shape-accurate per the SEC's public documentation. This environment's network proxy blocks `sec.gov`, so responses were not freshly pulled — run the Python starter in the main exploration guide from your own machine to verify.
- When a field uses `{placeholder}` notation, replace it with the relevant value (e.g. `{taxonomy}` → `us-gaap`).
- Field paths use dot notation for nested objects and `[]` to indicate arrays.