# EDGAR Financial Data Pipeline

Loads company metadata and raw XBRL financial facts from the SEC's free, public [EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) into PostgreSQL, then scores the loaded 10-Q data to flag companies whose fundamentals are deteriorating.

The project has two stages:

1. **Load** (`src/`) — a resumable ETL that dumps EDGAR data into Postgres as-is.
2. **Signals** (`signals/`) — reads the loaded 10-Q and 10-K facts and produces a ranked deterioration score per company per quarter, then turns flagged rows into short, grounded notes with a local Ollama model.

## What's in this repo

```
edgar/
├── README.md
├── requirements.txt
├── .env                          ← local secrets (gitignored)
├── src/                          ← stage 1: load EDGAR into Postgres
│   ├── db.py                     ← schema DDL + upsert helpers + get_connection()
│   ├── edgar_client.py           ← rate-limited SEC HTTP client
│   ├── etl_financial.py          ← entry point: initial bulk load of company metadata + 10-K/10-Q facts
│   └── etl_refresh.py            ← entry point: re-ingest already-loaded companies that have new filings
├── signals/                      ← stage 2: fundamentals-deterioration scoring
│   ├── tag_map.py                ← XBRL tag → fundamental concept mapping (priority fallbacks)
│   ├── feature_extraction.py     ← raw facts → per-company, per-quarter ratio table
│   ├── scoring.py                ← self/peer z-scores → deterioration ranking
│   ├── features.parquet          ← output of feature_extraction.py
│   ├── flags.parquet             ← output of scoring.py
│   ├── narrative.py              ← flagged rows → grounded plain-English notes via a local Ollama model
│   └── narratives.jsonl          ← output of narrative.py
├── edgar-docs/                   ← SEC EDGAR API reference (upstream docs)
│   ├── edgar-api-data-dictionary.md
│   ├── edgar-api-data-dictionary.xlsx
│   ├── edgar-api-erd.md
│   └── edgar-api-erd.mermaid
└── my-docs/                      ← earlier design notes (see "Documentation status" below)
    ├── edgar-schema-erd.mermaid
    ├── company-data-design.md
    ├── company-financials-design.md
    └── institutional-13f-design.md
```

## Database schema

Everything lives in one Postgres database (named by `DB_NAME`), split across three schemas. The load is a **raw dump**: no metric mapping and no restatement collapsing. Every XBRL observation the SEC publishes for a qualifying filing is kept as its own row, so duplicates are expected and are resolved later in `signals/`.

**`entity` schema** — submissions metadata, not specific to a form type

| Table | Key | Description |
|---|---|---|
| `entity.entity` | `cik` | One row per SEC entity: name, SIC code/description, category, fiscal year end, state of incorporation, etc. `loaded_at` is NULL until the entity is fully ingested, then the last time its facts were (re)loaded; `last_checked_at` is the last time `etl_refresh` compared it with the SEC |
| `entity.entity_address` | `id`; unique `(cik, address_type)` | Mailing and business addresses |
| `entity.entity_former_name` | `id` | Prior legal names with date range |
| `entity.ticker` | `ticker` | Trading symbols with exchange; one entity can have several |

**`"10k"` and `"10q"` schemas** — identical table shapes, each holding only its own form type

| Table | Key | Description |
|---|---|---|
| `filing` | `accession_number` | One row per filing with the full `filings.recent` field set (dates, document names, XBRL flags). Filings since 2020 |
| `xbrl_tag_meta` | `(taxonomy, tag)` | Label and description for each tag seen in this form type |
| `financial_fact_raw` | `id` | Every XBRL observation: `cik, taxonomy, tag, unit, value, period_start, period_end, accession_number, fiscal_year, fiscal_period, form, filed_date, frame`. `period_start` is NULL for balance-sheet (instant) facts |

**Currently loaded** (row counts queried from the database on 2026-11-13, after the refresh described below):

| Table | Rows |
|---|---|
| `entity.entity` | 8,315 (all with `loaded_at` set) |
| `entity.entity_address` | 16,630 |
| `entity.entity_former_name` | 6,673 |
| `entity.ticker` | 10,263 |
| `"10k".filing` / `"10k".financial_fact_raw` / `"10k".xbrl_tag_meta` | 27,332 / 10,285,677 / 8,374 |
| `"10q".filing` / `"10q".financial_fact_raw` / `"10q".xbrl_tag_meta` | 91,301 / 25,082,593 / 8,214 |

