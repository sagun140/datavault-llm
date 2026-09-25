"""CLI: ingest revisions into the vault, then interrogate it."""

import argparse
import sys

from .extract import extract
from .query import ask
from .vault import Vault
from .verify import Grounding_Error
from .wiki import fetch_revision


def cmd_ingest(args):
    vault = Vault(args.db)
    for asof in (args.asof or [None]):
        rev = fetch_revision(args.page, asof)
        report = vault.load_revision(rev, extract(rev))
        print(report)
        print()
    vault.close()


def cmd_ask(args):
    vault = Vault(args.db)
    try:
        print(ask(vault, " ".join(args.question)).render())
    except Grounding_Error as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 1
    finally:
        vault.close()
    return 0


def cmd_history(args):
    vault = Vault(args.db)
    rows = vault.history(args.subject)
    if not rows:
        print("No superseded values recorded.")
    for r in rows:
        print(
            f"{r['subject']} / {r['predicate']}\n"
            f"  was : {r['old_value']}  (rev {r['old_revision']}, from {r['valid_from']})\n"
            f"  now : {r['new_value']}  (rev {r['new_revision']}, since {r['superseded_at']})"
        )
    vault.close()


def cmd_stats(args):
    vault = Vault(args.db)
    for k, v in vault.stats().items():
        print(f"{k:24} {v}")
    vault.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="dvl", description=__doc__)
    p.add_argument("--db", default="vault.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="load one or more revisions of a Wikipedia page")
    i.add_argument("page")
    i.add_argument("--asof", action="append",
                   help="ISO timestamp; loads the revision current at that time. Repeatable, oldest first.")
    i.set_defaults(func=cmd_ingest)

    a = sub.add_parser("ask", help="ask the vault a question")
    a.add_argument("question", nargs="+")
    a.set_defaults(func=cmd_ask)

    h = sub.add_parser("history", help="show values the vault has superseded")
    h.add_argument("subject", nargs="?")
    h.set_defaults(func=cmd_history)

    s = sub.add_parser("stats", help="vault contents by table")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
