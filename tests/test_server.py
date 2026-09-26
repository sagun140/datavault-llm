"""The front end's API surface answers without raising."""

import json

import pytest

from dvl import server
from dvl.extract import Fact
from dvl.vault import Vault
from dvl.wiki import Revision


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    v = Vault(path)
    v.load_revision(
        Revision(title="Acme", pageid=1, revid=1, timestamp="2024-01-01T00:00:00Z",
                 wikitext="[[Acme]] is a [[company]] that ships things worldwide."),
        [Fact("Acme", "founded", "1999", "| founded = 1999")],
    )
    v.close()
    return path


@pytest.mark.parametrize("path,qs", [
    ("/api/overview", {}),
    ("/api/ask", {"q": ["when was acme founded"]}),
    ("/api/table", {"name": ["hub_entity"]}),
    ("/api/sentences", {"entity": ["Acme"]}),
    ("/api/generate", {"entity": ["Acme"]}),
    ("/api/updates", {}),
    ("/api/conflicts", {}),
])
def test_every_endpoint_returns_serializable_json(db, path, qs):
    v = Vault(db)
    try:
        payload = server.ROUTES[path](v, qs)
    finally:
        v.close()

    assert "error" not in payload
    json.dumps(payload, default=str)


def test_the_table_endpoint_rejects_an_unknown_table(db):
    v = Vault(db)
    try:
        assert "error" in server.ROUTES["/api/table"](v, {"name": ["sqlite_master"]})
    finally:
        v.close()
