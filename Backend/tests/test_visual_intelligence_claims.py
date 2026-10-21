"""Visual intelligence keeps documented / observed / inferred / interpreted apart
and never presents sampled stills as a full-film analysis."""

import json

import pytest

from app.inference.models import InferenceResponse
from app.services import visual_intelligence as visual


def movie_with_backdrops(count):
    return {
        "title": "Rain City",
        "images": {
            "backdrops": [
                {
                    "file_path": f"/b{i}.jpg",
                    "iso_639_1": None if i % 2 == 0 else "en",
                    "vote_average": 5 + i,
                }
                for i in range(count)
            ]
        },
    }


def test_select_stills_prefers_textless_backdrops_and_caps_count():
    stills = visual.select_stills(movie_with_backdrops(20))
    assert len(stills) == visual.MAX_STILLS
    assert all(s["url"].startswith("https://image.tmdb.org/") for s in stills)
    assert stills[0]["url"].endswith("/b18.jpg")  # highest-rated textless still first


class VisionProvider:
    def __init__(self):
        self.calls = []

    async def generate(self, request):
        images = [p for p in request.messages[0]["content"] if p["type"] == "image_url"]
        self.calls.append(len(images))
        offset = int(request.messages[0]["content"][0]["text"].split("numbered from ")[1].split(".")[0])
        return InferenceResponse(
            text=json.dumps(
                {
                    "observations": [
                        {
                            "image": offset,
                            "aspect": "lighting",
                            "kind": "observed",
                            "text": "Low-key lighting.",
                        },
                        {
                            "image": offset + 1,
                            "aspect": "composition",
                            "kind": "inferred",
                            "text": "The framing appears to isolate the figure.",
                        },
                        {
                            "image": 99,
                            "aspect": "palette",
                            "kind": "observed",
                            "text": "Hallucinated image.",
                        },
                    ]
                }
            )
        )


class Router:
    def __init__(self, vision):
        self._vision = vision

    def vision(self):
        return self._vision


@pytest.mark.asyncio
async def test_observations_are_tied_to_shown_images_and_batched_by_provider_limit():
    provider = VisionProvider()
    stills = visual.select_stills(movie_with_backdrops(10))[:5]
    observations = await visual.observe_stills(Router(provider), "Rain City", stills)

    assert provider.calls == [3, 2]
    assert all(0 <= o["image_index"] < 5 for o in observations)
    assert not any("Hallucinated" in o["text"] for o in observations)
    assert {o["type"] for o in observations} == {"observed", "inferred"}

    coverage = visual.coverage_for(stills, observations)
    assert coverage["scope"] == "partial" and coverage["frames_from_film"] is False
    assert "not a frame-by-frame analysis" in coverage["statement"]

    rendered = visual.render_markdown(observations, coverage)
    assert rendered.index("Observed in the stills") < rendered.index("Inferred from what is visible")


def test_confidence_requires_authoritative_documentation_and_image_coverage():
    evidence = [
        {"url": "https://ascmag.example/a", "source_domain": "variety.com"},
        {"url": "https://x.example", "source_domain": "theguardian.com"},
    ]
    claims = [
        {"type": "documented", "source_index": 0},
        {"type": "documented", "source_index": 1},
    ]
    assert visual.confidence_for(claims, {"images_analyzed": 4}, evidence) == "high"
    assert visual.confidence_for(claims[:1], {"images_analyzed": 0}, evidence) == "medium"
    assert visual.confidence_for([], {"images_analyzed": 1}, evidence) == "low"
