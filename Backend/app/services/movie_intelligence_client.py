from __future__ import annotations

import asyncio
import re
from typing import Any

from app.agent.budget import budgeted_leaf
from app.config import settings
from app.observability import logger
from app.services import http_client
from app.services.web_search_client import GeminiGroundedSearchProvider, WebSearchClient
from app.services.wikidata_client import WikidataClient

_AWARD_QUERY = (
    '"{title}" awards (site:oscars.org OR site:bafta.org OR site:goldenglobes.com OR '
    "site:festival-cannes.com OR site:labiennale.org OR site:filmfare.com OR "
    'site:en.wikipedia.org/wiki/List_of_accolades_received_by OR "National Film Awards" OR "shortlist")'
)
_LOCATION_QUERY = '"{title}" "filming locations" "shot in" production'
_FACTS_QUERY = '"{title}" production "behind the scenes" trivia budget lenses camera'
_WIN_MARKERS = re.compile(r"\b(won|wins|winner|awarded|received)\b", re.IGNORECASE)
_NOMINATION_MARKERS = re.compile(r"\b(nominated|nomination|shortlisted)\b", re.IGNORECASE)
_FACT_MARKERS = re.compile(
    r"\b(camera|lens|lenses|shot on|filmed on|35mm|65mm|imax|arri|budget|box office|"
    r"stunt|set design|built|constructed|casting|cast as|originally cast|production)\b",
    re.IGNORECASE,
)
_FLUFF_MARKERS = re.compile(
    r"\b(great chemistry|amazing chemistry|wonderful cast|great cast)\b", re.IGNORECASE
)
# A direct Wikidata REST lookup (wbsearchentities); independent of
# WikidataClient's own timeout since this call bypasses it.
_WIKIDATA_REQUEST_TIMEOUT = settings.wikidata_timeout_seconds
# A Gemini-grounded web search: must stay above settings.gemini_grounding_
# timeout_seconds so that call's own timeout fires first with a
# classifiable error, instead of this wrapper cutting it off early.
_GROUNDED_SEARCH_TIMEOUT = settings.gemini_grounding_timeout_seconds + 2.0


