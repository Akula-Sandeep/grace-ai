"""Soundtrack version classification (product spec §11.4, UI spec §57).

Distinguishes a language version of the same composition from a reprise, a
remix, a promotional track and an independent soundtrack track, and groups
versions of one composition only when the titles themselves support it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_KIND_PATTERNS = (
    ("reprise", re.compile(r"\breprise\b", re.IGNORECASE)),
    ("remix", re.compile(r"\b(?:remix|re-?mix|mix by|club mix|lofi|lo-fi|mashup)\b", re.IGNORECASE)),
    (
        "promotional",
        re.compile(r"\b(?:promo(?:tional)?|teaser|lyric video|title song promo)\b", re.IGNORECASE),
    ),
    ("instrumental", re.compile(r"\b(?:instrumental|karaoke|bgm|theme)\b", re.IGNORECASE)),
    (
        "language_version",
        re.compile(
            r"\b(?:dubbed|(?:hindi|tamil|telugu|malayalam|kannada|english|spanish|french|japanese|"
            r"korean|bengali|marathi|punjabi|german|italian|portuguese)\s+version)\b",
            re.IGNORECASE,
        ),
    ),
)

_DECORATION = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*"  # bracketed qualifiers: (Reprise), [From "X"], (Hindi Version)
    r"|\s+[-–|]\s+.*$"  # trailing " - Remix", " | From Movie"
    r"|\bfeat\.?.*$|\bft\.?.*$",
    re.IGNORECASE,
)


def version_kind(title: str) -> str:
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(title or ""):
            return kind
    return "soundtrack"


def composition_key(title: str) -> str:
    """Normalised base title used to recognise versions of the same composition."""
    base = _DECORATION.sub(" ", title or "")
    base = unicodedata.normalize("NFKD", base)
    base = "".join(ch for ch in base if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", base.casefold()).strip()


def group_versions(tracks_by_language: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Group tracks into compositions with their versions.

    A track is grouped with others only when its normalised base title matches;
    different titles in different languages are kept as separate compositions
    because the catalogue gives no evidence they are the same song.
    """
    compositions: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for language, tracks in tracks_by_language.items():
        for track in tracks:
            title = str(track.get("title") or "").strip()
            if not title:
                continue
            key = composition_key(title) or title.casefold()
            if key not in compositions:
                compositions[key] = {"composition": title, "versions": []}
                order.append(key)
            compositions[key]["versions"].append(
                {
                    "title": title,
                    "language": language,
                    "kind": version_kind(title),
                    "singers": list(track.get("singers") or []),
                }
            )
    out = []
    for key in order:
        group = compositions[key]
        versions = group["versions"]
        originals = [v for v in versions if v["kind"] == "soundtrack"]
        if originals:
            group["composition"] = originals[0]["title"]
        languages = sorted({v["language"] for v in versions})
        # A soundtrack-kind track in a second language is a language version of the same composition.
        seen_original = False
        for version in versions:
            if version["kind"] == "soundtrack":
                if seen_original and len(languages) > 1:
                    version["kind"] = "language_version"
                seen_original = True
        group["languages"] = languages
        group["grouping"] = "matched_titles" if len(versions) > 1 else "single"
        out.append(group)
    return out
