"""The guarantees, tested offline against a synthetic revision."""

import pytest

from dvl.extract import Fact
from dvl.query import ask
from dvl.vault import Vault
from dvl.verify import Grounding_Error, assert_grounded, ungrounded_tokens
from dvl.wiki import Revision


def rev(revid: int, ts: str, wikitext: str) -> Revision:
    return Revision(title="Acme", pageid=1, revid=revid, timestamp=ts, wikitext=wikitext)


@pytest.fixture
def vault():
    v = Vault(":memory:")
    yield v
    v.close()


SRC_V1 = "| founded = 1999\n| num_employees = 100"
SRC_V2 = "| founded = 1999\n| num_employees = 250"


def test_reloading_identical_content_is_a_noop(vault):
    r = rev(1, "2023-01-01T00:00:00Z", SRC_V1)
    facts = [Fact("Acme", "founded", "1999", "| founded = 1999")]

    first = vault.load_revision(r, facts)
    second = vault.load_revision(rev(2, "2024-01-01T00:00:00Z", SRC_V1), facts)

    assert first.inserted == 1
    assert second.inserted == 0 and second.changed == 0 and second.unchanged == 1
    assert vault.stats()["satellite rows"] == 1


def test_a_changed_value_end_dates_the_old_row_and_opens_a_new_one(vault):
    vault.load_revision(
        rev(1, "2023-01-01T00:00:00Z", SRC_V1),
        [Fact("Acme", "num_employees", "100", "| num_employees = 100")],
    )
    report = vault.load_revision(
        rev(2, "2024-01-01T00:00:00Z", SRC_V2),
        [Fact("Acme", "num_employees", "250", "| num_employees = 250")],
    )

    assert report.changed == 1
    history = vault.history()
    assert len(history) == 1
    assert history[0]["old_value"] == "100"
    assert history[0]["new_value"] == "250"
    assert history[0]["superseded_at"] == "2024-01-01T00:00:00Z"
    assert [f["value"] for f in vault.current_facts()] == ["250"]


def test_a_fact_whose_source_span_is_not_in_the_document_is_rejected(vault):
    report = vault.load_revision(
        rev(1, "2023-01-01T00:00:00Z", SRC_V1),
        [
            Fact("Acme", "founded", "1999", "| founded = 1999"),
            Fact("Acme", "ceo", "Jane Doe", "| ceo = Jane Doe"),  # never in the source
        ],
    )

    assert report.rejected_no_provenance == 1
    assert report.inserted == 1
    assert [f["predicate"] for f in vault.current_facts()] == ["founded"]


def test_out_of_order_loads_never_overwrite_newer_history(vault):
    vault.load_revision(
        rev(2, "2024-01-01T00:00:00Z", SRC_V2),
        [Fact("Acme", "num_employees", "250", "| num_employees = 250")],
    )
    vault.load_revision(
        rev(1, "2023-01-01T00:00:00Z", SRC_V1),
        [Fact("Acme", "num_employees", "100", "| num_employees = 100")],
    )

    assert [f["value"] for f in vault.current_facts()] == ["250"]
    assert vault.history() == []


def test_the_vault_refuses_rather_than_guess(vault):
    vault.load_revision(
        rev(1, "2023-01-01T00:00:00Z", SRC_V1),
        [Fact("Acme", "founded", "1999", "| founded = 1999")],
    )

    answer = ask(vault, "what is Acme's stock price")

    assert answer.refused
    assert "1999" not in answer.text
    assert not answer.citations


def test_a_known_fact_is_answered_with_its_provenance(vault):
    vault.load_revision(
        rev(7, "2023-01-01T00:00:00Z", SRC_V1),
        [Fact("Acme", "founded", "1999", "| founded = 1999")],
    )

    answer = ask(vault, "when was Acme founded")

    assert not answer.refused
    assert "1999" in answer.text
    assert answer.citations[0]["revision_id"] == 7


def test_the_verifier_rejects_a_claim_no_citation_supports():
    facts = [{"subject": "Acme", "predicate": "founded", "value": "1999"}]

    assert ungrounded_tokens("Acme founded: 1999.", facts) == []
    assert "microsoft" in ungrounded_tokens("Acme was founded by Microsoft.", facts)
    with pytest.raises(Grounding_Error):
        assert_grounded("Acme employs 5000 people.", facts)
