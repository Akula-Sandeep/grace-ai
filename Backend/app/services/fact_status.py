"""Fact status classification and conflict detection (product spec §9.2, §19).

Every important claim is classified as one of::

    verified · strongly_supported · reported · uncertain · conflicting · unavailable

The rules are deliberately conservative and deterministic: a claim never
becomes "verified" because a model sounds confident, only because a primary
source (official award body, studio, festival) or an authoritative structured
record states it. Disagreement between sources is preserved and surfaced
rather than resolved silently.
"""

from __future__ import annotations

import re
from typing import Any

from app.evidence import source_host, source_tier

STATUSES = ("verified", "strongly_supported", "reported", "uncertain", "conflicting", "unavailable")

STATUS_LABELS = {
    "verified": "Verified",
    "strongly_supported": "Strongly supported",
    "reported": "Reported",
    "uncertain": "Limited evidence",
    "conflicting": "Sources conflict",
    "unavailable": "Not available",
}

# How an answer should phrase a claim with each status.
CONFIDENCE_LANGUAGE = {
    "verified": "state it plainly and name the primary source",
    "strongly_supported": "state it directly; it is supported by several independent, reputable sources",
    "reported": "attribute it (for example 'according to <source>' or 'reportedly'); do not call it confirmed",
    "uncertain": "say the evidence is limited or unclear and avoid precise figures",
    "conflicting": "say sources disagree, give each version with its source, and do not pick one silently",
    "unavailable": "say this could not be established from the available sources",
}

_HEDGES = re.compile(
    r"\b(?:reportedly|rumou?red|allegedly|unconfirmed|it is said|supposedly|speculat\w*|"
    r"may have|might have|is believed|claims? that|according to (?:some|rumou?rs))\b",
    re.IGNORECASE,
)

_NUMBER = r"\$?\s?(\d+(?:[.,]\d+)?)\s*(million|billion|m|bn)?"
_NUMERIC_TOPICS: dict[str, re.Pattern[str]] = {
    "budget": re.compile(r"\bbudget(?:ed)?\b[^.$\d]{0,30}" + _NUMBER, re.IGNORECASE),
    "box office": re.compile(
        r"\b(?:grossed|box[- ]office (?:of|total of)?|earned)\b[^.$\d]{0,30}" + _NUMBER, re.IGNORECASE
    ),
    "shooting days": re.compile(r"\b(?:shot|filmed) in (\d+) days\b", re.IGNORECASE),
    "runtime": re.compile(r"\b(?:runs?|runtime of|running time of)\s+(\d{2,3})\s*minutes\b", re.IGNORECASE),
    "release year": re.compile(r"\breleased\b[^.\d]{0,30}\b(19\d{2}|20\d{2})\b", re.IGNORECASE),
    "Oscar wins": re.compile(r"\bwon (\d{1,2}) (?:academy awards|oscars)\b", re.IGNORECASE),
}

# Categorical claims: two sources conflict when each names something and the
# named sets do not overlap at all (one naming more places is not a conflict).
_PLACE = r"([A-Z][\w'’\-]+(?:[ -](?:[A-Z][\w'’\-]+|de|del|la|of))*(?:, [A-Z][\w'’\-]+(?: [A-Z][\w'’\-]+)*)?)"
_CATEGORICAL_TOPICS: dict[str, re.Pattern[str]] = {
    "filming location": re.compile(
        rf"\b(?:was |were )?(?:filmed|shot|photographed)(?: on location| mainly| mostly| largely| primarily)? (?:in|at) {_PLACE}"
    ),
    "director": re.compile(r"\bdirected by ([A-Z][\w.'’\-]+(?: [A-Z][\w.'’\-]+){0,3})"),
    "composer": re.compile(
        r"\b(?:score|music) (?:was )?(?:composed|written) by ([A-Z][\w.'’\-]+(?: [A-Z][\w.'’\-]+){0,3})"
    ),
}


def _text(item: dict[str, Any]) -> str:
    return str(item.get("excerpt") or item.get("text") or item.get("snippet") or "")


def _host(item: dict[str, Any]) -> str:
    return source_host(item.get("domain") or item.get("source_domain") or item.get("url"))


def _tier(item: dict[str, Any]) -> int:
    return source_tier(item.get("domain") or item.get("source_domain") or item.get("url"))


def classify(
    sources: list[dict[str, Any]],
    *,
    structured: bool = False,
    structured_primary: bool = False,
    hedged: bool = False,
    conflicting: bool = False,
) -> str:
    """Return the status of one claim from the sources that state it."""
    if conflicting:
        return "conflicting"
    hosts = {_host(source) for source in sources if _host(source)}
    tiers = [_tier(source) for source in sources]
    if structured and structured_primary:
        return "verified"
    if not sources and not structured:
        return "unavailable"
    if hedged:
        return "uncertain"
    if any(tier >= 3 for tier in tiers):
        return "verified"
    strong = sum(1 for tier in tiers if tier >= 2)
    if structured or (len(hosts) >= 2 and strong >= 1) or len(hosts) >= 3:
        return "strongly_supported"
    if hosts:
        return "reported"
    return "uncertain"


def _scale(value: str, unit: str | None) -> float:
    number = float(value.replace(",", ""))
    unit = (unit or "").casefold()
    if unit in {"billion", "bn"}:
        return number * 1000
    return number


