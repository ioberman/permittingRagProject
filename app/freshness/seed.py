"""Ensures the fixed, hardcoded set of demo Jurisdiction/FreshnessSource rows
exist. Idempotent (checks before inserting) so it's safe to call on every app
startup, the same way app/db.py's init_db() is - no separate migration step
needed to introduce a new default source later.
"""

from app.freshness.icc import ICC_SITEMAP_URL
from app.models import FreshnessSource, FreshnessSourceKind, Jurisdiction

_ICC_DAILY_INTERVAL_SECONDS = 24 * 60 * 60
_MUNICODE_DEFAULT_INTERVAL_SECONDS = 4 * 60 * 60
# State code PDFs are large (hundreds of pages) and change far less often
# than a local ordinance - daily is plenty, and doesn't hammer a state
# government's PDF server for no reason.
_STATE_CODE_DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60

# Each entry confirmed live against api.municode.com while building this
# feature (see app/freshness/municode.py) - not guessed from a URL pattern.
# City-proper picks that turned out to be stale/unreachable and were swapped
# for a real alternative:
#   - Pittsburgh, PA -> Marshall Township (Allegheny County): Pittsburgh's
#     actual current code moved to eCode360, which is Cloudflare-protected;
#     its Municode listing is stale/abandoned.
#   - Boston, MA: excluded outright - Municode only has its Redevelopment
#     Authority content, not a real Code of Ordinances; Boston's actual code
#     is on American Legal Publishing.
_MUNICODE_JURISDICTIONS = [
    {
        "jurisdiction_name": "Marshall Township, Allegheny County, PA",
        "label": "Marshall Township, Allegheny County, PA - Building Construction Code (Ch. 52)",
        "product_id": 15481,
        "node_id": "PTIIGELE_CH52BUCOCO",
    },
    {
        "jurisdiction_name": "Providence, RI",
        "label": "Providence, RI - Buildings and Structural Appurtenances (Ch. 5)",
        "product_id": 11458,
        "node_id": "PTIICOOR_CH5BUSTAP",
    },
    {
        "jurisdiction_name": "Hartford, CT",
        "label": "Hartford, CT - Buildings and Property (Ch. 9)",
        "product_id": 10895,
        "node_id": "PTIIMUCO_CH9BUPR",
    },
    {
        "jurisdiction_name": "Jersey City, NJ",
        "label": "Jersey City, NJ - Construction Codes, Uniform (Ch. 131)",
        "product_id": 16093,
        "node_id": "CH131COCOUN",
    },
    {
        # A second CT jurisdiction alongside Hartford, deliberately - to
        # exercise the UI for multiple jurisdictions in the same state (map
        # coloring/tooltip listing more than one entry per state), not
        # because Bridgeport's coverage gap needed filling on its own.
        # Chapter-level node, not the Title-15 node it lives under - the
        # Title-level node only returns table-of-contents entries (empty
        # Content on every child), confirmed live while setting this up;
        # only a specific chapter's node returns real section text.
        "jurisdiction_name": "Bridgeport, CT",
        "label": "Bridgeport, CT - Building Permits and Fees (Ch. 15.08)",
        "product_id": 16075,
        "node_id": "TIT15BUCO_CH15.08BUPEFE",
    },
    {
        # Washington County, not Allegheny County like Marshall Township -
        # PA has no real county-level Municode clients (confirmed against
        # the live API - PA building-code enforcement is municipal/township,
        # not county), so this is the closest real match to "another county
        # near Pittsburgh": a township in the neighboring county.
        "jurisdiction_name": "North Strabane Township, Washington County, PA",
        "label": "North Strabane Township, Washington County, PA - Code Enforcement (Ch. 5)",
        "product_id": 17363,
        "node_id": "CH5COEN",
    },
]

