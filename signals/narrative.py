"""Turns flagged rows from flags.parquet into short, grounded, descriptive
narratives using a local Ollama model.

Role in the pipeline
--------------------
    scoring.py -> flags.parquet -> narrative.py -> narratives.jsonl

This is the first output meant to be read by people, so the bar is "do not
overclaim". A bad ratio can be caught by re-running scoring; a bad sentence
cannot be un-read. The design therefore makes it structurally hard for the
model to say anything the data does not support:

1. **Fact sheet, not raw data.** Every number the model may use is
   pre-formatted into a string (``"-12.4%"``, ``"$37.2M"``) from the row's own
   values. The model is told to copy them verbatim and use no others.
2. **Mandatory disclosures.** Every true flag (impairment-driven,
   discontinued-operations-driven, negative equity, derived quarter) becomes a
   required statement. Impairment, discontinued operations and negative equity must appear in the first two sentences, not as a footnote;
   the derived-figures note, which is routine, may close the text.
3. **Ineligible rows are never sent to the model.** Rows that are
   ``is_financial_sector`` (excluded from the ranking) or ``cause_unclear``
   (an extreme loss the data cannot explain, e.g. FMC 2026Q3) are skipped and
   written to the output with their reason, so a model is never asked to explain
   something the data cannot explain.
4. **Validation after generation.** The text is rejected (and retried with
   feedback, up to ``MAX_ATTEMPTS``) if it contains a number not in the fact
   sheet, causal wording ("because", "due to", ...), investment language
   ("undervalued", "buy", ...), or omits a required disclosure. A row that
   never passes is stored with ``status = "failed_validation"`` and its last
   attempt, and must be reviewed by a person.

Inputs per row (all from flags.parquet)
---------------------------------------
Identity and period; ``deterioration_score`` and ``peer_weakness_score``; the
average self-z of each ratio cluster that contributed; the individual ratios
whose self-z is at least ``TRIP_Z`` (raw value now, value a year earlier and
the change) plus up to ``MAX_FAVORABLE`` ratios moving favorably (to keep the
picture two-sided); revenue and net income; and the flags
``is_impairment_driven`` (with ``impairment_total``, ``impairment_pct_of_loss``
and the unmasked net margin / return on assets), ``is_discontinued_ops_driven``
(with the discontinued result and the income from continuing operations), ``is_derived_q4`` /
``is_derived_quarter``, ``is_negative_equity``.

Output
------
``signals/narratives.jsonl``: one JSON object per row with ``cik, name,
period_end, status`` (``ok`` | ``failed_validation`` | ``skipped``),
``skip_reason`` / ``violations`` where relevant, ``narrative``, ``facts`` (the
exact fact sheet sent), ``model`` and ``attempts``. Narratives are descriptive
fundamentals narration, not a buy/sell/hold view.

Usage
-----
    python3 -m signals.narrative --names "Camping World,Fathom,WM Technology"
    python3 -m signals.narrative --quarter 2026Q3 --top-n 25
    python3 -m signals.narrative --names "Organogenesis" --dry-run   # print prompt only

Options: ``--model`` (default env ``OLLAMA_MODEL`` or ``gemma4:31b-mlx``),
``--host`` (default ``http://localhost:11434``), ``--out``, ``--include-ranked-out``.
Run from the ``edgar`` project folder. Requires a running Ollama server.

Known limits
------------
* The validator checks numbers, banned wording and required disclosures; it
  cannot check that a sentence is a *fair reading* of the numbers. Read the
  narratives; spot-check them against filings before publishing.
* The trip threshold (``TRIP_Z``), word limits and ban lists are judgment calls.
* Prior-year values are taken 4 rows back and only used when that period is
  about a year earlier (``YOY_SPAN_DAYS``); otherwise the ratio is shown without
  a prior value.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
from pathlib import Path

import pandas as pd
import requests

from signals.feature_extraction import YOY_SPAN_DAYS, span_ok
from signals.scoring import RATIO_CLUSTERS, RATIO_DIRECTION

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b-mlx")
DEFAULT_HOST = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")

# A ratio is reported as "tripped" when its direction-adjusted self z-score is at
# least this many standard deviations in the bad direction.
TRIP_Z = 1.5
# How many favorably moving ratios (self z <= -TRIP_Z) to include for balance.
MAX_FAVORABLE = 3
# Generation attempts per row (first try plus retries with violation feedback).
MAX_ATTEMPTS = 3
# Cap on tripped ratios shown (strongest first) so the prompt stays focused.
MAX_TRIPPED = 6

# Human-readable ratio name and unit kind: pct (a fraction shown as %), days, x.
RATIO_META: dict[str, tuple[str, str]] = {
    "gross_margin": ("gross margin", "pct"),
    "net_margin": ("net margin", "pct"),
    "sga_pct_revenue": ("SG&A as a share of revenue", "pct"),
    "dso_days": ("days sales outstanding (receivables)", "days"),
    "inventory_to_revenue": ("inventory relative to quarterly revenue", "pct"),
    "revenue_yoy_growth": ("revenue change versus the same quarter a year earlier", "pct"),
    "current_ratio": ("current ratio", "x"),
    "long_term_debt_to_assets": ("long-term debt as a share of total assets", "pct"),
    "ocf_to_assets": ("operating cash flow as a share of total assets (quarterly)", "pct"),
    "return_on_assets": ("return on assets (quarterly)", "pct"),
}
CLUSTER_NAMES = {
    "revenue_quality": "revenue and margin quality",
    "balance_sheet": "balance sheet",
    "cash_flow": "cash flow and returns",
}

# Wording that implies a cause or a recommendation; rejected in generated text.
_CAUSAL = re.compile(
    r"\b(because|due to|caused|causing|as a result|owing to|thanks to|led to|leading to|resulting from|"
    r"stems? from|stemming from|driven by|attributable to|on account of)\b", re.I)
_INVEST = re.compile(
    r"\b(buy|sell|hold rating|undervalued|overvalued|recommend\w*|upside|downside|price target|"
    r"investors? should|should (?:buy|sell|avoid)|outlook|forecast|will likely|expected to|bullish|bearish|"
    r"opportunity|attractive|avoid)\b", re.I)
_NUM = re.compile(r"\d[\d,]*\.?\d*")

SYSTEM_PROMPT = """You write short, factual notes about a company's reported quarterly fundamentals for a general investing audience. You are given a FACT SHEET. Follow these rules exactly.

