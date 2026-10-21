from __future__ import annotations

import asyncio
import re

from app.agent.budget import budgeted_leaf
from app.observability import logger
from app.services.async_cache import loop_scoped_alru_cache

DEFAULT_LANGUAGES = ("Telugu", "Hindi", "Tamil", "Malayalam")
REQUEST_TIMEOUT_SECONDS = 8


class YouTubeMusicClient:
    _SINGER_PATTERN = re.compile(
        r"(?:singer|vocals?|sung\s+by)\s*:\s*"
        r"(.+?)(?=\s*(?:[|;\n]|(?:\b(?:music|lyrics|composed|produced)\s+by\b)|$))",
        re.IGNORECASE,
    )

    def __init__(self, ytmusic=None):
        self._ytmusic = ytmusic

    async def _client(self):
        if self._ytmusic is None:
            from ytmusicapi import YTMusic

            self._ytmusic = await asyncio.wait_for(
                asyncio.to_thread(YTMusic),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        return self._ytmusic

    @classmethod
    def _extract_singers(cls, *values: object) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            for match in cls._SINGER_PATTERN.finditer(value):
                raw_names = re.split(r"\s*(?:,|&|\band\b)\s*", match.group(1))
                for raw_name in raw_names:
                    name = re.sub(r"\s+", " ", raw_name).strip(" .:-")
                    key = name.casefold()
                    if name and key not in seen:
                        seen.add(key)
                        names.append(name)
        return names

    async def get_official_soundtracks(
        self,
        movie_title: str,
        music_director: str,
        languages: list[str] | tuple[str, ...] = DEFAULT_LANGUAGES,
    ) -> dict[str, list[dict[str, object]]]:
        return await self._get_official_soundtracks_cached(
            movie_title,
            music_director,
            tuple(languages),
        )

    @loop_scoped_alru_cache(maxsize=100, ttl=300)
    async def _get_official_soundtracks_cached(
        self,
        movie_title: str,
        music_director: str,
        languages: tuple[str, ...],
    ) -> dict[str, list[dict[str, object]]]:
        soundtracks = {language: [] for language in dict.fromkeys(languages)}
        if not movie_title.strip() or not music_director.strip():
            return soundtracks

        try:
            ytmusic = await self._client()
        except Exception as exc:
            logger.warning("youtube_music_client_initialization_failed error=%r", exc)
            return soundtracks

        for language in soundtracks:
            query = f"{movie_title} {language} {music_director}"
            try:
                async with budgeted_leaf():
                    albums = await asyncio.wait_for(
                        asyncio.to_thread(ytmusic.search, query=query, filter="albums"),
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                album = next(
                    (
                        item
                        for item in albums or []
                        if isinstance(item, dict) and isinstance(item.get("browseId"), str)
                    ),
                    None,
                )
                if album is None:
                    continue

                async with budgeted_leaf():
                    album_details = await asyncio.wait_for(
                        asyncio.to_thread(ytmusic.get_album, album["browseId"]),
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                tracks = album_details.get("tracks", []) if isinstance(album_details, dict) else []
                for track in tracks or []:
                    if not isinstance(track, dict):
                        continue
                    track_name = track.get("title")
                    if not isinstance(track_name, str) or not track_name.strip():
                        continue
                    artists = track.get("artists") or []
                    singers = [
                        artist["name"].strip()
                        for artist in artists
                        if isinstance(artist, dict)
                        and isinstance(artist.get("name"), str)
                        and artist["name"].strip()
                    ]
                    for singer in self._extract_singers(
                        track_name,
                        track.get("description"),
                    ):
                        if singer.casefold() not in {name.casefold() for name in singers}:
                            singers.append(singer)
                    soundtracks[language].append({"track_name": track_name.strip(), "singers": singers})
            except TimeoutError:
                logger.warning(
                    "youtube_music_request_timed_out movie=%r language=%r",
                    movie_title,
                    language,
                )
            except Exception as exc:
                logger.warning(
                    "youtube_music_lookup_failed movie=%r language=%r error=%r",
                    movie_title,
                    language,
                    exc,
                )

        return soundtracks


async def get_official_soundtracks(
    movie_title: str,
    music_director: str,
    languages: list[str] | tuple[str, ...] = DEFAULT_LANGUAGES,
) -> dict[str, list[dict[str, object]]]:
    return await _get_official_soundtracks_cached(
        movie_title,
        music_director,
        tuple(languages),
    )


@loop_scoped_alru_cache(maxsize=100, ttl=300)
async def _get_official_soundtracks_cached(
    movie_title: str,
    music_director: str,
    languages: tuple[str, ...],
) -> dict[str, list[dict[str, object]]]:
    return await YouTubeMusicClient().get_official_soundtracks(
        movie_title,
        music_director,
        languages,
    )
