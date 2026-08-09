"""State-published building code PDF fetcher - POC scope, but a genuinely
official/public-record source (a .gov PDF), unlike ICC (paywalled SPA) or
Municode (unofficial reverse-engineered API). Several states publish their
full, effective building code - the base model code (e.g. IBC) merged with
that state's own amendments - as a single free PDF. That's a real gap-filler:
a jurisdiction's local Municode amendment ordinance only covers what it
changed, not the underlying code those amendments modify - see the
"shouldn't the system know the national code" conversation this came out of.

Confirmed manually per state before wiring up (this is not available
everywhere - e.g. Pennsylvania has no equivalent free full-text PDF, only
paywalled ICC/UpCodes access, so no PA source is configured).

Reuses the exact same PDF text extraction already used for human-uploaded
jurisdiction documents (app/clause_extraction.py), since it's the same kind
of PDF, just fetched automatically on a schedule instead of uploaded by hand.
"""

import requests

from app.clause_extraction import extract_pages, split_into_clauses
from app.models import DocType

_USER_AGENT = "Mozilla/5.0 (compatible; permitting-dev-freshness-poc/1.0)"


def fetch_state_code_pdf(url: str, timeout: int = 30) -> bytes:
    """Raises on HTTP failure - callers should catch and log, not let one bad
    fetch crash a scheduler loop, same as the other freshness fetchers."""
    response = requests.get(url, timeout=timeout, headers={"User-Agent": _USER_AGENT})
    response.raise_for_status()
    return response.content


def extract_sections(raw_pdf: bytes) -> list[tuple[str, str, str]]:
    """Returns [(doc_id, label, text), ...] - one entry per clause split out
    of each page. doc_id embeds the page number so it's usable directly as
    JurisdictionClause.location without a second extraction pass, matching
    how app/freshness/municode.py's doc_id (a Municode node ID) is reused the
    same way."""
    pages = extract_pages(raw_pdf, DocType.PDF_2D, "state_code.pdf")
    sections = []
    for page_number, page_text in enumerate(pages, start=1):
        for label, body in split_into_clauses(page_text):
            sections.append((f"p{page_number}-{label}", label, body))
    return sections


def normalize_sections(sections: list[tuple[str, str, str]]) -> str:
    """One line per section, tab-separated - matches app/freshness/municode.py's
    convention so a line-level diff maps directly onto "which section changed"."""
    return "\n".join(f"{doc_id}\t{title}\t{text}" for doc_id, title, text in sections)
