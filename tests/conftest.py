import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.ingest import get_or_create_jurisdiction
from app.models import Base
from app.storage import LocalFileStorage


@pytest.fixture
def session():
    """Fresh in-memory SQLite DB per test. StaticPool keeps the same connection
    alive for the whole test, since a plain :memory: DB would otherwise vanish
    between connections."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def jurisdiction(session):
    """A default jurisdiction for tests that just need a valid jurisdiction_id
    and don't care about jurisdiction behavior itself."""
    return get_or_create_jurisdiction(session, "Test County")


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Points app.ingest's and app.clause_extraction's storage at a temp dir,
    so tests never touch the real ./storage directory."""
    test_storage = LocalFileStorage(root=str(tmp_path / "storage"))
    monkeypatch.setattr("app.ingest.storage", test_storage)
    monkeypatch.setattr("app.clause_extraction.storage", test_storage)
    return test_storage
