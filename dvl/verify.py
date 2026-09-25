"""The anti-hallucination gate.

Any composed answer must be *entailed by its citations at the token level*: every
content token in the answer has to appear in the cited vault rows. Tokens the
composer is allowed to add on its own are limited to a fixed function-word list.

This runs on whatever produced the answer -- the deterministic composer here, or
an LLM composer later. The guarantee comes from the check, not from the writer.
"""

import re

# The only words a composer may introduce that are not in the cited facts.
SCAFFOLD = {
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "at",
    "and", "or", "as", "by", "for", "with", "its", "it", "this", "that", "has",
    "have", "had", "according", "vault", "recorded", "records", "per", "from",
    "no", "not", "answer", "found", "s",
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
