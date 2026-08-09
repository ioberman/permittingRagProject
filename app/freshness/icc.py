"""ICC I-Codes edition-freshness fetcher - POC, public/unofficial source.

codes.iccsafe.org itself is a JS-rendered app; its HTML shell has no code
content until JavaScript runs, so it can't be hashed/diffed with a plain GET.
Instead this fetches ICC's own sitemap, which lists one URL per I-Codes
edition year (e.g. .../codes/i-codes/2024-icodes) - a new edition year
appearing there is the cheapest signal available that a new edition shipped.

This relies on ICC's sitemap structure, not a documented API - same
unofficial/could-break-without-notice caveat as the Municode fetcher. It only
proves an edition *page exists*, not that the edition is fully published.
"""

import re

import requests

ICC_SITEMAP_URL = "https://codes.iccsafe.org/sitemap/building-codes-categories.xml"
_EDITION_URL_RE = re.compile(r"/codes/i-codes/(\d{4})-icodes")
_USER_AGENT = "Mozilla/5.0 (compatible; permitting-dev-freshness-poc/1.0)"


def fetch_icc_sitemap(timeout: int = 15) -> bytes:
    """Raises on HTTP failure - callers should catch and log, not let one bad
    fetch crash a scheduler loop (per the spec's "don't fail the whole loop
    if one source errors" requirement)."""
    response = requests.get(ICC_SITEMAP_URL, timeout=timeout, headers={"User-Agent": _USER_AGENT})
    response.raise_for_status()
    return response.content


def extract_edition_years(raw_sitemap: bytes) -> list[int]:
    """Split out from fetching so a *previously stored* raw snapshot can be
    re-parsed the same way, without re-fetching, when building a change summary."""
    text = raw_sitemap.decode("utf-8", errors="replace")
    return sorted({int(year) for year in _EDITION_URL_RE.findall(text)})
