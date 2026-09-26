"""Question -> vault rows -> grounded answer.

There is no generative step that can invent content. The composer only rearranges
strings that came out of the vault, and verify.assert_grounded re-checks that
claim on every answer before it is returned. Below the retrieval threshold the
system refuses.
"""

import re
from dataclasses import dataclass, field

from .grammar import CLAUSES, describe, lexicalization
from .vault import Vault
from .verify import assert_grounded, tokens

# Question vocabulary -> infobox predicate vocabulary. Deliberately small and
# explicit: an unmapped question word simply fails to retrieve, and we refuse.
SYNONYMS = {
    "founded": {"founded", "found", "start", "started", "began", "inception", "established", "formation", "formed"},
    "founders": {"founder", "founders", "founded", "cofounder", "cofounders", "creator", "created", "started"},
    "key_people": {"ceo", "chief", "executive", "leader", "leadership", "president", "boss", "runs", "head", "people"},
    "num_employees": {"employees", "employee", "headcount", "staff", "workforce", "size", "big"},
    "hq_location": {"headquarters", "hq", "based", "located", "location", "headquartered", "address"},
    "hq_location_city": {"headquarters", "hq", "based", "city", "located", "headquartered"},
    "industry": {"industry", "sector", "business", "field"},
    "products": {"products", "product", "makes", "make", "builds", "build", "sells", "offerings", "models"},
    "revenue": {"revenue", "sales", "turnover", "earnings", "money", "makes"},
    "type": {"type", "kind", "structure", "corporation", "company"},
    "website": {"website", "site", "url", "homepage", "web", "domain"},
    "is_a": {"describe", "about", "overview"},
    "name": {"name", "called", "legal"},
    "parent": {"parent", "owner", "owned", "subsidiary"},
    "owner": {"owner", "owns", "owned", "parent"},
    "birth_date": {"born", "birth", "birthday", "age"},
    "nationality": {"nationality", "national", "citizen", "country"},
    "occupation": {"occupation", "job", "profession", "work"},
}

# A wh-word constrains what KIND of predicate can answer. "who founded X" must not
# resolve to a date just because "founded" matches the `founded` predicate.
WH_EXPECTS = {
    "who": {"founders", "key_people", "owner", "parent", "occupation"},
    "where": {"hq_location", "hq_location_city", "location", "birth_place", "nationality"},
    "when": {"founded", "birth_date", "dissolved", "defunct"},
}
WH_WORDS = set(WH_EXPECTS)

STOPWORDS = {
    "is", "are", "was", "were", "the", "a", "an", "of", "to", "in", "on", "at",
    "for", "did", "do", "does", "s", "tell", "me", "and", "or", "it", "its", "has", "have",
    "be", "been", "that", "this", "you", "know", "what", "which", "how", "many",
    "much", "there",
}

CHANGE_WORDS = {"changed", "change", "changes", "updated", "update", "used", "previously", "before", "history", "differ"}

# A row qualifies only on predicate-side evidence. Subject overlap alone is not a
# reason to surface a fact -- that is what turned every answer into noise.
MIN_PREDICATE_SCORE = 3.0


@dataclass
class Scored:
    predicate_score: float
    lexical: float   # evidence from the question's own words, excluding wh-intent
    total: float
    row: dict


def _readable(predicate: str) -> str:
    return predicate.replace("_", " ")


def _score(q_tokens: set[str], wh: str | None, row) -> Scored:
    predicate_t = set(tokens(row["predicate"]))
    aliases = SYNONYMS.get(row["predicate"], set())

    lexical = 3.0 * len(q_tokens & predicate_t)
    if q_tokens & aliases:
        lexical += 3.0
    predicate_score = lexical

    if wh:
        expected = WH_EXPECTS[wh]
        if row["predicate"] in expected:
            predicate_score += 4.0
        else:
            predicate_score -= 3.0   # a "who" question cannot be answered by a date

    total = predicate_score
    total += 2.0 * len(q_tokens & set(tokens(row["subject"])))
    total += 0.5 * len(q_tokens & set(tokens(row["value"])))
    return Scored(predicate_score, lexical, total, dict(row))


@dataclass
class Answer:
    text: str
    citations: list[dict] = field(default_factory=list)
    grounded: bool = True
    refused: bool = False

    def render(self) -> str:
        out = [self.text]
        if self.citations:
            out.append("")
            out.append("Sources (vault rows):")
            for i, c in enumerate(self.citations, 1):
                out.append(
                    f"  [{i}] {c['subject']} / {c['predicate']} = {c['value']}"
                    f"\n      rev {c['revision_id']} @ {c['revision_ts']}  ·  {c['record_source']}"
                )
        return "\n".join(out)