# Official state-published building code PDFs - each confirmed by hand
# (fetched, opened, first-page text checked) to be a real, current government
# document, not guessed from a URL pattern. Only wired up where a state
# actually publishes one for free - not every state does. Notably absent:
# Pennsylvania (Marshall Township's state) - PA incorporates IBC by reference
# without republishing the full text for free; the only full-text sources are
# ICC's paywall or UpCodes, so no PA entry exists here.
_STATE_CODE_JURISDICTIONS = [
    {
        "jurisdiction_name": "Hartford, CT",
        "label": "Connecticut State Building Code (2022, incl. 2021 IBC portion)",
        "url": "https://portal.ct.gov/-/media/das/office-of-state-building-inspector/2022-state-codes/2022-csbc-final.pdf",
    },
    {
        "jurisdiction_name": "Providence, RI",
        "label": "Rhode Island State Building Code (SBC-1)",
        "url": "https://risos-apa-production-public.s3.amazonaws.com/BCSC/5976.pdf",
    },
    {
        "jurisdiction_name": "Jersey City, NJ",
        "label": "New Jersey Uniform Construction Code - Building Subcode (N.J.A.C. 5:23-3)",
        "url": "https://www.nj.gov/dca/codes/codreg/pdf_regs/njac_5_23_3.pdf",
    },
    {
        # Same PDF as Hartford, deliberately - see _ensure_state_code_source's
        # docstring on why a shared source_ref must not collapse into one row.
        "jurisdiction_name": "Bridgeport, CT",
        "label": "Connecticut State Building Code (2022, incl. 2021 IBC portion)",
        "url": "https://portal.ct.gov/-/media/das/office-of-state-building-inspector/2022-state-codes/2022-csbc-final.pdf",
    },
]


def _ensure_icc_source(session) -> None:
    exists = (
        session.query(FreshnessSource)
        .filter_by(kind=FreshnessSourceKind.ICC_PUBLIC, source_ref=ICC_SITEMAP_URL)
        .first()
    )
    if exists is not None:
        return

    session.add(
        FreshnessSource(
            kind=FreshnessSourceKind.ICC_PUBLIC,
            label="ICC I-Codes edition sitemap",
            source_ref=ICC_SITEMAP_URL,
            check_interval_seconds=_ICC_DAILY_INTERVAL_SECONDS,
        )
    )
    session.commit()


def _ensure_municode_source(session, entry: dict) -> None:
    source_ref = f"{entry['product_id']}:{entry['node_id']}"
    exists = session.query(FreshnessSource).filter_by(kind=FreshnessSourceKind.MUNICODE, source_ref=source_ref).first()
    if exists is not None:
        return

    jurisdiction = session.query(Jurisdiction).filter_by(name=entry["jurisdiction_name"]).first()
    if jurisdiction is None:
        jurisdiction = Jurisdiction(name=entry["jurisdiction_name"])
        session.add(jurisdiction)
        session.flush()  # assigns jurisdiction.id, needed below before commit

    session.add(
        FreshnessSource(
            kind=FreshnessSourceKind.MUNICODE,
            label=entry["label"],
            jurisdiction_id=jurisdiction.id,
            source_ref=source_ref,
            check_interval_seconds=_MUNICODE_DEFAULT_INTERVAL_SECONDS,
        )
    )
    session.commit()


def _ensure_state_code_source(session, entry: dict) -> None:
    """A (kind, source_ref) pair is not unique on its own - the same state
    code URL is deliberately reused across every jurisdiction in that state
    that's configured (see the module docstring), so a jurisdiction with no
    FreshnessSource of its own yet must not be skipped just because some
    *other* jurisdiction already has a row pointing at the same URL."""
    jurisdiction = session.query(Jurisdiction).filter_by(name=entry["jurisdiction_name"]).first()
    if jurisdiction is None:
        jurisdiction = Jurisdiction(name=entry["jurisdiction_name"])
        session.add(jurisdiction)
        session.flush()  # assigns jurisdiction.id, needed below before commit

    exists = (
        session.query(FreshnessSource)
        .filter_by(
            kind=FreshnessSourceKind.STATE_CODE_PDF,
            source_ref=entry["url"],
            jurisdiction_id=jurisdiction.id,
        )
        .first()
    )
    if exists is not None:
        return

    session.add(
        FreshnessSource(
            kind=FreshnessSourceKind.STATE_CODE_PDF,
            label=entry["label"],
            jurisdiction_id=jurisdiction.id,
            source_ref=entry["url"],
            check_interval_seconds=_STATE_CODE_DEFAULT_INTERVAL_SECONDS,
        )
    )
    session.commit()


def ensure_default_sources(session) -> None:
    _ensure_icc_source(session)
    for entry in _MUNICODE_JURISDICTIONS:
        _ensure_municode_source(session, entry)
    for entry in _STATE_CODE_JURISDICTIONS:
        _ensure_state_code_source(session, entry)
