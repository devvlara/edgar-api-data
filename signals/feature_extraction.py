"""Builds a wide per-company, per-quarter fundamentals feature table from
``"10q".financial_fact_raw``.

Role in the pipeline
--------------------
    tag_map.py  ->  feature_extraction.py  ->  features.parquet  ->  scoring.py

Input : the raw XBRL fact tables ``"10q".financial_fact_raw`` (quarters Q1-Q3)
        and ``"10k".financial_fact_raw`` (fiscal Q4), duplicated by design
        across filings, restricted to the curated tags in tag_map.py.
Output: ``features.parquet`` — one row per ``(cik, period_end)``, one column
        per fundamental concept (revenue, net_income, ...), plus the derived
        ratios listed under "Output columns" below.

Data-quality problems this file solves
--------------------------------------
Found by querying the live ``"10q"`` schema on 2026-11-12 (not assumed from
the XBRL spec):

1. **Year-to-date duplication.** Duration facts (revenue, net income, SG&A,
   operating cash flow) are tagged identically whether they cover one quarter
   or a year-to-date cumulative period. A company's Q3 ``NetIncomeLoss`` and
   its 9-month YTD ``NetIncomeLoss`` share the same tag and period_end,
   differing only in period_start. Treating both as "this quarter's income"
   would inflate later quarters 2-4x. Fixed by the span filter
   (``_QUARTER_DURATION_DAYS``).

2. **Cross-accession duplication.** The same (cik, tag, period_end) fact is
   re-reported in later filings as a prior-year comparative. Summing or
   averaging across accessions double-counts. Fixed by keeping the filing
   whose ``filed_date`` is closest to period_end (the original report, not a
   later restatement).

3. **Corrupt dates.** At least one source row had a period/filed date in year
   6016. Fixed by bounding ``period_end`` to ``[_PERIOD_LOW, today]`` in SQL.

4. **Near-zero revenue.** Ratios with revenue as the denominator explode as
   revenue approaches zero (observed: net_margin of -31,791%). Fixed by
   masking those ratios to NaN below a revenue floor (``_usable_revenue``).

5. **Missing fiscal Q4.** Fiscal Q4 has no 10-Q; it appears only in the 10-K.
   It is filled in from ``"10k"`` (``_add_fiscal_q4``):
   * balance-sheet concepts: the 10-K's fiscal-year-end value is the Q4 snapshot;
   * flow concepts: the 10-K's standalone Q4 figure if it reports one,
     otherwise ``Q4 = FY - (Q1 + Q2 + Q3)`` (``_derive_q4_flows``), and only
     when all three quarters are present, contiguous and on the same XBRL tag.
     Otherwise Q4 is NaN, never estimated.
   Quarters are matched to a fiscal year by dates, not by the ``fiscal_year``
   label, because on a fact that label is the *filing's* fiscal year (the same
   quarter appears under several values). Growth/delta comparisons are still
   masked to NaN where the calendar span between periods is implausible
   (``span_ok``), which now only affects companies with genuine history gaps.

Output columns
--------------
Keys            : ``cik``, ``period_end``
Flag            : ``is_derived_quarter`` — True when any flow value on the row was
                  derived by subtraction: a fiscal Q4 (FY minus Q1-Q3, or FY minus
                  the 9-month figure) or a Q2/Q3 derived from year-to-date
                  cumulative facts (operating cash flow only; see
                  ``tag_map.YTD_DERIVED_CONCEPTS``). Superset of ``is_derived_q4``.
Flag            : ``is_derived_q4`` — True when at least one flow value on the
                  row was derived by subtraction (FY minus Q1-Q3). Such values
                  mix reporting bases (a recast quarter makes the difference
                  absorb the restatement) and are less reliable than a directly
                  reported quarter. Directly reported Q4 values and balance-sheet
                  values do not set it.
Raw concepts    : one column per key of ``tag_map.TAG_MAP`` (NaN if not
                  reported that quarter)
Derived ratios  : ``gross_margin``, ``net_margin``, ``sga_pct_revenue``,
                  ``dso_days``, ``inventory_to_revenue``,
                  ``ocf_to_assets``, ``current_ratio``,
                  ``long_term_debt_to_assets``, ``revenue_qoq_growth``,
                  ``revenue_yoy_growth``, ``return_on_assets``
Label          : ``cause_unclear`` — True for an unusually large net loss (>= 10% of
                  total assets and >= 5x the company's own median |net income|;
                  >= 25% of assets when there is no history) that impairments
                  explain less than half of. The data cannot say why; narratives
                  must not guess (see ``CAUSE_UNCLEAR_*``).
Label          : ``discontinued_ops_income`` (signed after-tax result of discontinued
                  operations, 0 if none), ``income_continuing_ops`` (net income minus
                  it) and ``is_discontinued_ops_driven`` (its size is at least half
                  of net income's). Not scored itself; on ``is_discontinued_ops_driven``
                  rows ``net_margin`` / ``return_on_assets`` are masked to NaN like
                  impairment-driven rows (originals in ``*_unmasked``).
Distress label  : ``is_negative_equity`` (stockholders_equity <= 0; never scored)
Impairment label: ``impairment_total`` (USD, goodwill + asset + intangible
                  impairment charges reported for the quarter; 0 if none),
                  ``impairment_pct_of_loss`` (impairment / |net loss|, capped at
                  1; NaN unless the quarter is a net loss), ``is_impairment_driven``
                  (True when that share is at least IMPAIRMENT_DRIVEN_THRESHOLD,
                  0.5). ``net_margin`` and ``return_on_assets`` are masked to NaN on
                  ``is_impairment_driven`` rows; the original values are kept in
                  ``net_margin_unmasked`` / ``return_on_assets_unmasked``.

Missing values are always NaN, never imputed or zero-filled. NaN means "not
meaningfully computable" (no data, sub-floor revenue, or a gap in history) and
scoring.py treats it as absent rather than as a good or bad value.

Usage
-----
    python3 -m signals.feature_extraction [--out PATH]

Run from the ``edgar`` project folder. ``--out`` defaults to
``signals/features.parquet`` regardless of the working directory.

Requirements
------------
Reuses ``src.db.get_connection()`` (same ``.env`` / Postgres settings as
etl_financial.py). Needs pandas and pyarrow
(``pip3 install pandas pyarrow --break-system-packages``).
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

import numpy as np
import pandas as pd

from pathlib import Path

# Folder containing this file; default parquet outputs are written here so the
# result location doesn't depend on the caller's working directory.
HERE = Path(__file__).resolve().parent

from src import db
from signals.tag_map import (ALL_TAGS, DURATION_CONCEPTS, IMPAIRMENT_CONCEPTS, INSTANT_CONCEPTS,
                             SPARSE_FLOW_CONCEPTS, TAG_MAP, YTD_DERIVED_CONCEPTS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# Sanity bound on period_end — the source data has at least one corrupted
# filing date (observed: a period/filed date in year 6016). Anything before
# EDGAR's XBRL mandate or after today is dropped rather than silently kept.
_PERIOD_LOW = date(2009, 1, 1)

# A single fiscal quarter is ~91 days. Widened on both sides to tolerate
# fiscal calendars that don't align to exact 91-day boundaries (4-4-5 retail
# calendars, 52/53-week years, etc.) while still rejecting half-year/YTD facts
# (~180 / ~270 days). Inclusive (min_days, max_days) on period_end - period_start.
_QUARTER_DURATION_DAYS = (75, 100)

# A 10-K's full-year duration facts span ~365 days (52/53-week years give
# 357-371). Used to keep the fiscal-year total and reject the quarter-length
# comparative facts a 10-K also carries under the same tags.
_FULL_YEAR_DURATION_DAYS = (350, 380)

# Slack (days) allowed when matching the three 10-Q quarters to a 10-K's fiscal
# year: the first quarter must start within this many days of the fiscal-year
# start, and consecutive quarters may be separated by at most this many days.
_Q4_ALIGN_TOLERANCE_DAYS = 7

# Year-to-date spans (days) for concepts reported cumulatively only (see
# tag_map.YTD_DERIVED_CONCEPTS): ~6 months and ~9 months. Slightly wider than
# 182 / 273 to tolerate 52/53-week and 4-4-5 calendars.
_HALF_YEAR_DURATION_DAYS = (170, 195)
_NINE_MONTH_DURATION_DAYS = (255, 290)


def _fetch_raw_facts(conn, schema: str = "10q") -> pd.DataFrame:
    """Pull every fact row for the tags in TAG_MAP, with sane period bounds.

    ``schema`` selects the form-type schema to read: ``"10q"`` (quarterly
    facts) or ``"10k"`` (annual facts, used to fill in fiscal Q4).

    Filters taxonomy+tag to exactly the curated set (ALL_TAGS) rather than
    pulling the full table — ``"10q".financial_fact_raw`` holds every tag any
    filer ever used (7,938 distinct us-gaap tags alone), most of which are
    irrelevant here.

    The (taxonomy, tag) match is done as a join against two parallel
    ``unnest``-ed arrays. Postgres cannot hash an array of anonymous tuples,
    so ``(taxonomy, tag) = ANY(%s)`` fails with "could not identify a hash
    function for type unknown".

    Args:
        conn: open psycopg2 connection (see ``src.db.get_connection``).
        schema: ``"10q"`` or ``"10k"``.

    Returns:
        DataFrame with columns cik, taxonomy, tag, value, period_start,
        period_end, accession_number, filed_date, fiscal_year, fiscal_period,
        form. ``fiscal_year``/``fiscal_period`` are carried for traceability
        only: on a fact they describe the *filing's* fiscal year, not the
        fact's own period (the same fact reappears in later filings under a
        later fiscal_year), so they are NOT used to group quarters. ``value`` is float64
        (Postgres returns Decimal, which breaks 0/0 division in pandas).
        Rows with NULL value, ``period_end`` before ``_PERIOD_LOW`` or after
        today are excluded.
    """
    if schema not in ("10q", "10k"):
        raise ValueError(f"schema must be '10q' or '10k', got {schema!r}")
    tag_tuples = list(ALL_TAGS)
    taxonomies = [t for t, _ in tag_tuples]
    tags = [g for _, g in tag_tuples]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.cik, f.taxonomy, f.tag, f.value, f.period_start, f.period_end,
                   f.accession_number, f.filed_date,
                   f.fiscal_year, f.fiscal_period, f.form
            FROM "{schema}".financial_fact_raw f
            JOIN unnest(%s::text[], %s::text[]) AS t(taxonomy, tag)
              ON f.taxonomy = t.taxonomy AND f.tag = t.tag
            WHERE f.period_end >= %s
              AND f.period_end <= CURRENT_DATE
              AND f.value IS NOT NULL
            """.replace("{schema}", schema),
            (taxonomies, tags, _PERIOD_LOW),
        )
        cols = [c.name for c in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    df["value"] = pd.to_numeric(df["value"], errors="coerce").astype("float64")
    log.info(f"Fetched {len(df):,} raw {schema} fact rows across {df['cik'].nunique():,} CIKs")
    return df


def _concept_for_tag(taxonomy: str, tag: str) -> str | None:
    """Return the TAG_MAP concept a (taxonomy, tag) pair belongs to, else None."""
    for concept, tags in TAG_MAP.items():
        if (taxonomy, tag) in tags:
            return concept
    return None


def _tag_priority(concept: str, taxonomy: str, tag: str) -> int:
    """Index of the tag in its concept's priority list (0 = most preferred)."""
    return TAG_MAP[concept].index((taxonomy, tag))


def _dedupe_to_canonical(df: pd.DataFrame, duration_days: tuple[int, int] = _QUARTER_DURATION_DAYS) -> pd.DataFrame:
    """Collapse raw facts to one canonical value per (cik, concept, period_end).

    Steps:
      1. Map each (taxonomy, tag) to its concept and drop unmapped rows.
      2. Duration concepts only: keep facts whose period_start -> period_end
         span is within ``duration_days`` — a single quarter
         (``_QUARTER_DURATION_DAYS``, the default, for 10-Q facts) or a full
         year (``_FULL_YEAR_DURATION_DAYS`` for 10-K facts). This drops the
         YTD/half-year cumulative facts, or the quarter-length comparatives a
         10-K carries, that share the same tag. Instant concepts have no
         period_start and skip this filter entirely.
      3. Sort by (tag priority, then recency) and keep the first row, so that:
         * the highest-priority tag for the concept wins, and
         * among rows with that tag, the filing whose ``filed_date`` is
           closest to ``period_end`` wins (the original report, not a later
           comparative restatement).

    Args:
        df: output of ``_fetch_raw_facts``.
        duration_days: inclusive (min, max) span accepted for duration concepts.

    Returns:
        DataFrame with one row per (cik, concept, period_end), keeping the
        original columns plus ``concept``, ``recency`` and ``priority``.
    """
    df = df.copy()
    df["concept"] = [
        _concept_for_tag(t, g) for t, g in zip(df["taxonomy"], df["tag"])
    ]
    df = df.dropna(subset=["concept"])

    is_duration = df["concept"].isin(DURATION_CONCEPTS)
    has_start = df["period_start"].notna()
    span_days = (pd.to_datetime(df["period_end"]) - pd.to_datetime(df["period_start"])).dt.days

    quarter_ok = (~is_duration) | (
        has_start & span_days.between(*duration_days)
    )
    dropped = len(df) - quarter_ok.sum()
    log.info(f"Dropping {dropped:,} rows outside the {duration_days[0]}-{duration_days[1]} day duration filter")
    df = df[quarter_ok].copy()

    # Recency: prefer the filing that first reported this period. filed_date
    # before period_end shouldn't exist, but guard with abs() rather than
    # trusting the sign if a filer's metadata is off.
    df["recency"] = (pd.to_datetime(df["filed_date"]) - pd.to_datetime(df["period_end"])).abs()
    df["priority"] = [
        _tag_priority(c, t, g) for c, t, g in zip(df["concept"], df["taxonomy"], df["tag"])
    ]

    df = df.sort_values(["cik", "concept", "period_end", "priority", "recency"])
    canonical = df.drop_duplicates(subset=["cik", "concept", "period_end"], keep="first")
    log.info(f"Collapsed to {len(canonical):,} canonical (cik, concept, period_end) facts")
    return canonical


def _derive_q4_flows(fy: pd.DataFrame, quarters: pd.DataFrame) -> pd.DataFrame:
    """Derive standalone fiscal-Q4 flow values: ``Q4 = FY - (Q1 + Q2 + Q3)``.

    A 10-K reports only the full-year total for flow concepts (revenue, net
    income, ...), never a standalone Q4, and Q4 has no 10-Q. So Q4 is derived
    from the 10-K total and the three 10-Q quarters of the same fiscal year.

    Quarters are matched to a fiscal year by DATES, not by the
    ``fiscal_year`` / ``fiscal_period`` labels: on a fact those labels carry
    the fiscal year of the *filing* that reported it, so one quarter appears
    under several fiscal_year values.

    A Q4 value is produced only when ALL of the following hold; otherwise
    nothing is emitted for that company-year-concept (NaN downstream, never an
    estimate):

    * exactly three single-quarter facts for the same cik, concept AND
      (taxonomy, tag) fall inside the fiscal year (same tag so the four
      numbers are on the same reporting basis);
    * the first quarter starts within ``_Q4_ALIGN_TOLERANCE_DAYS`` of the
      fiscal-year start, and consecutive quarters are contiguous (gap of 0 to
      ``_Q4_ALIGN_TOLERANCE_DAYS`` days);
    * the implied Q4 (last quarter end -> fiscal-year end) lasts a plausible
      quarter (``_QUARTER_DURATION_DAYS``).

    Args:
        fy: canonical 10-K duration facts (full-year spans), from
            ``_dedupe_to_canonical(..., _FULL_YEAR_DURATION_DAYS)``.
        quarters: canonical 10-Q duration facts (single-quarter spans).

    Returns:
        One row per derived Q4 fact with columns cik, concept, taxonomy, tag,
        value, period_start (the day after Q3 ends), period_end (the
        fiscal-year end).
    """
    key = ["cik", "concept", "taxonomy", "tag", "period_start", "period_end", "value"]
    f = fy[key].copy()
    f.columns = ["cik", "concept", "taxonomy", "tag", "fy_start", "fy_end", "fy_value"]
    f["fy_start_d"] = pd.to_datetime(f["fy_start"])
    f["fy_end_d"] = pd.to_datetime(f["fy_end"])

    q = quarters[["cik", "concept", "taxonomy", "tag", "period_start", "period_end", "value"]].copy()
    q.columns = ["cik", "concept", "taxonomy", "tag", "q_start", "q_end", "q_value"]
    q["q_start_d"] = pd.to_datetime(q["q_start"])
    q["q_end_d"] = pd.to_datetime(q["q_end"])

    tol = pd.Timedelta(days=_Q4_ALIGN_TOLERANCE_DAYS)
    m = f.merge(q, on=["cik", "concept", "taxonomy", "tag"])
    # Quarters that fall inside this fiscal year, with room left for a Q4.
    m = m[(m["q_start_d"] >= m["fy_start_d"] - tol) & (m["q_end_d"] < m["fy_end_d"] - pd.Timedelta(days=30))]

    gkey = ["cik", "concept", "taxonomy", "tag", "fy_start", "fy_end", "fy_value"]
    m = m.sort_values(gkey + ["q_end_d"])
    m["r"] = m.groupby(gkey).cumcount()
    m = m[m.groupby(gkey)["r"].transform("size") == 3]
    if m.empty:
        return pd.DataFrame(columns=["cik", "concept", "taxonomy", "tag", "value", "period_start", "period_end"])

    w = m.set_index(gkey + ["r"])[["q_value", "q_start_d", "q_end_d"]].unstack("r")
    q_start = [w[("q_start_d", i)] for i in range(3)]
    q_end = [w[("q_end_d", i)] for i in range(3)]
    fy_start_d = pd.to_datetime(w.index.get_level_values("fy_start"))
    fy_end_d = pd.to_datetime(w.index.get_level_values("fy_end"))

    ok = (q_start[0].values >= fy_start_d - tol) & (q_start[0].values <= fy_start_d + tol)
    for i in (1, 2):
        gap = (q_start[i].values - q_end[i - 1].values) / pd.Timedelta(days=1)
        ok &= (gap >= 0) & (gap <= _Q4_ALIGN_TOLERANCE_DAYS)
    q4_days = (fy_end_d.values - q_end[2].values) / pd.Timedelta(days=1)
    ok &= (q4_days >= _QUARTER_DURATION_DAYS[0]) & (q4_days <= _QUARTER_DURATION_DAYS[1])

    fy_value = w.index.get_level_values("fy_value").to_numpy(dtype="float64")
    q_sum = sum(w[("q_value", i)].to_numpy(dtype="float64") for i in range(3))
    out = w.index.to_frame(index=False)[["cik", "concept", "taxonomy", "tag", "fy_end"]].rename(columns={"fy_end": "period_end"})
    out["value"] = fy_value - q_sum
    out["period_start"] = (q_end[2] + pd.Timedelta(days=1)).dt.date.values
    out = out[ok]
    log.info(f"Derived {len(out):,} fiscal-Q4 flow facts (FY minus Q1-Q3)")
    return out.reset_index(drop=True)


def _derive_q4_sparse(fy: pd.DataFrame, quarters: pd.DataFrame) -> pd.DataFrame:
    """Derive fiscal-Q4 values for sparse (impairment) concepts: ``FY - sum(reported Q1-Q3)``.

    ``_derive_q4_flows`` needs all three quarters, which is right for revenue or
    net income (always reported) but wrong for impairments: a company reports
    one only in the quarter it is taken, and the tag is simply absent in the
    others. Absent therefore means zero here, so the quarters inside the fiscal
    year that ARE reported (0 to 3 of them, same cik / concept / tag) are
    subtracted from the full-year figure. Annual impairment tests are usually
    run in Q4, so skipping this would miss most Q4 write-downs.

    Known risk: if a company reports an earlier quarter's impairment only as a
    year-to-date figure (no single-quarter fact), that quarter is treated as
    zero and Q4 is overstated. A negative result (recast comparatives) is
    dropped rather than clipped.

    Args:
        fy: canonical 10-K full-year duration facts for impairment concepts.
        quarters: canonical 10-Q single-quarter facts for impairment concepts.

    Returns:
        Columns cik, concept, period_end, value (one row per derived Q4).
    """
    cols = ["cik", "concept", "period_end", "value"]
    if fy.empty:
        return pd.DataFrame(columns=cols)
    f = fy[["cik", "concept", "taxonomy", "tag", "period_start", "period_end", "value"]].copy()
    f.columns = ["cik", "concept", "taxonomy", "tag", "fy_start", "fy_end", "fy_value"]
    f["fy_start_d"] = pd.to_datetime(f["fy_start"])
    f["fy_end_d"] = pd.to_datetime(f["fy_end"])
    q = quarters[["cik", "concept", "taxonomy", "tag", "period_start", "period_end", "value"]].copy()
    q.columns = ["cik", "concept", "taxonomy", "tag", "q_start", "q_end", "q_value"]
    q["q_start_d"] = pd.to_datetime(q["q_start"])
    q["q_end_d"] = pd.to_datetime(q["q_end"])

    tol = pd.Timedelta(days=_Q4_ALIGN_TOLERANCE_DAYS)
    m = f.merge(q, on=["cik", "concept", "taxonomy", "tag"], how="left")
    inside = (m["q_start_d"] >= m["fy_start_d"] - tol) & (m["q_end_d"] < m["fy_end_d"] - pd.Timedelta(days=30))
    m["q_value"] = m["q_value"].where(inside)
    g = m.groupby(["cik", "concept", "taxonomy", "tag", "fy_end", "fy_value"], dropna=False)["q_value"].sum(min_count=1)
    out = g.reset_index()
    out["value"] = out["fy_value"].astype("float64") - out["q_value"].fillna(0.0).astype("float64")
    # A negative impairment difference means recast comparatives, so drop it; the
    # discontinued-operations result is signed, so a negative value is valid.
    out = out[(out["value"] >= 0) | ~out["concept"].isin(IMPAIRMENT_CONCEPTS)].rename(columns={"fy_end": "period_end"})
    log.info(f"Derived {len(out):,} fiscal-Q4 impairment facts (FY minus reported Q1-Q3, missing = 0)")
    return out[cols].reset_index(drop=True)


def _derive_ytd_flows(raw_q: pd.DataFrame, canon_q: pd.DataFrame, canon_k: pd.DataFrame) -> pd.DataFrame:
    """Derive standalone quarters from adjacent year-to-date facts (cash-flow items).

    For concepts in ``YTD_DERIVED_CONCEPTS`` the 10-Q reports only cumulative
    figures, so each quarter is the difference of two adjacent cumulatives that
    share a fiscal-year start::

        Q2 = 6-month YTD - 3-month YTD (the 3-month figure is Q1)
        Q3 = 9-month YTD - 6-month YTD
        Q4 = full year (10-K) - 9-month YTD

    Each step needs only TWO adjacent facts, so one missing quarter does not
    break the others (the Q1+Q2+Q3 chain used for ordinary flows does). Facts
    are matched by DATES, not by ``fiscal_year`` labels: same cik, concept and
    (taxonomy, tag); period starts within ``_Q4_ALIGN_TOLERANCE_DAYS``; and the
    later period ends 75-110 days after the earlier one.

    Q1 needs no derivation (the 3-month fact is the standalone quarter and is
    already in ``canon_q``). Directly reported standalone facts take precedence
    over these derived ones; that is applied by the caller.

    Args:
        raw_q: raw 10-Q facts from ``_fetch_raw_facts``.
        canon_q: canonical 10-Q single-quarter facts (the 3-month figures).
        canon_k: canonical 10-K full-year facts.

    Returns:
        Columns cik, concept, period_end, value, kind (``"Q2"``, ``"Q3"`` or
        ``"Q4"``).
    """
    cols = ["cik", "concept", "period_end", "value", "kind"]
    tags = {t for c in YTD_DERIVED_CONCEPTS for t in TAG_MAP[c]}
    sub = raw_q[[(t, g) in tags for t, g in zip(raw_q["taxonomy"], raw_q["tag"])]]
    keep = ["cik", "concept", "taxonomy", "tag", "period_start", "period_end", "value"]
    c3 = canon_q[canon_q["concept"].isin(YTD_DERIVED_CONCEPTS)][keep]
    c6 = _dedupe_to_canonical(sub, _HALF_YEAR_DURATION_DAYS)[keep]
    c9 = _dedupe_to_canonical(sub, _NINE_MONTH_DURATION_DAYS)[keep]
    cfy = canon_k[canon_k["concept"].isin(YTD_DERIVED_CONCEPTS)][keep]

    def step(later: pd.DataFrame, earlier: pd.DataFrame, kind: str) -> pd.DataFrame:
        a = later.rename(columns={"period_start": "s1", "period_end": "e1", "value": "v1"})
        b = earlier.rename(columns={"period_start": "s0", "period_end": "e0", "value": "v0"})
        m = a.merge(b, on=["cik", "concept", "taxonomy", "tag"])
        start_gap = (pd.to_datetime(m["s1"]) - pd.to_datetime(m["s0"])).dt.days.abs()
        end_gap = (pd.to_datetime(m["e1"]) - pd.to_datetime(m["e0"])).dt.days
        m = m[(start_gap <= _Q4_ALIGN_TOLERANCE_DAYS) & end_gap.between(75, 110)]
        out = pd.DataFrame({"cik": m["cik"], "concept": m["concept"], "period_end": m["e1"],
                            "value": m["v1"].astype("float64") - m["v0"].astype("float64"), "kind": kind})
        return out.drop_duplicates(subset=["cik", "concept", "period_end"])

    out = pd.concat([step(c6, c3, "Q2"), step(c9, c6, "Q3"), step(cfy, c9, "Q4")], ignore_index=True)
    log.info("Derived %s standalone quarters from year-to-date facts (%s)",
             f"{len(out):,}", ", ".join(f"{k}: {int((out['kind'] == k).sum()):,}" for k in ("Q2", "Q3", "Q4")))
    return out[cols] if len(out) else pd.DataFrame(columns=cols)


def _pivot_wide(canonical: pd.DataFrame) -> pd.DataFrame:
    """Pivot canonical facts to one row per (cik, period_end), one column per concept.

    Every concept in TAG_MAP is guaranteed to exist as a column afterwards
    (all-NaN if no company reported it), so downstream ratio code never has to
    check for missing columns.
    """
    wide = canonical.pivot_table(
        index=["cik", "period_end"], columns="concept", values="value", aggfunc="first"
    ).reset_index()
    wide.columns.name = None
    for concept in TAG_MAP:
        if concept not in wide.columns:
            wide[concept] = pd.NA
    return wide


# Revenue-denominated ratios are meaningless when revenue is tiny (the ratio
# explodes toward infinity). A quarter's revenue counts as "usable" only if it
# clears BOTH an absolute floor and a fraction of the company's own trailing
# median revenue, so $10k means something different for a mid-cap than a shell.
# Below the floor the ratio is NaN (missing), not imputed, so the company is
# still scored on its revenue-independent ratios.
_MIN_REVENUE_ABS = 10_000        # dollars; absolute minimum usable quarterly revenue
_MIN_REVENUE_REL = 0.10          # fraction of the trailing-median revenue
_REVENUE_TRAILING_QTRS = 8       # window (in rows/quarters) for the trailing median


def _usable_revenue(df: pd.DataFrame) -> pd.Series:
    """Revenue with sub-floor quarters set to NaN.

    A quarter's revenue is kept only if it is at least
    ``max(_MIN_REVENUE_ABS, _MIN_REVENUE_REL * trailing median)``, where the
    trailing median is over the company's previous ``_REVENUE_TRAILING_QTRS``
    rows (``shift(1)`` so the current quarter never sets its own floor). A
    company with no prior history gets only the absolute floor.

    Known trade-off: a formerly healthy company whose revenue collapses below
    10% of its norm loses ALL revenue-based ratios that quarter, including
    ``revenue_yoy_growth`` (growth is computed from this same masked series),
    so the collapse itself is not scored as a signal that quarter. The company
    is still scored on its revenue-independent ratios (current_ratio,
    long_term_debt_to_assets, ocf_to_assets, return_on_assets).

    Args:
        df: wide frame sorted by (cik, period_end) with ``cik`` and ``revenue``.

    Returns:
        Float Series aligned to ``df``'s index.
    """
    rev = pd.to_numeric(df["revenue"], errors="coerce")
    trailing = rev.groupby(df["cik"]).transform(
        lambda s: s.shift(1).rolling(_REVENUE_TRAILING_QTRS, min_periods=1).median()
    )
    floor = (trailing.fillna(0) * _MIN_REVENUE_REL).clip(lower=_MIN_REVENUE_ABS)
    return rev.where(rev >= floor)


# A net-loss quarter is labelled ``is_impairment_driven`` when impairment
# charges explain at least this fraction of the loss. Not fit to outcomes; a
# judgment call (0.5 = "more than half the loss is a write-down").
IMPAIRMENT_DRIVEN_THRESHOLD = 0.5

# A row is labelled ``is_discontinued_ops_driven`` when the discontinued-operations
# result is at least this fraction of the size of net income (0.5 = half). Judgment
# call, not fit to outcomes.
DISCONTINUED_DRIVEN_THRESHOLD = 0.5

# ``cause_unclear``: a net-loss quarter whose size is unusual AND which the
# impairment tags explain less than half of, i.e. the data we hold cannot say
# why the loss happened (FMC 2026Q3: -$2.07B on $105.9M revenue, impairment only
# 14% of it). "Unusual" means BOTH large against the company's own asset base
# (loss >= 10% of total assets in one quarter) AND an outlier against its own
# history (loss >= 5x the median |net income| of its previous 8 quarters, at
# least 3 needed). With under 3 quarters of history the history test cannot be
# applied, so a stricter asset test is used instead (loss >= 25% of assets).
# A plain "loss is large vs. assets" test would flag a quarter of all loss rows,
# mostly steady cash-burning biotechs whose losses are routine; the history test
# is what separates a break in pattern from a business model. Judgment-call
# thresholds, not fit to outcomes. Label only; the narrative layer refuses to
# narrate these rows.
CAUSE_UNCLEAR_ASSET_FRAC = 0.10
CAUSE_UNCLEAR_HISTORY_MULT = 5.0
CAUSE_UNCLEAR_ASSET_FRAC_NO_HISTORY = 0.25
CAUSE_UNCLEAR_MIN_HISTORY = 3
_NI_HISTORY_QTRS = 8

# Plausible calendar spans (days) between a period_end and the one 1 / 4 rows
# earlier. Growth rates computed by row position are only valid when the span
# matches; otherwise a missing quarter (e.g. fiscal Q4, which lives only in the
# 10-K and is absent from the 10-Q data) makes them compare mismatched periods.
# Inclusive (min_days, max_days). Public: scoring.py reuses them for its deltas.
QOQ_SPAN_DAYS = (75, 110)
YOY_SPAN_DAYS = (330, 400)


def span_ok(df: pd.DataFrame, lag: int, bounds: tuple[int, int]) -> pd.Series:
    """Flag rows whose period ``lag`` rows back is the expected distance away.

    Used to guard row-position comparisons (``pct_change``, ``diff``) against
    gaps in a company's quarterly history. ``df`` must already be sorted by
    (cik, period_end); the shift never crosses company boundaries.

    Args:
        df: frame with ``cik`` and ``period_end`` columns, sorted.
        lag: number of rows back to compare against (1 = prior quarter,
            4 = same quarter last year).
        bounds: inclusive ``(min_days, max_days)`` accepted between the two
            period_end dates (e.g. ``QOQ_SPAN_DAYS``).

    Returns:
        Boolean Series aligned to ``df``. False where there is no row ``lag``
        back (first rows per company) or the span is outside ``bounds``.
    """
    pe = pd.to_datetime(df["period_end"])
    days = (pe - pe.groupby(df["cik"]).shift(lag)).dt.days
    return days.between(*bounds)


def _safe_div(numer: pd.Series, denom: pd.Series) -> pd.Series:
    """Element-wise numer / denom, with an exactly-zero denominator giving NaN.

    Only guards the exact-zero case. Tiny-but-nonzero denominators are handled
    separately for revenue (see ``_usable_revenue``).
    """
    out = numer / denom.replace(0, pd.NA)
    return out


def add_derived_ratios(wide: pd.DataFrame) -> pd.DataFrame:
    """Add the fundamentals ratios that scoring.py will z-score.

    Ratios (all per quarter; "revenue" below means floor-masked revenue):

    ====================  =========================================  =========
    column                definition                                  worse if
    ====================  =========================================  =========
    gross_margin          gross_profit / revenue                      falls
    net_margin            net_income / revenue                        falls
    sga_pct_revenue       sga_expense / revenue                       rises
    dso_days              receivables / revenue * 91                  rises
    inventory_to_revenue  inventory / revenue                         rises
    ocf_to_assets         operating_cash_flow / total_assets (> 0)    falls
    current_ratio         current_assets / current_liabilities        falls
    long_term_debt_to_assets  long_term_debt / total_assets (> 0)        rises
    revenue_qoq_growth    pct change vs. prior quarter                falls
    revenue_yoy_growth    pct change vs. same quarter last year       falls
    return_on_assets      net_income / total_assets (quarterly)       falls
    ====================  =========================================  =========

    (``revenue_qoq_growth`` is produced here but is not currently scored.)

    Also adds the boolean ``is_negative_equity`` (stockholders_equity <= 0; label
    only) and the impairment labels ``impairment_total``,
    ``impairment_pct_of_loss`` and ``is_impairment_driven`` (not scored), and
    masks ``net_margin`` / ``return_on_assets`` to NaN on impairment-driven
    rows, keeping the originals in ``net_margin_unmasked`` and
    ``return_on_assets_unmasked`` (see the comment in the body).

    Notes:
      * Revenue-based ratios are NaN for financial-sector filers (banks,
        REITs, insurers) that don't tag those concepts — see
        ``tag_map.is_financial_sic``. That is expected; scoring.py
        scores those companies on the ratios they do have.
      * Growth rates use row position after sorting, masked by ``span_ok`` so
        a missing quarter (e.g. fiscal Q4) yields NaN rather than a comparison
        across mismatched periods.
      * Sign-crossing denominators: every ratio is built on a denominator that
        cannot change sign (revenue is floor-masked to be positive; total
        assets is guarded to be positive; current liabilities are non-negative).
        Three earlier ratios failed this and were replaced, each after it was
        found hiding real declines: ``net_income_qoq_growth`` ->
        ``return_on_assets``, ``debt_to_equity`` -> ``long_term_debt_to_assets``,
        ``ocf_to_net_income`` -> ``ocf_to_assets``. Checked systematically
        across the whole ratio set, so no other sign-crossing ratio remains.

    Args:
        wide: output of ``_pivot_wide``.

    Returns:
        Copy of ``wide`` sorted by (cik, period_end) with the ratio columns added.
    """
    df = wide.sort_values(["cik", "period_end"]).copy()

    rev = _usable_revenue(df)
    df["gross_margin"] = _safe_div(df["gross_profit"], rev)
    df["net_margin"] = _safe_div(df["net_income"], rev)
    df["sga_pct_revenue"] = _safe_div(df["sga_expense"], rev)
    df["dso_days"] = _safe_div(df["receivables"], rev) * 91
    df["inventory_to_revenue"] = _safe_div(df["inventory"], rev)
    # Cash-flow return on assets (quarterly). Replaces operating_cash_flow /
    # net_income, whose denominator crosses zero in every net-loss quarter
    # (a loss with positive OCF and a loss with negative OCF both gave
    # confusing signs). Same non-positive-total-assets guard as above.
    df["ocf_to_assets"] = _safe_div(df["operating_cash_flow"], df["total_assets"].where(pd.to_numeric(df["total_assets"], errors="coerce") > 0))
    df["current_ratio"] = _safe_div(df["current_assets"], df["current_liabilities"])
    # Leverage on a denominator that cannot change sign. The earlier
    # long_term_debt / stockholders_equity flipped sign when equity went
    # negative (FMC: equity -$19M gave -204 and read as "improving"), so it was
    # replaced. A non-positive total_assets is masked to NaN (never imputed).
    df["long_term_debt_to_assets"] = _safe_div(df["long_term_debt"], df["total_assets"].where(pd.to_numeric(df["total_assets"], errors="coerce") > 0))
    # Negative equity is a distress signal in its own right and is NOT captured
    # by the ratio above (a company can have moderate debt/assets and negative
    # equity from accumulated losses or buybacks), so it gets an explicit flag.
    # Label only, never scored. Missing equity -> False.
    df["is_negative_equity"] = (pd.to_numeric(df["stockholders_equity"], errors="coerce") <= 0).fillna(False)
    # Quarterly (not annualized) return on assets. A level ratio like the others,
    # so its year-over-year change has none of the sign-flip / near-zero-base
    # blow-ups that a net-income growth rate has. Not revenue-based, so it is
    # not subject to the revenue floor.
    df["return_on_assets"] = _safe_div(df["net_income"], df["total_assets"])

    # Impairment labelling (not scored). A company reports a charge only in the
    # quarter it takes it, so a missing impairment column means zero. The charge
    # is pre-tax while net_income is after tax, so the ratio can exceed 1 and is
    # capped at 1. Only defined for a net-loss quarter (NaN when profitable or
    # net income is missing). Different concepts could in principle overlap
    # (e.g. an asset-impairment tag that already includes goodwill); the cap and
    # the first-tag-wins rule inside each concept bound the effect.
    imp = sum(pd.to_numeric(df[c], errors="coerce").fillna(0.0) for c in sorted(IMPAIRMENT_CONCEPTS))
    ni = pd.to_numeric(df["net_income"], errors="coerce")
    df["impairment_total"] = imp
    loss = ni < 0
    df["impairment_pct_of_loss"] = (imp / (-ni)).clip(lower=0, upper=1).where(loss)
    df["is_impairment_driven"] = (df["impairment_pct_of_loss"] >= IMPAIRMENT_DRIVEN_THRESHOLD).fillna(False)
    # Discontinued operations: the signed after-tax result of a business that was
    # sold or wound down. It is part of net income but not of operating
    # performance, so a row where it is at least half the size of net income is
    # labelled; ``income_continuing_ops`` (net income minus that result) shows
    # what is left. Absent means none. The columns are labels, never scored;
    # net_margin / return_on_assets ARE masked on driven rows (see below).
    disc = pd.to_numeric(df["discontinued_ops_income"], errors="coerce").fillna(0.0)
    df["discontinued_ops_income"] = disc
    df["income_continuing_ops"] = (ni - disc).where(ni.notna())
    df["is_discontinued_ops_driven"] = ((disc != 0) & (disc.abs() >= DISCONTINUED_DRIVEN_THRESHOLD * ni.abs())).fillna(False)

    # Unexplained extreme loss (see CAUSE_UNCLEAR_* above).
    hist = ni.abs().groupby(df["cik"]).transform(
        lambda x: x.shift(1).rolling(_NI_HISTORY_QTRS, min_periods=CAUSE_UNCLEAR_MIN_HISTORY).median()
    )
    assets = pd.to_numeric(df["total_assets"], errors="coerce").where(lambda x: x > 0)
    loss_size = (-ni).where(loss)
    with_hist = hist.notna() & (loss_size / assets >= CAUSE_UNCLEAR_ASSET_FRAC) & (loss_size >= CAUSE_UNCLEAR_HISTORY_MULT * hist)
    no_hist = hist.isna() & (loss_size / assets >= CAUSE_UNCLEAR_ASSET_FRAC_NO_HISTORY)
    unexplained = df["impairment_pct_of_loss"].fillna(0) < IMPAIRMENT_DRIVEN_THRESHOLD
    df["cause_unclear"] = (loss & unexplained & (with_hist | no_hist)).fillna(False)

    # An impairment lowers net income AND total assets in the same quarter, so
    # net_margin and return_on_assets are contaminated by a non-cash charge on
    # these rows. Same pattern as the revenue floor: mask to NaN (never
    # "correct"), so the other ratios and the scoring gates carry the row and
    # both headline scores inherit it. The unmasked values stay available in
    # ``*_unmasked`` for display and for the narrative layer.
    df["net_margin_unmasked"] = df["net_margin"]
    df["return_on_assets_unmasked"] = df["return_on_assets"]
    # Discontinued-operations-driven rows get the same treatment: that result is
    # part of net income but not of operating performance, so it contaminates
    # net_margin and return_on_assets in exactly the same way.
    df.loc[df["is_impairment_driven"] | df["is_discontinued_ops_driven"], ["net_margin", "return_on_assets"]] = np.nan

    # Growth: QoQ = vs. the prior row for this cik; YoY = vs. 4 rows back.
    # Compared by row position, then masked with span_ok so that a company with
    # gaps in its quarterly history (notably the missing fiscal Q4) yields NaN
    # instead of silently comparing mismatched periods as if they were adjacent.
    grp = df.groupby("cik")
    # Revenue growth uses the floor-masked revenue too: a jump off a near-zero
    # base is noise, not growth.
    df["_rev_usable"] = rev
    ugrp = df.groupby("cik")["_rev_usable"]
    ok_q, ok_y = span_ok(df, 1, QOQ_SPAN_DAYS), span_ok(df, 4, YOY_SPAN_DAYS)
    df["revenue_qoq_growth"] = ugrp.pct_change(1, fill_method=None).where(ok_q)
    df["revenue_yoy_growth"] = ugrp.pct_change(4, fill_method=None).where(ok_y)
    df = df.drop(columns="_rev_usable")

    return df


def _add_fiscal_q4(canon_q: pd.DataFrame, canon_k: pd.DataFrame, canon_k_q: pd.DataFrame,
                   ytd_flows: pd.DataFrame | None = None) -> tuple[pd.DataFrame, set, set]:
    """Build the fiscal-Q4 rows from 10-K facts and merge them with the 10-Q facts.

    * Instant (balance-sheet) concepts: the 10-K's fiscal-year-end value IS the
      Q4-end snapshot, used directly.
    * Duration (flow) concepts: if the 10-K itself reports a standalone Q4
      (a quarter-length fact ending on the fiscal-year end; some filers include
      one), that figure is used as-is. Otherwise Q4 is derived as FY minus
      Q1-Q3 (``_derive_q4_flows``). Checked against the 5,898 cases where both
      exist, the derived value agrees within 0.1% about 84% of the time; the
      rest differ by a median ~24% (mostly recast comparatives), which is why a
      directly reported Q4 takes priority.

    Collisions with facts the 10-Q data already holds are handled explicitly
    and logged, never silently overwritten:

    * instants usually already exist as 10-Q prior-year-end comparatives; where
      the value differs, the original 10-K value is used;
    * a derived flow never replaces a directly reported 10-Q fact at the same
      period_end.

    Args:
        canon_q: canonical 10-Q facts (single-quarter durations + instants).
        canon_k: canonical 10-K facts (full-year durations + instants).
        canon_k_q: canonical 10-K facts filtered to quarter-length durations
            (standalone Q4 figures some 10-Ks report).

    Returns:
        ``(combined, derived_keys, derived_quarter_keys)``: all canonical
        facts; the ``(cik, period_end)`` pairs of fiscal-Q4 rows where a flow
        was derived by subtraction (FY minus Q1-Q3, or FY minus the 9-month
        figure); and those pairs plus Q2/Q3 rows derived from year-to-date
        differences. Directly reported values do not count.
    """
    k_dur = canon_k["concept"].isin(DURATION_CONCEPTS)
    q_dur = canon_q["concept"].isin(DURATION_CONCEPTS)

    instants = canon_k[~k_dur][["cik", "concept", "period_end", "value"]]
    imp_k = canon_k["concept"].isin(SPARSE_FLOW_CONCEPTS)
    imp_q = canon_q["concept"].isin(SPARSE_FLOW_CONCEPTS)
    ytd_k = canon_k["concept"].isin(YTD_DERIVED_CONCEPTS)
    ytd_q = canon_q["concept"].isin(YTD_DERIVED_CONCEPTS)
    derived = _derive_q4_flows(canon_k[k_dur & ~imp_k & ~ytd_k], canon_q[q_dur & ~imp_q & ~ytd_q])
    # Impairments are sparse (absent = zero), so they get their own Q4 rule.
    # They are NOT added to ``derived_flows`` below, so they never set
    # is_derived_q4 (that flag is about revenue/income-type subtraction).
    sparse_q4 = _derive_q4_sparse(canon_k[k_dur & imp_k], canon_q[q_dur & imp_q])

    # Standalone Q4 reported inside the 10-K: quarter-length duration facts that
    # end on one of the company's fiscal-year-end dates. Preferred over derived.
    fy_ends = pd.MultiIndex.from_frame(canon_k[k_dur][["cik", "period_end"]].drop_duplicates())
    kq = canon_k_q[canon_k_q["concept"].isin(DURATION_CONCEPTS)]
    direct = kq[pd.MultiIndex.from_frame(kq[["cik", "period_end"]]).isin(fy_ends)][["cik", "concept", "period_end", "value"]]
    didx = pd.MultiIndex.from_frame(direct[["cik", "concept", "period_end"]])
    derived = derived[~pd.MultiIndex.from_frame(derived[["cik", "concept", "period_end"]]).isin(didx)]
    log.info(f"{len(direct):,} Q4 flow facts reported directly in 10-Ks; {len(derived):,} derived by subtraction")
    sparse_q4 = sparse_q4[~pd.MultiIndex.from_frame(sparse_q4[["cik", "concept", "period_end"]]).isin(didx)]
    if ytd_flows is None or ytd_flows.empty:
        ytd_flows = pd.DataFrame(columns=["cik", "concept", "period_end", "value", "kind"])
    ytd_flows = ytd_flows[~pd.MultiIndex.from_frame(ytd_flows[["cik", "concept", "period_end"]]).isin(didx)]
    flows = pd.concat([direct, derived[["cik", "concept", "period_end", "value"]], sparse_q4,
                       ytd_flows[["cik", "concept", "period_end", "value"]]], ignore_index=True)
    derived_flows = derived

    existing = canon_q.set_index(["cik", "concept", "period_end"])["value"]

    # Instants: a 10-Q balance sheet carries the prior fiscal year-end as a
    # comparative column, so these facts usually already exist from the 10-Q
    # data. The 10-K is the ORIGINAL report of the fiscal-year-end balance (the
    # same "first report wins" rule used to de-duplicate everything else), while
    # the 10-Q comparative may be recast. Where they differ, the 10-K value
    # replaces the 10-Q comparative.
    inst_idx = pd.MultiIndex.from_frame(instants[["cik", "concept", "period_end"]])
    in_q = inst_idx.isin(existing.index)
    prior = existing.reindex(inst_idx[in_q]).to_numpy(dtype="float64")
    cur = instants.loc[in_q, "value"].to_numpy(dtype="float64")
    differs = ~np.isclose(prior, cur, rtol=1e-6, atol=1.0)
    log.info(
        f"{int(in_q.sum()):,} fiscal-year-end instant facts already appear as 10-Q comparatives "
        f"({int(differs.sum()):,} differ; the original 10-K value replaces the 10-Q comparative)"
    )
    replaced = pd.MultiIndex.from_frame(instants.loc[in_q][differs][["cik", "concept", "period_end"]])
    qidx = pd.MultiIndex.from_frame(canon_q[["cik", "concept", "period_end"]])
    canon_q = canon_q[~qidx.isin(replaced)]
    existing = canon_q.set_index(["cik", "concept", "period_end"])["value"]
    instants = instants[~pd.MultiIndex.from_frame(instants[["cik", "concept", "period_end"]]).isin(existing.index)]

    # Flows: a directly reported fact at this period_end (rare, e.g. 52/53-week
    # filers that tag a quarter ending at fiscal year-end) beats a derived one.
    flow_idx = pd.MultiIndex.from_frame(flows[["cik", "concept", "period_end"]])
    flow_collide = flow_idx.isin(existing.index)
    if flow_collide.any():
        log.info(f"{int(flow_collide.sum()):,} derived Q4 flow facts skipped: a directly reported 10-Q fact exists")
    flows = flows[~flow_collide]

    new = pd.concat([instants, flows], ignore_index=True).drop_duplicates(
        subset=["cik", "concept", "period_end"], keep="first"
    )
    kept = pd.MultiIndex.from_frame(derived_flows[["cik", "concept", "period_end"]]).isin(
        pd.MultiIndex.from_frame(flows[["cik", "concept", "period_end"]])
    )
    derived_keys = set(zip(derived_flows.loc[kept, "cik"], derived_flows.loc[kept, "period_end"]))
    # Quarters derived from year-to-date differences: any of Q2/Q3/Q4. Those
    # that are Q4 also set is_derived_q4 (same meaning: a Q4 flow derived by
    # subtraction); all of them set is_derived_quarter.
    ytd_kept = ytd_flows[pd.MultiIndex.from_frame(ytd_flows[["cik", "concept", "period_end"]]).isin(
        pd.MultiIndex.from_frame(flows[["cik", "concept", "period_end"]]))]
    derived_keys |= set(zip(ytd_kept.loc[ytd_kept["kind"] == "Q4", "cik"], ytd_kept.loc[ytd_kept["kind"] == "Q4", "period_end"]))
    derived_quarter_keys = derived_keys | set(zip(ytd_kept["cik"], ytd_kept["period_end"]))
    log.info(f"{len(ytd_kept):,} quarters kept from year-to-date differencing")
    log.info(f"Adding {len(new):,} fiscal-year-end facts from 10-K data")
    combined = pd.concat([canon_q[["cik", "concept", "period_end", "value"]], new], ignore_index=True)
    return combined, derived_keys, derived_quarter_keys


def build_feature_table(conn) -> pd.DataFrame:
    """Run the full extraction: fetch -> dedupe -> add fiscal Q4 -> pivot -> derive ratios.

    Quarterly facts come from ``"10q"``; fiscal-Q4 facts are filled in from
    ``"10k"`` (see ``_add_fiscal_q4``), so each company-year has up to four
    rows. Rows whose flow values were derived by subtraction carry
    ``is_derived_q4 = True``.

    Args:
        conn: open psycopg2 connection.

    Returns:
        The feature table described in the module docstring ("Output columns").
    """
    raw_q = _fetch_raw_facts(conn, "10q")
    canon_q = _dedupe_to_canonical(raw_q)
    raw_k = _fetch_raw_facts(conn, "10k")
    canon_k = _dedupe_to_canonical(raw_k, _FULL_YEAR_DURATION_DAYS)
    canon_k_q = _dedupe_to_canonical(raw_k, _QUARTER_DURATION_DAYS)
    ytd_flows = _derive_ytd_flows(raw_q, canon_q, canon_k)
    combined, derived_keys, derived_quarter_keys = _add_fiscal_q4(canon_q, canon_k, canon_k_q, ytd_flows)
    # An impairment fact must not create a (cik, period_end) row of its own:
    # an extra all-NaN row would break the row-position lags used for growth
    # and self z-scores. Keep impairment facts only where another concept exists.
    # The same applies to year-to-date-derived concepts (operating cash flow).
    is_sup = combined["concept"].isin(SPARSE_FLOW_CONCEPTS | YTD_DERIVED_CONCEPTS)
    base_rows = pd.MultiIndex.from_frame(combined.loc[~is_sup, ["cik", "period_end"]].drop_duplicates())
    keep = ~is_sup | pd.MultiIndex.from_frame(combined[["cik", "period_end"]]).isin(base_rows)
    wide = _pivot_wide(combined[keep])
    wide["is_derived_q4"] = [k in derived_keys for k in zip(wide["cik"], wide["period_end"])]
    wide["is_derived_quarter"] = [k in derived_quarter_keys for k in zip(wide["cik"], wide["period_end"])]
    featured = add_derived_ratios(wide)
    return featured


def main() -> None:
    """CLI entry point: build the feature table and write it to parquet."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(HERE / "features.parquet"), help="Output path (parquet)")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        featured = build_feature_table(conn)
    finally:
        conn.close()

    featured.to_parquet(args.out, index=False)
    log.info(f"Wrote {len(featured):,} rows ({featured['cik'].nunique():,} companies) to {args.out}")


if __name__ == "__main__":
    main()
