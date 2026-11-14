"""Refresh already-loaded companies with filings submitted since they were loaded.

Why this exists
---------------
etl_financial.run_bulk_load() skips every CIK once ``entity.loaded_at`` is set,
so by itself it never picks up a company's later 10-K / 10-Q filings. This
script revisits loaded companies and re-ingests only those that have new
filings.

How it works (two tiers, cheap check first)
-------------------------------------------
1. For every CIK in ``entity.entity``, fetch its submissions document (one
   small request) and list its 10-K / 10-Q filings with the same parser the
   bulk load uses.
2. Compare their accession numbers with the ones already stored in
   ``"10k".filing`` / ``"10q".filing``.
   * Any accession not stored, or the CIK has ``loaded_at IS NULL`` (an earlier
     load never finished)  ->  re-ingest the company with
     ``etl_financial.load_company_by_cik``. That refetches every XBRL fact
     (the companyfacts endpoint has no delta mode) and replaces the company's
     stored facts, which also picks up newly available quarters.
   * Nothing new  ->  skip the expensive refetch and only stamp
     ``last_checked_at``.

``loaded_at`` is cleared before a re-ingest, so a crash part-way leaves the CIK
marked incomplete and it is picked up again on the next run (filings are written
before facts, so without this a failure could leave new filings recorded but
their facts missing, and the next refresh would see nothing new).

``entity.last_checked_at`` records when each company was last compared to the
SEC, whether or not anything changed, so staleness is one query away:

    SELECT MIN(last_checked_at), MAX(last_checked_at) FROM entity.entity;

Usage
-----
    python3 -m src.etl_refresh                # refresh everything stale
    python3 -m src.etl_refresh --dry-run      # report what would be refreshed, write nothing
    python3 -m src.etl_refresh --limit 200    # only check the first N CIKs (testing)

Run from the ``edgar`` project folder. Do not run it at the same time as
``etl_financial`` (both write the same tables).

Cost: one submissions request per company (~9 requests/second shared rate
limit) plus one extra companyfacts request per company that has new filings.
Re-run ``signals`` (feature_extraction, scoring) afterwards so scores reflect
the new data.

Requirements
------------
Same ``.env`` as etl_financial.py (``SEC_USER_AGENT`` and the DB settings).
"""
from __future__ import annotations

import argparse
import logging
import os

from dotenv import load_dotenv
from tqdm import tqdm

from src import db
from src.edgar_client import EdgarClient
from src.etl_financial import _parse_filings, load_company_by_cik

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_refresh(dry_run: bool = False, limit: int | None = None) -> dict[str, int]:
    """Check every entity against its submissions and re-ingest the stale ones.

    Args:
        dry_run: if True, only report what would be refreshed; nothing is written.
        limit: if set, only the first ``limit`` CIKs (sorted) are checked.

    Returns:
        Counts: ``checked``, ``refreshed`` (re-ingested, or would be in a dry
        run), ``incomplete`` (of those, CIKs whose earlier load never
        finished), ``unchanged`` and ``failed``.
    """
    user_agent = os.getenv("SEC_USER_AGENT")
    if not user_agent:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. Add it to your .env:\n"
            "  SEC_USER_AGENT=Your Name your@email.com"
        )

    client = EdgarClient(user_agent=user_agent)
    conn = db.get_connection()
    db.create_schema(conn)  # also applies the last_checked_at column if missing

    ciks = db.get_all_entity_ciks(conn)
    if limit:
        ciks = ciks[:limit]
    loaded = db.get_loaded_ciks(conn)
    known = db.get_known_accessions(conn)
    log.info(f"Checking {len(ciks):,} companies{' (dry run, nothing will be written)' if dry_run else ''}")

    counts = {"checked": 0, "refreshed": 0, "incomplete": 0, "unchanged": 0, "failed": 0}
    failed: list[str] = []

    for cik in tqdm(ciks, unit="co", desc="Checking"):
        counts["checked"] += 1
        try:
            subs = client.submissions(cik)
            stored = known.get(cik, set())
            new_accessions = [f["accession_number"] for f in _parse_filings(cik, subs)
                              if f["accession_number"] not in stored]
            incomplete = cik not in loaded

            if new_accessions or incomplete:
                counts["refreshed"] += 1
                counts["incomplete"] += int(incomplete)
                if not dry_run:
                    db.clear_loaded(conn, cik)
                    load_company_by_cik(client, conn, cik, subs=subs)
            else:
                counts["unchanged"] += 1
                if not dry_run:
                    db.mark_checked(conn, cik)
        except Exception as exc:
            conn.rollback()
            counts["failed"] += 1
            failed.append(cik)
            log.warning(f"FAILED {cik}: {exc}")

    log.info(
        f"Done. checked={counts['checked']:,} refreshed={counts['refreshed']:,} "
        f"(of which {counts['incomplete']:,} were incomplete loads) "
        f"unchanged={counts['unchanged']:,} failed={counts['failed']:,}"
    )
    if failed:
        log.info(f"Failed CIKs (retried automatically next run): {failed}")
    conn.close()
    return counts


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would be refreshed; write nothing")
    parser.add_argument("--limit", type=int, default=None, help="only check the first N CIKs (testing)")
    args = parser.parse_args()
    run_refresh(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
