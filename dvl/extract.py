"""Deterministic wikitext -> facts.

Every fact carries a `source_span`: the exact substring of the source document it
came from. Nothing enters the vault without one (see vault.load_facts), which is
the ingest-side guarantee that no fact is invented.
"""

import re
from dataclasses import dataclass

from .wiki import Revision


@dataclass(frozen=True)
class Fact:
    subject: str       # business key of the entity, e.g. "Anthropic"
    predicate: str     # normalized attribute name, e.g. "founded"
    value: str         # cleaned literal, e.g. "2021"
    source_span: str   # verbatim slice of the source document
    object_entity: str | None = None  # linked entity, when the value is a wikilink


# --- wikitext cleaning -------------------------------------------------------

_REF = re.compile(r"<ref[^>]*?/>|<ref.*?</ref>", re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _split_params(body: str) -> list[str]:
    """Split a template body on top-level pipes, ignoring nested {{ }} and [[ ]]."""
    parts, depth, buf = [], 0, []
    i = 0
    while i < len(body):
        two = body[i : i + 2]
        if two in ("{{", "[["):
            depth += 1
            buf.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            depth -= 1
            buf.append(two)
            i += 2
            continue
        if body[i] == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(body[i])
        i += 1
    parts.append("".join(buf))
    return parts


def _match_braces(text: str, start: int) -> int:
    """Index just past the `}}` closing the `{{` at `start`."""
    depth, i = 0, start
    while i < len(text):
        if text[i : i + 2] == "{{":
            depth += 1
            i += 2
        elif text[i : i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return len(text)


_LIST_TEMPLATES = {
    "plainlist", "plain list", "ubl", "ubil", "ublist", "unbulleted list",
    "bulleted list", "hlist", "flatlist", "collapsible list",
}


def _render_template(body: str) -> str:
    params = _split_params(body)
    name = params[0].strip().lower()
    args = [p.strip() for p in params[1:]]
    positional = [a for a in args if "=" not in a.split("[")[0][:20]]

    if name in ("start date", "end date", "start date and age", "birth date"):
        return "-".join(p for p in positional[:3] if p.isdigit())
    if name == "convert":
        return " ".join(positional[:2])
    if name in ("nowrap", "nobr", "small"):
        return clean(positional[0]) if positional else ""
    if name == "url":
        return positional[0] if positional else ""
    if name in _LIST_TEMPLATES:
        return "; ".join(clean(p) for p in positional if p)
    if name in ("ill", "interlanguage link"):
        return positional[0] if positional else ""
    return ""  # unknown template: drop rather than guess


def clean(text: str) -> str:
    """Wikitext -> plain text. Unknown constructs are dropped, never guessed at."""
    text = _REF.sub("", text)
    text = _COMMENT.sub("", text)

    out, i = [], 0
    while i < len(text):
        if text[i : i + 2] == "{{":
            end = _match_braces(text, i)
            out.append(_render_template(text[i + 2 : end - 2]))
            i = end
        elif text[i : i + 2] == "[[":
            end = text.find("]]", i)
            if end == -1:
                out.append(text[i:])
                break
            inner = text[i + 2 : end]
            out.append(inner.split("|")[-1])
            i = end + 2
        else:
            out.append(text[i])
            i += 1

    text = "".join(out)
    text = re.sub(r"<br\s*/?>", "; ", text, flags=re.I)
    text = _TAG.sub("", text)
    text = text.replace("'''", "").replace("''", "")
    text = text.replace("&nbsp;", " ").replace("&ndash;", "-").replace("&amp;", "&")
    return _WS.sub(" ", text).strip(" ;,")


def _first_wikilink(raw: str) -> str | None:
    m = re.search(r"\[\[([^\]|]+)", raw)
    return m.group(1).strip() if m else None


# --- extraction --------------------------------------------------------------

def normalize_predicate(key: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    return re.sub(r"_\d+$", "", key)  # infobox repeats: key1, key2 -> key


_SKIP_PREDICATES = {
    "image", "logo", "image_size", "logo_size", "caption", "image_caption",
    "logo_caption", "alt", "image_alt", "footnotes", "module", "embed", "width",
    "logo_upright", "image_upright", "native_name_lang", "collapsible",
}


def extract(rev: Revision) -> list[Fact]:
    facts: list[Fact] = []
    facts.extend(_extract_infobox(rev))
    facts.extend(_extract_lead(rev))

    seen, unique = set(), []
    for f in facts:
        if f.value and (f.subject, f.predicate) not in seen:
            seen.add((f.subject, f.predicate))
            unique.append(f)
    return unique


def _extract_infobox(rev: Revision) -> list[Fact]:
    m = re.search(r"\{\{\s*Infobox", rev.wikitext, re.I)
    if not m:
        return []
    end = _match_braces(rev.wikitext, m.start())
    body = rev.wikitext[m.start() + 2 : end - 2]

    facts = []
    for param in _split_params(body)[1:]:
        if "=" not in param:
            continue
        key, _, raw = param.partition("=")
        predicate = normalize_predicate(key)
        if not predicate or predicate in _SKIP_PREDICATES:
            continue
        value = clean(raw)
        if not value or len(value) > 300:
            continue
        facts.append(
            Fact(
                subject=rev.title,
                predicate=predicate,
                value=value,
                source_span=param.strip(),
                object_entity=_first_wikilink(raw),
            )
        )
    return facts


def _extract_lead(rev: Revision) -> list[Fact]:
    """The 'X is a Y' definition sentence, which the infobox never carries."""
    text = rev.wikitext
    m = re.search(r"\{\{\s*Infobox", text, re.I)
    if m:
        text = text[_match_braces(text, m.start()) :]

    for para in text.split("\n"):
        para = para.strip()
        if not para or para.startswith(("{", "|", "[[File:", "[[Image:", "*", "=", "<")):
            continue
        sentence = re.split(r"(?<=[a-z0-9\)\]])\.\s", clean(para))[0].strip()
        m2 = re.match(r"^(.{2,120}?)\s+(?:is|was|are|were)\s+(?:an?|the)\s+(.{3,200})$", sentence)
        if m2:
            return [
                Fact(
                    subject=rev.title,
                    predicate="is_a",
                    value=m2.group(2).rstrip("."),
                    source_span=para,
                )
            ]
        return []
    return []
