"""Curated mapping of fundamental concepts to prioritized XBRL tags.

Role in the pipeline
--------------------
This module is pure configuration (no I/O, no functions). It is the single
place that says *which raw XBRL tags mean which fundamental concept*:

    tag_map.py  ->  feature_extraction.py  ->  features.parquet
                                                    |
                                                    v
                    scoring.py (also uses is_financial_sic)

``feature_extraction.py`` uses TAG_MAP / ALL_TAGS / DURATION_CONCEPTS to pull
and normalize facts from ``"10q".financial_fact_raw``; ``scoring.py`` uses
is_financial_sic() (SIC 6000-6799 minus real-estate operators) to label rows from sectors where missing
revenue-based ratios are expected.

Provenance of the tag list
--------------------------
Built from an actual usage survey of the live ``"10q".financial_fact_raw``
table (2026-11-12), not guessed from the XBRL spec. The trailing comments on
each tag below are ``(fact rows / distinct companies)`` at that time, which
is how fallbacks were chosen and ordered.

Priority semantics
------------------
Tag order within each concept is priority order. When one company reports the
same concept under several tags for the same period, the tag that appears
*earliest* in the list wins (see ``_dedupe_to_canonical`` in
feature_extraction.py). Later tags only fill gaps the earlier ones leave. This
mirrors real filer behavior: e.g. revenue is tagged
``RevenueFromContractWithCustomerExcludingAssessedTax`` by post-ASC-606
filers but ``Revenues`` by many others.

Known coverage limits (by design, not bugs)
-------------------------------------------
* Coverage is uneven: banks, REITs, insurers and blank-check shells rarely tag
  ``Revenues`` / ``GrossProfit`` / ``SG&A`` cleanly (see
  FINANCIAL_SECTOR_SIC_RANGE), so ratios built on those concepts come back
  NaN for a large share of companies. Treat that as missing data; do not force
  a tag match that isn't there.
* Only ``us-gaap`` tags are used. ``ifrs-full`` tags were observed for exactly
  one company in the survey and are excluded (not worth the row-duplication
  risk for 1 CIK out of ~5,300). Revisit if the universe shifts toward foreign
  private issuers.
* Fiscal Q4 exists only in the 10-K, not the 10-Q data. feature_extraction.py
  fills it in from ``"10k"`` (balance sheet directly; flows from a reported
  Q4 or as FY minus Q1-Q3). The same tag map applies to both schemas.
* Impairment concepts (``IMPAIRMENT_CONCEPTS``) are not scored; they only feed
  the ``impairment_pct_of_loss`` / ``is_impairment_driven`` labels.

Extending
---------
To add a concept: add it to TAG_MAP, classify it in exactly one of
DURATION_CONCEPTS / INSTANT_CONCEPTS (the module-level assert enforces this),
then derive any ratio from it in ``add_derived_ratios`` and, if it should be
scored, add it to ``RATIO_DIRECTION`` in scoring.py.
"""
from __future__ import annotations

