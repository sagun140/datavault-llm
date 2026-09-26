"""Text generation with token-level provenance.

Every token emitted carries the construct it came from, so a generated sentence
can be audited word by word. There are exactly five origins:

    hub        hub_entity.business_key  (or link_slot.surface for a mention)
    satellite  sat_statement.value
    predicate  hub_predicate.business_key
    pattern    hub_pattern.business_key -- literal text from the source document
    scaffold   a closed-class function word from verify.SCAFFOLD

Only `scaffold` does not come from the vault, and it is a fixed finite list that
cannot carry a fact. That is the whole no-invention claim, made visible.

Generation SELECTS AND ORDERS attested bindings. It never rebinds a pattern to
different fillers: "{0} is the capital of {1}" with fresh entities would read
fluently and assert something no source ever said.
"""

import re
from dataclasses import dataclass, field

from .grammar import CLAUSES, coordinate, generic, _article, _possessive
from .sentence import construct
from .vault import Vault
from .verify import SCAFFOLD, tokens

SLOT = re.compile(r"(\{\d+\})")
NAMED = re.compile(r"(\{[a-z_]+\})")


@dataclass
class Segment:
    text: str
    origin: str          # hub | satellite | predicate | pattern | scaffold
    detail: str = ""     # which row it came from

    def as_dict(self) -> dict:
        return {"text": self.text, "origin": self.origin, "detail": self.detail}


@dataclass
class Generated:
    sentences: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s["text"] for s in self.sentences)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.sentences:
            for seg in s["segments"]:
                for _ in tokens(seg["text"]):
                    out[seg["origin"]] = out.get(seg["origin"], 0) + 1
        return out


def _classify_literal(text: str, predicate: str, detail: str) -> list[Segment]:
    """Split literal template text into scaffold words and predicate-derived words."""
    out = []
    for part in re.split(r"([a-z0-9]+)", text, flags=re.I):
        if not part:
            continue
        if re.fullmatch(r"[a-z0-9]+", part, re.I):
            origin = "scaffold" if part.lower() in SCAFFOLD else "predicate"
            out.append(Segment(part, origin, detail if origin == "predicate" else "verify.SCAFFOLD"))
        else:
            out.append(Segment(part, "scaffold", "punctuation"))
    return out


def fact_segments(subject: str, predicate: str, value: str, revision: int | None = None) -> list[Segment]:
    """A realized fact clause, segmented by where each piece came from."""
    template = CLAUSES.get(predicate) or generic(predicate)
    obj = coordinate(value)
    sat = f"sat_statement.value (rev {revision})" if revision else "sat_statement.value"

    out: list[Segment] = []
    for part in NAMED.split(template):
        if part == "{subject}":
            out.append(Segment(subject, "hub", f"hub_entity.business_key = {subject!r}"))
        elif part == "{possessive}":
            poss = _possessive(subject)
            if poss == "Its":
                out.append(Segment(poss, "scaffold", "pronoun"))
            else:
                out.append(Segment(subject, "hub", f"hub_entity.business_key = {subject!r}"))
                out.append(Segment("'s", "scaffold", "possessive marker"))
        elif part == "{object}":
            out.append(Segment(obj, "satellite", sat))
        elif part == "{article}":
            out.append(Segment(_article(obj), "scaffold", "article"))
        elif part:
            out.extend(_classify_literal(part, predicate, f"hub_predicate.business_key = {predicate!r}"))
    out.append(Segment(".", "scaffold", "punctuation"))
    return out


def utterance_segments(pattern: str, fillers: list[str], surfaces: list[str]) -> list[Segment]:
    """A reconstructed source sentence, segmented into pattern text and hub fills."""
    out: list[Segment] = []
    for part in SLOT.split(pattern):
        m = re.fullmatch(r"\{(\d+)\}", part)
        if m:
            i = int(m.group(1))
            out.append(Segment(
                surfaces[i], "hub",
                f"link_slot.surface → hub_entity.business_key = {fillers[i]!r}",
            ))
        elif part:
            out.append(Segment(part, "pattern", "hub_pattern.business_key"))
    return out


def _sentence(segments: list[Segment], kind: str, cites: list[str]) -> dict:
    return {
        "text": "".join(s.text for s in segments),
        "segments": [s.as_dict() for s in segments],
        "kind": kind,
        "cites": cites,
    }


def generate(vault: Vault, subject: str, include_prose: bool = True, limit: int = 8) -> Generated:
    """Compose text about one hub from attested vault content only."""
    out = Generated()

    if include_prose:
        for u in vault.reconstruct(subject)[:limit]:
            if not u["fillers"]:
                continue   # zero-slot utterance: a frozen string, no structure to show
            segs = utterance_segments(u["pattern"], u["fillers"], u["fillers"])
            out.sentences.append(_sentence(
                segs, "reconstructed utterance",
                [f"hub_pattern + {len(u['fillers'])} × link_slot, rev {u['revision_id']}"],
            ))

    rows = [r for r in vault.current_facts() if r["subject"].lower() == subject.lower()]
    for r in rows:
        segs = fact_segments(r["subject"], r["predicate"], r["value"], r["revision_id"])
        out.sentences.append(_sentence(
            segs, "realized fact",
            [f"link_statement {r['subject']}/{r['predicate']}, rev {r['revision_id']}"],
        ))
    return out
