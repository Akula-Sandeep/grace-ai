"""Live provider smoke check — run by the owner with real (rotated) credentials.

    cd Backend && python scripts/live_provider_smoke.py            # every provider
    cd Backend && python scripts/live_provider_smoke.py --only tmdb,wikidata

Each check goes through Grace's own client code where one exists, so a PASS
means the production code path works against the live service, not just that
the key is valid. Keys come from the normal settings (.env / environment) and
are never printed; error text is redacted before display.

Exit status: 0 when nothing FAILed (SKIP = credential not configured), 1 otherwise.
This script is not run in CI; it needs network egress and real credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import struct
import sys
import time
import zlib
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app.config import settings

TIMEOUT = 30.0
SECRETS = [
    value
    for value in (settings.groq_api_key, settings.gemini_api_key, settings.tmdb_api_key)
    if value and len(value) >= 8
]


class Skip(Exception):
    """The provider's credential is not configured."""


def redact(text: str) -> str:
    for secret in SECRETS:
        text = text.replace(secret, "[redacted]")
    return text[:300]


def _png(width: int = 64, height: int = 64, rgb: tuple[int, int, int] = (220, 20, 20)) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


async def check_groq() -> str:
    if not settings.groq_api_key:
        raise Skip("GROQ_API_KEY not set")
    from groq import AsyncGroq

    client = AsyncGroq(api_key=settings.groq_api_key, timeout=TIMEOUT, max_retries=0)
    notes = []
    for label, model in (("fast", settings.groq_model), ("deep", settings.groq_deep_model)):
        reply = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly the word: pong"}],
            max_tokens=200,
        )
        text = (reply.choices[0].message.content or "").strip().lower()
        if "pong" not in text:
            raise AssertionError(f"{label} model {model} replied {text[:60]!r}")
        notes.append(f"{label}={model}")
    return ", ".join(notes)


async def check_vision() -> str:
    if not settings.groq_api_key:
        raise Skip("GROQ_API_KEY not set")
    from groq import AsyncGroq

    client = AsyncGroq(api_key=settings.groq_api_key, timeout=TIMEOUT, max_retries=0)
    image = "data:image/png;base64," + base64.b64encode(_png()).decode()
    reply = await client.chat.completions.create(
        model=settings.groq_vision_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What single colour fills this image? Answer with one word."},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        ],
        max_tokens=300,
    )
    text = (reply.choices[0].message.content or "").lower()
    if "red" not in text:
        raise AssertionError(f"expected 'red', got {text[:60]!r}")
    return f"model={settings.groq_vision_model}"


async def check_gemini() -> str:
    if not settings.gemini_api_key:
        raise Skip("GEMINI_API_KEY not set")
    from app.services.web_search_client import GeminiGroundedSearchProvider, WebSearchClient

    client = WebSearchClient(GeminiGroundedSearchProvider())
    try:
        result = await client.search("Who composed the score for Interstellar (2014 film)?")
    finally:
        await client.close()
    if result.get("fallback"):
        raise AssertionError(f"grounding fell back: {result['fallback']}")
    sources = result.get("sources") or []
    if not sources:
        raise AssertionError("no grounded sources returned")
    if "zimmer" not in (result.get("summary") or "").lower():
        raise AssertionError("grounded summary does not name Hans Zimmer")
    return f"{len(sources)} grounded source(s), model={settings.gemini_model}"


async def check_tmdb() -> str:
    if not settings.tmdb_api_key:
        raise Skip("TMDB_API_KEY not set")
    from app.services.tmdb_client import TMDBClient
    from app.services.upstream_errors import UpstreamError

    # Goes through TMDBClient's own retry/timeout logic (not a raw httpx
    # call), so a PASS here also verifies that code path, not just that the
    # key and the network are fine.
    client = TMDBClient()
    try:
        details = await client.get_movie_details(27205)
        if details.get("title") != "Inception":
            raise AssertionError(f"unexpected title {details.get('title')!r}")
        await client.get_watch_providers(27205)
    except UpstreamError as exc:
        raise AssertionError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        await client.close()
    headers, _ = TMDBClient.credentials(settings.tmdb_api_key)
    auth = "v4 bearer" if "Authorization" in headers else "v3 api_key"
    return f"details+credits+watch providers OK ({auth})"


