from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

PRIMARY_HOSTS = {
    "oscars.org",
    "bafta.org",
    "goldenglobes.com",
    "bfi.org.uk",
    "loc.gov",
    "afi.com",
    "sundance.org",
    "festival-cannes.com",
    "venicebiennale.it",
    "labiennale.org",
    "berlinale.de",
    "tiff.net",
    "filmfare.com",
    "dff.gov.in",
    "pib.gov.in",
    "iffi.gov.in",
    "spiritawards.com",
    "cesarducinema.com",
}
STRONG_HOSTS = PRIMARY_HOSTS | {
    "criterion.com",
    "variety.com",
    "hollywoodreporter.com",
    "deadline.com",
    "indiewire.com",
    "imdb.com",
    "rottentomatoes.com",
    "metacritic.com",
    "theguardian.com",
    "nytimes.com",
    "spotify.com",
    "open.spotify.com",
    "music.apple.com",
}


def normalize_url(url):
    if not url:
        return None
    u = url.strip().casefold()
    u = u.split("#", 1)[0].rstrip("/")
    u = u.replace("://www.", "://", 1)
    return u


def source_host(value):
    """Return the publisher host for a URL or a bare domain such as ``variety.com``."""
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.casefold().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def item_tier(item):
    """Authority tier of an evidence item, preferring its resolved publisher domain.

    Search-grounding providers return redirect URLs whose host says nothing
    about the publisher; the item's ``source_domain`` field carries the real one.
    """
    metadata = item.get("metadata") or item.get("metadata_json") or {}
    return source_tier(item.get("source_domain") or metadata.get("source_domain") or item.get("url"))


def source_tier(url):
    host = source_host(url)
    if host.startswith("www."):
        host = host[4:]
    if host in PRIMARY_HOSTS or any(host.endswith("." + x) for x in PRIMARY_HOSTS):
        return 3
    if host in STRONG_HOSTS or any(host.endswith("." + x) for x in STRONG_HOSTS):
        return 2
    return 1 if host else 0


def fingerprint(text):
    clean = re.sub(r"\s+", " ", (text or "").strip().casefold())
    return hashlib.sha256(clean.encode()).hexdigest()[:24]


def dedupe(items):
    seen = set()
    out = []
    for item in items:
        domain = (item.get("domain") or "").strip().casefold()
        url_key = normalize_url(item.get("url"))
        text_key = fingerprint(item.get("snippet") or item.get("text"))
        key = (domain, url_key or text_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def rank(items):
    return sorted(
        items,
        key=lambda x: (
            item_tier(x),
            bool(x.get("published_at")),
            len(x.get("snippet") or x.get("text") or ""),
        ),
        reverse=True,
    )


def independent_sources(items):
    hosts = set()
    out = []
    for x in items:
        host = source_host(x.get("source_domain") or x.get("url"))
        if host and host not in hosts:
            hosts.add(host)
            out.append(x)
    return out


def evidence_score(items):
    """Conservative score based on source quality, independence and coverage."""
    items = dedupe(items or [])
    if not items:
        return {"score": 0.0, "count": 0, "independent_sources": 0, "tier3": 0}
    indep = independent_sources(items)
    tier3 = sum(1 for x in items if item_tier(x) >= 3)
    score = min(
        1.0,
        0.45 * min(1, len(indep) / 4) + 0.35 * min(1, len(items) / 6) + 0.20 * min(1, tier3 / 2),
    )
    return {
        "score": round(score, 3),
        "count": len(items),
        "independent_sources": len(indep),
        "tier3": tier3,
    }
