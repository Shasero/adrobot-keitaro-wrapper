import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.services.offer_shares import EditorError
from app.services.offer_store import Store, _is_memory_sqlite


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        ("sqlite://", True),
        ("sqlite:///:memory:", True),
        ("sqlite:///", True),
        ("sqlite:///file::memory:?cache=shared&uri=true", True),
        ("sqlite:///file:memdb1?mode=memory&cache=shared&uri=true", True),
        ("sqlite:///./adrobot.db", False),
        ("sqlite:///file:memdb1?cache=shared&uri=true", False),
        ("sqlite:///file::memory:", False),
        ("postgresql://user:pass@localhost/adrobot", False),
    ],
)
def test_memory_sqlite_url_detection(database_url, expected):
    assert _is_memory_sqlite(make_url(database_url)) is expected


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///",
        "sqlite:///file::memory:?cache=shared&uri=true",
        "sqlite:///file:memdb1?mode=memory&cache=shared&uri=true",
    ],
)
def test_publication_lock_rejects_memory_sqlite(database_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = create_engine(database_url)
    try:
        store = Store(sessionmaker(bind=engine), campaign_id=1, stream_id=2)

        with pytest.raises(EditorError, match="storage"):
            with store.publication_lock():
                pytest.fail("An in-memory SQLite database must not acquire a file lock")

        assert not (tmp_path / ".adrobot-editor-locks").exists()
    finally:
        engine.dispose()
