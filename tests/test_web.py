"""HTTP-level tests for app/web.py, using Flask's test_client() against a
throwaway file-backed SQLite DB and a throwaway storage dir.

DATABASE_URL/STORAGE_ROOT must be set before app.web (and therefore app.db,
whose engine is a module-level singleton keyed off DATABASE_URL) is first
imported anywhere in the test process - see app/db.py's docstring. That's
why both are set at module import time, above the `from app.web import app`
line, rather than in a fixture (fixtures run after collection-time imports).

Only the mock/preview engines are exercised here - neither needs an API key,
so this suite (and the CI that runs it) works with zero secrets configured.
"""

import io
import os
import re
import tempfile
import time

_tmp_dir = tempfile.mkdtemp(prefix="plan_review_web_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_web.db"

import pytest

import app.clause_extraction as clause_extraction_module
import app.ingest as ingest_module
import app.web as web_module
from app.storage import LocalFileStorage

# STORAGE_ROOT alone isn't reliable here: pytest imports every conftest.py
# in the session before it imports this module, and tests/conftest.py's own
# top-level `from app.ingest import ...` already constructs app.ingest's
# module-level `storage = LocalFileStorage()` (reading STORAGE_ROOT at that
# moment) before this file's env var override ever runs - it landed in the
# real repo-root ./storage the first time this was tried. Patching the
# already-constructed objects directly sidesteps import order entirely.
_test_storage = LocalFileStorage(root=f"{_tmp_dir}/storage")


@pytest.fixture(scope="module", autouse=True)
def _patched_storage():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ingest_module, "storage", _test_storage)
    monkeypatch.setattr(clause_extraction_module, "storage", _test_storage)
    monkeypatch.setattr(web_module, "storage", _test_storage)
    yield
    monkeypatch.undo()


SPEC_TEXT = b"""4.1 Corridor width shall be a minimum of 44 inches clear.

4.2 Fire-rated door assemblies shall provide 90 minutes of protection.
"""

OTHER_DISCIPLINE_TEXT = b"""7.1 Ductwork clearance above the corridor ceiling shall be 12 inches.

7.2 Sprinkler heads shall be spaced no more than 15 feet apart.
"""


@pytest.fixture(scope="module")
def client():
    web_module.app.testing = True
    with web_module.app.test_client() as c:
        yield c


@pytest.fixture(scope="module")
def jurisdiction_id(client):
    resp = client.get("/")
    assert resp.status_code == 200
    from app.ingest import get_or_create_jurisdiction
    from app.db import get_session

    s = get_session()
    j = get_or_create_jurisdiction(s, "Test County")
    s.commit()
    jid = j.id
    s.close()
    return jid


@pytest.fixture(scope="module")
def project_id(client, jurisdiction_id):
    resp = client.post(
        "/projects",
        data={"project_name": "HTTP Test Tower", "jurisdiction_id": jurisdiction_id},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    return resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]


def _ingest(client, project_id, sheet_number, discipline, title, content, filename="sheet.txt"):
    return client.post(
        f"/projects/{project_id}/ingest",
        data={
            "sheet_number": sheet_number,
            "discipline": discipline,
            "title": title,
            "file": (io.BytesIO(content), filename),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def test_index_lists_project(client, project_id):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"HTTP Test Tower" in resp.data


def test_unknown_project_redirects_home(client):
    resp = client.get("/projects/does-not-exist", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Project not found" in resp.data


def test_ingest_two_disciplines(client, project_id):
    resp1 = _ingest(client, project_id, "A-101", "architectural", "Corridor Plan", SPEC_TEXT)
    assert resp1.status_code == 302

    resp2 = _ingest(client, project_id, "M-101", "mechanical", "Duct Layout", OTHER_DISCIPLINE_TEXT)
    assert resp2.status_code == 302

    detail = client.get(f"/projects/{project_id}")
    assert detail.status_code == 200
    assert b"A-101" in detail.data
    assert b"M-101" in detail.data


def test_delete_project_is_disabled_when_no_password_configured(client, jurisdiction_id, monkeypatch):
    project_id = _new_project(client, jurisdiction_id, "Delete Disabled Test Project")
    monkeypatch.delenv("DELETE_PASSWORD", raising=False)

    resp = client.post(f"/projects/{project_id}/delete", data={"password": ""})
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]

    still_there = client.get(f"/projects/{project_id}")
    assert still_there.status_code == 200


def test_delete_project_rejects_wrong_password(client, jurisdiction_id, monkeypatch):
    project_id = _new_project(client, jurisdiction_id, "Delete Wrong Password Test Project")
    monkeypatch.setenv("DELETE_PASSWORD", "correct-horse-battery-staple")

    resp = client.post(f"/projects/{project_id}/delete", data={"password": "guess"})
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]

    still_there = client.get(f"/projects/{project_id}")
    assert still_there.status_code == 200


def test_delete_project_succeeds_with_correct_password(client, jurisdiction_id, monkeypatch):
    project_id = _new_project(client, jurisdiction_id, "Delete Correct Password Test Project")
    monkeypatch.setenv("DELETE_PASSWORD", "correct-horse-battery-staple")

    resp = client.post(f"/projects/{project_id}/delete", data={"password": "correct-horse-battery-staple"})
    assert resp.status_code == 302
    assert "deleted=" in resp.headers["Location"]

    gone = client.get(f"/projects/{project_id}", follow_redirects=True)
    assert gone.status_code == 200
    assert b"Project not found" in gone.data


def _new_project(client, jurisdiction_id, name):
    # Isolated from the module-scoped `project_id` fixture other tests
    # share/depend on the exact contents of - these tests ingest their own
    # sheets and would otherwise pollute that shared state.
    resp = client.post("/projects", data={"project_name": name, "jurisdiction_id": jurisdiction_id}, follow_redirects=False)
    assert resp.status_code == 302
    return resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]