`period_end` in `"10q".financial_fact_raw` runs from 2020-01-01 to a corrupt source value in year 6016. `signals/feature_extraction.py` bounds `period_end` to 2009-01-01 through today, so that row never reaches the scores.

> **Quoting:** `"10k"` and `"10q"` start with a digit, so Postgres requires double quotes everywhere, including manual queries: `SELECT * FROM "10q".filing;`. Unquoted `10q.filing` is a syntax error.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt
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

`SEC_USER_AGENT` is required by the SEC and is sent with every request. All commands below run from the `edgar/` folder.

## Stage 1: loading data (`src/`)

```bash
python3 -m src.etl_financial
```

1. Fetches the full EDGAR ticker universe
2. Skips any CIK already marked `loaded_at IS NOT NULL`
3. For each remaining company: upserts entity metadata, addresses, former names and tickers; upserts 10-K / 10-Q filings; fetches every XBRL fact the company has reported and writes it to `"10k"` or `"10q"` `financial_fact_raw` according to the form type
4. Marks the company loaded

The script is fully resumable, so you can stop and restart it without redoing finished companies. It is a one-time backfill: it never revisits a company once `loaded_at` is set, so use the refresh job below to pick up new filings. A full run from the pasted log loaded 7,623 companies in about 1 h 27 min with 0 failures; the database now holds 8,315 entities, all marked loaded.

### Keeping the data current (`etl_refresh`)

```bash
python3 -m src.etl_refresh --dry-run     # report how many companies are stale; writes nothing
python3 -m src.etl_refresh               # re-ingest companies with new filings
```

