"""Person / entity pages (UI spec §51, §108; product spec §3.5 entity awareness).

A person page shows who someone is (TMDB biography and department), their
filmography grouped by role, awards from structured Wikidata records (won and
nominated kept separate), frequent collaborators derived from their own credits,
and nothing that the sources do not state.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import httpx

from app.agent.budget import budgeted_leaf
from app.observability import logger
from app.services.tmdb_client import TMDBClient

PROFILE_BASE = "https://image.tmdb.org/t/p/w342"
POSTER_BASE = "https://image.tmdb.org/t/p/w185"


def _year(value: Any) -> int | None:
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else None


def _credit(item: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "tmdb_id": item.get("id"),
        "title": item.get("title") or item.get("original_title"),
        "year": _year(item.get("release_date")),
        "role": role,
        "poster_url": f"{POSTER_BASE}{item['poster_path']}" if item.get("poster_path") else None,
        "vote_average": item.get("vote_average"),
        "popularity": item.get("popularity") or 0,
    }


def build_filmography(credits: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group a person's movie credits by role, newest first, de-duplicated."""
    groups: dict[str, dict[Any, dict[str, Any]]] = {}
    for item in credits.get("cast") or []:
        if item.get("id"):
            character = item.get("character")
            groups.setdefault("Acting", {}).setdefault(
                item["id"], _credit(item, f"as {character}" if character else "Actor")
            )
    for item in credits.get("crew") or []:
        if not item.get("id"):
            continue
        department = item.get("department") or "Crew"
        bucket = groups.setdefault(department, {})
        existing = bucket.get(item["id"])
        if existing:
            if item.get("job") and item["job"] not in existing["role"]:
                existing["role"] = f"{existing['role']}, {item['job']}"
        else:
            bucket[item["id"]] = _credit(item, item.get("job") or department)
    ordered = {}
    for department, items in groups.items():
        ordered[department] = sorted(
            items.values(), key=lambda c: (c["year"] is None, -(c["year"] or 0), -c["popularity"])
        )
    # Largest body of work first.
    return dict(sorted(ordered.items(), key=lambda kv: -len(kv[1])))


def known_for(filmography: dict[str, list[dict[str, Any]]], limit: int = 8) -> list[dict[str, Any]]:
    seen, out = set(), []
    everything = [credit for credits in filmography.values() for credit in credits]
    for credit in sorted(everything, key=lambda c: -(c["popularity"] or 0)):
        if credit["tmdb_id"] not in seen:
            seen.add(credit["tmdb_id"])
            out.append(credit)
        if len(out) >= limit:
            break
    return out


async def _awards(wikidata_id: str | None) -> list[dict[str, Any]]:
    if not wikidata_id:
        return []
    from app.services.wikidata_client import WikidataClient

    client = WikidataClient()
    records: list[dict[str, Any]] = []
    try:
        for prop, result in (("P166", "Won"), ("P1411", "Nominated")):
            query = f"""
            SELECT ?awardLabel ?year ?forWorkLabel WHERE {{
              wd:{wikidata_id} p:{prop} ?statement .
              ?statement ps:{prop} ?award .
              OPTIONAL {{ ?statement pq:P585 ?date . BIND(YEAR(?date) AS ?year) }}
              OPTIONAL {{ ?statement pq:P1686 ?forWork . }}
              SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
            }}
            LIMIT 120
            """
            async with budgeted_leaf():
                rows = await client.query_sparql(query)
            for row in rows:
                value = {k: v.get("value") for k, v in row.items() if isinstance(v, dict)}
                if value.get("awardLabel"):
                    records.append(
                        {
                            "result": result,
                            "award": value["awardLabel"],
                            "year": value.get("year"),
                            "for_work": value.get("forWorkLabel"),
                            "source": f"Wikidata {prop}",
                        }
                    )
    except (TimeoutError, OSError, RuntimeError, ValueError, KeyError, httpx.HTTPError) as exc:
        # Awards are optional enrichment on a person page.
        logger.warning("person_awards_failed wikidata_id=%s error=%r", wikidata_id, exc)
    finally:
        close = getattr(client, "close", None)
        if close:
            await close()
    won = {(r["award"], r["year"]) for r in records if r["result"] == "Won"}
    records = [r for r in records if r["result"] == "Won" or (r["award"], r["year"]) not in won]
    records.sort(
        key=lambda r: (r["result"] != "Won", -(int(r["year"]) if str(r["year"] or "").isdigit() else 0))
    )
    return records