def test_project_page_shows_batch_upload_form_by_default(client, jurisdiction_id):
    project_id = _new_project(client, jurisdiction_id, "Batch UI Test Project")
    detail = client.get(f"/projects/{project_id}")
    assert detail.status_code == 200
    assert b'id="batch_files"' in detail.data
    assert b'multiple' in detail.data
    assert b'id="ingest_file"' not in detail.data  # single-file form only renders while revising


def test_revise_document_shows_single_file_prefilled_form(client, jurisdiction_id):
    project_id = _new_project(client, jurisdiction_id, "Revise UI Test Project")
    resp1 = _ingest(client, project_id, "R-101", "architectural", "Roof Plan", SPEC_TEXT)
    assert resp1.status_code == 302

    from app.db import get_session
    from app.models import Document, DocumentSeries

    s = get_session()
    series = s.query(DocumentSeries).filter_by(project_id=project_id, sheet_number="R-101").one()
    document = s.query(Document).filter_by(document_series_id=series.id).one()

    revise_resp = client.post(f"/documents/{document.id}/revise")
    assert revise_resp.status_code == 302
    assert "revise_sheet=R-101" in revise_resp.headers["Location"]

    follow = client.get(revise_resp.headers["Location"])
    assert follow.status_code == 200
    assert b"Revising" in follow.data
    assert b'id="ingest_file"' in follow.data  # single-file form, not the batch UI
    assert b'value="R-101"' in follow.data


def test_ingest_duplicate_sheet_number_in_same_revision_is_a_friendly_error(client, project_id):
    # Document has a (document_series_id, submission_id) unique constraint -
    # one version of a sheet per revision, by design. Uploading the same
    # sheet number twice into the same revision used to crash with a raw
    # IntegrityError/500 instead of telling the user what to do about it.
    resp = _ingest(client, project_id, "A-101", "architectural", "Corridor Plan (again)", SPEC_TEXT)
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]

    follow = client.get(resp.headers["Location"])
    assert follow.status_code == 200
    assert b"already exists" in follow.data


def test_document_clauses_search(client, project_id):
    detail = client.get(f"/projects/{project_id}")
    match = re.search(rb"/documents/([\w-]+)/clauses", detail.data)
    assert match, "expected an ingested sheet's clause link on the project page"
    document_id = match.group(1).decode()

    unfiltered = client.get(f"/documents/{document_id}/clauses")
    assert unfiltered.status_code == 200
    assert b"4.1" in unfiltered.data and b"4.2" in unfiltered.data

    hit = client.get(f"/documents/{document_id}/clauses?q=fire-rated")
    assert hit.status_code == 200
    assert b"4.2" in hit.data
    assert b"4.1" not in hit.data  # "corridor width" clause has no match for "fire-rated"

    miss = client.get(f"/documents/{document_id}/clauses?q=zzz_no_such_term")
    assert miss.status_code == 200
    assert b"No clauses match" in miss.data


def test_check_mock_engine_runs_without_error(client, project_id):
    resp = client.post(f"/projects/{project_id}/check", data={"engine": "mock", "force": "1"})
    assert resp.status_code == 302
    assert "checking=jurisdiction" in resp.headers["Location"]

    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = client.get(f"/projects/{project_id}/check-status?type=jurisdiction").get_json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status is not None and not status["running"]
    assert status["error"] is None

    flags_page = client.get(f"/projects/{project_id}/flags")
    assert flags_page.status_code == 200