For every company it fetches the (small) submissions document and compares its 10-K / 10-Q accession numbers with the ones stored in `"10k".filing` / `"10q".filing`. Companies with a new filing, or whose earlier load never finished, are fully re-ingested (the SEC's companyfacts endpoint has no incremental mode, so the whole fact set is refetched and replaced); the rest only get `last_checked_at` stamped. `loaded_at` is cleared before each re-ingest so a crash mid-way is retried on the next run. Cost is about one request per company plus one extra per stale company at the shared 9 requests/second limit. Don't run it at the same time as `etl_financial`. Check how current the data is with:

```sql
SELECT MIN(last_checked_at), MAX(last_checked_at) FROM entity.entity;
```

Re-run the `signals/` steps after a refresh so scores reflect the new filings. The refresh is a command you run; nothing schedules it yet.

**First full refresh (2026-11-13):** 8,315 companies checked in about 1 h 15 min with 0 failures; 5,150 (62%) had new filings and were re-ingested, 3,165 were unchanged, none were incomplete loads. Before it the data was roughly two quarters behind (latest scored quarter 2026Q1; after it, 2026Q3).

## Stage 2: signals (`signals/`)

`signals/` turns the loaded **10-Q** facts into a ranked list of companies whose fundamentals are worsening. It is an unsupervised statistical score, not a predictive model: there is no labeled outcome data to train against, so every flag traces back to specific ratios moving in a specific direction.

Extra dependency: `pyarrow` (for parquet), which `requirements.txt` includes.

```bash
python3 -m signals.feature_extraction    # -> signals/features.parquet
python3 -m signals.scoring               # -> signals/flags.parquet (+ prints top flags)
```

Both steps write into `signals/` regardless of the working directory. Override the paths with `--out` (and `--features` for scoring).

1. **`tag_map.py`** maps 13 fundamental concepts (revenue, gross profit, net income, SG&A, operating cash flow, total assets, total liabilities, current assets, current liabilities, receivables, inventory, long-term debt, stockholders' equity) to prioritized `us-gaap` XBRL tags, ordered by real usage counts from the data. It also maps three impairment concepts (goodwill, asset, intangible) that are used only to label rows, never scored.
2. **`feature_extraction.py`** pulls those facts from `"10q".financial_fact_raw` and `"10k".financial_fact_raw` and:
   - keeps single-quarter values only (drops year-to-date duplicates, which share a tag with the quarter);
   - fills in fiscal Q4, which has no 10-Q: balance-sheet values come straight from the 10-K, and flow values use the 10-K's own Q4 figure when it reports one, otherwise FY minus Q1-Q3 (only when all three quarters are present and contiguous). Rows with a subtraction-derived value carry `is_derived_q4 = True`;
   - collapses facts re-reported as prior-year comparatives to the original filing;
   - derives 11 ratios: gross margin, net margin, SG&A %, DSO, inventory/revenue, OCF / total assets, current ratio, long-term debt / total assets, revenue QoQ and YoY growth (QoQ is produced but not scored), and return on assets (quarterly net income / total assets). Ten ratios are scored;
   - sets ratios to NaN (never imputes) when revenue is near zero or the quarterly history has a gap;
   - labels impairment-driven losses: `impairment_total`, `impairment_pct_of_loss` (impairment / net loss, capped at 1, only for net-loss quarters) and `is_impairment_driven` (True at 50% or more). Impairments are sparse, so a missing quarter counts as zero and fiscal Q4 is FY minus whichever of Q1-Q3 were reported. On impairment-driven rows `net_margin` and `return_on_assets` are masked to NaN (the originals stay in `net_margin_unmasked` / `return_on_assets_unmasked`), the same pattern as the revenue floor;
3. **`scoring.py`** z-scores each ratio two ways, against the company's own history (trend: this quarter's change versus the same quarter a year earlier, standardized over its trailing 8 such changes, so seasonality cancels) and against same-calendar-quarter, same-SIC peers (level, with a fallback to the 2-digit SIC group for small peer groups). Z-scores are sign-adjusted so higher always means worse, capped at ±5, then averaged within three driver clusters (revenue-quality, balance-sheet, cash-flow) and across clusters, so one event such as a revenue collapse counts once rather than once per revenue-based ratio. A composite needs at least 2 of the 3 clusters and 6 ratios. The clusters are revenue-quality (net margin, gross margin, SG&A %, DSO, inventory/revenue, revenue YoY growth), balance-sheet (current ratio, long-term debt / total assets) and cash-flow (OCF / total assets, return on assets). Key output columns:
   - `deterioration_score` — the headline ranking. It exists only when a self-trend signal exists, and blends 70% self / 30% peer.
   - `peer_weakness_score` — "weak versus peers right now" with no trend claim, kept as a separate screen.
   - `is_derived_q4` — True where a Q4 flow value was derived by subtraction rather than reported; less reliable.
   - `is_derived_quarter` — True where any flow value on the row (Q2, Q3 or Q4) was derived by subtraction, including operating cash flow derived from year-to-date facts; a superset of `is_derived_q4`.
   - `impairment_total`, `impairment_pct_of_loss`, `is_impairment_driven` — label only. They are shown in the printed top-flag tables. Impairment-driven rows also have `net_margin` and `return_on_assets` masked to NaN before scoring (see "Impairment handling" below); the unmasked values are in `net_margin_unmasked` and `return_on_assets_unmasked`.
   - `discontinued_ops_income`, `income_continuing_ops`, `is_discontinued_ops_driven` — signed after-tax result of discontinued operations (0 if none), net income minus it, and True when its size is at least half of net income's (1,767 rows). The labels are not scored; on these rows `net_margin` and `return_on_assets` are masked to NaN, as for impairment-driven rows (originals in `net_margin_unmasked` / `return_on_assets_unmasked`).
   - `cause_unclear` — True for an unusually large net loss that impairments explain less than half of; the narrative layer does not narrate these rows.
   - `is_negative_equity` — True when stockholders' equity is zero or negative (label only, not scored). Common (18% of rows that report equity) and not by itself distress: Planet Fitness is profitable with negative equity from buybacks and securitized debt. The narrative layer should pair it with profitability and the other flags.
   - `n_clusters_self_z`, `n_clusters_peer_z` — how many of the 3 clusters fed each composite.
   - `is_financial_sector` — marks banks, brokers, insurers, REITs and shells (SIC codes 6000-6799), where revenue-based ratios are expected to be sparse, except real-estate operating businesses (6531 agents/managers, 6552 land developers), which have conventional revenue, cost and inventory and stay in. The printed top-flag tables exclude these rows by default (`top_flags(..., exclude_financial=True)`); their scores stay in `flags.parquet`.

Scores are in standard-deviation units, not probabilities. Treat a flag as a prompt for review and check it against the actual filing first. Spot-checks against SEC `companyfacts` so far are listed under "Validation log" below.

**Known limits**
- About a quarter of scored rows are fiscal Q4. A subtraction-derived Q4 agrees with a directly reported Q4 within 0.1% about 84% of the time (checked on 5,898 cases where both exist); the rest differ, typically because of recast comparatives.
- The trend signal is year-over-year: seasonality cancels, but a deterioration is detected at one-year cadence rather than one-quarter cadence, and a company needs about 7 quarters of history before it can be scored.
- Roughly 2,450 companies in 2026Q3 (about 2,700 in earlier full quarters) have enough history to receive a `deterioration_score`; thinly reporting companies do not.
- Non-operating events score as deterioration. A goodwill or asset impairment lowers both net income and total assets, so it hits `return_on_assets` (and net margin) hard. Impairment-driven rows are labelled and have `net_margin` / `return_on_assets` masked, so they are scored on their other ratios only. An acquisition (Dauch: revenue +93%, assets doubled) is a regime change that the self-trend can misread and is not detected. Other one-time items (restructuring, fair-value changes, gains on sale, litigation) are not labelled.
- The impairment label compares a pre-tax charge with an after-tax loss, so the share is capped at 1. Only goodwill, asset and intangible impairment tags are used; combined "restructuring and impairment" tags are excluded. A company that reports an earlier quarter's impairment only as a year-to-date figure gets its Q4 impairment overstated.
- Ratios inside a cluster are still correlated and equally weighted within it; the cluster grouping is a judgment call.
- The 70/30 blend, 8-quarter window and minimum-signal thresholds are judgment calls, not fit to outcomes.
- Peer groups use SIC code only, with no size control.

Each file's top-of-file docstring has the full detail (design, output columns, constants).

### Narratives (`narrative.py`)

```bash
python3 -m signals.narrative --quarter 2026Q3 --top-n 25      # top-ranked rows -> signals/narratives.jsonl
python3 -m signals.narrative --names "Camping World,WM Technology"   # named companies, latest scored row
python3 -m signals.narrative --names "Organogenesis" --dry-run       # print the fact sheet only
```

Needs a running Ollama server (default `http://localhost:11434`, model `gemma4:31b-mlx`, override with `--model` or `OLLAMA_MODEL`). The notes are descriptive fundamentals narration, not investment advice, and every row is built to be hard to over-claim:

- **Fact sheet, not raw data.** Every number the model may use is pre-formatted from the row's own values (`-12.4%`, `$37.2M`) and must be copied exactly. The sheet carries the scores, the ratios whose self-z is at least 1.5 (value now, value a year earlier, change, direction), up to three improving ratios for balance, revenue and net result, and the flags.
- **Mandatory disclosures.** `is_impairment_driven` (stated as a non-cash write-down, in the first two sentences), `is_discontinued_ops_driven` (the discontinued result and the income from continuing operations, first two sentences; net margin and return on assets are then left out of the ratio lists), `is_negative_equity` (stated neutrally, first two sentences) and derived figures (one short closing sentence). The prompt bans causal wording except for these structured causes, investment language, z-score jargon and mentions of flags that are not set.
- **Rows that are never sent to the model:** `is_financial_sector` rows and `cause_unclear` rows (an unusually large net loss that impairments explain less than half of; for example FMC 2026Q3). They are written to the output with `status: skipped` and the reason; a person must review them.
- **Validation after generation.** The text is rejected and regenerated (up to 3 attempts, with the violations fed back) if it contains a number not in the fact sheet, causal or investment wording, z-scores, more than 120 words, or misses a required disclosure. A row that never passes is stored as `failed_validation` with its last attempt.
- **What validation cannot do:** it cannot judge whether a sentence is a fair reading of the numbers. Read the notes and spot-check against filings before publishing.

`cause_unclear` is computed in `feature_extraction.py`: net loss of at least 10% of total assets AND at least 5 times the company's own median absolute net income over its previous 8 quarters (at least 3 needed; with less history the test is 25% of assets), with impairments explaining under 50%. It flags 2,747 of 126,375 rows, 11 of the 2,076 scored rows in 2026Q3.

### Validation log

Checked against SEC `companyfacts` (via the project's `EdgarClient`); all matched unless noted.

| Company | What was checked | Result |
|---|---|---|
| C3.ai | Revenue, net loss, gross margin for the quarter ended 2026-01-31; derived Q4 FY25 | Match |
| General Mills | Derived Q4 FY26 net loss vs. 10-K | Match |
| Frequency Electronics | Derived Q4 FY26 arithmetic | Match |
| Organogenesis | Q1 2026 revenue collapse and derived-Q4 spike; 2026-06-30 / 2026-09-30 10-Qs after the refresh | Match. Score fell from rank 1 to rank 30 (2026Q1) once clustering stopped counting the revenue event several times |
| Greystone Logistics | Revenue, gross profit, net income (2026-02-28, 2026-08-31) and derived Q4 FY26 | Match |
| Camping World, Fathom | Net loss and total assets for 2026Q3, which validate `return_on_assets` | Match. Both flags are driven mainly by impairments (goodwill $751.7M to $464.9M and $17.7M to $7.2M) |
| WM Technology | 2026Q3 revenue $38.5M, operating loss -$6.4M (vs. +$2.0M a year earlier), net loss -$4.2M | Match. The z-cap of 5 on net margin / return on assets reflects a very stable, low-variance history (margin +5.8% to -10.8%), not a small-dollar artifact; the swing is real |
| FMC | 2026Q3 revenue $105.9M and net loss -$2.07B (10-Q filed 2026-10-30). Also exposed the `debt_to_equity` sign bug | Match as filed; the pipeline is not mis-reading the data. Impairment ($287.5M) is only 14% of the loss so it is not labelled impairment-driven. The cause of the revenue and loss collapse is not explained by XBRL facts alone: read the 10-Q |
| Solana Co, Dauch | Why each scored highly | Solana: sign bug in the old net-income growth ratio. Dauch: capped net-income growth off a near-zero base plus acquisition leverage |

Impairment sweep of the 2026Q3 top 25 (financial-sector names excluded), run with the new labels instead of one-by-one filing lookups:

| Impairment-driven (`is_impairment_driven` True) | Not impairment-driven |
|---|---|
| Bright Horizons, Camping World, Fathom, Latch, TripAdvisor, Shutterstock, Charter, Fiserv (8 of 25) | Hornbeck, Belden, Sylvamo, CVR Partners, Zillow, Tarsus, Peraso and others |

Zillow's loss is not impairment: restructuring charges are about $32M of a $158M loss. Sylvamo, Tarsus, Hornbeck and Athene show no impairment or one-time-item tags for the quarter; their large losses are not explained by this sweep and are not verified against filings. Over the last eight quarters impairment-driven rows are 2-6% of scored rows but 6-10 of each quarter's top 25.

### Development history (what changed and why)

Each item below was found by checking output against the data, not assumed from the formula.

1. **Year-to-date duplication** (quarter vs. YTD facts share a tag): fixed with a duration filter (75-100 days for 10-Q, 350-380 for 10-K).
2. **Cross-accession duplicates and restatements**: kept one fact per (company, concept, period) by tag priority, then filing closest to period end.
3. **Peer grouping fragmentation**: peers are grouped by calendar quarter and 4-digit SIC, falling back to the 2-digit group when fewer than 5 peers.
4. **Ratio explosion near zero revenue**: revenue floor (max of $10k and 10% of trailing 8-quarter median), winsorizing to the 1st-99th percentile on internal copies, z-scores capped at ±5.
5. **Self-gated ranking**: `deterioration_score` exists only with a self-trend signal; `peer_weakness_score` is the separate peer-only screen.
6. **Gap masking**: growth and self-trend deltas are NaN when the span between rows is not about one quarter / one year (`span_ok`).
7. **Fiscal Q4 fill-in** from the 10-K (direct Q4 when reported, otherwise FY minus Q1-Q3), flagged `is_derived_q4`. Found that 10-Q comparatives already hold prior-year-end balance sheet values, so the 10-K value wins where they differ.
8. **Year-over-year self-z** so seasonality cancels; `min_rows` for the printed quarter is 500.
9. **Stale data**: added `src/etl_refresh.py` and `entity.last_checked_at`; ran a full refresh (see above).
10. **Cluster-based composite** (replaces a flat average of 10 ratios): stops one revenue event counting about five times. Gate was 2 of 3 clusters AND 4 raw ratios (now 6, see item 19); clusters alone admitted thinly-reported banks and biotechs.
11. **Net-income growth ratio replaced by return on assets.** The old ratio divided by a possibly negative prior quarter, so a swing from loss to profit scored as deterioration (Solana), and growth off a near-zero base hit the cap (Dauch and several others). Return on assets is a level ratio with no such failure.
12. **Financial-sector exclusion** from the printed rankings (scores kept in `flags.parquet`).

13. **Impairment labels** (`impairment_total`, `impairment_pct_of_loss`, `is_impairment_driven`). Tags were chosen from a usage survey of `"10q".financial_fact_raw` (about 1,500 companies each for `GoodwillImpairmentLoss` and `AssetImpairmentCharges`). Camping World (impairment $293.4M vs. loss $197.0M, capped to 100%) and Fathom (63%) match the filing figures. 3,081 of 56,362 net-loss rows are labelled impairment-driven.
14. **Impairment masking (decision made).** On `is_impairment_driven` rows, `net_margin` and `return_on_assets` are set to NaN in `feature_extraction.py`, where the revenue floor already masks, so both headline scores inherit it with no special-casing in `scoring.py`. A discount multiplier was rejected: it is an arbitrary unfit weight and hides the reason. Effect on the 2026Q3 top 25: impairment-driven rows fall from 8 to 2 (those two are scored on their other ratios). Camping World 2nd to 37th, Fathom 4th to 241st, Charter 23rd to 1,077th, Fiserv 24th to 531st, TripAdvisor 16th to 238th. Scored rows fall slightly (49,786 to 49,010) because some rows lose too many ratios for the gates.

15. **`debt_to_equity` replaced by `long_term_debt_to_assets`, plus `is_negative_equity`.** The old ratio flipped sign when equity went negative (FMC: equity +$1.6B to -$19M gave -204 and a -5 "improving" z; 11% of rows with D/E had negative equity and 1,642 of 2,891 scored ones read as improving). Debt over total assets has an always-positive denominator (non-positive total assets is masked to NaN), same fix as the net-income growth ratio. Because that ratio cannot show negative equity, an explicit `is_negative_equity` flag was added. Effect, 2026Q3 top 25: FMC 19th to 1st (leverage z now +3.41); System1 412th to 20th (equity +$58M to -$78M, debt/assets 1.09), Xponential Fitness 152nd to 40th, Genworth 106th to 39th, Deluxe 109th to 42nd. Scored rows 49,010 to 49,343. `is_negative_equity` is True on 21,206 rows.

16. **`ocf_to_net_income` replaced by `ocf_to_assets`** (operating cash flow / total assets, non-positive assets masked), the third sign-crossing ratio fixed (after `net_income_qoq_growth` and `debt_to_equity`). All ratios were then checked systematically: every remaining denominator cannot change sign (revenue is floor-masked positive, total assets guarded positive, current liabilities non-negative), so no other sign-crossing ratio remains. Direction stays -1. Rankings barely moved: 2026Q3 top-25 overlap 24 of 25 (only ranks 21-25 shuffled), 2026Q1 22 of 25 (Microvast 80th to 12th), 2026Q2 25 of 25. FMC, System1, Camping World and Fathom were not affected: they have no single-quarter operating cash flow for 2026Q3.
17. **Operating cash flow derived from year-to-date facts.** Cash-flow statements are reported year-to-date, and the 75-100 day filter had discarded nearly all of them: operating cash flow was present in about 78% of Q1 rows but only 6-9% of Q2-Q4, and `ocf_to_assets` had a self-z on only 0.7% of rows, so the cash-flow cluster had effectively been `return_on_assets` alone. `operating_cash_flow` is now in `tag_map.YTD_DERIVED_CONCEPTS`, and `_derive_ytd_flows` computes each quarter from two adjacent cumulative facts matched by date (same tag, period starts within 7 days, later period ends 75-110 days after the earlier): Q2 = 6-month minus 3-month, Q3 = 9-month minus 6-month, Q4 = FY minus 9-month (Q1 is already standalone). Directly reported standalone facts take precedence. Result: 87,121 quarters derived (Q2 30,413, Q3 30,044, Q4 26,664); operating cash flow present in 92-95% of rows in every quarter; `ocf_to_assets` self-z on 60% of rows (peer z 90%); the cash-flow cluster is represented in 99.5% of scored rows and 91% of scored rows now have all 3 clusters. Tied out to SEC cumulative facts (Camping World Q2/Q3 2026 = $398.8M / $154.8M, Q4 2025 = -$227.2M; Greystone Q2-Q4 FY26). New flag `is_derived_quarter` marks any row with a flow derived by subtraction (Q4 FY minus Q1-Q3, or any year-to-date difference); `is_derived_q4` stays Q4-specific. Because the cash-flow cluster now contributes broadly, the clustering runs as designed (three clusters) for most rows for the first time, and the rankings moved a lot: scored rows 49,338 to 55,312, and the top 25 overlaps the previous run by only 14-15 of 25 in 2026Q1-Q3.

18. **Financial-sector flag by SIC range, with a real-estate carve-out.** The exact-description list caught only 20,380 of 29,557 rows in SIC 60-67 and let Stewart Title (6361), Northrim (6035), Credit Acceptance (6141) and Mesabi (6795) into the headline ranking. `is_financial_sector` is now `tag_map.is_financial_sic`: SIC 6000-6799 except 6531 and 6552 (29,013 rows flagged; 544 rows carved out). The carve-out boundary came from a survey of the SIC codes present in `entity.entity` and how often each ratio exists for them (share of rows): 6552 land developers (revenue 96%, inventory/revenue 19%) and 6531 agents and managers (revenue 91%, DSO 47%; Fathom is one) are operating businesses; 6510-6519 lessors/operators of buildings (gross margin on 4-32% of rows, inventory about 0%) and the generic 6500 "Real Estate" bucket (gross margin 11%) are rental or holding structures like REITs and stay excluded.
19. **Gate review after the cash-flow recovery** (non-financial rows only). Scored rows 47,089 to 47,675 for that population. 95.7% of scored rows now have all 3 clusters, so the 2-of-3 cluster floor almost never binds (4.3% have exactly 2); the raw-ratio floor of 4 is what binds (5.8% of rows sit at exactly 4 ratios, 13% at 4-5). Because clusters are averaged first, a cluster holding a single ratio carries a full third of the score, so a 4-ratio row can be dominated by one noisy ratio. Five of the 2026Q3 top 25 have only 4-5 ratios (Great Elm 4, ProKidney 4, AMREP 5, Xenetic 5, Conduent 5). Raising `_MIN_SIGNALS` to 5 keeps 96% of 2026Q3 rows (top-25 overlap 23 of 25); to 6 keeps 91% (overlap 20 of 25). **Applied on 2026-11-14: `_MIN_SIGNALS` = 6.** Result (non-financial rows): scored rows 47,675 to 41,329 across all quarters (about 9% fewer in 2026Q3: 2,275 to 2,076); top-25 overlap with the previous run 23 of 25 in 2026Q1 and 2026Q2, 20 of 25 in 2026Q3. The five thin names left the 2026Q3 top 25 (Great Elm, ProKidney, AMREP, Xenetic, Conduent). **Known remaining gap:** the raw-count and cluster-count floors are separate gates and neither guarantees each represented cluster has more than one ratio behind it: 6 ratios can all come from the revenue-quality cluster, and a one-ratio cluster still carries a third of the weight. A per-cluster minimum (for example 2 ratios in every cluster that counts) would close it; not implemented.

20. **`cause_unclear` flag.** FMC's -$2.07B loss on $105.9M revenue (impairment only 14% of it) is not explained by any other flag, and a language model handed an unexplained loss is likely to invent a cause. The flag marks an unusually large loss the data cannot explain; a plain "loss is large against assets" test would flag a quarter of all loss rows (mostly steady cash-burning biotechs), so it also requires the loss to be an outlier against the company's own history. Narratives are not generated for these rows.
21. **Narrative layer (`signals/narrative.py`), built and checked in stages.** First on verified names (Camping World, Fathom, C3.ai, Organogenesis, WM Technology, FMC) whose real answers were known; reading them as a reader would exposed problems the validator could not catch, each fixed: impairment described as "explaining 100%" of a smaller loss (now: charge is larger than the loss, mainly a write-down), a "change in a growth rate" (dropped for revenue growth), z-score jargon, filler about flags that are not set, and the derived-figures note leading the text. The small default model (`gemma4:latest`) ignored instructions on some rows; `gemma4:31b-mlx` followed them and is the default. A broader run on the 2026Q3 top 25 produced 23 notes, 2 skipped (`cause_unclear`: FMC and LFTD) and 0 failures. Spot-checks of Hornbeck, Sylvamo and Incyte against SEC data matched on revenue and net result.

22. **Discontinued-operations flag.** Found by spot-checking Sylvamo's 2026Q3 narrative against SEC data: its -$199M net loss includes -$284M from discontinued operations (continuing operations earned +$85M), which no flag showed. `discontinued_ops_income` (tag `IncomeLossFromDiscontinuedOperationsNetOfTax`, fallback the "AttributableToReportingEntity" variant) is a signed, sparse concept (absent means none; fiscal Q4 is FY minus reported Q1-Q3, and unlike impairments a negative result is valid). It yields `income_continuing_ops` and `is_discontinued_ops_driven` (|result| at least half of |net income|): 7,276 rows have a discontinued result, 1,767 are driven, 24 of the 2,076 scored 2026Q3 rows and only Sylvamo in the 2026Q3 top 25. Values tie out to SEC for FMC (Q3'25 -$20.4M, Q4'25 -$32.6M, Q1'26 -$19.9M, Q2'26 +$6.5M, Q3'26 -$36.8M; FMC is not discontinued-driven and stays `cause_unclear`). The narrative layer now states the discontinued result and the continuing-operations income first, and keeps net margin and return on assets out of the ratio lists for these rows. **Masking applied (decision made):** `net_margin` and `return_on_assets` are masked to NaN on `is_discontinued_ops_driven` rows, the same pattern as impairment-driven rows. Effect: scored rows 43,059 to 42,515 across all quarters (825 driven rows were scored, 585 still are); 2026Q3 scored 2,076 to 2,061, top-25 overlap 24 of 25 (25 of 25 in 2026Q1, 23 of 25 in 2026Q2); Sylvamo moves from 13th to 212th (score 1.50 to 0.63), which is consistent with continuing operations having earned $85M.

### Impairment handling

- **Label:** `is_impairment_driven` is True when goodwill, asset and intangible impairment charges explain at least 50% (`IMPAIRMENT_DRIVEN_THRESHOLD`) of a net loss.
- **Mask:** on impairment-driven and discontinued-operations-driven rows `net_margin` and `return_on_assets` are NaN, so the row is scored on its other ratios and still has to meet the 2-of-3 cluster and 6-ratio gates. Operating deterioration that coexists with a write-down still shows in the other ratios.
- **Side effect on history:** masked rows also leave the company's own trailing window and the peer groups. A big write-down used to inflate a company's trailing spread and damp the z-scores of later real declines; with it removed, those declines register (ZoomInfo: net income $38.7M to $2.2M year on year, 791st to 25th; WM Technology and LFTD also rose). The caveat is that a shorter, cleaner window can give a large z to a small decline (WM Technology scores 5.0 on a -$4.2M loss), so check such names against the filing.
- **Not covered:** profitable quarters shrunk by an impairment (the label needs a net loss); impairment only reported as a year-to-date figure (Q4 overstated); the pre-tax vs. after-tax mismatch (share capped at 1); other one-time items (restructuring, fair-value changes, gains on sale, litigation); acquisitions.

Open items (found while checking the narratives): (a) **Decision: no further one-time-item flags.** Impairments (item 13) and discontinued operations (item 22) were added because each was found to be larger than the whole net result in a spot-check. Restructuring, gains/losses on sale, litigation, fair-value and tax items are an open-ended list, are usually a modest share of a result (Zillow's restructuring was about $32M of a $158M loss), and the worst cases are already blocked by `cause_unclear`. Narratives only state what is in the fact sheet, so an unflagged item makes a note incomplete rather than invented, and a person reads every note before publication. Documented as a known limitation. If another misleading note turns up, add ONE generic check instead of more categories: a large gap between `OperatingIncomeLoss` and net income would flag any below-the-operating-line item (it would not catch items booked above operating income). (b) `cause_unclear` uses a history multiple, so a swing from a large profit to a large loss can slip through (Incyte 2026Q3: net loss $784.8M against typical profits is narrated without a cause). (c) `is_derived_quarter` is true for most rows because operating cash flow is derived from year-to-date facts, so most notes end with the same derived-figures sentence; per-concept derivation flags would make it specific. (d) a per-cluster minimum ratio count (item 19, known remaining gap in the scoring gate); FMC's -$2.07B loss on $105.9M revenue is unexplained by XBRL facts (read the 10-Q before using it in a narrative); acquisition detection; a broader one-time-items sweep (restructuring, gains/losses on sale, litigation, fair-value changes); the Ollama narrative layer (not started; it should consume `is_impairment_driven` (with `net_margin_unmasked` and `return_on_assets_unmasked` for context), `is_derived_q4` and `is_financial_sector`, and must not describe a masked row's loss as operating decline); scheduling the refresh (deliberately not set up).

## EDGAR API limits

The SEC enforces a limit of **10 requests per second** per IP and requires a descriptive `User-Agent`. `edgar_client.py` throttles to 9 requests per second and sends `SEC_USER_AGENT` with every request.

## Documentation status

- `edgar-docs/` is upstream SEC API reference material and is current.
- `my-docs/` holds design notes written for an earlier version of the pipeline. They describe a curated `financial_fact` table of ~45 standardized metrics with restatement resolution, and a 13F institutional-holdings pipeline. Neither exists in the current code or database: the load is a raw dump into the `entity` / `"10k"` / `"10q"` schemas, there is no 13F ETL in `src/` (`edgar_client.py` still has the 13F helper methods), and the database has no 13F tables. Treat those notes as historical.
- The current schema is documented in `src/db.py`'s module docstring and DDL.

## References

- [EDGAR API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Accessing EDGAR data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- [SEC Developer Resources](https://www.sec.gov/about/developer-resources)
