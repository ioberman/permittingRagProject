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