async def check_wikidata() -> str:
    """Runs the location query, the film-awards query, and BOTH
    award-people sub-queries (won, nominated -- split in this batch from
    one combined UNION query that kept timing out) separately and
    concurrently, as the app does, so a single slow or failing query is
    identifiable instead of being folded into one pass/fail.

    Calls WikidataClient.query_sparql(..., raise_on_failure=True) directly
    (bypassing MovieIntelligenceClient._sparql's production swallow-to-[]
    behaviour, which exists for graceful degradation, not for a
    verification tool) so each subquery's real outcome is known precisely
    -- a query that failed and a query that genuinely found nothing must
    never be reported as the same thing here.

    Reports exactly the three categories the app itself reports (locations,
    film_awards, award_people), but award_people's status is now derived
    from its two underlying sub-queries:
      - "ok"     both the won- and nominated-people queries succeeded.
      - "partial" one succeeded and one failed/timed out -- the people who
                  DID resolve are still real, correctly attributed data,
                  not a reason to call the whole category failed.
      - "failed" both sub-queries failed/timed out -- no people-credit
                 data at all for this film in this run.
    locations and film_awards each stay a single query, so for them "ok"
    and "failed" are the only possible statuses.

    Only locations or film_awards failing outright, or award_people fully
    failing (both sub-queries), raises AssertionError here and fails the
    check. A "partial" award_people is reported plainly in the returned
    status line -- never silently upgraded to "ok" -- but does not by
    itself fail the run, since real partial data was obtained.

    Also times the title->QID (wbsearchentities) lookup separately from
    the four SPARQL queries (reported as ``resolve_qid=``) and the whole
    check end-to-end (``total=``) -- a 2026-10-09 live run passed overall
    but took 16.8s with "multiple timeout/retry events" and no way to
    tell, from the output alone, which part of that was title resolution
    (serial, no retry, see WikidataClient._get_with_deadline) versus the
    concurrent SPARQL batch (each query bounded and retried on its own).
    """
    from app.services.movie_intelligence_client import (
        MovieIntelligenceClient,
        _award_people_nominated_query,
        _award_people_won_query,
        _film_awards_query,
        _location_query,
        award_records,
        location_records,
    )

    def _flatten(rows):
        return [
            {key: value.get("value") for key, value in row.items() if isinstance(value, dict)} for row in rows
        ]

    check_started = time.monotonic()
    client = MovieIntelligenceClient(web=object())
    try:
        # Timed on its own, separately from the four SPARQL subqueries
        # below: the wbsearchentities title->QID lookup runs serially
        # BEFORE any of them can even be built (they all need the QID),
        # and unlike those four it has no retry at all (see
        # WikidataClient._get_with_deadline) -- a slow resolution here
        # adds straight to the total with nothing else running
        # concurrently to hide it. Without this split out, that time was
        # invisible inside whatever gap preceded the four queries' own
        # (correctly instrumented) timings, making it impossible to tell
        # from the smoke output alone how much of a slow total run was
        # title resolution versus the SPARQL batch itself.
        resolve_started = time.monotonic()
        qid = await client._wikidata_id("Inception", 2010)
        resolve_seconds = round(time.monotonic() - resolve_started, 2)
        if not qid:
            raise AssertionError(f"could not resolve Inception to a Wikidata item ({resolve_seconds}s)")

        async def timed(label, query):
            started = time.monotonic()
            try:
                rows = _flatten(await client.wikidata.query_sparql(query, raise_on_failure=True))
                return label, rows, None, round(time.monotonic() - started, 2)
            except Exception as exc:
                return label, None, exc, round(time.monotonic() - started, 2)

        results = await asyncio.gather(
            timed("locations", _location_query(qid)),
            timed("film_awards", _film_awards_query(qid)),
            timed("award_people_won", _award_people_won_query(qid)),
            timed("award_people_nominated", _award_people_nominated_query(qid)),
        )
    finally:
        await client.close()

    by_label = {label: (rows, exc, seconds) for label, rows, exc, seconds in results}
    total_seconds = round(time.monotonic() - check_started, 2)
    timing = (
        f"resolve_qid={resolve_seconds}s, "
        + ", ".join(
            f"{label}={seconds}s/" + (f"{type(exc).__name__}" if exc is not None else f"{len(rows)}row(s)")
            for label, (rows, exc, seconds) in by_label.items()
        )
        + f", total={total_seconds}s"
    )

    hard_failed = {
        label: exc
        for label in ("locations", "film_awards")
        for (_rows, exc, _seconds) in [by_label[label]]
        if exc is not None
    }
    if hard_failed:
        detail = ", ".join(f"{label}: {redact(str(exc))}" for label, exc in hard_failed.items())
        raise AssertionError(f"{qid}: required subquery failed ({timing}) -- {detail}")

    won_rows, won_exc, _ = by_label["award_people_won"]
    nominated_rows, nominated_exc, _ = by_label["award_people_nominated"]
    if won_exc is not None and nominated_exc is not None:
        detail = ", ".join(
            f"{label}: {redact(str(exc))}"
            for label, exc in (("award_people_won", won_exc), ("award_people_nominated", nominated_exc))
        )
        raise AssertionError(f"{qid}: award_people fully failed, both sub-queries ({timing}) -- {detail}")

    for row in won_rows or []:
        row.setdefault("result", "Won")
    for row in nominated_rows or []:
        row.setdefault("result", "Nominated")
    people_rows = (won_rows or []) + (nominated_rows or [])
    if won_exc is None and nominated_exc is None:
        award_people_status = "ok"
    else:
        failed_half = "won" if won_exc is not None else "nominated"
        award_people_status = f"partial (award_people_{failed_half} failed)"

    locations = location_records(by_label["locations"][0])
    awards = award_records(by_label["film_awards"][0], people_rows, "Inception")

    if not locations:
        raise AssertionError(f"{qid}: no location records parsed ({timing})")
    if not awards:
        raise AssertionError(f"{qid}: no award records parsed ({timing})")
    return (
        f"{qid}: {len(locations)} location record(s), {len(awards)} award record(s), "
        f"award_people={award_people_status} [{timing}]"
    )


