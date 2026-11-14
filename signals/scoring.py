"""Scores and ranks companies on a quarterly "fundamentals deterioration"
signal, built from the feature table produced by feature_extraction.py.

Role in the pipeline
--------------------
    feature_extraction.py -> features.parquet -> scoring.py -> flags.parquet

Input : ``features.parquet`` (one row per cik x period_end, raw ratios) plus
        ``entity.entity`` from Postgres for sic_code / name / sic_description.
Output: ``flags.parquet`` — the same rows with z-scores, composites and the two
        headline scores added (see "Output columns"); the top flags for the
        latest well-populated quarter are also printed.

Design: unsupervised on purpose
-------------------------------
There is no labeled "this company later blew up" dataset in the schema to
train a classifier against, so this is a statistical anomaly score, not a
predictive model. Every flag traces back to specific ratios moving in a
specific direction — which is also what's needed when publishing a call on a
real company. Nothing here is fit to outcomes: the blend weight, windows and
thresholds are judgment calls, not calibrated parameters.

How a score is built
--------------------
For each of the ratios in ``RATIO_DIRECTION``:

1. **Self z-score** — the change in the ratio vs. the SAME quarter one year
   earlier (year-over-year, so seasonality cancels), compared with the
   mean/std of the company's own previous year-over-year changes
   (trailing ``_TRAILING_WINDOW`` quarters, current quarter excluded).
   Answers "is this company breaking its own pattern?" — a *trend* claim.
2. **Peer z-score** — the ratio's *level* vs. companies in the same calendar
   quarter and same SIC code (falling back to the 2-digit SIC major group when
   the 4-digit group is too small). Answers "is this company weak relative to
   its sector right now?" — a *level* claim with no trend.

Each z-score is multiplied by ``RATIO_DIRECTION`` so positive always means
"worse", clipped to +/- ``_Z_CAP``, then combined in two stages into
``composite_self_z`` / ``composite_peer_z``: ratios are averaged within their
cluster in ``RATIO_CLUSTERS`` (revenue-quality, balance-sheet, cash-flow;
grouped by shared driver), then the cluster scores are averaged. One
underlying event (e.g. a revenue collapse hitting margin, SG&A %, DSO,
inventory/revenue and growth together) therefore counts once, not once per
ratio. The composite is NaN unless at least ``_MIN_CLUSTERS`` (2 of 3)
clusters AND ``_MIN_SIGNALS`` (6) raw ratios are present.

Headline outputs
----------------
``deterioration_score``
    The ranking for "worsening fundamentals". **Gated on the self composite**:
    NaN whenever ``composite_self_z`` is NaN, so a company that is merely
    chronically weak can never outrank one that is actually getting worse.
    Where a peer composite also exists it is ``0.7 * self + 0.3 * peer``
    (``_SELF_WEIGHT``); otherwise it equals the self composite.
``peer_weakness_score``
    The peer composite on its own — "weak vs. peers right now", no trend
    claim. A legitimate separate screen, deliberately kept apart from
    ``deterioration_score``.

Output columns (added to the feature columns)
---------------------------------------------
* ``name, sic_code, sic_description``       from entity.entity
* ``fiscal_quarter, sic_major``             peer-grouping keys (calendar
                                            quarter Period, 2-digit SIC)
* ``<ratio>__delta, <ratio>__self_z, <ratio>__peer_z``   per-ratio working columns
* ``composite_self_z, composite_peer_z``    averaged z-scores
* ``n_clusters_self_z, n_clusters_peer_z``  how many of the 3 clusters fed each composite
* ``n_signals_self_z, n_signals_peer_z``    how many raw ratio z-scores were present
* ``deterioration_score, peer_weakness_score``
* ``is_derived_q4``                         True if the row's flow values include a
                                            Q4 derived by subtraction (see feature_extraction.py)
* ``is_derived_quarter``                    True if any flow value (Q2, Q3 or Q4) was derived by
                                            subtraction, incl. operating cash flow derived from
                                            year-to-date facts (superset of is_derived_q4)
* ``impairment_total, impairment_pct_of_loss, is_impairment_driven``
                                            pass-through labels from feature_extraction.py:
                                            how much of a net loss is explained by
                                            goodwill/asset/intangible impairment charges.
                                            On impairment-driven rows net_margin and
                                            return_on_assets are masked (see below)
* ``is_negative_equity``                   True when stockholders_equity <= 0 (label only; not
                                            captured by the debt/assets ratio)
* ``cause_unclear``                         True for an unusually large net loss that impairments
                                            explain < 50% of (see feature_extraction.py); the
                                            narrative layer refuses to narrate these rows
* ``discontinued_ops_income, income_continuing_ops, is_discontinued_ops_driven``
                                            pass-through labels: signed discontinued-operations
                                            result, net income minus it, and True when it is at
                                            least half of net income; on these rows net_margin
                                            and return_on_assets are masked to NaN, as for
                                            impairment-driven rows (README item 22)
* ``net_margin_unmasked, return_on_assets_unmasked``  those two ratios before masking
* ``is_financial_sector``                   True for SIC codes in 6000-6799 except
                                            real-estate operators 6531 / 6552
                                            (``tag_map.is_financial_sic``); revenue-based
                                            ratios expected sparse

The ratio columns themselves in the output hold the RAW values from
features.parquet (so ``net_margin`` / ``return_on_assets`` are NaN on
impairment-driven rows; see ``*_unmasked``). Winsorizing is applied to internal
copies only.

Interpreting the numbers
------------------------
Scores are in standard-deviation units, not probabilities. A deterioration
score of 2.9 means "the ratios that moved against this company moved roughly
3 sigma more than its own history", not "2.9% risk". Treat a flag as a
prompt for human review: verify against the actual 10-Q before relying on it.

Known limitations
-----------------
* About a quarter of scored rows are fiscal Q4. Where a 10-K does not report Q4
  directly, its flow values are derived (FY minus Q1-Q3) and flagged
  ``is_derived_q4``; those can carry restatement noise, so check a flagged
  company's derived-Q4 row before relying on it.
* The self z-score is year-over-year, which removes seasonality but detects a
  deterioration at one-year cadence rather than one-quarter cadence, and needs
  more history (about 7 quarters) before a company can be scored. Adjacent
  year-over-year deltas overlap in the data they use, so the trailing std is an
  approximate yardstick.
* Clusters are a judgment grouping by shared driver; the ratios inside a
  cluster are still correlated and equally weighted within it.
* Thinly reporting companies (too few clusters or too little history) get no
  deterioration_score; roughly 2,700 companies per full quarter do.
* Peer groups are by SIC code only (no size/market-cap control).
* Operating cash flow is reported year-to-date, so its Q2/Q3/Q4 values are derived
  by subtraction from adjacent cumulative facts (flagged ``is_derived_quarter``);
  a restatement between the two source filings carries into the difference.
* ``is_financial_sector`` is by SIC code range 6000-6799 minus real-estate operators
  (6531 agents/managers, 6552 land developers). Lessors/operators of buildings (6510-6519)
  and generic 6500 "Real Estate" stay excluded as rental/holding structures.
* Non-operating events read as deterioration. A goodwill/asset impairment
  lowers both net income and total assets and so hits ``return_on_assets`` and
  ``net_margin``. Rows where impairments explain at least half of a net loss
  (``is_impairment_driven``) or where discontinued operations are at least half of
  net income (``is_discontinued_ops_driven``) have those two ratios masked to NaN upstream in
  feature_extraction.py (originals kept in ``*_unmasked``), so they are scored
  on their other ratios only and must still pass the cluster/signal floors
  (about 45% of such rows still get a score). The masked rows also leave the
  company's trailing window and the peer groups, which lets later real declines
  register but can give a large z to a small decline off a short clean window.
  Not handled: impairments on profitable quarters, acquisitions (Dauch), and
  other one-time items (restructuring, fair-value changes, gains on sale).
* Financial-sector rows are scored but excluded from the printed rankings
  (``top_flags(exclude_financial=True)``); their ratios are sparse or
  non-comparable.

Usage
-----
    python3 -m signals.scoring [--features PATH] [--out PATH] [--top-n-print N]

Run from the ``edgar`` project folder. ``--features`` and ``--out`` default to
``signals/features.parquet`` and ``signals/flags.parquet`` regardless of the
working directory. ``--top-n-print`` (default 25) sets how many rows each
printed table shows.

Requirements
------------
pandas, pyarrow. Reuses ``src.db.get_connection()`` to pull
sic_code/sic_description from entity.entity for peer grouping.
"""
from __future__ import annotations

