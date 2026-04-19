"""Minimal rate-limited client for the SEC EDGAR public APIs.

Usage:
    from src.edgar_client import EdgarClient

    client = EdgarClient(user_agent="Your Name you@example.com")
    subs = client.submissions(cik="0000320193")   # Apple
    facts = client.company_facts(cik="0000320193")

Notes
-----
* The SEC enforces a 10 req/s rate limit per IP. This client sleeps between
  calls to stay well under it. If you get 403 Forbidden, check your User-Agent
  header first, then throttle further.
* CIKs must be zero-padded to 10 digits in the URL. The client does this for
  you — you can pass either an int or a string.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests


BASE = "https://data.sec.gov"
ARCHIVE = "https://www.sec.gov"


class EdgarClient:
    """Thin wrapper around the EDGAR JSON endpoints with polite defaults."""

    def __init__(
        self,
        user_agent: str | None = None,
        min_interval: float = 0.12,  # ~8 req/s, safely under the 10 req/s cap
    ) -> None:
        ua = user_agent or os.getenv("SEC_USER_AGENT")
        if not ua:
            raise ValueError(
                "Set a User-Agent. Either pass user_agent='Your Name you@example.com' "
                "or export SEC_USER_AGENT in your environment."
            )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval
        self._last_call: float = 0.0

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _pad_cik(cik: int | str) -> str:
        """Return the 10-digit zero-padded 'CIK##########' string."""
        digits = str(cik).lstrip("CIK").lstrip("cik").lstrip("0") or "0"
        return f"CIK{int(digits):010d}"

    def _get(self, url: str) -> Any:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        r = self.session.get(url, timeout=30)
        self._last_call = time.monotonic()
        r.raise_for_status()
        return r.json()

    # -- endpoints --------------------------------------------------------

    def company_tickers(self) -> dict:
        """Ticker-to-CIK mapping for every filer (~10k entries)."""
        return self._get(f"{ARCHIVE}/files/company_tickers.json")

    def company_tickers_exchange(self) -> dict:
        """Same as company_tickers but includes listing exchange."""
        return self._get(f"{ARCHIVE}/files/company_tickers_exchange.json")

    def submissions(self, cik: int | str) -> dict:
        """One filer's full filing history + entity metadata."""
        return self._get(f"{BASE}/submissions/{self._pad_cik(cik)}.json")

    def company_facts(self, cik: int | str) -> dict:
        """Every XBRL-tagged fact a company has ever filed."""
        return self._get(f"{BASE}/api/xbrl/companyfacts/{self._pad_cik(cik)}.json")

    def company_concept(self, cik: int | str, taxonomy: str, tag: str) -> dict:
        """Full time-series for one (company, taxonomy, tag)."""
        return self._get(
            f"{BASE}/api/xbrl/companyconcept/{self._pad_cik(cik)}/{taxonomy}/{tag}.json"
        )

    def frames(self, taxonomy: str, tag: str, unit: str, period: str) -> dict:
        """Cross-company snapshot for one tag in one calendar period.

        `period` examples: 'CY2023' (annual), 'CY2023Q4' (quarter, duration),
        'CY2023Q4I' (quarter-end instant).
        """
        return self._get(f"{BASE}/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json")


if __name__ == "__main__":
    # Sanity check: resolve AAPL -> CIK -> latest Revenues observations.
    client = EdgarClient()   # expects SEC_USER_AGENT env var
    tickers = client.company_tickers()
    aapl = next(v for v in tickers.values() if v["ticker"] == "AAPL")
    print(f"Found AAPL: CIK={aapl['cik_str']}")

    rev = client.company_concept(aapl["cik_str"], "us-gaap", "Revenues")
    latest = sorted(rev["units"]["USD"], key=lambda o: o["filed"])[-5:]
    print("\nLast 5 Revenue observations:")
    for o in latest:
        print(f"  {o['end']:>10}  ${o['val']:>18,}  ({o['form']} filed {o['filed']})")
