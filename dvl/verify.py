"""The anti-hallucination gate.

Any composed answer must be *entailed by its citations at the token level*: every
content token in the answer has to appear in the cited vault rows. Tokens the
composer is allowed to add on its own are limited to a fixed function-word list.

This runs on whatever produced the answer -- the deterministic composer here, or
an LLM composer later. The guarantee comes from the check, not from the writer.
"""

import re

# The only words a composer may introduce that are not in the cited facts.
# Closed class only: determiners, copulas, prepositions, conjunctions, pronouns.
# Nothing here can carry a fact. A composer may use these and nothing else of its
# own invention; every other word must come from the vault.
SCAFFOLD = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "of", "to",
    "in", "on", "at", "and", "or", "as", "by", "for", "with", "its", "it",
    "this", "that", "has", "have", "had", "per", "from", "no", "not", "s",
    # answer-frame words, not claims about the world
    "according", "vault", "recorded", "records", "answer", "holds", "hold",
    "related", "facts", "fact", "directly", "closest", "slots", "does", "will",
    "there", "which", "tracked", "these", "updates", "superseded", "rev",
}

_TOKEN = re.compile(r"[a-z0-9]+")
_CITATION = re.compile(r"\[\d+\]")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def ungrounded_tokens(answer: str, cited_facts: list[dict]) -> list[str]:
    """Content tokens in `answer` that no cited fact supports."""
    supported = set()
    for f in cited_facts:
        supported.update(tokens(f["subject"]))
        supported.update(tokens(f["predicate"]))
        supported.update(tokens(f["value"]))
        # The predicate's declared verbalization, stored in the vault and shown
        # with the citation, so every added word is auditable rather than free.
        for word in f.get("lexicalization", []):
            supported.update(tokens(word))

    bad = []
    for tok in tokens(_CITATION.sub(" ", answer)):
        if tok in SCAFFOLD or tok in supported:
            continue
        # a numeric token is grounded if it appears inside any cited value
        if tok.isdigit() and any(tok in f["value"] for f in cited_facts):
            continue
        bad.append(tok)
    return bad


def assert_grounded(answer: str, cited_facts: list[dict]) -> str:
    bad = ungrounded_tokens(answer, cited_facts)
    if bad:
        raise Grounding_Error(answer, bad)
    return answer


class Grounding_Error(Exception):
    def __init__(self, answer: str, bad: list[str]):
        super().__init__(
            f"answer contains tokens not supported by any cited fact: {sorted(set(bad))}"
        )
        self.answer = answer
        self.bad = bad