import argparse
import logging

import pandas as pd

from pathlib import Path

# Folder containing this file; default parquet paths resolve here so results
# don't depend on the caller's working directory.
HERE = Path(__file__).resolve().parent

from src import db
from signals.feature_extraction import YOY_SPAN_DAYS, span_ok
from signals.tag_map import is_financial_sic

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ratio -> +1 if an INCREASE in the ratio is the deterioration direction,
# -1 if a DECREASE is. Applied to the z-score (of the period-over-period
# change for self z, of the level for peer z), so every entry in a composite
# points the same way: higher composite = more deterioration, regardless of
# the ratio's own sign. Also defines the set of ratios that get scored.
RATIO_DIRECTION: dict[str, int] = {
    "gross_margin": -1,
    "net_margin": -1,
    "sga_pct_revenue": +1,
    "dso_days": +1,
    "inventory_to_revenue": +1,
    "ocf_to_assets": -1,
    "current_ratio": -1,
    "long_term_debt_to_assets": +1,
    "revenue_yoy_growth": -1,
    "return_on_assets": -1,
}

# Ratios grouped by shared underlying driver (NOT by statement category).
# Six ratios share revenue as numerator/denominator or measure revenue itself,
# so one revenue collapse used to be counted ~5 times in a plain average. Each
# cluster is averaged into ONE z-score first, then the cluster scores are
# averaged, so one event counts once per cluster it touches. Every ratio in
# RATIO_DIRECTION must appear in exactly one cluster (asserted below).
# Ratios are kept (not dropped) because each can signal something the others
# miss, e.g. DSO blowing out while margin holds.
RATIO_CLUSTERS: dict[str, list[str]] = {
    "revenue_quality": ["net_margin", "gross_margin", "sga_pct_revenue",
                        "dso_days", "inventory_to_revenue", "revenue_yoy_growth"],
    "balance_sheet": ["current_ratio", "long_term_debt_to_assets"],
    "cash_flow": ["ocf_to_assets", "return_on_assets"],
}

