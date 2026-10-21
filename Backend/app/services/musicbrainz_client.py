"""MusicBrainz soundtrack lookup (product spec §11.1, §25 "structured secondary music source").

Finds the soundtrack release group for a film, then one release with its
tracks and credits: performers, producers (recording relationships), and
lyricists/composers (work relationships). MusicBrainz asks clients for a
descriptive User-Agent and at most one request per second; both are honoured.
Results are cached in-process because soundtrack metadata is stable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.observability import logger

API = "https://musicbrainz.org/ws/2"
USER_AGENT = "Grace/1.0 ( https://github.com/ai-movie-agent )"
_LANGUAGES = {
    "eng": "English",
    "hin": "Hindi",
    "tel": "Telugu",
    "tam": "Tamil",
    "mal": "Malayalam",
    "kan": "Kannada",
    "ben": "Bengali",
    "mar": "Marathi",
    "pan": "Punjabi",
    "jpn": "Japanese",
    "kor": "Korean",
    "spa": "Spanish",
    "fra": "French",
    "deu": "German",
    "ita": "Italian",
}
_cache: dict[tuple[str, int | None], dict[str, Any] | None] = {}
_lock = asyncio.Lock()
_last_request = 0.0


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    global _last_request
    async with _lock:  # one request per second across the process
        wait = 1.0 - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()
        response = await client.get(f"{API}{path}", params={**params, "fmt": "json"})
    response.raise_for_status()
    return response.json()


def _names(credits: list[dict[str, Any]] | None) -> list[str]:
    names = (c.get("name") or (c.get("artist") or {}).get("name") for c in credits or [] if c)
    return [str(name) for name in names if name]


def _related(relations: list[dict[str, Any]] | None, kinds: set[str]) -> list[str]:
    out = []
    for relation in relations or []:
        if relation.get("type") in kinds and (relation.get("artist") or {}).get("name"):
            name = relation["artist"]["name"]
            if name not in out:
                out.append(name)
    return out


def pick_release_group(groups: list[dict[str, Any]], title: str, year: int | None) -> dict[str, Any] | None:
    wanted = title.casefold()
    scored = []
    for group in groups:
        if "Soundtrack" not in (group.get("secondary-types") or []):
            continue
        name = str(group.get("title") or "").casefold()
        if wanted not in name:
            continue
        group_year = str(group.get("first-release-date") or "")[:4]
        if year and group_year.isdigit() and abs(int(group_year) - year) > 1:
            continue
        scored.append((name == wanted or name.startswith(wanted), group.get("score") or 0, group))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2] if scored else None


def parse_release(release: dict[str, Any]) -> dict[str, Any]:
    language_code = (release.get("text-representation") or {}).get("language")
    tracks = []
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            relations = recording.get("relations") or []
            work_relations = [
                rel
                for relation in relations
                if relation.get("type") == "performance"
                for rel in ((relation.get("work") or {}).get("relations") or [])
            ]
            tracks.append(
                {
                    "title": track.get("title") or recording.get("title"),
                    "length_seconds": round((track.get("length") or 0) / 1000) or None,
                    "artists": [
                        n for n in _names(track.get("artist-credit") or recording.get("artist-credit")) if n
                    ],
                    "producers": _related(relations, {"producer"}),
                    "lyricists": _related(work_relations, {"lyricist", "writer"}),
                    "composers": _related(work_relations, {"composer"}),
                }
            )
    labels = [(item.get("label") or {}).get("name") for item in release.get("label-info") or []]
    return {
        "title": release.get("title"),
        "release_date": release.get("date") or None,
        "country": release.get("country") or None,
        "labels": [label for label in labels if label],
        "language": _LANGUAGES.get(language_code or "", language_code),
        "url": f"https://musicbrainz.org/release/{release.get('id')}" if release.get("id") else None,
        "tracks": tracks,
    }


async def soundtrack(
    title: str, year: int | None = None, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """The film's soundtrack album with per-track credits, or None when not found."""
    key = (title.casefold().strip(), year)
    if key in _cache:
        return _cache[key]
    owns = client is None
    client = client or httpx.AsyncClient(timeout=8.0, headers={"User-Agent": USER_AGENT})
    try:
        search = await _get(
            client,
            "/release-group",
            {"query": f'releasegroup:"{title}" AND secondarytype:soundtrack', "limit": 10},
        )
        group = pick_release_group(search.get("release-groups") or [], title, year)
        if group is None:
            _cache[key] = None
            return None
        releases = [r for r in group.get("releases") or [] if r.get("id")]
        official = [r for r in releases if r.get("status") == "Official"] or releases
        if not official:
            _cache[key] = None
            return None
        release = await _get(
            client,
            f"/release/{official[0]['id']}",
            {
                "inc": "recordings+artist-credits+labels+recording-level-rels+work-rels+work-level-rels+artist-rels"
            },
        )
        album = parse_release(release)
        album["release_group_url"] = f"https://musicbrainz.org/release-group/{group.get('id')}"
        album["source"] = "MusicBrainz"
        _cache[key] = album
        return album
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("musicbrainz_lookup_failed title=%r error=%r", title, exc)
        return None
    finally:
        if owns:
            await client.aclose()
