"""Memory recall benchmark (audit MEM-001): does a stated fact reach the model when it matters?

Ten facts a user stated are stored among fifty unrelated memories (as the
post-turn extractor would store them). Twelve later questions refer to them
with shared keywords, paraphrases, or in another language. Two things are
measured on the real database:

* ``search_hit``: the fact is in ``MemoryService.memories(uid, question, 4)``
  (any-word full-text ranking, or vectors when embeddings are installed);
* ``in_prompt``: the fact is in what a model-led turn actually sends
  (``MemoryService.prompt_memories``: the most relevant, then standing memories,
  within a size budget), so the multilingual model can apply it;
* ``recall_hit``: the fact is found by ``recall_conversation`` given the
  keywords listed with each question, standing in for the query the model
  writes when told that older memories were left out. Whether a real model
  writes such keywords is model behaviour and is only checked live;
* ``reachable``: in the prompt, or found by that recall.

Run against a configured database:  ``python -m app.evals.memory_recall``
(the test suite runs it on PostgreSQL in ``tests/test_memory_recall_benchmark.py``).
"""

from __future__ import annotations

import json
import sys
from typing import Any

FACTS = {
    "grandmother": "Watches films with their grandmother.",
    "dubbed": "Prefers dubbed Hindi versions over subtitles.",
    "club": "Their film club meets on Thursdays and picks Korean films.",
    "jump": "Dislikes jump scares.",
    "spanish": "Is learning Spanish and enjoys Spanish-language films.",
    "name": "Their name is Priya.",
    "short": "Prefers films under two hours on weeknights.",
    "villeneuve": "Loves Denis Villeneuve's films.",
    "musicals": "Can't stand musicals.",
    "laptop": "Usually watches on a small laptop screen.",
}

DISTRACTORS = [
    f"After watching {title}, said the {aspect} was {verdict}."
    for title, aspect, verdict in [
        ("Interstellar", "soundtrack", "stunning"),
        ("Joker", "ending", "too bleak"),
        ("Se7en", "atmosphere", "great"),
        ("Argo", "pacing", "tight"),
        ("Drive", "music", "hypnotic"),
        ("Sully", "story", "moving"),
        ("Coherence", "twist", "clever"),
        ("Panic Room", "tension", "solid"),
        ("Zodiac", "length", "a bit long"),
        ("The Departed", "cast", "excellent"),
    ]
] + [
    f"Mentioned {topic}."
    for topic in [
        "liking popcorn with butter",
        "a trip to Rome last spring",
        "working night shifts sometimes",
        "a favourite cinema in Mumbai",
        "preferring the cinema over streaming for epics",
        "collecting Blu-rays",
        "a cat named Pixel",
        "rewatching comfort films on Sundays",
        "enjoying behind-the-scenes documentaries",
        "disliking shaky camera work",
        "liking films with strong female leads",
        "an interest in film scores",
        "a dislike of long recaps",
        "watching trailers before deciding",
        "liking heist films",
        "a soft spot for 80s films",
        "following award seasons",
        "liking slow cinema occasionally",
        "watching with friends on Fridays",
        "preferring colour over black and white",
        "an interest in animation techniques",
        "liking courtroom dramas",
        "skipping post-credit scenes",
        "enjoying space films",
        "a preference for practical effects",
        "liking ensemble casts",
        "reading the book before the film",
        "disliking cliffhanger endings",
        "liking Studio Ghibli",
        "a fondness for noir lighting",
        "watching documentaries about chefs",
        "liking films set in Japan",
        "enjoying mystery boxes",
        "preferring original versions of remakes",
        "liking quiet character studies",
        "enjoying sports films",
        "liking western landscapes",
        "a preference for short films at festivals",
        "liking satirical comedies",
        "enjoying films about music",
    ]
]

# (question, fact, kind, keywords a model would pass to recall_conversation)
QUESTIONS = [
    ("what should we watch with my grandmother tonight?", "grandmother", "keyword", "grandmother gran"),
    ("something my gran would enjoy", "grandmother", "paraphrase", "gran grandmother grandma"),
    ("should I pick the subtitled or the dubbed version?", "dubbed", "keyword", "dubbed subtitles"),
    ("which audio language do I usually want?", "dubbed", "paraphrase", "dubbed subtitles audio language"),
    ("what day does my film club meet?", "club", "keyword", "film club meets"),
    ("something scary but not too jumpy", "jump", "paraphrase", "jump scares scary horror"),
    ("¿qué película en español me recomiendas?", "spanish", "other language", "Spanish español learning"),
    ("दादी के साथ क्या देखें?", "grandmother", "other language", "grandmother दादी grandma"),
    ("do you remember my name?", "name", "keyword", "name"),
    ("something short for a Tuesday night", "short", "paraphrase", "short hours weeknights runtime"),
    ("more from the director of Dune", "villeneuve", "needs knowledge", "Villeneuve director Dune"),
    ("no singing and dancing please", "musicals", "paraphrase", "musicals singing dancing"),
]


def run(db, user_id: int, *, distractors: int = len(DISTRACTORS)) -> dict[str, Any]:
    """``distractors`` unrelated memories are stored around the ten facts (half before, half after)."""
    from app.memory import MemoryService

    service = MemoryService(db)
    noise = DISTRACTORS[:distractors]
    half = len(noise) // 2
    for content in [*noise[:half], *FACTS.values(), *noise[half:]]:
        service.save_memory(user_id, content, "fact", 0.7)
    rows: list[dict[str, Any]] = []
    for question, key, kind, keywords in QUESTIONS:
        fact = FACTS[key]
        relevant = [m.content for m in service.memories(user_id, question, 4, touch=False)]
        prompt = service.prompt_memories(user_id, question)
        recalled = [m.content for m in service.memories(user_id, keywords, 6, touch=False)]
        rows.append(
            {
                "question": question,
                "kind": kind,
                "fact": fact,
                "search_hit": fact in relevant,
                "in_prompt": fact in prompt,
                "recall_hit": fact in recalled,
                "reachable": fact in prompt or fact in recalled,
            }
        )
    by_kind: dict[str, dict[str, int]] = {}
    measures = ("search_hit", "in_prompt", "recall_hit", "reachable")
    for row in rows:
        bucket = by_kind.setdefault(row["kind"], {"cases": 0, **dict.fromkeys(measures, 0)})
        bucket["cases"] += 1
        for measure in measures:
            bucket[measure] += int(row[measure])
    return {
        "memories_stored": len(FACTS) + len(noise),
        "semantic_search": service._embedding("probe", load=False) is not None,
        "by_kind": by_kind,
        "rows": rows,
    }


def main() -> int:
    from app.database import SessionLocal, User

    with SessionLocal() as db:
        user = User(username="memory-recall-benchmark", password_hash="!")  # noqa: S106 - no login
        db.add(user)
        db.flush()
        report = run(db, user.id)
        db.rollback()
    sys.stdout.write(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