# Require at least this many CLUSTERS to have a non-null z-score before
# trusting a composite. Gating on raw ratio count let 4 of the 6
# revenue-quality ratios (one underlying signal) pass alone; a cluster count
# cannot be satisfied by one driver. 2 of 3 clusters is the floor. Applied
# separately to the self and the peer composite. Raising it shrinks coverage.
_MIN_CLUSTERS = 2

# ...AND at least this many raw ratio z-scores. The cluster floor alone is
# looser than the old gate: balance-sheet + cash-flow clusters can be satisfied
# by just 2 ratios (common for banks and pre-revenue biotechs), which is a thin
# basis for a ranking. Both floors must hold.
# Raised from 4 to 6 on 2026-11-14 after the cash-flow cluster recovery: clusters
# are averaged first, so a cluster holding one ratio carries a full third of the
# score and a 4-ratio row can be driven by a single noisy ratio. On 2026Q3
# non-financial rows, 6 keeps 91% of rows and 20 of the old top 25 (not fit to
# outcomes; a judgment call). KNOWN REMAINING GAP: neither floor guarantees each
# represented cluster has more than one ratio behind it (6 ratios can all come
# from the revenue-quality cluster; a one-ratio cluster still gets a third of
# the weight). A per-cluster minimum would close it; not implemented.
_MIN_SIGNALS = 6