_COLLABORATOR_JOBS = {
    "Director",
    "Director of Photography",
    "Original Music Composer",
    "Screenplay",
    "Writer",
    "Editor",
}


async def collaborators(person_id: int, films: list[dict[str, Any]], *, tmdb: TMDBClient, limit: int = 8):
    """People who appear in at least two of this person's best-known films.

    Derived only from those films' TMDB credits, so every collaborator is
    backed by named shared titles.
    """

    async def credits(film):
        try:
            async with budgeted_leaf():
                details = await tmdb.get_movie_details(film["tmdb_id"])
            return film, details.get("credits") or {}
        except (TimeoutError, OSError, RuntimeError, ValueError, KeyError, httpx.HTTPError) as exc:
            logger.warning("collaborator_credits_failed movie_id=%s error=%r", film.get("tmdb_id"), exc)
            return film, {}

    results = await asyncio.gather(*(credits(film) for film in films[:6] if film.get("tmdb_id")))
    counts: Counter = Counter()
    shared: dict[int, dict[str, Any]] = {}
    for film, credit in results:
        people = [(p, "Cast") for p in (credit.get("cast") or [])[:10]]
        people += [(p, p.get("job")) for p in credit.get("crew") or [] if p.get("job") in _COLLABORATOR_JOBS]
        seen_here = set()
        for person, role in people:
            pid = person.get("id")
            if not pid or pid == person_id or pid in seen_here:
                continue
            seen_here.add(pid)
            counts[pid] += 1
            entry = shared.setdefault(
                pid, {"id": pid, "name": person.get("name"), "roles": set(), "titles": []}
            )
            entry["roles"].add(role)
            entry["titles"].append(film["title"])
    out = []
    for pid, count in counts.most_common():
        if count < 2:
            break
        entry = shared[pid]
        out.append(
            {
                "id": pid,
                "name": entry["name"],
                "roles": sorted(r for r in entry["roles"] if r),
                "shared_titles": entry["titles"],
            }
        )
        if len(out) >= limit:
            break
    return out


async def build_person(person_id: int, *, tmdb: TMDBClient, awards_lookup=None) -> dict[str, Any]:
    async with budgeted_leaf():
        person = await tmdb._get(
            f"/person/{person_id}",
            {"language": "en-US", "append_to_response": "movie_credits,external_ids"},
        )
    filmography = build_filmography(person.get("movie_credits") or {})
    wikidata_id = (person.get("external_ids") or {}).get("wikidata_id")
    best_known = known_for(filmography)
    awards, frequent = await asyncio.gather(
        (awards_lookup or _awards)(wikidata_id),
        collaborators(person_id, best_known, tmdb=tmdb),
    )
    return {
        "id": person.get("id"),
        "name": person.get("name"),
        "known_for_department": person.get("known_for_department"),
        "biography": person.get("biography") or None,
        "birthday": person.get("birthday"),
        "deathday": person.get("deathday"),
        "place_of_birth": person.get("place_of_birth"),
        "also_known_as": (person.get("also_known_as") or [])[:6],
        "profile_url": f"{PROFILE_BASE}{person['profile_path']}" if person.get("profile_path") else None,
        "known_for": best_known,
        "filmography": filmography,
        "awards": awards,
        "collaborators": frequent,
        "sources": [
            {
                "title": "TMDB person record",
                "url": f"https://www.themoviedb.org/person/{person_id}",
                "authority": "strong",
            },
            *(
                [
                    {
                        "title": "Wikidata",
                        "url": f"https://www.wikidata.org/wiki/{wikidata_id}",
                        "authority": "secondary",
                    }
                ]
                if wikidata_id
                else []
            ),
        ],
        "coverage": (
            "Filmography comes from TMDB movie credits. Awards are structured Wikidata records and can be "
            "incomplete; official award-body records are authoritative."
        ),
    }


async def search_people(query: str, *, tmdb: TMDBClient, limit: int = 8) -> list[dict[str, Any]]:
    async with budgeted_leaf():
        data = await tmdb._get("/search/person", {"query": query, "include_adult": False, "page": 1})
    return [
        {
            "id": person.get("id"),
            "name": person.get("name"),
            "known_for_department": person.get("known_for_department"),
            "profile_url": (
                f"{PROFILE_BASE}{person['profile_path']}" if person.get("profile_path") else None
            ),
            "known_for": [
                item.get("title") or item.get("name")
                for item in (person.get("known_for") or [])[:3]
                if item.get("title") or item.get("name")
            ],
        }
        for person in (data.get("results") or [])[:limit]
    ]
