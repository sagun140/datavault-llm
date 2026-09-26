"""Updates vs contradictions: the same document revising itself is not two sources disagreeing."""

from dvl.extract import Fact
from dvl.vault import Vault
from dvl.wiki import Revision


def rev(title, revid, ts, wikitext):
    return Revision(title=title, pageid=1, revid=revid, timestamp=ts, wikitext=wikitext)


SRC_A1 = "| capital = Kathmandu"
SRC_A2 = "| capital = Pokhara"
SRC_B = "| capital = Pokhara"


def test_a_later_revision_of_the_same_document_supersedes():
    v = Vault(":memory:")
    v.load_revision(rev("Nepal", 1, "2023-01-01T00:00:00Z", SRC_A1),
                    [Fact("Nepal", "capital", "Kathmandu", "| capital = Kathmandu")])
    v.load_revision(rev("Nepal", 2, "2024-01-01T00:00:00Z", SRC_A2),
                    [Fact("Nepal", "capital", "Pokhara", "| capital = Pokhara")])

    assert [f["value"] for f in v.current_facts()] == ["Pokhara"]
    assert len(v.history()) == 1
    assert v.contradictions() == []          # an update, not a conflict
    v.close()


def test_two_documents_disagreeing_both_stay_open_and_are_flagged():
    v = Vault(":memory:")
    v.load_revision(rev("Nepal", 1, "2023-01-01T00:00:00Z", SRC_A1),
                    [Fact("Nepal", "capital", "Kathmandu", "| capital = Kathmandu")])
    # a different document asserting something else about the same slot
    v.load_revision(rev("Nepal Almanac", 9, "2024-01-01T00:00:00Z", SRC_B),
                    [Fact("Nepal", "capital", "Pokhara", "| capital = Pokhara")])

    conflicts = v.contradictions()
    assert len(conflicts) == 1
    assert conflicts[0]["subject"] == "Nepal"
    assert conflicts[0]["predicate"] == "capital"
    assert sorted(c["value"] for c in conflicts[0]["claims"]) == ["Kathmandu", "Pokhara"]
    # neither was silently end-dated
    assert sorted(f["value"] for f in v.current_facts()) == ["Kathmandu", "Pokhara"]
    assert v.history() == []
    v.close()


def test_asking_about_a_contested_slot_reports_the_disagreement():
    from dvl.query import ask

    v = Vault(":memory:")
    v.load_revision(rev("Nepal", 1, "2023-01-01T00:00:00Z", SRC_A1),
                    [Fact("Nepal", "capital", "Kathmandu", "| capital = Kathmandu")])
    v.load_revision(rev("Nepal Almanac", 9, "2024-01-01T00:00:00Z", SRC_B),
                    [Fact("Nepal", "capital", "Pokhara", "| capital = Pokhara")])

    a = ask(v, "what is the capital of nepal")

    assert a.refused                          # no single answer is asserted
    assert "disagree" in a.text
    assert "Kathmandu" in a.text and "Pokhara" in a.text
    v.close()