# Trailing window (in quarters) for the self z-score. 8 quarters = 2 years of
# history; shorter windows are noisier, longer ones wash out real regime
# changes (e.g. a post-acquisition company looks nothing like its old self).
# The window also requires at least 3 prior year-over-year changes (min_periods=3).
_TRAILING_WINDOW = 8

# The self z-score compares each quarter with the SAME quarter one year earlier
# (a 4-row lag), not with the previous quarter. A quarter-over-quarter delta
# would treat a recurring seasonal swing (retail Q4 -> Q1, etc.) as an anomaly
# until several years of that swing had accumulated in the trailing window;
# a year-over-year delta removes seasonality by construction. The cost is
# slower detection: a deterioration shows up against last year's level, so it
# is seen at one-year rather than one-quarter cadence.
_SELF_DELTA_LAG = 4

# Minimum non-null peers (same calendar quarter + SIC group) required for a
# peer z-score; smaller groups give a meaningless std. Counts non-null values
# of the specific ratio, not rows in the group.
_MIN_PEER_GROUP = 5

# Belt-and-suspenders winsorization: clip each ratio to these dataset-wide
# (all companies, all quarters) percentiles before z-scoring, so a surviving
# extreme value can't dominate a company's trailing mean/std or a peer group's
# mean/std. The raw (unclipped) values are restored in the output.
_WINSOR_PCTS = (0.01, 0.99)

# Cap on any single ratio's z-score (applied after sign-adjustment) so one
# ratio can contribute at most this many standard deviations to a composite.
_Z_CAP = 5.0

# Weight on the self-trend composite in the headline deterioration score.
# Self-trend is the primary claim ("getting worse"); peer weakness is
# corroborating context. Not fit to outcomes — there is no labeled data. The
# self-composite gate matters more than this exact ratio: a peer-only company
# can never outrank a self-confirmed one regardless of the weight.
_SELF_WEIGHT = 0.7

assert sorted(r for m in RATIO_CLUSTERS.values() for r in m) == sorted(RATIO_DIRECTION), \
    "every ratio in RATIO_DIRECTION must be in exactly one RATIO_CLUSTERS cluster"


