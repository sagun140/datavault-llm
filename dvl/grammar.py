"""Surface realization: turn vault topology into sentences.

A link_statement IS a proposition -- hub(subject) -> predicate -> satellite(value)
maps onto subject -> verb -> object. So sentences are not looked up, they are
*walked out of the graph*.

The invariant that makes "nothing is invented" provable:

    every word in a clause template is either
      (a) a closed-class function word in verify.SCAFFOLD, or
      (b) a morphological variant of the predicate's own business key
          (shares a 3-character stem with one of its tokens).

Nothing else may appear. Content words can therefore only enter a sentence from
hub_entity.business_key or sat_statement.value. tests/test_grammar.py enforces
this over every template, so adding a bad one fails the build rather than
quietly inventing a fact.
"""

import re

STEM = 3


# predicate business key -> clause template.
# {subject} and {object} are the only slots; see the invariant above.
CLAUSES: dict[str, str] = {
    "is_a":          "{subject} is {article} {object}",
    "type":          "{possessive} type is {object}",
    "founded":       "{subject} was founded on {object}",
    "founders":      "{subject} was founded by {object}",
    "hq_location":   "{subject} is located in {object}",
    "industry":      "{possessive} industry is {object}",
    "products":      "{possessive} products are {object}",
    "num_employees": "{possessive} number of employees is {object}",
    "revenue":       "{possessive} revenue is {object}",
    "key_people":    "{possessive} key people are {object}",
    "website":       "{possessive} website is {object}",
    "name":          "{possessive} name is {object}",
    "parent":        "{possessive} parent is {object}",
    "owner":         "{possessive} owner is {object}",
    "occupation":    "{possessive} occupation is {object}",
    "nationality":   "{possessive} nationality is {object}",
    "birth_date":    "{possessive} birth date is {object}",
}

# Frames fuse several links on one hub into a single clause. This is where
# sentence structure comes from the graph rather than from a row: two links out
# of the same hub become one clause with two objects.
FRAMES: list[tuple[tuple[str, ...], str]] = [
    (("founded", "founders"), "{subject} was founded on {founded} by {founders}"),
    (("hq_location", "industry"), "{subject} is located in {hq_location} and its industry is {industry}"),
]

# The order a description walks the graph in.
NARRATIVE = [
    "is_a", "type", "founded", "founders", "hq_location", "industry",
    "products", "num_employees", "revenue", "key_people", "website",
]


def lexicalization(predicate: str, template: str) -> list[str]:
    """Non-scaffold words a template contributes: the predicate's own verbalization.

    These are reported with the citation, so the reader can audit every word that
    did not come from a hub or a satellite.
    """
    from .verify import SCAFFOLD

    words = re.findall(r"[a-z]+", re.sub(r"\{[^}]*\}", " ", template.lower()))
    return [w for w in words if w not in SCAFFOLD]


def stems(predicate: str) -> set[str]:
    return {t[:STEM] for t in re.findall(r"[a-z0-9]+", predicate.lower())}


def coordinate(value: str) -> str:
    """'A; B; C' -> 'A, B, and C'. Only commas and 'and' are added."""
    items = [i.strip(" ,;") for i in value.split(";") if i.strip(" ,;")]
    if len(items) <= 1:
        return value.strip()
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _stop(sentence: str) -> str:
    return sentence if sentence.endswith(".") else sentence + "."


def _possessive(subject: str) -> str:
    """Grammatical case of the subject. 'It' + possessive is 'Its', not \"It's\"."""
    return "Its" if subject == "It" else f"{subject}'s"


def _article(obj: str) -> str:
    return "an" if obj[:1].lower() in "aeiou" else "a"


def generic(predicate: str) -> str:
    """Fallback clause built from the predicate's own business key.

    Satisfies the closure invariant by construction: every content word in it IS
    the predicate. This is what keeps grammar coverage from capping answer
    coverage -- an unseen predicate still gets a sentence, just a plainer one.
    """
    words = predicate.replace("_", " ").strip()
    copula = "are" if words.endswith("s") and not words.endswith("ss") else "is"
    return "{possessive} " + words + " " + copula + " {object}"


def realize(subject: str, predicate: str, value: str) -> str | None:
    """One link + its satellite -> one clause."""
    if not predicate or not value:
        return None
    template = CLAUSES.get(predicate) or generic(predicate)
    obj = coordinate(value)
    return template.format(
        subject=subject, possessive=_possessive(subject),
        object=obj, article=_article(obj),
    )


def realize_frame(subject: str, facts: dict[str, str], head: str | None = None) -> tuple[str, list[str]] | None:
    """Fuse co-occurring links on one hub into a single clause, if a frame fits."""
    for predicates, template in FRAMES:
        if head is not None and predicates[0] != head:
            continue
        if all(p in facts for p in predicates):
            filled = template.format(
                subject=subject, possessive=_possessive(subject),
                **{p: coordinate(facts[p]) for p in predicates},
            )
            return filled, list(predicates)
    return None


def describe(subject: str, facts: dict[str, str]) -> list[tuple[str, list[str]]]:
    """Walk the hub's links in narrative order, producing (sentence, predicates).

    After the first sentence the subject is pronominalized to "It" -- a closed-class
    substitution, so it adds no content.
    """
    remaining = dict(facts)
    out: list[tuple[str, list[str]]] = []

    while remaining:
        subj = subject if not out else "It"

        nxt = next((p for p in NARRATIVE if p in remaining), next(iter(remaining)))

        # Fuse only if a frame begins at the predicate narrative order chose, so
        # frames never jump the queue ahead of the definition sentence.
        frame = realize_frame(subj, remaining, head=nxt)
        if frame:
            sentence, used = frame
            out.append((_stop(sentence), used))
            for p in used:
                remaining.pop(p)
            continue

        clause = realize(subj, nxt, remaining.pop(nxt))
        if clause:
            out.append((_stop(clause), [nxt]))
    return out