# concept -> [("taxonomy", "tag"), ...] in priority order (first = preferred).
TAG_MAP: dict[str, list[tuple[str, str]]] = {
    # ── Income statement / cash flow (duration facts — must pass the
    #    single-quarter period-duration filter) ──
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),  # 119,647 / 2,807
        ("us-gaap", "Revenues"),                                            #  85,818 / 2,354
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"), #  24,804 /   783
    ],
    "gross_profit": [
        ("us-gaap", "GrossProfit"),  # 99,000 / 2,291
    ],
    "net_income": [
        ("us-gaap", "NetIncomeLoss"),  # 287,439 / 5,231 — best-covered income-statement tag
    ],
    "sga_expense": [
        ("us-gaap", "SellingGeneralAndAdministrativeExpense"),  # 88,427 / 1,945
    ],
    "operating_cash_flow": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),  # 151,718 / 5,265
    ],

    # ── Non-cash impairment charges (duration facts). NOT scored: used only to
    #    label rows whose net loss is mostly a write-down (see
    #    ``impairment_pct_of_loss`` / ``is_impairment_driven`` in
    #    feature_extraction.py). Tag choice comes from a usage survey of
    #    ``"10q".financial_fact_raw`` (2026-11-14); counts are
    #    (fact rows / distinct companies). Kept as three concepts so a company
    #    reporting e.g. both goodwill and asset impairments gets both counted
    #    (tags inside a concept are fallbacks, only the first present is used).
    #    Combined "restructuring and impairment" tags are deliberately left out:
    #    they are not pure impairments (see the one-time-items sweep follow-up).
    "goodwill_impairment": [
        ("us-gaap", "GoodwillImpairmentLoss"),               # 19,698 / 1,480
        ("us-gaap", "GoodwillAndIntangibleAssetImpairment"), #  4,515 /   426 — fallback, also covers intangibles
    ],
    "asset_impairment": [
        ("us-gaap", "AssetImpairmentCharges"),                  # 23,828 / 1,526
        ("us-gaap", "ImpairmentOfLongLivedAssetsHeldForUse"),   #  9,081 /   709
        ("us-gaap", "ImpairmentOfRealEstate"),                  #  3,907 /   179
        ("us-gaap", "TangibleAssetImpairmentCharges"),          #  2,955 /   291
        ("us-gaap", "OtherAssetImpairmentCharges"),             #  2,815 /   295
        ("us-gaap", "ImpairmentOfLongLivedAssetsToBeDisposedOf"), # 4,817 /   505
    ],
    "intangible_impairment": [
        ("us-gaap", "ImpairmentOfIntangibleAssetsExcludingGoodwill"),                 # 5,933 / 530
        ("us-gaap", "ImpairmentOfIntangibleAssetsFinitelived"),                       # 5,570 / 585
        ("us-gaap", "ImpairmentOfIntangibleAssetsIndefinitelivedExcludingGoodwill"),  # 3,455 / 364
    ],

    # ── Discontinued operations (duration fact, signed: a loss is negative).
    #    NOT scored: labels rows whose net income is mostly the result of a
    #    discontinued business (see ``is_discontinued_ops_driven``). Sparse like
    #    the impairments: absent means none. Survey of ``"10q".financial_fact_raw``
    #    (2026-11-14), (fact rows / distinct companies).
    "discontinued_ops_income": [
        ("us-gaap", "IncomeLossFromDiscontinuedOperationsNetOfTax"),                                # 11,407 / 621
        ("us-gaap", "IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity"),  # 10,688 / 556 — fallback
    ],

    # ── Balance sheet (instant facts — period_start is NULL, so the duration
    #    filter does not apply) ──
    "total_assets": [
        ("us-gaap", "Assets"),  # 159,498 / 5,325
    ],
    "total_liabilities": [
        ("us-gaap", "Liabilities"),  # 134,851 / 4,792
    ],
    "current_assets": [
        ("us-gaap", "AssetsCurrent"),  # 125,652 / 4,361
    ],
    "current_liabilities": [
        ("us-gaap", "LiabilitiesCurrent"),  # 125,499 / 4,344
    ],
    "receivables": [
        ("us-gaap", "AccountsReceivableNetCurrent"),  # 81,989 / 3,045
    ],
    "inventory": [
        ("us-gaap", "InventoryNet"),  # 68,381 / 2,373
    ],
    "long_term_debt": [
        ("us-gaap", "LongTermDebtNoncurrent"),  # 46,590 / 1,952
        ("us-gaap", "LongTermDebt"),            # 41,163 / 2,251 — fallback, some filers skip the Noncurrent-suffixed tag
    ],
    "stockholders_equity": [
        ("us-gaap", "StockholdersEquity"),  # 304,067 / 5,122 — highest-volume tag of all (components reported per-line too)
    ],
}

# Concepts that are income-statement / cash-flow *flows* reported over a
# period. Each fact carries a (period_start, period_end) pair and the same tag
# is used for a single quarter AND for year-to-date cumulative totals, so these
# must be filtered to single-quarter duration (75-100 days, see
# _QUARTER_DURATION_DAYS in feature_extraction.py) or later-quarter YTD figures
# silently inflate them 2-4x.
DURATION_CONCEPTS = frozenset({
    "revenue", "gross_profit", "net_income", "sga_expense", "operating_cash_flow",
    "goodwill_impairment", "asset_impairment", "intangible_impairment",
    "discontinued_ops_income",
})

# The subset of DURATION_CONCEPTS that are impairment charges. They are sparse
# by nature (a company reports one only in the quarter it takes it; the tag is
# simply absent otherwise), so a missing quarter means "none", not "unknown".
# feature_extraction.py therefore derives their fiscal Q4 as FY minus whichever
# of Q1-Q3 are reported (missing = 0) instead of requiring all three quarters.
IMPAIRMENT_CONCEPTS = frozenset({"goodwill_impairment", "asset_impairment", "intangible_impairment"})