1. Use ONLY numbers that appear in the FACT SHEET, copied exactly as written there (same digits, same units). Do not compute new numbers, round differently, or add any figure from memory.
2. Every statement must be backed by a field in the FACT SHEET. Do not describe the business, its products, its industry conditions, its management or its market share beyond what the sheet says.
3. No causal language. Never use "because", "due to", "caused by", "as a result", "driven by", "led to" or similar. The ONLY cause you may state is one the sheet lists under MANDATORY DISCLOSURES (for example an impairment charge). For everything else say that things happened "alongside" or "at the same time as" one another.
4. State any "Impairment-driven loss", "Discontinued operations" or "Negative equity" item from MANDATORY DISCLOSURES within the first two sentences, in plain words, as a main point of the note. State any "Derived figures" item in one short closing sentence. If there are no disclosures, begin with the largest deterioration.
5. Descriptive only. No investment advice, no buy/sell/hold view, no price or outlook talk, no predictions, no words like "attractive", "opportunity" or "avoid".
6. Mention any ratio listed as moving favorably only briefly, so the note is balanced.
7. Do not describe a row as unusually bad compared with other companies unless the sheet gives a peer figure.
8. Do not quote z-scores or cluster averages. Describe things with the ratio values and changes. You may mention the deterioration score once, as a number of standard deviations versus the company's own history. Say whether each ratio is worse or better using the "direction" field; do not call a change good or bad on your own.
9. If nothing is listed under ratios_worsening_versus_own_history, say plainly that no ratio is notably worse than the company's own history this quarter.
10. When you cite an earlier value, call it the value "a year earlier", never "its own history" (that phrase is only for the worse/better direction). Never mention a flag that is not listed under MANDATORY DISCLOSURES. Do not say that something was "not reported", "no impairment" or "no negative equity".
11. Length: 3 to 5 sentences, at most 120 words, plain text, no bullet points, no headings, no preamble.
"""


# ---------------------------------------------------------------- formatting

def _usd(v: float) -> str:
    """Format dollars compactly with an ASCII minus sign, e.g. ``-$197.0M``."""
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.1f}K"
    return f"{sign}${a:.0f}"


def _val(v: float, kind: str) -> str:
    """Format a ratio value by its unit kind."""
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "days":
        return f"{v:.0f} days"
    return f"{v:.2f}x"


def _chg(cur: float, prior: float, kind: str) -> str:
    """Format a change in a ratio (percentage points for pct ratios)."""
    d = cur - prior
    if kind == "pct":
        return f"{d * 100:+.1f} percentage points"
    if kind == "days":
        return f"{d:+.0f} days"
    return f"{d:+.2f}x"


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and not pd.isna(v)


# ----------------------------------------------------------------- fact sheet

def add_prior_year(flags: pd.DataFrame) -> pd.DataFrame:
    """Add ``<ratio>__prior_year`` columns: the value 4 rows back, only if ~1 year earlier.

    Uses the unmasked net margin / return on assets so impairment rows show a
    real prior value. Returns a sorted copy.
    """
    df = flags.sort_values(["cik", "period_end"]).reset_index(drop=True).copy()
    ok = span_ok(df, 4, YOY_SPAN_DAYS)
    for r in RATIO_META:
        src = {"net_margin": "net_margin_unmasked", "return_on_assets": "return_on_assets_unmasked"}.get(r, r)
        df[f"{r}__prior_year"] = df.groupby("cik")[src].shift(4).where(ok)
    return df


def ineligible_reason(row: pd.Series) -> str | None:
    """Why a row must not be narrated, or None if it may be."""
    if not _ok(row.get("deterioration_score")) and not _ok(row.get("peer_weakness_score")):
        return "no score for this row"
    if bool(row.get("is_financial_sector")):
        return "financial-sector row (excluded from the ranking)"
    if bool(row.get("cause_unclear")):
        return "cause_unclear: extreme net loss the available data cannot explain; manual review required"
    return None


def build_facts(row: pd.Series) -> dict:
    """Build the pre-formatted fact sheet for one row (see module docstring)."""
    facts: dict = {
        "company": row["name"],
        "period_end": str(row["period_end"])[:10],
        "industry_sic_description": row.get("sic_description"),
        "deterioration_score": f"{row['deterioration_score']:.2f}" if _ok(row.get("deterioration_score")) else None,
        "peer_weakness_score": f"{row['peer_weakness_score']:.2f}" if _ok(row.get("peer_weakness_score")) else None,
        "score_note": "Scores are in standard-deviation units: higher means worse versus the company's own history (deterioration) or versus peers (peer weakness). They are not probabilities.",
    }
    if _ok(row.get("revenue")):
        facts["revenue_this_quarter"] = _usd(row["revenue"])
    if _ok(row.get("net_income")):
        ni = row["net_income"]
        facts["net_result_this_quarter"] = f"net loss of {_usd(-ni)}" if ni < 0 else f"net income of {_usd(ni)}"

    # Cluster contributions (average direction-adjusted self z per cluster).
    clusters = []
    for name, members in RATIO_CLUSTERS.items():
        zs = [row[f"{r}__self_z"] for r in members if _ok(row.get(f"{r}__self_z"))]
        if zs:
            clusters.append(f"{CLUSTER_NAMES[name]}: average self z-score {sum(zs) / len(zs):+.2f} (from {len(zs)} ratio(s))")
    facts["cluster_contributions"] = clusters

    disc_driven = bool(row.get("is_discontinued_ops_driven"))
    tripped, favorable = [], []
    for r, (label, kind) in RATIO_META.items():
        z = row.get(f"{r}__self_z")
        if not _ok(z):
            continue
        # Net margin and return on assets include the discontinued result on these
        # rows, so they are not shown as ordinary operating ratios.
        if disc_driven and r in ("net_margin", "return_on_assets"):
            continue
        cur_col = {"net_margin": "net_margin", "return_on_assets": "return_on_assets"}.get(r, r)
        cur = row.get(cur_col)
        if not _ok(cur):
            continue
        prior = row.get(f"{r}__prior_year")
        item = {"ratio": label, "direction": "worse than its own history" if z > 0 else "better than its own history",
                "value_now": _val(cur, kind)}
        # Revenue growth is already a year-over-year change; a "change in a change"
        # is confusing, so no prior value is shown for it.
        if _ok(prior) and r != "revenue_yoy_growth":
            item["value_a_year_earlier"] = _val(prior, kind)
            item["change"] = _chg(cur, prior, kind)
        (tripped if z >= TRIP_Z else favorable if z <= -TRIP_Z else []).append((abs(z), item))
    facts["ratios_worsening_versus_own_history"] = [i for _, i in sorted(tripped, key=lambda t: -t[0])][:MAX_TRIPPED]
    facts["ratios_improving_versus_own_history"] = [i for _, i in sorted(favorable, key=lambda t: -t[0])][:MAX_FAVORABLE]

    # Mandatory disclosures: one plain statement per true flag.
    must: list[str] = []
    if bool(row.get("is_impairment_driven")):
        pct = row.get("impairment_pct_of_loss")
        loss = _usd(-row["net_income"])
        if row["impairment_total"] >= -row["net_income"]:
            core = (f"Impairment charges of {_usd(row['impairment_total'])} (non-cash, measured before tax) were larger than the "
                    f"net loss of {loss}, so the loss is mainly an accounting write-down rather than necessarily operating decline.")
        else:
            core = (f"Impairment charges of {_usd(row['impairment_total'])} (non-cash, measured before tax) account for about "
                    f"{pct * 100:.0f}% of the net loss of {loss}, so a large part of the loss is an accounting write-down rather than operating decline.")
        must.append(
            "Impairment-driven loss. Say this (you may reword it but keep every number): " + core + " "
            "Net margin and return on assets are not scored for this row. "
            f"Unmasked net margin: {_val(row['net_margin_unmasked'], 'pct') if _ok(row.get('net_margin_unmasked')) else 'n/a'}; "
            f"unmasked return on assets: {_val(row['return_on_assets_unmasked'], 'pct') if _ok(row.get('return_on_assets_unmasked')) else 'n/a'}."
        )
    if disc_driven:
        disc, cont = row["discontinued_ops_income"], row["income_continuing_ops"]
        cont_txt = f"a net loss of {_usd(-cont)}" if cont < 0 else f"net income of {_usd(cont)}"
        disc_txt = f"a loss of {_usd(-disc)}" if disc < 0 else f"a gain of {_usd(disc)}"
        facts["net_income_from_continuing_operations"] = cont_txt
        if _ok(row.get("revenue")) and row["revenue"] > 0:
            facts["net_margin_from_continuing_operations"] = _val(cont / row["revenue"], "pct")
        must.append(
            "Discontinued operations. Say this in the first two sentences (you may reword it but keep every number): "
            f"the quarter's net result of {_usd(row['net_income'])} includes {disc_txt} from discontinued operations, "
            f"so continuing operations produced {cont_txt}. "
            "The reported net margin and return on assets include the discontinued result and must not be presented as operating performance."
        )
    if bool(row.get("is_negative_equity")) and _ok(row.get("stockholders_equity")):
        must.append(f"Negative equity: stockholders' equity is {_usd(row['stockholders_equity'])}. Negative equity is not by itself a sign of distress (it can come from buybacks), so state it neutrally.")
    if bool(row.get("is_derived_quarter")) or bool(row.get("is_derived_q4")):
        must.append("Derived figures. Say it in ONE short closing sentence, for example: 'Some figures for this quarter were derived by subtraction and may carry restatement noise.'")
    facts["MANDATORY_DISCLOSURES"] = must
    return facts


def _facts_text(facts: dict) -> str:
    return json.dumps(facts, indent=2, ensure_ascii=False)


def build_messages(facts: dict, feedback: list[str] | None = None) -> list[dict]:
    """Chat messages for Ollama: the system rules plus the fact sheet (and retry feedback)."""
    user = "FACT SHEET:\n" + _facts_text(facts) + "\n\nWrite the note now."
    if feedback:
        user += "\n\nYour previous attempt was rejected for these reasons. Fix them and write the note again:\n- " + "\n- ".join(feedback)
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


# ----------------------------------------------------------------- validation

def validate(text: str, facts: dict) -> list[str]:
    """Return a list of rule violations (empty list = the text passes).

    Checks: numbers not in the fact sheet, causal wording, investment language,
    word limit, and that every required disclosure is mentioned in the first two
    sentences.
    """
    problems: list[str] = []
    allowed = {t.replace(",", "").rstrip(".") for t in _NUM.findall(_facts_text(facts))}
    for tok in _NUM.findall(text):
        t = tok.replace(",", "").rstrip(".")
        if t not in allowed:
            problems.append(f"number '{tok}' is not in the fact sheet")
    for m in _CAUSAL.finditer(text):
        problems.append(f"causal wording '{m.group(0)}' is not allowed")
    for m in _INVEST.finditer(text):
        problems.append(f"investment/outlook wording '{m.group(0)}' is not allowed")
    if re.search(r"z[- ]?scores?", text, re.I):
        problems.append("do not quote z-scores or cluster averages")
    if len(text.split()) > 130:
        problems.append("longer than 120 words")
    first_two = " ".join(re.split(r"(?<=[.!?])\s+", text.strip())[:2]).lower()
    disclosures = " ".join(facts.get("MANDATORY_DISCLOSURES", [])).lower()
    if "impairment-driven" in disclosures and "impairment" not in first_two:
        problems.append("the impairment disclosure must appear in the first two sentences")
    if re.search(r"\bno (?:impairment|negative equity)|not (?:impairment|negative)", text, re.I) and not re.search("impairment-driven loss|negative equity", disclosures):
        problems.append("do not mention flags that are not listed under MANDATORY DISCLOSURES")
    if "impairment-driven" in disclosures and not re.search(r"write-?down", first_two, re.I):
        problems.append("the first two sentences must say the loss is mainly (or partly) an accounting write-down, not only that impairment charges exist")
    if "discontinued operations. say this" in disclosures and "discontinued" not in first_two:
        problems.append("the discontinued-operations disclosure must appear in the first two sentences")
    if "negative equity" in disclosures and not ("equity" in first_two and ("negative" in first_two or "-$" in first_two)):
        problems.append("the negative-equity disclosure must appear in the first two sentences")
    if "derived figures" in disclosures and "derived" not in text.lower():
        problems.append("the derived-figures disclosure must appear (a short closing sentence is fine)")
    return list(dict.fromkeys(problems))


# --------------------------------------------------------------------- ollama

def generate(facts: dict, model: str, host: str, feedback: list[str] | None = None) -> str:
    """Call Ollama's chat endpoint once and return the cleaned text."""
    resp = requests.post(
        f"{host}/api/chat",
        json={"model": model, "messages": build_messages(facts, feedback), "stream": False, "think": False,
              "options": {"temperature": 0.2, "num_predict": 400}},
        timeout=600,
    )
    resp.raise_for_status()
    text = resp.json()["message"]["content"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text


def narrate_row(row: pd.Series, model: str, host: str) -> dict:
    """Narrate one row, or record why it was skipped; returns the output record."""
    rec = {"cik": row["cik"], "name": row["name"], "period_end": str(row["period_end"])[:10], "model": model}
    reason = ineligible_reason(row)
    if reason:
        return {**rec, "status": "skipped", "skip_reason": reason}
    facts = build_facts(row)
    feedback: list[str] | None = None
    text, problems = "", []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = generate(facts, model, host, feedback)
        problems = validate(text, facts)
        if not problems:
            return {**rec, "status": "ok", "narrative": text, "facts": facts, "attempts": attempt}
        feedback = problems
    return {**rec, "status": "failed_validation", "narrative": text, "violations": problems,
            "facts": facts, "attempts": MAX_ATTEMPTS}


# ------------------------------------------------------------------ selection

def select_rows(flags: pd.DataFrame, names: list[str] | None, quarter: str | None, top_n: int) -> pd.DataFrame:
    """Pick the rows to narrate.

    With ``names``: for each name fragment, the company's row in ``quarter`` if
    given, else its latest scored row (ineligible rows are kept so the skip is
    recorded). Without names: the ``top_n`` ranked rows of ``quarter`` among
    non-financial companies (``cause_unclear`` rows are skipped at narration
    time and reported, and do not consume ranks beyond ``top_n``).
    """
    if names:
        picks = []
        for n in names:
            m = flags[flags["name"].str.contains(n, case=False, na=False, regex=False)]
            if quarter:
                m = m[m["fiscal_quarter"] == pd.Period(quarter)]
            m = m[m["deterioration_score"].notna() | m["peer_weakness_score"].notna()]
            if m.empty:
                log.warning("no scored row found for %r", n)
                continue
            picks.append(m.sort_values("period_end").iloc[[-1]])
        return pd.concat(picks) if picks else flags.iloc[0:0]
    q = pd.Period(quarter) if quarter else flags.loc[flags["deterioration_score"].notna(), "fiscal_quarter"].max()
    x = flags[(flags["fiscal_quarter"] == q) & ~flags["is_financial_sector"] & flags["deterioration_score"].notna()]
    return x.nlargest(top_n, "deterioration_score")


def main() -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flags", default=str(HERE / "flags.parquet"))
    ap.add_argument("--out", default=str(HERE / "narratives.jsonl"))
    ap.add_argument("--names", help="comma-separated company-name fragments")
    ap.add_argument("--quarter", help="calendar quarter, e.g. 2026Q3")
    ap.add_argument("--top-n", type=int, default=25)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--dry-run", action="store_true", help="print the fact sheet and prompt, do not call Ollama")
    args = ap.parse_args()

    flags = add_prior_year(pd.read_parquet(args.flags))
    names = [n.strip() for n in args.names.split(",")] if args.names else None
    rows = select_rows(flags, names, args.quarter, args.top_n)
    log.info("%d row(s) selected; model %s", len(rows), args.model)

    out = []
    for _, row in rows.iterrows():
        if args.dry_run:
            reason = ineligible_reason(row)
            print(f"\n=== {row['name']} {str(row['period_end'])[:10]} ===")
            print("SKIP: " + reason if reason else _facts_text(build_facts(row)))
            continue
        rec = narrate_row(row, args.model, args.host)
        out.append(rec)
        print(f"\n=== {rec['name']} {rec['period_end']} [{rec['status']}] ===")
        print(rec.get("narrative") or rec.get("skip_reason"))
        if rec.get("violations"):
            print("VIOLATIONS:", rec["violations"])
    if out:
        with open(args.out, "w") as fh:
            for rec in out:
                fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        log.info("Wrote %d record(s) to %s", len(out), args.out)


if __name__ == "__main__":
    main()
