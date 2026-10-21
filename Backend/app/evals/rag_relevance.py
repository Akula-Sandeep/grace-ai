"""Retrieval relevance benchmark for the stored research cache (audit RAG-001).

``knowledge_documents`` holds web snippets saved by film research
(``MovieTools._persist_research``) and anything an admin ingests. They are read
back by ``movie_intelligence``, visual-style research and the classic
``rag_search`` tool. This measures whether ``RAGService.search`` puts the
snippet that answers a question at the top, on a small hand-written corpus
(three films, six aspects each, in the style of real research snippets).

It measures retrieval only, with the lexical path (no embedding model is
installed by default). Run against a configured database:
``python -m app.evals.rag_relevance``; the suite runs it in
``tests/test_rag_relevance_benchmark.py``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

CORPUS: dict[str, dict[str, str]] = {
    "Dune": {
        "awards": "Dune won six Academy Awards at the 94th Oscars, including Best Cinematography, Best Film "
        "Editing, Best Original Score, Best Sound, Best Production Design and Best Visual Effects.",
        "locations": "Principal photography took place at Origo Studios in Budapest, with desert exteriors "
        "shot in Wadi Rum in Jordan and the Liwa Oasis near Abu Dhabi.",
        "music": "Hans Zimmer composed the score, using invented instruments and a female vocal lead to give "
        "the planet Arrakis its own sound.",
        "visual": "Cinematographer Greig Fraser shot the film digitally and printed it to film and back, "
        "aiming for a muted, sun-bleached palette and brutalist scale.",
        "reception": "Critics praised the scale and sound design, though several reviewers felt the story "
        "stops abruptly because it covers only the first half of the novel.",
        "budget": "The production cost was reported at around 165 million dollars, and the film grossed "
        "more than 400 million worldwide despite a simultaneous streaming release.",
    },
    "Arrival": {
        "awards": "Arrival received eight Academy Award nominations, including Best Picture, and won the "
        "Oscar for Best Sound Editing.",
        "locations": "The film was shot in and around Montreal, Quebec, over the summer of 2015, with the "
        "alien shell landing site recreated near Saint-Fabien.",
        "music": "Jóhann Jóhannsson wrote the score, and Max Richter's piece On the Nature of Daylight opens "
        "and closes the film.",
        "visual": "Bradford Young's cinematography favours soft natural light, grey overcast tones and "
        "shallow focus to keep the aliens mysterious.",
        "reception": "Reviewers called it an intelligent, emotional science fiction drama and singled out "
        "Amy Adams's lead performance as a linguist.",
        "budget": "Made for about 47 million dollars, it earned roughly 200 million at the worldwide box "
        "office.",
    },
    "Interstellar": {
        "awards": "Interstellar won the Academy Award for Best Visual Effects and was nominated for its "
        "score, sound mixing, sound editing and production design.",
        "locations": "Farm scenes were filmed in Alberta, Canada, while the icy planet sequences were shot "
        "on the Svínafellsjökull glacier in Iceland.",
        "music": "Hans Zimmer's score is built around a church pipe organ recorded at the Temple Church in "
        "London.",
        "visual": "Hoyte van Hoytema shot much of it on IMAX 65mm film, and the black hole Gargantua was "
        "rendered with physicist Kip Thorne's equations.",
        "reception": "Critics admired its ambition and visuals, while some found the dialogue heavy with "
        "exposition and the ending sentimental.",
        "budget": "The budget was about 165 million dollars, and it grossed over 700 million worldwide.",
    },
}

# (question, film it is about, aspect that answers it)
QUESTIONS = [
    ("which Oscars did Dune win?", "Dune", "awards"),
    ("where was Dune filmed?", "Dune", "locations"),
    ("who composed the Dune soundtrack?", "Dune", "music"),
    ("what does the cinematography of Dune look like?", "Dune", "visual"),
    ("how did critics react to Dune?", "Dune", "reception"),
    ("how much did Dune cost to make?", "Dune", "budget"),
    ("did Arrival win any Academy Awards?", "Arrival", "awards"),
    ("where was Arrival shot?", "Arrival", "locations"),
    ("what music plays at the start of Arrival?", "Arrival", "music"),
    ("why does Arrival look so grey and soft?", "Arrival", "visual"),
    ("was Arrival well reviewed?", "Arrival", "reception"),
    ("Arrival box office", "Arrival", "budget"),
    ("Interstellar awards", "Interstellar", "awards"),
    ("filming locations for Interstellar", "Interstellar", "locations"),
    ("what instrument is in the Interstellar score?", "Interstellar", "music"),
    ("how was the black hole in Interstellar made?", "Interstellar", "visual"),
    ("what were the main criticisms of Interstellar?", "Interstellar", "reception"),
    ("Interstellar production budget", "Interstellar", "budget"),
]

# Written before the retrieval changes made after the first run, and never tuned
# against: a check that changes generalise beyond QUESTIONS.
HELD_OUT = [
    ("Dune Oscar wins", "Dune", "awards"),
    ("what desert did they use for Dune?", "Dune", "locations"),
    ("Dune composer", "Dune", "music"),
    ("Dune reviews", "Dune", "reception"),
    ("Arrival soundtrack", "Arrival", "music"),
    ("how much money did Arrival make?", "Arrival", "budget"),
    ("Arrival lighting and colour", "Arrival", "visual"),
    ("Interstellar critics", "Interstellar", "reception"),
    ("was Interstellar filmed on IMAX?", "Interstellar", "visual"),
    ("where did they film the ice planet in Interstellar?", "Interstellar", "locations"),
]


def _seed(db) -> dict[str, int]:
    from app.database import Movie
    from app.rag import RAGService

    ids: dict[str, int] = {}
    tmdb_ids = {"Dune": 438631, "Arrival": 329865, "Interstellar": 157336}
    for title, aspects in CORPUS.items():
        movie = db.query(Movie).filter(Movie.tmdb_id == tmdb_ids[title]).one_or_none()
        if movie is None:
            movie = Movie(tmdb_id=tmdb_ids[title], title=title)
            db.add(movie)
            db.flush()
        ids[title] = movie.id
        RAGService(db).ingest_many(
            [
                {
                    "movie_id": movie.id,
                    "text": text,
                    "source": f"https://example.test/{title.lower()}/{aspect}",
                    "source_type": "web",
                    "document_type": aspect,
                    "url": f"https://example.test/{title.lower()}/{aspect}",
                    "metadata_json": {"title": title, "aspect": aspect},
                }
                for aspect, text in aspects.items()
            ],
            embed=False,
        )
    return ids


def run(db) -> dict[str, Any]:
    from app.rag import RAGService

    ids = _seed(db)
    service = RAGService(db)
    rows: list[dict[str, Any]] = []
    for question, title, aspect, held_out in [
        *((*q, False) for q in QUESTIONS),
        *((*q, True) for q in HELD_OUT),
    ]:
        expected = CORPUS[title][aspect]
        for scope, movie_id in (("film", ids[title]), ("all films", None)):
            results = service.search(question, movie_id, limit=3)
            texts = [item["text"] for item in results]
            rank = texts.index(expected) + 1 if expected in texts else None
            rows.append(
                {
                    "question": question,
                    "set": "held out" if held_out else "tuning",
                    "scope": scope,
                    "rank": rank,
                    "top": (
                        (results[0]["metadata"].get("title"), results[0]["metadata"].get("aspect"))
                        if results
                        else None
                    ),
                }
            )
    summary: dict[str, dict[str, float]] = {}
    for name, scope in (
        ("tuning, one film", "film"),
        ("tuning, all films", "all films"),
        ("held out, one film", "film"),
        ("held out, all films", "all films"),
    ):
        held = name.startswith("held")
        scoped = [r for r in rows if r["scope"] == scope and (r["set"] == "held out") == held]
        summary[name] = {
            "questions": len(scoped),
            "hit_at_1": sum(r["rank"] == 1 for r in scoped),
            "hit_at_3": sum(r["rank"] is not None for r in scoped),
            "mrr": round(sum(1 / r["rank"] for r in scoped if r["rank"]) / len(scoped), 3),
        }
    return {"documents": sum(len(a) for a in CORPUS.values()), "summary": summary, "rows": rows}


def main() -> int:
    from app.database import SessionLocal

    with SessionLocal() as db:
        report = run(db)
        db.rollback()
    sys.stdout.write(json.dumps(report["summary"], indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
