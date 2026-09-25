"""Generate a full description of one hub by walking every link out of it."""

from .grammar import CLAUSES, describe, lexicalization
from .query import Answer
from .vault import Vault
from .verify import assert_grounded


def describe_entity(vault: Vault, subject: str) -> Answer:
    rows = [r for r in vault.current_facts() if r["subject"].lower() == subject.lower()]
    if not rows:
        known = sorted({r["subject"] for r in vault.current_facts()})
        return Answer(
            f"The vault holds no entity named {subject!r}. It knows: {', '.join(known)}",
            refused=True,
        )

    by_predicate = {r["predicate"]: r for r in rows}
    facts = {p: r["value"] for p, r in by_predicate.items()}

    citations, parts = [], []
    for sentence, predicates in describe(rows[0]["subject"], facts):
        for p in predicates:
            row = dict(by_predicate[p])
            row["lexicalization"] = lexicalization(p, CLAUSES.get(p, ""))
            citations.append(row)
        parts.append(f"{sentence} [{len(citations)}]")

    text = " ".join(parts)
    assert_grounded(text, citations)
    return Answer(text, citations)