class MovieIntelligenceClient:
    """Acquire and conservatively synthesize source-backed movie facts."""

    def __init__(self, *, web=None, wikidata=None, router=None):
        self.web = web or WebSearchClient(GeminiGroundedSearchProvider())
        self.wikidata = wikidata or WikidataClient()
        self.router = router

    async def close(self) -> None:
        for client in (self.web, self.wikidata):
            close = getattr(client, "close", None)
            if close:
                try:
                    await close()
                except Exception as exc:
                    logger.warning("movie_intelligence_client_cleanup_failed error=%r", exc)

    async def _wikidata_id(self, movie_title: str, year: int | None = None) -> str | None:
        try:
            async with budgeted_leaf():
                response = await asyncio.wait_for(
                    http_client.get_shared_client().get(
                        WikidataClient.SEARCH_URL,
                        params={
                            "action": "wbsearchentities",
                            "search": movie_title,
                            "language": "en",
                            "format": "json",
                            "limit": 5,
                            "type": "item",
                        },
                        headers={"User-Agent": WikidataClient.USER_AGENT},
                    ),
                    timeout=_WIKIDATA_REQUEST_TIMEOUT,
                )
            response.raise_for_status()
            candidates = response.json().get("search", [])
            expected = self._normalize(movie_title)
            matches = [
                candidate
                for candidate in candidates
                if self._normalize(candidate.get("label", "")) == expected
                and isinstance(candidate.get("id"), str)
                and re.fullmatch(r"Q\d+", candidate["id"])
            ]

            def rank(candidate):
                # Same-title items include novels, songs and remakes; prefer the
                # film, and the film from the requested year when one is known.
                description = str(candidate.get("description") or "").casefold()
                is_film = "film" in description or "movie" in description
                same_year = bool(year) and str(year) in description
                return (same_year, is_film)

            matches.sort(key=rank, reverse=True)
            if matches:
                description = str(matches[0].get("description") or "").casefold()
                if "film" in description or "movie" in description or len(matches) == 1:
                    return matches[0]["id"]
        except Exception as exc:
            logger.warning(
                "movie_intelligence_wikidata_id_failed title=%r error=%r",
                movie_title,
                exc,
            )
        return None

    async def _sparql(self, query: str) -> list[dict[str, Any]]:
        async with budgeted_leaf():
            rows = await self.wikidata.query_sparql(query)
        return [
            {key: value.get("value") for key, value in row.items() if isinstance(value, dict)} for row in rows
        ]

    async def _search(self, query: str) -> list[dict[str, str]]:
        try:
            async with budgeted_leaf(web=True):
                response = await asyncio.wait_for(self.web.search(query), timeout=_GROUNDED_SEARCH_TIMEOUT)
            results = response.get("results") or []
            return [
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "text": str(item.get("snippet") or item.get("content") or item.get("text") or ""),
                }
                for item in results
                if isinstance(item, dict) and (item.get("snippet") or item.get("content") or item.get("text"))
            ]
        except Exception as exc:
            logger.warning("movie_intelligence_search_failed query=%r error=%r", query, exc)
            return []

    async def collect(self, category: str, movie_title: str, year: int | None = None) -> dict[str, Any]:
        """Collect structured records and source snippets for one factual category."""
        if category not in {"awards", "locations", "facts"}:
            raise ValueError(f"Unsupported movie intelligence category: {category}")
        title = movie_title.strip()
        if not title:
            return {"category": category, "records": [], "sources": []}

        wikidata_id = await self._wikidata_id(title, year) if year else await self._wikidata_id(title)
        records: list[dict[str, Any]] = []
        if wikidata_id and category == "awards":
            # Independent queries: run concurrently so a slow one does not
            # add its latency on top of the others', within the same
            # overall budget. award_people is split into a Won-only and a
            # Nominated-only query (see _award_people_query_for_result) so
            # that a timeout on one half does not cost the other half's
            # already-arrived, correctly-attributed rows -- _sparql() never
            # raises on failure (query_sparql's raise_on_failure defaults to
            # False), so a failed branch simply contributes an empty list
            # here rather than losing the whole awards category.
            film_rows, won_people_rows, nominated_people_rows = await asyncio.gather(
                self._sparql(_film_awards_query(wikidata_id)),
                self._sparql(_award_people_won_query(wikidata_id)),
                self._sparql(_award_people_nominated_query(wikidata_id)),
            )
            people_rows = won_people_rows + nominated_people_rows
            # award_records() matches people rows to film rows by
            # (award, result); the dropped OPTIONAL year binding in the
            # split queries means people rows no longer carry "year", so
            # fill it in so the merge key stays complete for callers that
            # read it off unmatched people-only records.
            for row in won_people_rows:
                row.setdefault("result", "Won")
            for row in nominated_people_rows:
                row.setdefault("result", "Nominated")
            records = award_records(film_rows, people_rows, title)
            searched = await self._search(_AWARD_QUERY.format(title=title))
            # Search snippets corroborate or add official shortlists; they never create wins
            # or nominations on their own.
            sources = [
                item
                for item in searched
                if _WIN_MARKERS.search(item["text"]) or _NOMINATION_MARKERS.search(item["text"])
            ]
        elif category == "locations":
            if wikidata_id:
                records = location_records(await self._sparql(_location_query(wikidata_id)))
            verified_zones = {record["zone"] for record in records}
            if verified_zones != {"in_universe", "filming_location"}:
                sources = await self._search(_LOCATION_QUERY.format(title=title))
            else:
                sources = []
        else:
            searched = await self._search(_FACTS_QUERY.format(title=title))
            sources = [
                item
                for item in searched
                if _FACT_MARKERS.search(item["text"]) and not _FLUFF_MARKERS.search(item["text"])
            ]

        return {
            "category": category,
            "movie_title": title,
            "wikidata_id": wikidata_id,
            "records": records,
            "sources": sources,
        }

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _location_query(wikidata_id: str) -> str:
    """Story settings (P840) and filming locations (P915) with per-place detail.

    Each statement brings its coordinates, country, the part of the film it
    applies to (P518, e.g. a sequence), its reference URL, and whether the
    place is fictional (instance of a fictional location class).
    """
    return f"""
    SELECT ?zone ?place ?placeLabel ?coord ?countryLabel ?partLabel ?refURL ?fictional WHERE {{
      {{ wd:{wikidata_id} p:P840 ?st . ?st ps:P840 ?place . BIND("in_universe" AS ?zone) }}
      UNION
      {{ wd:{wikidata_id} p:P915 ?st . ?st ps:P915 ?place . BIND("filming_location" AS ?zone) }}
      OPTIONAL {{ ?place wdt:P625 ?coord . }}
      OPTIONAL {{ ?place wdt:P17 ?country . }}
      OPTIONAL {{ ?st pq:P518 ?part . }}
      OPTIONAL {{ ?st prov:wasDerivedFrom/pr:P854 ?refURL . }}
      BIND(EXISTS {{ ?place wdt:P31/wdt:P279* wd:Q3895768 }} AS ?fictional)
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    LIMIT 150
    """


def _coordinates(value: str | None) -> dict[str, float] | None:
    match = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", value or "")
    if not match:
        return None
    return {"lon": float(match.group(1)), "lat": float(match.group(2))}


def location_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge SPARQL rows into one record per (zone, place) with details and provenance."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        place = row.get("placeLabel")
        zone = row.get("zone")
        if not place or zone not in {"in_universe", "filming_location"} or re.fullmatch(r"Q\d+", place):
            continue
        record = merged.setdefault(
            (zone, place),
            {
                "zone": zone,
                "place": place,
                "source": "Wikidata P840" if zone == "in_universe" else "Wikidata P915",
                "wikidata_url": (row["place"] if str(row.get("place") or "").startswith("http") else None),
                "country": row.get("countryLabel") or None,
                "coordinates": _coordinates(row.get("coord")),
                "fictional": str(row.get("fictional")).lower() == "true",
                "scenes": [],
                "references": [],
            },
        )
        part = row.get("partLabel")
        if part and part not in record["scenes"]:
            record["scenes"].append(part)
        ref = row.get("refURL")
        if ref and ref not in record["references"]:
            record["references"].append(ref)
    return list(merged.values())