def detect_conflicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find numeric claims (budget, box office, runtime, shoot length) that disagree across publishers."""
    conflicts = []
    for topic, pattern in _NUMERIC_TOPICS.items():
        values: dict[float, dict[str, Any]] = {}
        for item in items:
            match = pattern.search(_text(item))
            if not match:
                continue
            groups = match.groups()
            amount = _scale(groups[0], groups[1] if len(groups) > 1 else None)
            host = _host(item)
            if amount not in values:
                values[amount] = {"value": match.group(0).strip(), "amount": amount, "sources": []}
            if host and host not in values[amount]["sources"]:
                values[amount]["sources"].append(host)
        amounts = sorted(values)
        tolerance = 1.0 if topic in {"release year", "Oscar wins"} else 1.1
        if len(amounts) >= 2 and (
            amounts[-1] > amounts[0] * tolerance if tolerance > 1 else amounts[-1] != amounts[0]
        ):
            conflicts.append(
                {
                    "topic": topic,
                    "values": [
                        {"claim": values[a]["value"], "sources": values[a]["sources"]} for a in amounts
                    ],
                }
            )
    conflicts.extend(_categorical_conflicts(items))
    return conflicts


def _categorical_conflicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts = []
    for topic, pattern in _CATEGORICAL_TOPICS.items():
        by_host: dict[str, set[str]] = {}
        for item in items:
            host = _host(item) or "unattributed"
            for match in pattern.finditer(_text(item)):
                value = match.group(1).strip(" ,.")
                if value:
                    by_host.setdefault(host, set()).add(value)
        hosts = list(by_host)
        disagreeing: dict[str, set[str]] = {}
        for index, host in enumerate(hosts):
            for other in hosts[index + 1 :]:
                mine = {v.casefold() for v in by_host[host]}
                theirs = {v.casefold() for v in by_host[other]}
                overlap = any(a in b or b in a for a in mine for b in theirs)
                if not overlap:
                    disagreeing.setdefault(host, by_host[host])
                    disagreeing.setdefault(other, by_host[other])
        if disagreeing:
            grouped: dict[str, list[str]] = {}
            for host, values in disagreeing.items():
                grouped.setdefault(", ".join(sorted(values)), []).append(host)
            conflicts.append(
                {
                    "topic": topic,
                    "values": [
                        {"claim": claim, "sources": sorted(hosts_)} for claim, hosts_ in grouped.items()
                    ],
                }
            )
    return conflicts


def _tokens(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]{4,}", text.casefold())}


def cluster_claims(items: list[dict[str, Any]], threshold: float = 0.45) -> list[list[dict[str, Any]]]:
    """Group excerpts that state the same thing so independent support can be counted."""
    clusters: list[tuple[set[str], list[dict[str, Any]]]] = []
    for item in items:
        tokens = _tokens(_text(item))
        if not tokens:
            continue
        for cluster_tokens, members in clusters:
            overlap = len(tokens & cluster_tokens) / max(1, len(tokens | cluster_tokens))
            if overlap >= threshold:
                members.append(item)
                cluster_tokens |= tokens
                break
        else:
            clusters.append((set(tokens), [item]))
    return [members for _tokens_, members in clusters]


def classify_facts(sources: list[dict[str, Any]], limit: int = 8) -> dict[str, Any]:
    """Turn source excerpts into status-labelled facts plus surfaced conflicts."""
    conflicts = detect_conflicts(sources)
    conflicting_hosts = {
        host for conflict in conflicts for value in conflict["values"] for host in value["sources"]
    }
    facts = []
    for members in cluster_claims([s for s in sources if _text(s)]):
        lead = max(members, key=_tier)
        member_hosts = {_host(m) for m in members}
        in_conflict = bool(conflicting_hosts & member_hosts) and any(
            pattern.search(_text(lead))
            for pattern in (*_NUMERIC_TOPICS.values(), *_CATEGORICAL_TOPICS.values())
        )
        status = classify(members, hedged=bool(_HEDGES.search(_text(lead))), conflicting=in_conflict)
        facts.append(
            {
                "text": _text(lead),
                "status": status,
                "status_label": STATUS_LABELS[status],
                "source": lead,
                "supporting_sources": sorted(h for h in member_hosts if h),
            }
        )
    order = {status: index for index, status in enumerate(STATUSES)}
    facts.sort(key=lambda fact: order[str(fact["status"])])
    return {"facts": facts[:limit], "conflicts": conflicts}


def assess_evidence(
    sources: list[dict[str, Any]], texts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Overall status of the evidence behind one chat answer, with phrasing guidance."""
    conflicts = detect_conflicts(texts or sources)
    status = classify(sources, conflicting=bool(conflicts))
    return {
        "status": status,
        "label": STATUS_LABELS[status],
        "independent_sources": len({_host(s) for s in sources if _host(s)}),
        "primary_sources": sum(1 for s in sources if _tier(s) >= 3),
        "conflicts": conflicts,
        "guidance": CONFIDENCE_LANGUAGE[status],
    }


def evidence_texts(evidence: Any, limit: int = 40) -> list[dict[str, Any]]:
    """Flatten excerpt-bearing items (url + text/snippet) from an evidence payload."""
    found: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6 or len(found) >= limit:
            return
        if isinstance(node, dict):
            if isinstance(node.get("url"), str) and (node.get("text") or node.get("snippet")):
                found.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(evidence)
    return found
