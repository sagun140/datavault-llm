"""Decompose a sentence into vault constructs, and construct it back.

A sentence is stored as two things:

    hub_pattern    the frame, with its entity mentions replaced by slots
                   "{0} is an American {1} headquartered in {2}, California."
    hub_entity     each filler, one hub per distinct mention

The pattern is *derived from the input text*, never authored. Wikipedia already
marks its entities with [[links]] and '''bold''', so the split between frame and
filler is given by the source, not guessed at.

construct() is the inverse of decompose(). A sentence is only admitted to the
vault if construct(decompose(s)) == s exactly, so the vault can regenerate every
sentence it holds, character for character.
"""

import re

from .extract import _COMMENT, _REF, clean

MARK = "\x01"
_SLOT = re.compile(r"\x01(\d+)\x01")
_SENTENCE_END = re.compile(r"(?<=[a-z0-9\)\]])\.\s+(?=[A-Z\['])")


class RoundTripError(Exception):
    """A sentence could not be regenerated from its own decomposition."""


def decompose(wikitext: str) -> tuple[str, list[str]]:
    """Sentence wikitext -> (pattern, fillers).

    Entity mentions ([[link]], '''bold''') become numbered slots; everything
    between them is the pattern.
    """
    fillers: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(wikitext):
        if wikitext[i : i + 2] == "[[":
            end = wikitext.find("]]", i)
            if end == -1:
                out.append(wikitext[i:])
                break
            fillers.append(clean(wikitext[i + 2 : end].split("|")[-1]))
            out.append(f"{MARK}{len(fillers) - 1}{MARK}")
            i = end + 2
        elif wikitext[i : i + 3] == "'''":
            end = wikitext.find("'''", i + 3)
            if end == -1:
                out.append(wikitext[i:])
                break
            fillers.append(clean(wikitext[i + 3 : end]))
            out.append(f"{MARK}{len(fillers) - 1}{MARK}")
            i = end + 3
        else:
            out.append(wikitext[i])
            i += 1

    pattern = _SLOT.sub(r"{\1}", clean("".join(out)))
    return pattern, fillers


def construct(pattern: str, fillers: list[str]) -> str:
    """The sentence creator: pattern + hub values -> the original sentence.

    Adds nothing. Every character is either pattern text that came from the
    source document or a filler that came from a hub.
    """
    def fill(m):
        i = int(m.group(1))
        if i >= len(fillers):
            raise RoundTripError(f"pattern {pattern!r} references missing filler {{{i}}}")
        return fillers[i]

    # Substitution, not str.format: literal braces occur in real prose and must
    # pass through untouched rather than be parsed as format syntax.
    return re.sub(r"\{(\d+)\}", fill, pattern)


def roundtrip(wikitext: str) -> tuple[str, list[str], str]:
    """Decompose, reconstruct, and refuse if the two do not match exactly."""
    pattern, fillers = decompose(wikitext)
    rebuilt = construct(pattern, fillers)
    original = clean(wikitext)
    if rebuilt != original:
        raise RoundTripError(
            f"cannot regenerate sentence\n  original: {original!r}\n  rebuilt : {rebuilt!r}"
        )
    return pattern, fillers, rebuilt


def sentences(wikitext: str) -> list[str]:
    """Prose sentences of a document, in order, as wikitext."""
    out = []
    for para in _COMMENT.sub("", _REF.sub("", wikitext)).split("\n"):
        para = para.strip()
        if not para or para.startswith(("{", "|", "}", "*", "=", "<", "!", ":")):
            continue
        # namespace links are metadata, not prose
        if re.match(r"\[\[(File|Image|Category|Template):", para, re.I):
            continue
        for s in _SENTENCE_END.split(para):
            s = s.strip()
            if len(s) > 20:
                out.append(s if s.endswith(".") else s + ".")
    return out