def ask(vault: Vault, question: str, top_k: int = 3) -> Answer:
    raw = set(tokens(question))
    wh = next((w for w in WH_WORDS if w in raw), None)
    q_tokens = raw - STOPWORDS - WH_WORDS

    if raw & CHANGE_WORDS:
        return _answer_change(vault, q_tokens)

    rows = vault.current_facts()
    if not rows:
        return Answer("The vault is empty. Ingest a page first.", refused=True)

    scored = sorted(
        (_score(q_tokens, wh, r) for r in rows),
        key=lambda x: (-x.predicate_score, -x.total),
    )
    hits = [s for s in scored if s.predicate_score >= MIN_PREDICATE_SCORE]

    if not hits:
        near = ", ".join(f"{s.row['subject']}/{s.row['predicate']}" for s in scored[:3])
        return Answer(
            "No fact in the vault answers that, so I will not answer.\n"
            f"Closest slots the vault does hold: {near}",
            refused=True,
        )

    # keep only what is competitive with the best match, so answers stay tight
    best = hits[0].total
    hits = [s for s in hits[:top_k] if s.total >= best * 0.75]

    # A match carried only by wh-intent means no word in the question named a slot
    # the vault holds. Those facts are related, not an answer -- say so rather
    # than let adjacent truths pose as the answer.
    indirect = all(h.lexical == 0 for h in hits)

    # Compose by walking the graph: the retrieved links become clauses, and
    # co-occurring links on the same hub fuse into one sentence.
    subject = hits[0].row["subject"]
    hits = [h for h in hits if h.row["subject"] == subject]
    facts = {h.row["predicate"]: h.row["value"] for h in hits}
    by_predicate = {h.row["predicate"]: h.row for h in hits}

    # If sources disagree on a retrieved slot, report the disagreement rather
    # than narrating whichever row sorted first.
    conflicts = {
        (c["subject"], c["predicate"]): c
        for c in vault.contradictions()
    }
    clash = [conflicts[(subject, p)] for p in facts if (subject, p) in conflicts]
    if clash:
        lines = ["Sources in the vault disagree, so there is no single answer:"]
        cits = []
        for c in clash:
            for claim in c["claims"]:
                lines.append(
                    f"  · {c['subject']} / {c['predicate']} = {claim['value']!r}"
                    f"   (per {claim['lineage']}, rev {claim['revision_id']})"
                )
                cits.append({
                    "subject": c["subject"], "predicate": c["predicate"],
                    "value": claim["value"], "revision_id": claim["revision_id"],
                    "revision_ts": "", "record_source": claim["record_source"],
                })
        return Answer("\n".join(lines), cits, refused=True)

    citations, parts = [], []
    for sentence, predicates in describe(subject, facts):
        for p in predicates:
            row = dict(by_predicate[p])
            row["lexicalization"] = lexicalization(p, CLAUSES.get(p, ""))
            citations.append(row)
        parts.append(f"{sentence} [{len(citations)}]")
    if not parts:
        return Answer(
            "The vault holds facts for that, but cannot render any of them as a "
            "sentence, so it will not answer.",
            refused=True,
        )
    text = " ".join(parts)
    assert_grounded(text, citations)  # raises rather than emit an unsupported claim

    if indirect:
        text = (
            "The vault holds no fact that directly answers that. "
            "Related facts it does hold:\n" + text
        )
    return Answer(text, citations, refused=indirect)


def _answer_change(vault: Vault, q_tokens: set[str]) -> Answer:
    rows = vault.history()
    if not rows:
        return Answer(
            "The vault records no changes yet: only one revision has been loaded.",
            refused=True,
        )

    scored = []
    for r in rows:
        overlap = len(q_tokens & (set(tokens(r["subject"])) | set(tokens(r["predicate"]))))
        aliases = SYNONYMS.get(r["predicate"], set())
        scored.append((overlap + (2 if q_tokens & aliases else 0), r))
    scored.sort(key=lambda x: -x[0])

    picked = [r for s, r in scored if s > 0] or [r for _, r in scored]
    lines = ["The vault tracked these updates:"]
    citations = []
    for r in picked[:5]:
        lines.append(
            f"  · {r['subject']} / {_readable(r['predicate'])}: "
            f"\"{r['old_value']}\" (rev {r['old_revision']}) -> "
            f"\"{r['new_value']}\" (rev {r['new_revision']}), superseded {r['superseded_at']}"
        )
        citations.append({
            "subject": r["subject"], "predicate": r["predicate"],
            "value": f"{r['old_value']} -> {r['new_value']}",
            "revision_id": r["new_revision"], "revision_ts": r["superseded_at"],
            "record_source": f"sat_statement history (from rev {r['old_revision']})",
        })
    return Answer("\n".join(lines), citations)
