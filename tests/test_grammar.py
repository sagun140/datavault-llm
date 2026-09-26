"""The closure invariant: templates may not contain content words."""

import re

import pytest

from dvl.grammar import CLAUSES, FRAMES, coordinate, describe, realize, stems
from dvl.verify import SCAFFOLD, ungrounded_tokens

SLOT = re.compile(r"\{[^}]*\}")


def template_words(template: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", SLOT.sub(" ", template.lower()))


@pytest.mark.parametrize("predicate,template", sorted(CLAUSES.items()))
def test_no_clause_template_can_introduce_a_content_word(predicate, template):
    """Every template word is closed-class, or a variant of the predicate itself.

    This is what makes the no-invention claim checkable: content words cannot
    reach a sentence except from hub_entity.business_key or sat_statement.value.
    """
    allowed = stems(predicate)
    for word in template_words(template):
        assert word in SCAFFOLD or word[:3] in allowed, (
            f"template for {predicate!r} introduces content word {word!r}"
        )


@pytest.mark.parametrize("predicates,template", FRAMES)
def test_no_frame_template_can_introduce_a_content_word(predicates, template):
    allowed = set().union(*(stems(p) for p in predicates))
    for word in template_words(template):
        assert word in SCAFFOLD or word[:3] in allowed, (
            f"frame {predicates} introduces content word {word!r}"
        )


def test_coordination_only_adds_commas_and_and():
    assert coordinate("A") == "A"
    assert coordinate("A; B") == "A and B"
    assert coordinate("A; B; C") == "A, B, and C"
    # every content word survives, nothing new appears
    assert set(re.findall(r"[A-Z]", coordinate("A; B; C"))) == {"A", "B", "C"}


def test_a_frame_fuses_two_links_into_one_clause():
    sentences = describe("Acme", {"founded": "1999", "founders": "Jane; John"})

    assert len(sentences) == 1
    text, used = sentences[0]
    assert text == "Acme was founded on 1999 by Jane and John."
    assert sorted(used) == ["founded", "founders"]


def test_the_subject_is_pronominalized_after_first_mention():
    sentences = describe("Acme", {"is_a": "company", "revenue": "$1"})

    assert sentences[0][0].startswith("Acme is a company")
    assert sentences[1][0].startswith("Its revenue")  # not "It's"


def test_an_unknown_predicate_yields_a_sentence_with_no_invented_word():
    """Previously this returned None, silently dropping the fact."""
    out = realize("Acme", "shoe_size", "11")

    assert out == "Acme's shoe size is 11"
    allowed = stems("shoe_size") | {w[:3] for w in ("acme", "11")}
    for word in template_words(out):
        assert word in SCAFFOLD or word[:3] in allowed


def test_generated_sentences_pass_the_grounding_gate():
    facts = {"founded": "1999", "founders": "Jane; John", "revenue": "$1"}
    cited = [
        {"subject": "Acme", "predicate": p, "value": v,
         "lexicalization": ["founded"] if p.startswith("found") else []}
        for p, v in facts.items()
    ]
    text = " ".join(s for s, _ in describe("Acme", facts))

    assert ungrounded_tokens(text, cited) == []


@pytest.mark.parametrize("predicate", ["capital", "population", "currency", "area_km2", "products"])
def test_the_generic_clause_introduces_no_content_word_either(predicate):
    from dvl.grammar import generic

    allowed = stems(predicate)
    for word in template_words(generic(predicate)):
        assert word in SCAFFOLD or word[:3] in allowed


def test_an_untemplated_predicate_still_yields_a_sentence():
    """Grammar coverage must not silently cap answer coverage."""
    assert realize("Nepal", "capital", "Kathmandu") == "Nepal's capital is Kathmandu"
    assert realize("Nepal", "official_languages", "Nepali") == (
        "Nepal's official languages are Nepali"
    )


def test_an_empty_value_yields_no_sentence():
    assert realize("Nepal", "capital", "") is None
