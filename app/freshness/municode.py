"""Municode ordinance-content fetcher - POC, public/unofficial source.

Uses api.municode.com directly (documented informally at
https://sr.ht/~partytax/unofficial-municode-api-documentation/), not the
library.municode.com pages themselves, which are a client-rendered SPA with no
content in the raw HTML. This is genuinely unofficial and undocumented by
Municode - confirmed live by trial against real jurisdictions while building
this, and some of the 2-year-old reference doc's endpoints (e.g. /Clients/name)
no longer behave as documented. Expect further drift without notice; every
call here should be treated as able to fail or change shape at any time.

FreshnessSource.source_ref for a MUNICODE-kind source encodes both IDs
CodesContent needs, as "{product_id}:{node_id}" (colon-separated - node IDs
never contain a colon), since the schema has one generic source_ref field
shared with ICC_PUBLIC's plain URL.
"""

import json
import re

import requests

MUNICODE_API_BASE = "https://api.municode.com"
_USER_AGENT = "Mozilla/5.0 (compatible; permitting-dev-freshness-poc/1.0)"
_REFERER = "https://library.municode.com/"

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def parse_source_ref(source_ref: str) -> tuple[int, str]:
    product_id_str, node_id = source_ref.split(":", 1)
    return int(product_id_str), node_id


def fetch_municode_content(node_id: str, product_id: int, timeout: int = 20) -> bytes:
    """Raises on HTTP failure - callers should catch and log, not let one bad
    fetch crash a scheduler loop, same as app/freshness/icc.py."""
    response = requests.get(
        f"{MUNICODE_API_BASE}/CodesContent",
        params={"nodeId": node_id, "productId": product_id},
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT, "Referer": _REFERER},
    )
    response.raise_for_status()
    return response.content


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    return _WHITESPACE_RE.sub(" ", text).strip()


def extract_sections(raw_content: bytes) -> list[tuple[str, str, str]]:
    """Returns [(doc_id, title, stripped_text), ...] for each chunk CodesContent
    returned, in document order. Kept separate from fetching so a previously
    stored raw snapshot can be re-parsed the same way without re-fetching."""
    data = json.loads(raw_content)
    sections = []
    for doc in data.get("Docs", []):
        doc_id = doc.get("Id", "")
        title = doc.get("Title", "")
        text = _strip_html(doc.get("Content", ""))
        sections.append((doc_id, title, text))
    return sections


def normalize_sections(sections: list[tuple[str, str, str]]) -> str:
    """One line per section, tab-separated, so a line-level diff maps directly
    onto "which section changed" - what the spec asks the change summary to
    convey, without needing real semantic diffing."""
    return "\n".join(f"{doc_id}\t{title}\t{text}" for doc_id, title, text in sections)
