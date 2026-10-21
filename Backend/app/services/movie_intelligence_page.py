"""Structured Movie Intelligence categories for the movie page.

Product spec §8-§12 and UI spec §52-§60: the page shows Facts, Awards, Music
and Locations as separate, evidence-backed views. Each category returns
structured records (never prose invented by a model) plus the sources they
came from, a status, and an explicit coverage note so the UI never implies
completeness that the evidence does not support.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.evidence import source_tier
from app.observability import logger
from app.services.fact_status import STATUS_LABELS, classify_facts, detect_conflicts
from app.services.movie_intelligence_client import MovieIntelligenceClient
from app.services.music_versions import group_versions

CATEGORIES = ("facts", "awards", "music", "locations")


def _year(details: dict[str, Any]) -> int | None:
    value = str(details.get("release_date") or "")[:4]
    return int(value) if value.isdigit() else None


def _names(people) -> list[str]:
    out = []
    for person in people or []:
        name = person.get("name") if isinstance(person, dict) else person
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return list(dict.fromkeys(out))


def _source(item: dict[str, Any], status: str = "reported") -> dict[str, Any]:
    url = item.get("url") or ""
    domain = item.get("source_domain") or (urlparse(url).netloc or "").removeprefix("www.") or None
    return {
        "title": item.get("title") or domain or "Source",
        "url": url or None,
        "domain": domain,
        "excerpt": (item.get("text") or item.get("snippet") or "")[:600],
        "authority": {3: "primary", 2: "strong", 1: "secondary"}.get(source_tier(url), "unknown"),
        "status": status,
    }


async def build_category(
    category: str,
    details: dict[str, Any],
    *,
    client: MovieIntelligenceClient | None = None,
    soundtrack_lookup=None,
    album_lookup=None,
) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown intelligence category: {category}")
    title = details.get("title") or details.get("original_title") or ""
    base = {"category": category, "movie_id": details.get("id"), "title": title}

    if category == "music":
        return await _music(base, details, soundtrack_lookup, album_lookup)

    owns_client = client is None
    client = client or MovieIntelligenceClient()
    try:
        data = await client.collect(category, title, _year(details))
    finally:
        if owns_client:
            await client.close()
    records = data.get("records") or []
    sources = [_source(item) for item in data.get("sources") or []]

    if category == "awards":
        awards = official_award_view(records, data.get("sources") or [])
        return {
            **base,
            "status": "found" if awards else "no_verified_records",
            "wins": sum(1 for a in awards if a["result"] == "Won"),
            "nominations": sum(1 for a in awards if a["result"] == "Nominated"),
            "shortlisted": sum(1 for a in awards if a["result"] == "Shortlisted"),
            "awards": awards,
            "sources": sources,
            "coverage": (
                "Wins and nominations come from Wikidata, grouped by award body, with the people "
                "credited for this film. A record is marked verified when an official award-body "
                "page found for it confirms it; shortlists are shown only from official award-body "
                "sources. Records can be incomplete."
            ),
        }

    if category == "locations":
        story = list(dict.fromkeys(r["place"] for r in records if r.get("zone") == "in_universe"))
        filming = list(dict.fromkeys(r["place"] for r in records if r.get("zone") == "filming_location"))
        return {
            **base,
            "status": "found" if story or filming or sources else "no_verified_records",
            "story_locations": story,
            "filming_locations": filming,
            # Per-place detail (UI spec §60): real vs fictional, where it is, which part
            # of the film it applies to, and the reference behind the record.
            "location_details": [
                {
                    "zone": "story" if r.get("zone") == "in_universe" else "filming",
                    "place": r["place"],
                    "kind": "fictional" if r.get("fictional") else "real",
                    "country": r.get("country"),
                    "scenes": r.get("scenes") or [],
                    "map_url": (
                        "https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=10/{lat}/{lon}".format(
                            **r["coordinates"]
                        )
                        if r.get("coordinates") and not r.get("fictional")
                        else None
                    ),
                    "sources": [
                        {"title": r.get("source") or "Wikidata", "url": r.get("wikidata_url")},
                        *({"title": "Reference", "url": url} for url in (r.get("references") or [])[:2]),
                    ],
                    "fact_status": "strongly_supported",
                }
                for r in records
                if r.get("place")
            ],
            "location_conflicts": [
                c for c in detect_conflicts(data.get("sources") or []) if c["topic"] == "filming location"
            ],
            # Structured Wikidata records are strongly supported; web-only claims are reported.
            "location_status": {
                "structured": "strongly_supported" if story or filming else "unavailable",
                "web": "reported" if sources else "unavailable",
            },
            "sources": sources,
            "coverage": (
                "Story settings (Wikidata P840) and filming locations (Wikidata P915) are kept "
                "separate. Web excerpts are reported claims, not verified records."
            ),
        }

    # Facts: excerpts stating the same thing are grouped so independent support is
    # counted; each fact carries a status and numeric disagreements are surfaced.
    classified = classify_facts(sources)
    facts = classified["facts"]
    return {
        **base,
        "status": "found" if facts else "no_verified_records",
        "facts": facts,
        "conflicts": classified["conflicts"],
        "sources": sources,
        "coverage": (
            "Facts are labelled verified (primary source), strongly supported (several independent "
            "reputable sources), reported (one source), limited evidence (hedged) or conflicting."
        ),
    }


async def _music(
    base: dict[str, Any], details: dict[str, Any], soundtrack_lookup, album_lookup=None
) -> dict[str, Any]:
    composers = _names(details.get("music_directors") or details.get("composers"))
    lyricists = _names(details.get("songwriters"))
    languages = [lang for lang in (details.get("spoken_languages") or []) if isinstance(lang, str) and lang]
    tracks_by_language: dict[str, list[dict[str, Any]]] = {}
    lookup_error = None
    if composers and base["title"]:
        if soundtrack_lookup is None:
            from app.services.youtube_music_client import get_official_soundtracks

            soundtrack_lookup = get_official_soundtracks
        try:
            found = await soundtrack_lookup(
                base["title"],
                ", ".join(composers),
                tuple(languages[:4]) or ("English",),
            )
            tracks_by_language = {
                language: [
                    {
                        "title": track.get("track_name"),
                        "singers": list(track.get("singers") or []),
                    }
                    for track in tracks
                    if track.get("track_name")
                ]
                for language, tracks in (found or {}).items()
                if tracks
            }
        except (
            TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            httpx.HTTPError,
        ) as exc:
            # Catalogue lookup is optional enrichment; the credits still answer the question.
            lookup_error = type(exc).__name__
            logger.warning("music_catalogue_lookup_failed title=%r error=%r", base["title"], exc)
    album = None
    if base["title"]:
        if album_lookup is None:
            from app.services import musicbrainz_client

            album_lookup = musicbrainz_client.soundtrack
        album = await album_lookup(base["title"], _year(details))
    credits: dict[str, dict[str, Any]] = {}
    if album:
        for track in album.get("tracks") or []:
            credits[_song_key(track.get("title"))] = track
        if not tracks_by_language and album.get("tracks"):
            # No catalogue match: the soundtrack album itself lists the songs.
            tracks_by_language = {
                album.get("language")
                or "Soundtrack": [
                    {"title": t["title"], "singers": t.get("artists") or []}
                    for t in album["tracks"]
                    if t.get("title")
                ]
            }
    for tracks in tracks_by_language.values():
        for track in tracks:
            credit = credits.get(_song_key(track.get("title")))
            if credit:
                track["lyricists"] = credit.get("lyricists") or []
                track["composers"] = credit.get("composers") or []
                track["producers"] = credit.get("producers") or []
                track["length_seconds"] = credit.get("length_seconds")
    compositions = group_versions(tracks_by_language)
    for composition in compositions:
        for version in composition["versions"]:
            credit = credits.get(_song_key(version["title"]))
            if credit:
                version["lyricists"] = credit.get("lyricists") or []
                version["producers"] = credit.get("producers") or []
    producers = sorted({name for t in (album or {}).get("tracks") or [] for name in t.get("producers") or []})
    track_count = sum(len(v) for v in tracks_by_language.values())
    return {
        **base,
        "status": "found" if composers or track_count else "no_verified_records",
        "composers": composers,
        "lyricists": lyricists,
        "tracks_by_language": tracks_by_language,
        "compositions": compositions,
        "producers": producers,
        "albums": (
            [
                {
                    "title": album.get("title"),
                    "release_date": album.get("release_date"),
                    "labels": album.get("labels") or [],
                    "language": album.get("language"),
                    "country": album.get("country"),
                    "track_count": len(album.get("tracks") or []),
                    "url": album.get("url"),
                    "source": "MusicBrainz",
                }
            ]
            if album
            else []
        ),
        "completeness": "partial" if track_count else "unknown",
        "sources": [
            {
                "title": "TMDB credits",
                "url": f"https://www.themoviedb.org/movie/{base['movie_id']}",
                "authority": "strong",
            },
            *(
                [
                    {
                        "title": "YouTube Music catalogue (unofficial)",
                        "url": "https://music.youtube.com/",
                        "authority": "secondary",
                    }
                ]
                if track_count
                else []
            ),
            *(
                [
                    {
                        "title": f"MusicBrainz: {album.get('title')}",
                        "url": album.get("url"),
                        "authority": "strong",
                    }
                ]
                if album
                else []
            ),
        ],
        "lookup_error": lookup_error,
        "coverage": (
            "Composer and lyricist credits come from TMDB. Tracks come from a music catalogue search "
            "and may be incomplete; dubbed versions are grouped by language and are not assumed to be "
            "the same composition unless the source says so."
        ),
    }


_SHORTLIST = re.compile(
    r"\bshort-?listed (?:for|in) (?:the )?(?P<award>[A-Z][^.;:\n]{3,90}?)(?: at| in \d{4}|[.;:\n]|$)"
    r"|\bshortlist (?:for|of) (?:the )?(?P<award2>[A-Z][^.;:\n]{3,90}?)(?: at| in \d{4}|[.;:\n]|$)"
)


def _award_tokens(text: str) -> set[str]:
    stop = {"award", "awards", "academy", "best", "for", "the", "and", "film", "annual"}
    return {t for t in re.findall(r"[a-z]{4,}", (text or "").casefold()) if t not in stop}


def official_award_view(
    records: list[dict[str, Any]], raw_sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Award rows with body, people, official corroboration and official shortlists (spec §10)."""
    official = [
        item for item in raw_sources if source_tier(item.get("source_domain") or item.get("url") or "") >= 3
    ]
    awards: list[dict[str, Any]] = []
    for record in records:
        if record.get("result") not in {"Won", "Nominated"}:
            continue
        award_name = record.get("award") or record.get("category") or record.get("ceremony") or ""
        wanted = _award_tokens(award_name)
        confirming = next(
            (
                item
                for item in official
                if wanted and len(wanted & _award_tokens(item.get("text") or "")) >= max(1, len(wanted) // 2)
            ),
            None,
        )
        status = "verified" if confirming else "strongly_supported"
        awards.append(
            {
                "result": record["result"],
                "year": record.get("year") or None,
                "body": record.get("body") or record.get("ceremony") or None,
                "ceremony": record.get("ceremony") or None,
                "award": award_name or None,
                "category": award_name or None,
                "recipients": record.get("recipients") or [],
                "source": record.get("source"),
                "official_source": (
                    {"title": confirming.get("title"), "url": confirming.get("url")} if confirming else None
                ),
                "fact_status": status,
                "fact_status_label": STATUS_LABELS[status],
            }
        )
    seen = {(a["award"] or "").casefold() for a in awards}
    for item in official:
        for match in _SHORTLIST.finditer(item.get("text") or ""):
            name = (match.group("award") or match.group("award2") or "").strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                awards.append(
                    {
                        "result": "Shortlisted",
                        "year": None,
                        "body": urlparse(item.get("url") or "").netloc.removeprefix("www.") or None,
                        "ceremony": None,
                        "award": name,
                        "category": name,
                        "recipients": [],
                        "source": item.get("title") or "Official award body",
                        "official_source": {"title": item.get("title"), "url": item.get("url")},
                        "fact_status": "verified",
                        "fact_status_label": STATUS_LABELS["verified"],
                    }
                )
    order = {"Won": 0, "Nominated": 1, "Shortlisted": 2}
    awards.sort(key=lambda a: (str(a["body"] or "~"), order[a["result"]], str(a["year"] or "")))
    return awards


def _song_key(title: Any) -> str:
    from app.services.music_versions import composition_key

    return composition_key(str(title or ""))
