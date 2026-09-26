"""Data Vault 2.0 store (SQLite).

  hub_entity      unique entities (business key = page/entity name)
  hub_predicate   unique attribute names
  link_statement  the (subject, predicate) pair -- a claim slot
  sat_statement   type-2 history of that slot's VALUE, with provenance
  sat_entity      type-2 history of entity-level document metadata

Modelling note: the value lives in the satellite rather than in the link, so a
changed value end-dates one satellite row and opens another instead of creating
a new link. That is what makes "the info was updated" a first-class, queryable
event rather than a silent overwrite.
"""

import hashlib
import sqlite3
from dataclasses import dataclass

from .extract import Fact, clean
from .sentence import RoundTripError, construct, roundtrip, sentences
from .wiki import Revision

SENTENCE_SCHEMA = ""  # defined at end of module; assigned below


SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_entity (
    entity_hk    TEXT PRIMARY KEY,
    business_key TEXT NOT NULL UNIQUE,
    load_ts      TEXT NOT NULL,
    record_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_predicate (
    predicate_hk TEXT PRIMARY KEY,
    business_key TEXT NOT NULL UNIQUE,
    load_ts      TEXT NOT NULL,
    record_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_statement (
    statement_hk TEXT PRIMARY KEY,
    subject_hk   TEXT NOT NULL REFERENCES hub_entity(entity_hk),
    predicate_hk TEXT NOT NULL REFERENCES hub_predicate(predicate_hk),
    load_ts      TEXT NOT NULL,
    record_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sat_statement (
    statement_hk TEXT NOT NULL REFERENCES link_statement(statement_hk),
    load_ts      TEXT NOT NULL,
    load_end_ts  TEXT,
    hash_diff    TEXT NOT NULL,
    value        TEXT NOT NULL,
    object_entity_hk TEXT,
    source_span  TEXT NOT NULL,
    revision_id  INTEGER NOT NULL,
    revision_ts  TEXT NOT NULL,
    record_source TEXT NOT NULL,
    -- Which document this assertion came from. A later revision of the SAME
    -- document supersedes (an update); a DIFFERENT document asserting something
    -- else does not (a contradiction), so both stay open and get flagged.
    lineage      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (statement_hk, load_ts)
);
CREATE TABLE IF NOT EXISTS sat_entity (
    entity_hk    TEXT NOT NULL REFERENCES hub_entity(entity_hk),
    load_ts      TEXT NOT NULL,
    load_end_ts  TEXT,
    hash_diff    TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    revision_id  INTEGER NOT NULL,
    revision_ts  TEXT NOT NULL,
    record_source TEXT NOT NULL,
    PRIMARY KEY (entity_hk, load_ts)
);
CREATE INDEX IF NOT EXISTS ix_sat_stmt_open ON sat_statement(statement_hk, load_end_ts);
CREATE INDEX IF NOT EXISTS ix_link_subject  ON link_statement(subject_hk);
"""


def hk(*parts: str) -> str:
    """Deterministic hash key over normalized business keys (DV 2.0 style)."""
    payload = "||".join(p.strip().upper() for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def hashdiff(*parts: str) -> str:
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()[:32]


@dataclass
class LoadReport:
    entity: str
    revision_id: int
    revision_ts: str
    facts_seen: int
    rejected_no_provenance: int
    inserted: int
    changed: int
    unchanged: int
    sentences_stored: int = 0
    sentences_rejected: int = 0

    def __str__(self) -> str:
        return (
            f"{self.entity} @rev {self.revision_id} ({self.revision_ts})\n"
            f"  facts extracted : {self.facts_seen}\n"
            f"  new statements  : {self.inserted}\n"
            f"  changed values  : {self.changed}\n"
            f"  unchanged       : {self.unchanged}\n"
            f"  rejected (no verbatim source span): {self.rejected_no_provenance}\n"
            f"  sentences stored: {self.sentences_stored}"
            f"  (rejected, not regenerable: {self.sentences_rejected})"
        )


class Vault:
    def __init__(self, path: str = "vault.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.executescript(SENTENCE_SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- loading -------------------------------------------------------------

    def _hub(self, table: str, key_col: str, business_key: str, ts: str, src: str) -> str:
        key = hk(business_key)
        self.db.execute(
            f"INSERT OR IGNORE INTO {table} ({key_col}, business_key, load_ts, record_source)"
            " VALUES (?,?,?,?)",
            (key, business_key, ts, src),
        )
        return key

    def load_revision(self, rev: Revision, facts: list[Fact]) -> LoadReport:
        """Load one revision. Idempotent: re-loading the same content changes nothing."""
        ts = rev.timestamp
        src = rev.record_source
        rep = LoadReport(rev.title, rev.revid, rev.timestamp, len(facts), 0, 0, 0, 0)

        entity_hk = self._hub("hub_entity", "entity_hk", rev.title, ts, src)
        self._load_sat(
            "sat_entity", "entity_hk", entity_hk,
            hashdiff(rev.title, rev.url),
            {"title": rev.title, "url": rev.url},
            rev, ts, src,
        )

        for fact in facts:
            # Provenance gate: a fact may only enter the vault if its source span
            # is verbatim present in the source document.
            if fact.source_span not in rev.wikitext:
                rep.rejected_no_provenance += 1
                continue

            subject_hk = self._hub("hub_entity", "entity_hk", fact.subject, ts, src)
            predicate_hk = self._hub("hub_predicate", "predicate_hk", fact.predicate, ts, src)
            object_hk = (
                self._hub("hub_entity", "entity_hk", fact.object_entity, ts, src)
                if fact.object_entity
                else None
            )

            stmt_hk = hk(fact.subject, fact.predicate)
            self.db.execute(
                "INSERT OR IGNORE INTO link_statement"
                " (statement_hk, subject_hk, predicate_hk, load_ts, record_source)"
                " VALUES (?,?,?,?,?)",
                (stmt_hk, subject_hk, predicate_hk, ts, src),
            )

            outcome = self._load_sat(
                "sat_statement", "statement_hk", stmt_hk,
                hashdiff(fact.value),
                {
                    "value": fact.value,
                    "object_entity_hk": object_hk,
                    "source_span": fact.source_span,
                },
                rev, ts, src, lineage=rev.title,
            )
            setattr(rep, outcome, getattr(rep, outcome) + 1)

        rep.sentences_stored, rep.sentences_rejected = self._load_sentences(rev)
        self.db.commit()
        return rep

    def _load_sat(self, table, key_col, key, hash_diff, cols, rev, ts, src, lineage=None) -> str:
        """Type-2 upsert, lineage-aware.

        Same document, later revision -> the value was updated: end-date and insert.
        Different document, different value -> the sources disagree: both stay open
        and `contradictions()` reports the slot. Silently end-dating here would
        make the vault assert whichever page was loaded last.
        """
        has_lineage = table == "sat_statement"
        where = f" AND lineage=?" if has_lineage else ""
        params = (key, lineage) if has_lineage else (key,)
        cur = self.db.execute(
            f"SELECT hash_diff, load_ts FROM {table}"
            f" WHERE {key_col}=? AND load_end_ts IS NULL{where}",
            params,
        ).fetchone()

        if cur and cur["hash_diff"] == hash_diff:
            return "unchanged"

        outcome = "inserted"
        if cur:
            if ts <= cur["load_ts"]:
                return "unchanged"  # out-of-order load: never rewrite newer history
            self.db.execute(
                f"UPDATE {table} SET load_end_ts=? WHERE {key_col}=? AND load_end_ts IS NULL{where}",
                (ts, key, lineage) if has_lineage else (ts, key),
            )
            outcome = "changed"

        names = [key_col, "load_ts", "hash_diff", *cols, "revision_id", "revision_ts", "record_source"]
        values = [key, ts, hash_diff, *cols.values(), rev.revid, rev.timestamp, src]
        if has_lineage:
            names.append("lineage")
            values.append(lineage)
        self.db.execute(
            f"INSERT INTO {table} ({','.join(names)})"
            f" VALUES ({','.join('?' * len(names))})",
            values,
        )
        return outcome

    # --- reading -------------------------------------------------------------

    def current_facts(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT s.statement_hk, e.business_key AS subject, p.business_key AS predicate,
                   s.value, s.source_span, s.revision_id, s.revision_ts, s.load_ts,
                   s.record_source
            FROM sat_statement s
            JOIN link_statement l ON l.statement_hk = s.statement_hk
            JOIN hub_entity e     ON e.entity_hk    = l.subject_hk
            JOIN hub_predicate p  ON p.predicate_hk = l.predicate_hk
            WHERE s.load_end_ts IS NULL
            """
        ).fetchall()

    def history(self, subject: str | None = None) -> list[sqlite3.Row]:
        """Every superseded value: the audit trail of what the world used to say."""
        sql = """
            SELECT e.business_key AS subject, p.business_key AS predicate,
                   old.value AS old_value, old.revision_id AS old_revision,
                   old.load_ts AS valid_from, old.load_end_ts AS superseded_at,
                   new.value AS new_value, new.revision_id AS new_revision
            FROM sat_statement old
            JOIN link_statement l ON l.statement_hk = old.statement_hk
            JOIN hub_entity e     ON e.entity_hk    = l.subject_hk
            JOIN hub_predicate p  ON p.predicate_hk = l.predicate_hk
            LEFT JOIN sat_statement new
                   ON new.statement_hk = old.statement_hk
                  AND new.load_ts = old.load_end_ts
            WHERE old.load_end_ts IS NOT NULL
        """
        params: tuple = ()
        if subject:
            sql += " AND upper(e.business_key) = upper(?)"
            params = (subject,)
        return self.db.execute(sql + " ORDER BY old.load_end_ts DESC", params).fetchall()

    def contradictions(self) -> list[dict]:
        """Slots where sources currently disagree. Not history -- live conflict."""
        rows = self.db.execute(
            """
            SELECT e.business_key AS subject, p.business_key AS predicate,
                   s.value, s.lineage, s.record_source, s.revision_id
            FROM sat_statement s
            JOIN link_statement l ON l.statement_hk = s.statement_hk
            JOIN hub_entity e     ON e.entity_hk    = l.subject_hk
            JOIN hub_predicate p  ON p.predicate_hk = l.predicate_hk
            WHERE s.load_end_ts IS NULL
              AND s.statement_hk IN (
                  SELECT statement_hk FROM sat_statement
                  WHERE load_end_ts IS NULL
                  GROUP BY statement_hk HAVING count(DISTINCT value) > 1
              )
            ORDER BY e.business_key, p.business_key, s.lineage
            """
        ).fetchall()
        grouped: dict[tuple, list] = {}
        for r in rows:
            grouped.setdefault((r["subject"], r["predicate"]), []).append(dict(r))
        return [
            {"subject": k[0], "predicate": k[1], "claims": v}
            for k, v in grouped.items()
        ]

    def stats(self) -> dict:
        one = lambda q: self.db.execute(q).fetchone()[0]
        return {
            "entities (hubs)": one("SELECT count(*) FROM hub_entity"),
            "predicates (hubs)": one("SELECT count(*) FROM hub_predicate"),
            "statements (links)": one("SELECT count(*) FROM link_statement"),
            "satellite rows": one("SELECT count(*) FROM sat_statement"),
            "current (open) facts": one("SELECT count(*) FROM sat_statement WHERE load_end_ts IS NULL"),
            "superseded facts": one("SELECT count(*) FROM sat_statement WHERE load_end_ts IS NOT NULL"),
        }


# --- sentence-level storage: patterns, slots, utterances ---------------------

SENTENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_pattern (
    pattern_hk   TEXT PRIMARY KEY,
    business_key TEXT NOT NULL UNIQUE,   -- the frame, e.g. "{0} is an American {1}."
    slot_count   INTEGER NOT NULL,
    load_ts      TEXT NOT NULL,
    record_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_utterance (
    utterance_hk TEXT PRIMARY KEY,
    doc_hk       TEXT NOT NULL REFERENCES hub_entity(entity_hk),
    pattern_hk   TEXT NOT NULL REFERENCES hub_pattern(pattern_hk),
    sequence     INTEGER NOT NULL,
    load_ts      TEXT NOT NULL,
    record_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_slot (
    utterance_hk TEXT NOT NULL REFERENCES link_utterance(utterance_hk),
    position     INTEGER NOT NULL,
    entity_hk    TEXT NOT NULL REFERENCES hub_entity(entity_hk),
    -- How this mention was written. The hub holds identity ("Artificial
    -- intelligence"); a mention may render it differently ("artificial
    -- intelligence"), and the sentence needs the form actually used.
    surface      TEXT NOT NULL,
    PRIMARY KEY (utterance_hk, position)
);
CREATE TABLE IF NOT EXISTS sat_utterance (
    utterance_hk TEXT NOT NULL REFERENCES link_utterance(utterance_hk),
    load_ts      TEXT NOT NULL,
    load_end_ts  TEXT,
    hash_diff    TEXT NOT NULL,
    source_span  TEXT NOT NULL,          -- kept for audit; reconstruction never reads it
    revision_id  INTEGER NOT NULL,
    revision_ts  TEXT NOT NULL,
    record_source TEXT NOT NULL,
    PRIMARY KEY (utterance_hk, load_ts)
);
"""


def _load_sentences(self, rev: Revision) -> tuple[int, int]:
    """Store each prose sentence as pattern + slots. Reject what cannot be rebuilt."""
    ts, src = rev.timestamp, rev.record_source
    doc_hk = hk(rev.title)
    stored = rejected = 0
    occurrences: dict[str, int] = {}

    for seq, raw in enumerate(sentences(rev.wikitext)):
        try:
            pattern, fillers, _ = roundtrip(raw)
        except RoundTripError:
            rejected += 1          # not regenerable -> not admitted
            continue

        pattern_hk = hk(pattern)
        self.db.execute(
            "INSERT OR IGNORE INTO hub_pattern"
            " (pattern_hk, business_key, slot_count, load_ts, record_source) VALUES (?,?,?,?,?)",
            (pattern_hk, pattern, len(fillers), ts, src),
        )

        # Identity is (document, frame, which use of that frame). Keyed on the
        # pattern rather than the position so it survives edits elsewhere in the
        # document; the occurrence index keeps a repeated frame distinct.
        n = occurrences.get(pattern, 0)
        occurrences[pattern] = n + 1
        utterance_hk = hk(rev.title, pattern, str(n))
        self.db.execute(
            "INSERT OR IGNORE INTO link_utterance"
            " (utterance_hk, doc_hk, pattern_hk, sequence, load_ts, record_source)"
            " VALUES (?,?,?,?,?,?)",
            (utterance_hk, doc_hk, pattern_hk, seq, ts, src),
        )
        for position, filler in enumerate(fillers):
            entity_hk = self._hub("hub_entity", "entity_hk", filler, ts, src)
            self.db.execute(
                "INSERT OR REPLACE INTO link_slot (utterance_hk, position, entity_hk, surface)"
                " VALUES (?,?,?,?)",
                (utterance_hk, position, entity_hk, filler),
            )

        self._load_sat(
            "sat_utterance", "utterance_hk", utterance_hk,
            hashdiff(pattern, *fillers),
            {"source_span": clean(raw)},
            rev, ts, src,
        )
        stored += 1

    return stored, rejected


def reconstruct(self, subject: str) -> list[dict]:
    """Rebuild every sentence of a document from hub_pattern + hub_entity.

    Reads only the pattern hub and the slot links -- never sat_utterance.source_span
    -- so a match against that column is a real verification, not a copy.
    """
    rows = self.db.execute(
        """
        SELECT u.utterance_hk, u.sequence, p.business_key AS pattern,
               s.source_span, s.revision_id
        FROM link_utterance u
        JOIN hub_pattern p ON p.pattern_hk = u.pattern_hk
        JOIN hub_entity d  ON d.entity_hk  = u.doc_hk
        JOIN sat_utterance s ON s.utterance_hk = u.utterance_hk AND s.load_end_ts IS NULL
        WHERE upper(d.business_key) = upper(?)
        ORDER BY u.sequence
        """,
        (subject,),
    ).fetchall()

    out = []
    for r in rows:
        fillers = [
            f[0] for f in self.db.execute(
                "SELECT l.surface FROM link_slot l"
                " JOIN hub_entity e ON e.entity_hk = l.entity_hk"
                " WHERE l.utterance_hk = ? ORDER BY l.position",
                (r["utterance_hk"],),
            ).fetchall()
        ]
        rebuilt = construct(r["pattern"], fillers)
        out.append({
            "sequence": r["sequence"], "pattern": r["pattern"], "fillers": fillers,
            "rebuilt": rebuilt, "original": r["source_span"],
            "exact": rebuilt == r["source_span"], "revision_id": r["revision_id"],
        })
    return out


Vault._load_sentences = _load_sentences
Vault.reconstruct = reconstruct
