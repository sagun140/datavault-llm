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

from .extract import Fact
from .wiki import Revision

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

    def __str__(self) -> str:
        return (
            f"{self.entity} @rev {self.revision_id} ({self.revision_ts})\n"
            f"  facts extracted : {self.facts_seen}\n"
            f"  new statements  : {self.inserted}\n"
            f"  changed values  : {self.changed}\n"
            f"  unchanged       : {self.unchanged}\n"
            f"  rejected (no verbatim source span): {self.rejected_no_provenance}"
        )


class Vault:
    def __init__(self, path: str = "vault.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

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
                rev, ts, src,
            )
            setattr(rep, outcome, getattr(rep, outcome) + 1)

        self.db.commit()
        return rep

    def _load_sat(self, table, key_col, key, hash_diff, cols, rev, ts, src) -> str:
        """Type-2 upsert: no-op if unchanged, else end-date the open row and insert."""
        cur = self.db.execute(
            f"SELECT hash_diff, load_ts FROM {table}"
            f" WHERE {key_col}=? AND load_end_ts IS NULL",
            (key,),
        ).fetchone()

        if cur and cur["hash_diff"] == hash_diff:
            return "unchanged"

        outcome = "inserted"
        if cur:
            if ts <= cur["load_ts"]:
                return "unchanged"  # out-of-order load: never rewrite newer history
            self.db.execute(
                f"UPDATE {table} SET load_end_ts=? WHERE {key_col}=? AND load_end_ts IS NULL",
                (ts, key),
            )
            outcome = "changed"

        names = [key_col, "load_ts", "hash_diff", *cols, "revision_id", "revision_ts", "record_source"]
        values = [key, ts, hash_diff, *cols.values(), rev.revid, rev.timestamp, src]
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