async def check_musicbrainz() -> str:
    from app.services.musicbrainz_client import soundtrack

    album = await soundtrack("Interstellar", 2014)
    if not album:
        raise AssertionError("no soundtrack release group found for Interstellar (2014)")
    tracks = album.get("tracks") or []
    if not tracks:
        raise AssertionError("soundtrack found but no tracks parsed")
    return f"{album.get('title')!r}: {len(tracks)} track(s)"


CHECKS: dict[str, Callable[[], Awaitable[str]]] = {
    "groq": check_groq,
    "vision": check_vision,
    "gemini": check_gemini,
    "tmdb": check_tmdb,
    "wikidata": check_wikidata,
    "musicbrainz": check_musicbrainz,
}


async def main(selected: list[str]) -> int:
    failures = 0
    for name in selected:
        started = time.monotonic()
        try:
            detail = await asyncio.wait_for(CHECKS[name](), timeout=TIMEOUT * 2)
            status = "PASS"
        except Skip as exc:
            status, detail = "SKIP", str(exc)
        except Exception as exc:  # report every failure kind per provider
            status, detail = "FAIL", f"{type(exc).__name__}: {redact(str(exc))}"
            failures += 1
        print(f"{status:4}  {name:<11} {time.monotonic() - started:5.1f}s  {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", help="comma-separated subset of: " + ", ".join(CHECKS))
    args = parser.parse_args()
    names = [n.strip() for n in args.only.split(",")] if args.only else list(CHECKS)
    unknown = [n for n in names if n not in CHECKS]
    if unknown:
        parser.error(f"unknown check(s): {', '.join(unknown)}")
    sys.exit(asyncio.run(main(names)))