# Every sparse "absent means zero" flow concept: the impairments plus the signed
# discontinued-operations result. These get the sparse fiscal-Q4 rule (FY minus
# whichever of Q1-Q3 were reported), are never allowed to create a row of their
# own, and are label-only (never scored). Only the impairments must be >= 0.
SPARSE_FLOW_CONCEPTS = IMPAIRMENT_CONCEPTS | frozenset({"discontinued_ops_income"})

# Duration concepts whose 10-Q facts are reported YEAR-TO-DATE ONLY (the cash
# flow statement shows 3-, 6- and 9-month cumulative figures, almost never a
# standalone Q2 or Q3). The single-quarter duration filter would discard
# nearly all of them, so feature_extraction.py derives each quarter from
# adjacent cumulative facts instead (Q2 = 6M - 3M, Q3 = 9M - 6M, Q4 = FY - 9M).
# Income-statement concepts are not listed: they have good standalone coverage.
YTD_DERIVED_CONCEPTS = frozenset({"operating_cash_flow"})

# Concepts that are balance-sheet *snapshots* as of period_end (period_start is
# NULL in the source data), so no duration filtering applies.
INSTANT_CONCEPTS = frozenset({
    "total_assets", "total_liabilities", "current_assets", "current_liabilities",
    "receivables", "inventory", "long_term_debt", "stockholders_equity",
})

# Guard against a concept being added to TAG_MAP without being classified (or
# classified twice) — an unclassified concept would silently skip, or wrongly
# receive, the quarter-duration filter.
assert DURATION_CONCEPTS | INSTANT_CONCEPTS == TAG_MAP.keys()

# Financial-sector definition, by SIC CODE (not description text). Revenue /
# gross-profit / SG&A-based ratios are expected to be sparse or meaningless for
# banks, brokers, insurers, REITs and blank-check shells, which use different
# statement structures than operating companies. Used by scoring.py to set the
# ``is_financial_sector`` column, so a reader can tell "NaN because it's a bank"
# from "NaN because of a data problem", and to keep those rows out of the printed
# rankings. Matching the numeric range (SIC divisions 60-67: finance, insurance,
# real estate) instead of an exact description list is deliberate: the earlier
# description list missed variants (title insurers, savings institutions,
# personal credit, mineral royalty traders) and let ~9,000 such rows through.
FINANCIAL_SECTOR_SIC_RANGE = (6000, 6799)

# Real-estate OPERATING businesses carved out of the range: unlike banks and
# REITs they have real revenue, cost of sales, inventory (land / homes under
# development) and receivables, so the pipeline's ratios apply to them. Chosen
# from a survey of the SIC codes present in entity.entity (2026-11-14) and how
# often each ratio exists for them: 6552 land subdividers & developers
# (revenue 96% of rows, inventory/revenue 19%) and 6531 real estate agents &
# managers (revenue 91%, DSO 47%; e.g. brokerages). NOT carved out: 6510-6519
# lessors/operators of buildings and the generic 6500 "Real Estate" bucket,
# which are rental / holding structures with almost no cost of sales or
# inventory (gross margin present on only 4-32% of rows), much like REITs.
FINANCIAL_SECTOR_SIC_EXCEPTIONS = frozenset({6531, 6552})


def is_financial_sic(sic_code) -> bool:
    """True if a SIC code is in the financial-sector range and not a carved-out operator.

    Args:
        sic_code: SIC code as stored in entity.entity (string or number); a
            missing / non-numeric value returns False.
    """
    try:
        code = int(str(sic_code).strip())
    except (TypeError, ValueError):
        return False
    return FINANCIAL_SECTOR_SIC_RANGE[0] <= code <= FINANCIAL_SECTOR_SIC_RANGE[1] and code not in FINANCIAL_SECTOR_SIC_EXCEPTIONS

# Flat set of every (taxonomy, tag) pair in TAG_MAP. Used to restrict the SQL
# pull to exactly the curated tags instead of the full table (which holds
# every tag any filer ever used — 7,938 distinct us-gaap tags alone).
ALL_TAGS: frozenset[tuple[str, str]] = frozenset(
    tag for tags in TAG_MAP.values() for tag in tags
)