def _winsorize_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with each scored ratio clipped to ``_WINSOR_PCTS`` quantiles.

    Quantiles are computed over the whole frame (all companies and quarters)
    per ratio, and the columns are coerced to float64. This is only for
    z-score computation; ``score()`` restores the raw values afterwards.
    """
    df = df.copy()
    for ratio in RATIO_DIRECTION:
        col = pd.to_numeric(df[ratio], errors="coerce").astype("float64")
        lo, hi = col.quantile(_WINSOR_PCTS[0]), col.quantile(_WINSOR_PCTS[1])
        df[ratio] = col.clip(lo, hi)
    return df


def _load_sector_map(conn) -> pd.DataFrame:
    """Load cik, name, sic_code, sic_description for every entity.

    Args:
        conn: open psycopg2 connection.

    Returns:
        One row per entity in ``entity.entity``; merged onto the features by cik.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT cik, name, sic_code, sic_description FROM entity.entity")
        cols = [c.name for c in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def _self_zscores(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``<ratio>__delta`` and ``<ratio>__self_z`` for every scored ratio.

    For each ratio: ``delta`` is the change vs. the same quarter one year
    earlier (``_SELF_DELTA_LAG`` rows back; NaN unless that row really is ~one
    year earlier — see ``span_ok``), and the self z-score is that delta
    standardized against the mean/std of the company's own previous year-over-year
    deltas, then sign-adjusted by RATIO_DIRECTION and clipped to +/- ``_Z_CAP``.
    Comparing like quarters with like removes seasonality.

    The trailing statistics are computed on data *before* the current row
    (``shift(1)`` before the rolling window) so a quarter's own value never
    leaks into its own baseline — otherwise a single huge move would inflate
    the std used to judge itself and understate its own anomaly. A zero std
    (a perfectly flat history) yields NaN rather than infinity.

    Note: year-over-year deltas of consecutive quarters overlap in the data they
    use, so adjacent deltas in the trailing window are correlated; the std is
    an approximate yardstick, not an independent-samples estimate.

    Args:
        df: features merged with sector info; ratios may be winsorized.

    Returns:
        Copy sorted by (cik, period_end) with the new columns added.
    """
    df = df.sort_values(["cik", "period_end"]).copy()
    ok = span_ok(df, _SELF_DELTA_LAG, YOY_SPAN_DAYS)
    for ratio in RATIO_DIRECTION:
        delta_col = f"{ratio}__delta"
        z_col = f"{ratio}__self_z"
        grp = df.groupby("cik")[ratio]
        # Only treat the diff as a year-over-year change if the row 4 back really
        # is the same quarter of the prior year (not across a gap in history).
        df[delta_col] = grp.diff(_SELF_DELTA_LAG).where(ok)

        delta_grp = df.groupby("cik")[delta_col]
        roll_mean = delta_grp.transform(lambda s: s.shift(1).rolling(_TRAILING_WINDOW, min_periods=3).mean())
        roll_std = delta_grp.transform(lambda s: s.shift(1).rolling(_TRAILING_WINDOW, min_periods=3).std())

        df[z_col] = (df[delta_col] - roll_mean) / roll_std.replace(0, pd.NA)
        df[z_col] = (pd.to_numeric(df[z_col], errors='coerce').astype('float64') * RATIO_DIRECTION[ratio]).clip(-_Z_CAP, _Z_CAP)
    return df


def _group_z(df: pd.DataFrame, ratio: str, keys: list[str]) -> pd.Series:
    """Z-score ``ratio`` within ``keys`` groups (not yet sign-adjusted or clipped).

    NaN where the group has fewer than ``_MIN_PEER_GROUP`` non-null values of
    the ratio, or where the group's std is zero/undefined.

    Args:
        df: frame containing ``ratio`` and the grouping ``keys`` columns.
        ratio: name of the ratio column to standardize.
        keys: grouping columns, e.g. ``["fiscal_quarter", "sic_code"]``.

    Returns:
        Float Series aligned to ``df``.
    """
    grp = df.groupby(keys)[ratio]
    n = grp.transform("count")
    mean = grp.transform("mean")
    std = grp.transform("std")
    z = (df[ratio] - mean) / std.where(std > 0)
    return z.where(n >= _MIN_PEER_GROUP)


def _peer_zscores(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``fiscal_quarter``, ``sic_major`` and ``<ratio>__peer_z`` columns.

    Peer z-scores compare a company's ratio *level* with same-sector peers
    in the same calendar quarter.

    * Companies don't share fiscal calendars (most don't end on
      03-31/06-30/09-30/12-31), so grouping on the literal ``period_end`` leaves
      most groups with a single company. ``period_end`` is therefore bucketed
      to a calendar quarter (``fiscal_quarter``) first.
    * Peers are first taken at 4-digit SIC. Where that group has fewer than
      ``_MIN_PEER_GROUP`` non-null values, the score falls back to the 2-digit
      SIC major group (``sic_major``), and is NaN if that is also too thin.
    * The result is sign-adjusted by RATIO_DIRECTION and clipped to
      +/- ``_Z_CAP``.

    Args:
        df: frame with ``period_end``, ``sic_code`` and the ratio columns.

    Returns:
        Copy with the new columns added.
    """
    df = df.copy()
    df["fiscal_quarter"] = pd.to_datetime(df["period_end"]).dt.to_period("Q")
    sic = df["sic_code"].astype("string").str.strip()
    df["sic_major"] = sic.str.zfill(4).str[:2]
    for ratio in RATIO_DIRECTION:
        z4 = _group_z(df, ratio, ["fiscal_quarter", "sic_code"])
        z2 = _group_z(df, ratio, ["fiscal_quarter", "sic_major"])
        df[f"{ratio}__peer_z"] = (z4.fillna(z2) * RATIO_DIRECTION[ratio]).clip(-_Z_CAP, _Z_CAP)
    return df


def _composite(df: pd.DataFrame, suffix: str) -> pd.DataFrame:
    """Combine the per-ratio z-scores with the given suffix into one score per row.

    Two-stage: average the non-null z-scores inside each cluster of
    ``RATIO_CLUSTERS`` into one cluster score, then average the non-null
    cluster scores. Adds ``composite_<suffix>`` (NaN unless at least
    ``_MIN_CLUSTERS`` clusters AND ``_MIN_SIGNALS`` raw ratios are present), ``n_clusters_<suffix>`` (how
    many clusters were represented) and ``n_signals_<suffix>`` (how many raw
    ratio z-scores were present). Missing z-scores are skipped, not zero.

    Args:
        df: frame containing ``<ratio>__<suffix>`` columns.
        suffix: ``"self_z"`` or ``"peer_z"``.
    """
    cluster_scores = pd.concat(
        {name: df[[f"{r}__{suffix}" for r in members]].mean(axis=1, skipna=True)
         for name, members in RATIO_CLUSTERS.items()},
        axis=1,
    )
    n_clusters = cluster_scores.notna().sum(axis=1)
    n_signals = df[[f"{r}__{suffix}" for r in RATIO_DIRECTION]].notna().sum(axis=1)
    ok = (n_clusters >= _MIN_CLUSTERS) & (n_signals >= _MIN_SIGNALS)
    df[f"composite_{suffix}"] = cluster_scores.mean(axis=1, skipna=True).where(ok)
    df[f"n_clusters_{suffix}"] = n_clusters
    df[f"n_signals_{suffix}"] = n_signals
    return df


def score(featured: pd.DataFrame, sector_map: pd.DataFrame) -> pd.DataFrame:
    """Compute all z-scores, composites and headline scores.

    Pipeline: merge sector info -> winsorize ratio copies -> self z-scores ->
    peer z-scores -> composites -> restore raw ratio values -> headline scores.

    Args:
        featured: contents of ``features.parquet``.
        sector_map: output of ``_load_sector_map``.

    Returns:
        DataFrame with one row per input row and the columns listed in the
        module docstring ("Output columns"). Ratio columns hold raw values.
    """
    df = featured.merge(sector_map, on="cik", how="left")
    # Winsorized copies are used only for z-scoring; the stored ratio columns
    # keep their raw values so downstream consumers never see clipped numbers.
    raw_ratios = df[list(RATIO_DIRECTION)].copy()
    df = _winsorize_ratios(df)

    df = _self_zscores(df)
    df = _peer_zscores(df)
    df = _composite(df, "self_z")
    df = _composite(df, "peer_z")

    df[list(RATIO_DIRECTION)] = raw_ratios

    # Headline ranking: gated on a usable self-trend composite, so a company
    # that is merely chronically weak can never outrank one that is actually
    # getting worse. Where a peer composite also exists it is blended in
    # (70/30 self/peer); otherwise the self composite stands alone.
    self_z, peer_z = df["composite_self_z"], df["composite_peer_z"]
    blended = _SELF_WEIGHT * self_z + (1 - _SELF_WEIGHT) * peer_z
    df["deterioration_score"] = blended.where(peer_z.notna(), self_z).where(self_z.notna())

    # Separate screen: "weak vs. same-sector peers right now", no trend claim.
    df["peer_weakness_score"] = peer_z

    df["is_financial_sector"] = df["sic_code"].map(is_financial_sic)
    return df


def latest_scored_quarter(scored: pd.DataFrame, min_rows: int = 500, score_col: str = "deterioration_score"):
    """Return the latest calendar quarter with at least ``min_rows`` scored companies.

    The newest quarter is usually only partly filed, so ranking it would show
    a handful of early filers (or nothing) rather than a meaningful
    cross-section. Quarters below the threshold are skipped.

    Args:
        scored: output of ``score()``.
        min_rows: minimum number of rows with a non-null ``score_col``.
        score_col: which score decides whether a row counts as "scored".

    Returns:
        A pandas Period (e.g. ``Period('2026Q1')``), or NaN if no quarter
        qualifies.
    """
    counts = scored.loc[scored[score_col].notna()].groupby("fiscal_quarter").size()
    full = counts[counts >= min_rows]
    return full.index.max()


def top_flags(scored: pd.DataFrame, fiscal_quarter, n: int = 25, score_col: str = "deterioration_score",
              exclude_financial: bool = True) -> pd.DataFrame:
    """Return the ``n`` highest-scoring rows for one calendar quarter.

    Args:
        scored: output of ``score()``.
        fiscal_quarter: the calendar-quarter Period to rank within.
        n: number of rows to return.
        score_col: ``"deterioration_score"`` (self-gated trend ranking) or
            ``"peer_weakness_score"`` (level-vs-peers ranking).
        exclude_financial: drop ``is_financial_sector`` rows from the printed
            ranking (default True). Their revenue-based ratios are sparse and
            their statements aren't comparable; scores stay in flags.parquet.

    Returns:
        A display subset of columns (identity, scores, key ratios), sorted
        descending by ``score_col``. Companies with a NaN score sort last.
    """
    cols = [
        "cik", "name", "sic_description", "period_end", "deterioration_score",
        "peer_weakness_score", "composite_self_z", "composite_peer_z", "n_clusters_self_z",
        "revenue_yoy_growth", "net_margin_unmasked", "ocf_to_assets", "long_term_debt_to_assets",
        "is_financial_sector", "is_derived_q4", "is_derived_quarter", "is_impairment_driven", "impairment_pct_of_loss", "is_negative_equity", "cause_unclear",
    ]
    cols = [c for c in cols if c in scored.columns]
    subset = scored[scored["fiscal_quarter"] == fiscal_quarter]
    if exclude_financial:
        subset = subset[~subset["is_financial_sector"]]
    return subset.sort_values(score_col, ascending=False).head(n)[cols]


def main() -> None:
    """CLI entry point: score, write flags.parquet, print both top-flag tables."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--features", default=str(HERE / "features.parquet"),
                        help="Input feature table (default: signals/features.parquet)")
    parser.add_argument("--out", default=str(HERE / "flags.parquet"),
                        help="Output scored table (default: signals/flags.parquet)")
    parser.add_argument("--top-n-print", type=int, default=25,
                        help="Rows to print in each top-flags table (default: 25)")
    args = parser.parse_args()

    featured = pd.read_parquet(args.features)

    conn = db.get_connection()
    try:
        sector_map = _load_sector_map(conn)
    finally:
        conn.close()

    scored = score(featured, sector_map)
    scored.to_parquet(args.out, index=False)
    log.info(f"Wrote {len(scored):,} scored rows to {args.out}")

    latest_period = latest_scored_quarter(scored)
    flagged = top_flags(scored, latest_period, n=args.top_n_print)
    log.info(f"Top {len(flagged)} deterioration flags (self-gated) for {latest_period}:\n{flagged.to_string(index=False)}")

    weak = top_flags(scored, latest_period, n=args.top_n_print, score_col="peer_weakness_score")
    log.info(f"Top {len(weak)} peer-weakness names (no trend claim) for {latest_period}:\n{weak.to_string(index=False)}")


if __name__ == "__main__":
    main()