def test_sheet_info_from_file_prefills_sheet_number_and_title(client):
    import fitz

    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), "General notes.\nSHEET E-201\nEnd of notes.", fontsize=10)
        pdf_bytes = doc.tobytes()

    resp = client.post(
        "/sheet-info-from-file",
        data={"file": (io.BytesIO(pdf_bytes), "Electrical_Panel_Layout.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sheet_number"] == "E-201"
    assert data["title"] == "Electrical Panel Layout"  # underscores humanized, extension stripped


def test_sheet_info_from_file_falls_back_to_filename_for_sheet_number(client):
    # No SHEET label and no oversized token inside the file - only the
    # filename itself looks like a sheet number.
    import fitz

    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), "Just some plain prose, no sheet number here.", fontsize=10)
        pdf_bytes = doc.tobytes()

    resp = client.post(
        "/sheet-info-from-file",
        data={"file": (io.BytesIO(pdf_bytes), "S-301.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sheet_number"] == "S-301"


def test_sheet_info_from_file_no_file_returns_nulls(client):
    resp = client.post("/sheet-info-from-file", data={}, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json() == {"sheet_number": None, "title": None}


def test_describe_check_error_recognizes_rate_limit_status_code():
    class _FakeRateLimitError(Exception):
        status_code = 429

    message = web_module._describe_check_error(_FakeRateLimitError("nope"), "Check failed")
    assert "rate limit" in message.lower() or "quota" in message.lower()
    assert "Check failed" not in message  # friendly message replaces the generic wrapper, doesn't just prefix it


def test_describe_check_error_falls_back_to_generic_message_for_other_errors():
    message = web_module._describe_check_error(ValueError("boom"), "Check failed")
    assert message == "Check failed: boom"


def test_check_cross_discipline_preview_shows_candidates(client, project_id):
    resp = client.post(
        f"/projects/{project_id}/check-cross-discipline",
        data={"engine": "preview"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # The candidates page labels rows by discipline + clause label (e.g.
    # "architectural 4.1"), not by sheet number - see
    # cross_discipline_candidates.html.
    assert b"architectural" in resp.data
    assert b"mechanical" in resp.data or b"no candidates" in resp.data


def test_document_graph_json(client, project_id):
    resp = client.get(f"/projects/{project_id}/document-graph")
    assert resp.status_code == 200
    data = resp.get_json()
    assert {n["id"] for n in data["nodes"]} == {"A-101", "M-101"}
    # A-101/M-101 are different disciplines with retrieval-similar text
    # (see SPEC_TEXT/OTHER_DISCIPLINE_TEXT above), so at least one edge is
    # expected; no cross-discipline check has run in this test, so nothing
    # should be flagged/severity-colored yet.
    assert len(data["edges"]) >= 1
    assert all(e["severity"] is None for e in data["edges"])


def test_document_graph_unknown_project_404s(client):
    resp = client.get("/projects/does-not-exist/document-graph")
    assert resp.status_code == 404


def test_new_revision_and_diff(client, project_id):
    resp = client.post(f"/projects/{project_id}/new-revision", follow_redirects=False)
    assert resp.status_code == 302

    diff_resp = client.get(f"/projects/{project_id}/diff?from=1&to=2")
    assert diff_resp.status_code == 200


def test_audit_report_html_and_csv(client, project_id):
    html_resp = client.get(f"/projects/{project_id}/audit-report")
    assert html_resp.status_code == 200
    assert b"HTTP Test Tower" in html_resp.data

    csv_resp = client.get(f"/projects/{project_id}/audit-report.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["Content-Type"].startswith("text/csv")


def test_jurisdictions_page_loads(client):
    resp = client.get("/jurisdictions")
    assert resp.status_code == 200
    assert b'id="jurisdiction-files"' in resp.data
    assert b'multiple' in resp.data


def test_upload_request_limit_is_configured():
    assert web_module.app.config["MAX_CONTENT_LENGTH"] == 100 * 1024 * 1024


def test_jurisdiction_batch_document_upload(client):
    jurisdiction_name = "Batch Upload County"
    resp = client.post(
        "/jurisdictions/documents",
        data={
            "jurisdiction_name": jurisdiction_name,
            "titles": ["Residential Amendments", "Permit Guide"],
            "files": [
                (io.BytesIO(b"R101.1 Residential amendment text."), "residential_amendments.txt"),
                (io.BytesIO(b"1.1 Permit application guide text."), "permit_guide.txt"),
            ],
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    from app.db import get_session
    from app.models import Jurisdiction, JurisdictionDocument, JurisdictionClause

    session = get_session()
    jurisdiction = session.query(Jurisdiction).filter_by(name=jurisdiction_name).one()
    # Redirects to the jurisdiction's own detail page (not the flat list) so
    # an upload lands you where you can immediately see what you just added -
    # see app/web.py's add_jurisdiction_document.
    assert resp.headers["Location"] == f"/jurisdictions/{jurisdiction.id}"
    documents = (
        session.query(JurisdictionDocument)
        .filter_by(jurisdiction_id=jurisdiction.id)
        .order_by(JurisdictionDocument.title)
        .all()
    )
    assert [document.title for document in documents] == ["Permit Guide", "Residential Amendments"]
    assert all(
        session.query(JurisdictionClause).filter_by(jurisdiction_document_id=document.id).count() >= 1
        for document in documents
    )
