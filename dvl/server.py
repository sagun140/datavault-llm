"""Local web front end: ask the vault, and see every construct behind the answer."""

import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .generate import fact_segments, generate, utterance_segments
from .query import MIN_PREDICATE_SCORE, STOPWORDS, WH_WORDS, _score, ask
from .vault import Vault
from .verify import SCAFFOLD, tokens

DB = "vault.db"
HTML = Path(__file__).with_name("ui.html")


def _pages(v: Vault) -> list[dict]:
    rows = v.db.execute(
        """
        SELECT e.business_key AS title, s.url, s.revision_id, s.revision_ts,
               (SELECT count(*) FROM link_utterance u WHERE u.doc_hk = e.entity_hk) AS sentences
        FROM sat_entity s JOIN hub_entity e ON e.entity_hk = s.entity_hk
        WHERE s.load_end_ts IS NULL ORDER BY e.business_key
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _trace(v: Vault, q: str) -> dict:
    raw = set(tokens(q))
    wh = next((w for w in WH_WORDS if w in raw), None)
    q_tokens = raw - STOPWORDS - WH_WORDS
    scored = sorted(
        (_score(q_tokens, wh, r) for r in v.current_facts()),
        key=lambda x: (-x.predicate_score, -x.total),
    )
    return {
        "tokens": sorted(raw),
        "wh": wh,
        "content": sorted(q_tokens),
        "threshold": MIN_PREDICATE_SCORE,
        "rows": [
            {
                "subject": s.row["subject"], "predicate": s.row["predicate"],
                "lexical": s.lexical, "predicate_score": s.predicate_score,
                "total": round(s.total, 1),
                "passed": s.predicate_score >= MIN_PREDICATE_SCORE,
                "value": s.row["value"][:90],
            }
            for s in scored[:12]
        ],
    }


def _answer_payload(v: Vault, q: str) -> dict:
    a = ask(v, q)
    segments = []
    if not a.refused and a.citations:
        for c in a.citations:
            segments.append([
                s.as_dict()
                for s in fact_segments(c["subject"], c["predicate"], c["value"], c.get("revision_id"))
            ])
    return {
        "question": q, "text": a.text, "refused": a.refused,
        "citations": a.citations, "segments": segments, "trace": _trace(v, q),
    }


ROUTES = {}


def route(path):
    def deco(fn):
        ROUTES[path] = fn
        return fn
    return deco


@route("/api/overview")
def _overview(v, qs):
    return {"pages": _pages(v), "stats": v.stats(),
            "conflicts": len(v.contradictions()),
            "updates": len(v.history())}


@route("/api/ask")
def _ask(v, qs):
    return _answer_payload(v, qs.get("q", [""])[0])


@route("/api/table")
def _table(v, qs):
    name = qs.get("name", ["hub_entity"])[0]
    allowed = {
        "hub_entity": "SELECT entity_hk, business_key, load_ts FROM hub_entity ORDER BY business_key",
        "hub_predicate": "SELECT predicate_hk, business_key FROM hub_predicate ORDER BY business_key",
        "hub_pattern": "SELECT pattern_hk, slot_count, business_key FROM hub_pattern ORDER BY slot_count DESC",
        "link_statement": """SELECT l.statement_hk, e.business_key AS subject, p.business_key AS predicate
                             FROM link_statement l
                             JOIN hub_entity e ON e.entity_hk=l.subject_hk
                             JOIN hub_predicate p ON p.predicate_hk=l.predicate_hk
                             ORDER BY subject, predicate""",
        "sat_statement": """SELECT e.business_key AS subject, p.business_key AS predicate, s.value,
                                   s.load_ts, s.load_end_ts, s.revision_id, s.lineage
                            FROM sat_statement s
                            JOIN link_statement l ON l.statement_hk=s.statement_hk
                            JOIN hub_entity e ON e.entity_hk=l.subject_hk
                            JOIN hub_predicate p ON p.predicate_hk=l.predicate_hk
                            ORDER BY subject, predicate, s.load_ts""",
        "link_slot": """SELECT l.utterance_hk, l.position, l.surface, e.business_key AS entity
                        FROM link_slot l JOIN hub_entity e ON e.entity_hk=l.entity_hk
                        ORDER BY l.utterance_hk, l.position""",
    }
    if name not in allowed:
        return {"error": "unknown table"}
    rows = v.db.execute(allowed[name]).fetchall()
    return {"name": name, "columns": list(rows[0].keys()) if rows else [],
            "rows": [list(r) for r in rows[:400]], "total": len(rows)}


@route("/api/sentences")
def _sentences(v, qs):
    subject = qs.get("entity", [""])[0]
    out = []
    for u in v.reconstruct(subject)[:60]:
        out.append({
            **{k: u[k] for k in ("sequence", "pattern", "fillers", "rebuilt", "original", "exact")},
            "segments": [s.as_dict() for s in utterance_segments(u["pattern"], u["fillers"], u["fillers"])],
        })
    return {"entity": subject, "sentences": out,
            "exact": sum(s["exact"] for s in out), "count": len(out)}


@route("/api/generate")
def _generate(v, qs):
    g = generate(v, qs.get("entity", [""])[0], limit=6)
    return {"sentences": g.sentences, "counts": g.counts(), "text": g.text}


@route("/api/updates")
def _updates(v, qs):
    return {"updates": [dict(r) for r in v.history()]}


@route("/api/conflicts")
def _conflicts(v, qs):
    return {"conflicts": v.contradictions()}


@route("/api/scaffold")
def _scaffold(v, qs):
    return {"scaffold": sorted(SCAFFOLD)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = HTML.read_bytes()
            self._send(200, "text/html; charset=utf-8", body)
            return
        fn = ROUTES.get(u.path)
        if not fn:
            self._send(404, "text/plain", b"not found")
            return
        v = Vault(DB)
        try:
            payload = fn(v, parse_qs(u.query))
        except Exception as e:  # surface errors in the UI rather than a blank page
            payload = {"error": f"{type(e).__name__}: {e}"}
        finally:
            v.close()
        self._send(200, "application/json", json.dumps(payload, default=str).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(db: str = "vault.db", port: int = 8000):
    global DB
    DB = db
    print(f"datavault-llm → http://localhost:{port}   (db: {db}, Ctrl-C to stop)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
