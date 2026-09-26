"""Round-trip fidelity: the vault must regenerate what it was given."""

import pytest

from dvl.extract import clean
from dvl.sentence import RoundTripError, construct, decompose, roundtrip, sentences
from dvl.vault import Vault
from dvl.wiki import Revision

LEAD = (
    "'''Anthropic, PBC''' is an American [[artificial intelligence]] (AI) "
    "[[Benefit corporation|public benefit corporation]] headquartered in "
    "[[San Francisco]], California."
)


def test_the_pattern_is_derived_from_the_text_not_authored():
    pattern, fillers = decompose(LEAD)

    assert pattern == "{0} is an American {1} (AI) {2} headquartered in {3}, California."
    assert fillers == [
        "Anthropic, PBC", "artificial intelligence",
        "public benefit corporation", "San Francisco",
    ]


def test_construct_is_the_inverse_of_decompose():
    pattern, fillers, rebuilt = roundtrip(LEAD)
    assert rebuilt == clean(LEAD)


def test_construct_adds_nothing_of_its_own():
    """Every character of output comes from the pattern or a filler."""
    pattern, fillers = decompose(LEAD)
    out = construct(pattern, fillers)

    residue = out
    for f in fillers:
        residue = residue.replace(f, "", 1)
    skeleton = pattern
    for i in range(len(fillers)):
        skeleton = skeleton.replace("{%d}" % i, "", 1)
    assert residue == skeleton


def test_literal_braces_in_prose_survive_reconstruction():
    """Reconstruction is substitution, not str.format, so {} in text is safe."""
    assert construct("the set {0} uses {} notation", ["A"]) == "the set A uses {} notation"


def test_a_sentence_that_cannot_be_regenerated_is_refused():
    with pytest.raises(RoundTripError):
        construct("{0} and {1}", ["only one"])


def test_a_mention_keeps_its_own_surface_form():
    """One entity, two capitalizations: the hub is shared, the surfaces are not."""
    text = (
        "[[Artificial intelligence]] matters.\n"
        "Anthropic builds [[artificial intelligence|artificial intelligence]] systems."
    )
    v = Vault(":memory:")
    v.load_revision(
        Revision(title="Acme", pageid=1, revid=1, timestamp="2024-01-01T00:00:00Z",
                 wikitext=text),
        [],
    )
    rebuilt = v.reconstruct("Acme")

    assert [r["exact"] for r in rebuilt] == [True, True]
    surfaces = [r["fillers"][0] for r in rebuilt]
    assert surfaces == ["Artificial intelligence", "artificial intelligence"]
    # ...but both mentions resolve to a single hub
    assert v.db.execute(
        "SELECT count(DISTINCT entity_hk) FROM link_slot"
    ).fetchone()[0] == 1
    v.close()


def test_repeated_frames_do_not_collide():
    text = "[[Alpha]] shipped this year.\n[[Beta]] shipped this year."
    v = Vault(":memory:")
    v.load_revision(
        Revision(title="Acme", pageid=1, revid=1, timestamp="2024-01-01T00:00:00Z",
                 wikitext=text),
        [],
    )
    rebuilt = v.reconstruct("Acme")

    assert len(rebuilt) == 2
    assert all(r["exact"] for r in rebuilt)
    assert {r["rebuilt"] for r in rebuilt} == {
        "Alpha shipped this year.", "Beta shipped this year."
    }
    v.close()


def test_sentence_splitting_skips_namespace_links():
    assert sentences("[[Category:AI companies]]\n[[File:logo.png|thumb]]") == []