def _film_awards_query(wikidata_id: str) -> str:
    """Wins (P166) and nominations (P1411) on the film, with body and ceremony."""
    return f"""
    SELECT ?result ?awardLabel ?bodyLabel ?ceremonyLabel ?year WHERE {{
      {{ wd:{wikidata_id} p:P166 ?st . ?st ps:P166 ?award . BIND("Won" AS ?result) }}
      UNION
      {{ wd:{wikidata_id} p:P1411 ?st . ?st ps:P1411 ?award . BIND("Nominated" AS ?result) }}
      OPTIONAL {{ ?st pq:P585 ?date . BIND(YEAR(?date) AS ?year) }}
      OPTIONAL {{ ?st pq:P805 ?ceremony . }}
      OPTIONAL {{ ?award wdt:P1027 ?body . }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    LIMIT 200
    """


def _award_people_query_for_result(wikidata_id: str, *, main_property: str) -> str:
    """People who won or were nominated *for this work* (qualifier P1686
    'for work'), for a single result ("Won" or "Nominated") only.

    This used to be one query with both results combined via UNION. Split
    into two single-branch queries (one per result) run concurrently
    (collect() below), because:

    1. A UNION forces Blazegraph's planner to consider duplicate-
       elimination and plan each branch together, which is strictly more
       work than either branch alone -- and still timed out in practice
       even after the qualifier-first anchoring fix (Batch 11/13).
    2. Independently, a timeout on one result no longer costs the other:
       if "Won" people resolve but "Nominated" people time out (or vice
       versa), the half that succeeded is still real, attributed data --
       not silently dropped along with the half that failed. award_records()
       merges whatever rows actually arrived; it was never written to
       require both.

    The qualifier triple (``?st pq:P1686 wd:{wikidata_id}``) stays first so
    the query engine anchors on it before the far less selective
    ``?person p:...`` pattern (same reasoning as before, just applied to a
    single branch now instead of two). The redundant ``OPTIONAL`` year
    binding from the old combined query is dropped: award_records() never
    uses a people-row's year for merging (only for the rare case where no
    matching film-level record exists at all), so it cost a join for
    almost no benefit.
    """
    return f"""
    SELECT ?awardLabel ?personLabel WHERE {{
      ?st pq:P1686 wd:{wikidata_id} .
      ?person {main_property} ?st .
      ?st {main_property.replace("p:", "ps:")} ?award .
      ?person wdt:P31 wd:Q5 .
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    LIMIT 150
    """


def _award_people_won_query(wikidata_id: str) -> str:
    return _award_people_query_for_result(wikidata_id, main_property="p:P166")


def _award_people_nominated_query(wikidata_id: str) -> str:
    return _award_people_query_for_result(wikidata_id, main_property="p:P1411")


def award_records(
    film_rows: list[dict[str, Any]], people_rows: list[dict[str, Any]], title: str
) -> list[dict[str, Any]]:
    """One record per (award, result, year), with the people credited for it."""
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    def key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (row.get("awardLabel") or "", row.get("result") or "", str(row.get("year") or ""))

    for row in film_rows:
        if not row.get("awardLabel") or row.get("result") not in {"Won", "Nominated"}:
            continue
        record = merged.setdefault(
            key(row),
            {
                "award": row["awardLabel"],
                "body": row.get("bodyLabel") or None,
                "ceremony": row.get("ceremonyLabel") or None,
                "year": row.get("year") or "",
                "result": row["result"],
                "recipients": [],
                "recipient": title,
                "source": f"Wikidata {'P166 (award received)' if row['result'] == 'Won' else 'P1411 (nominated for)'}",
            },
        )
        record["body"] = record["body"] or row.get("bodyLabel") or None
        record["ceremony"] = record["ceremony"] or row.get("ceremonyLabel") or None
    for row in people_rows:
        if (
            not row.get("awardLabel")
            or not row.get("personLabel")
            or row.get("result") not in {"Won", "Nominated"}
        ):
            continue
        candidates = [
            r
            for (award, result, _year), r in merged.items()
            if award == row["awardLabel"] and result == row["result"]
        ]
        record = (
            candidates[0]
            if candidates
            else merged.setdefault(
                key(row),
                {
                    "award": row["awardLabel"],
                    "body": None,
                    "ceremony": None,
                    "year": row.get("year") or "",
                    "result": row["result"],
                    "recipients": [],
                    "recipient": title,
                    "source": "Wikidata (person's award for this film)",
                },
            )
        )
        if row["personLabel"] not in record["recipients"]:
            record["recipients"].append(row["personLabel"])
    won = {(r["award"], str(r["year"])) for r in merged.values() if r["result"] == "Won"}
    # A win is also recorded as a nomination on Wikidata; show it once, as a win.
    return [r for r in merged.values() if r["result"] == "Won" or (r["award"], str(r["year"])) not in won]
