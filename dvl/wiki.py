"""Fetch Wikipedia revisions via the MediaWiki API (stdlib only)."""

import json
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass

API = "https://en.wikipedia.org/w/api.php"
UA = "datavault-llm/0.1 (grounded-vault experiment; https://github.com/sagun140)"


@dataclass
class Revision:
    title: str
    pageid: int
    revid: int
    timestamp: str
    wikitext: str

    @property
    def url(self) -> str:
        return f"https://en.wikipedia.org/w/index.php?oldid={self.revid}"

    @property
    def record_source(self) -> str:
        return f"enwiki:{self.title}@{self.revid}"


def _ssl_context() -> ssl.SSLContext:
    """python.org macOS builds ship without a CA bundle; use certifi when present."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(params: dict) -> dict:
    params = {**params, "format": "json", "formatversion": "2"}
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_revision(title: str, asof: str | None = None) -> Revision:
    """Current revision of `title`, or the last one at/before ISO timestamp `asof`."""
    params = {
        "action": "query",
        "prop": "revisions",
        "titles": title,
        "rvprop": "ids|timestamp|content",
        "rvslots": "main",
        "rvlimit": "1",
        "redirects": "1",
    }
    if asof:
        params["rvstart"] = asof
        params["rvdir"] = "older"

    pages = _get(params)["query"]["pages"]
    page = pages[0]
    if "missing" in page:
        raise LookupError(f"No such Wikipedia page: {title!r}")
    if not page.get("revisions"):
        raise LookupError(f"No revision of {title!r} at or before {asof}")

    rev = page["revisions"][0]
    return Revision(
        title=page["title"],
        pageid=page["pageid"],
        revid=rev["revid"],
        timestamp=rev["timestamp"],
        wikitext=rev["slots"]["main"]["content"],
    )
