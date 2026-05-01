"""Rate-limited HTTP client for the SEC EDGAR APIs.

Shared by both pipelines — one instance should be passed to both etl_financial.py and
etl_13f.py so all outbound requests draw from the same 9 req/s budget.

Pipeline → method map
─────────────────────
Both pipelines
  submissions()               entity metadata for any CIK (company or manager)

Company fundamentals (etl_financial.py)
  company_tickers_exchange()  seed the universe of ticker-bearing companies
  company_facts()             XBRL financial facts (us-gaap); not available for
                              most investment managers

Asset manager / 13F (etl_13f.py)
  quarterly_index_13f()       discover 13F filer CIKs from quarterly full-index
  filing_index()              directory listing for one filing's archive folder
  filing_document()           raw document bytes from the filing archive
"""
from __future__ import annotations

import gzip
import time
import threading
from typing import Iterator

import requests

_BASE_DATA = "https://data.sec.gov"   # XBRL / submissions JSON API
_BASE_WWW  = "https://www.sec.gov"    # archive files, ticker lists, full-index

_13F_FORMS = frozenset({"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"})


class _RateLimiter:
    """Enforces a minimum interval between requests. Thread-safe."""

    def __init__(self, max_per_second: float = 9.0):
        self._interval = 1.0 / max_per_second
        self._last: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


class EdgarClient:
    """Thin wrapper around the SEC EDGAR APIs with shared rate limiting.

    Instantiate once and pass to both run_bulk_load() and run_13f_load() so
    both pipelines share the 9 req/s budget rather than each running at full
    speed and colliding at the SEC's 10 req/s hard limit.
    """

    def __init__(self, user_agent: str, max_per_second: float = 9.0):
        self._rl = _RateLimiter(max_per_second)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent

    def _get(self, url: str, **kwargs) -> requests.Response:
        self._rl.wait()
        resp = self._session.get(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    # ── Shared — entity metadata (both pipelines) ─────────────────────────────

    def submissions(self, cik: str) -> dict:
        """Fetch entity metadata + full filing history for any CIK.

        Used by the company pipeline to populate edgar.company / ticker /
        filing, and by the 13F pipeline to populate edgar.investment_manager /
        manager_filing. The response schema is identical for both entity types.
        """
        return self._get(f"{_BASE_DATA}/submissions/CIK{cik}.json").json()

    # ── Company fundamentals pipeline only (etl_financial.py) ───────────────────────────

    def company_tickers_exchange(self) -> dict:
        """Fetch the full universe of ticker-bearing companies with exchange info.

        Returns ~10 k rows in {fields, data} columnar format. Used by the
        company pipeline to seed the CIK work queue. Investment managers that
        lack a public ticker are absent from this list — the 13F pipeline
        discovers them through quarterly_index_13f() instead.
        """
        return self._get(f"{_BASE_WWW}/files/company_tickers_exchange.json").json()

    def company_facts(self, cik: str) -> dict:
        """Fetch all XBRL-tagged facts ever reported by a company.

        Returns every us-gaap (and other taxonomy) observation organized as
        facts → taxonomy → tag → unit → [observations]. Used exclusively by
        the company fundamentals pipeline to populate edgar.financial_fact.
        Most investment managers do not file XBRL data, so this endpoint
        returns 404 for the majority of 13F-only filers.
        """
        return self._get(f"{_BASE_DATA}/api/xbrl/companyfacts/CIK{cik}.json").json()

    # ── Asset manager pipeline only (etl_13f.py) — 13F archive fetches ────────

    def filing_index(self, cik: str, accession: str) -> dict:
        """Fetch the directory listing (index.json) for one filing's archive folder.

        Used by the 13F pipeline to locate the information table XML inside each
        13F-HR / 13F-HR/A filing package before calling filing_document().
        """
        accn = accession.replace("-", "")
        return self._get(
            f"{_BASE_WWW}/Archives/edgar/data/{int(cik)}/{accn}/index.json"
        ).json()

    def filing_document(self, cik: str, accession: str, filename: str) -> bytes:
        """Fetch a raw document from the filing archive.

        Used by the 13F pipeline after filing_index() identifies the
        information table filename (type == 'INFORMATION TABLE').
        """
        accn = accession.replace("-", "")
        return self._get(
            f"{_BASE_WWW}/Archives/edgar/data/{int(cik)}/{accn}/{filename}"
        ).content

    # ── Asset manager pipeline only (etl_13f.py) — 13F filer seeding ──────────

    def quarterly_index_13f(self, year: int, quarter: int) -> Iterator[dict]:
        """Yield one row per 13F filing in a quarterly full-index file.

        Used exclusively by the 13F pipeline to discover manager CIKs that are
        not present in company_tickers_exchange() (most investment managers
        have no publicly traded ticker).

        Fetches and parses company.gz from EDGAR's quarterly full-index at:
            /Archives/edgar/full-index/{year}/QTR{quarter}/company.gz

        company.gz format: pipe-delimited, header line then a separator line of
        dashes, then data rows:
            Company Name|Form Type|CIK|Date Filed|Filename

        Only rows whose Form Type is in {13F-HR, 13F-HR/A, 13F-NT, 13F-NT/A}
        are yielded. All other form types are discarded during parsing.
        """
        url = f"{_BASE_WWW}/Archives/edgar/full-index/{year}/QTR{quarter}/company.gz"
        raw = gzip.decompress(self._get(url).content).decode("latin-1")

        col_map: dict[str, int] = {}
        in_data = False

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("---"):
                in_data = True
                continue
            if not in_data:
                if "|" in stripped:
                    col_map = {
                        c.strip().lower(): i
                        for i, c in enumerate(stripped.split("|"))
                    }
                continue

            parts = stripped.split("|")
            # Positional fallbacks match the documented column order in case
            # the header line is absent or uses unexpected column names.
            ni = col_map.get("company name", 0)
            fi = col_map.get("form type",    1)
            ci = col_map.get("cik",          2)
            di = col_map.get("date filed",   3)
            pi = col_map.get("filename",     4)

            if len(parts) <= max(ni, fi, ci, di, pi):
                continue
            form = parts[fi].strip()
            if form not in _13F_FORMS:
                continue
            cik_raw = parts[ci].strip()
            if not cik_raw.isdigit():
                continue
            yield {
                "company_name": parts[ni].strip(),
                "form_type":    form,
                "cik":          str(int(cik_raw)).zfill(10),
                "date_filed":   parts[di].strip(),
                "filename":     parts[pi].strip(),
            }
