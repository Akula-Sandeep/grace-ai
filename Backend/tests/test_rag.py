import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.rag import RAGService


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/rag_test.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_chunk_splits_long_text_with_overlap():
    text = " ".join(f"word{i}" for i in range(500))
    chunks = RAGService.chunk(text, size=220, overlap=40)
    assert len(chunks) > 1
    # consecutive chunks should share the overlapping tail/head words
    first_tail = chunks[0].split()[-40:]
    second_head = chunks[1].split()[:40]
    assert first_tail == second_head


def test_chunk_returns_single_chunk_for_short_text():
    text = "A short review of a great movie."
    chunks = RAGService.chunk(text, size=220, overlap=40)
    assert chunks == [text]


def test_chunk_handles_overlap_greater_than_or_equal_to_size():
    # overlap >= size would otherwise loop forever (start never advances);
    # the guard clamps it instead.
    text = " ".join(f"word{i}" for i in range(50))
    chunks = RAGService.chunk(text, size=10, overlap=10)
    assert len(chunks) > 0  # completes without hanging


def test_ingest_rejects_empty_text(db):
    with pytest.raises(ValueError):
        RAGService(db).ingest(text="   ", source="test", document_type="article")


def test_ingest_persists_and_returns_a_document(db):
    doc = RAGService(db).ingest(
        text="A great review of a great film.", source="critic", document_type="review"
    )
    assert doc.id is not None
    assert doc.text == "A great review of a great film."
    assert doc.source == "critic"


def test_ingest_chunks_long_text_into_multiple_documents(db):
    text = " ".join(f"word{i}" for i in range(500))
    created = RAGService(db).ingest_many(
        [{"text": text, "source": "critic", "document_type": "review"}], embed=False
    )
    assert len(created) > 1
    assert all(doc.metadata_json["chunk_count"] == len(created) for doc in created)
    assert [doc.metadata_json["chunk_index"] for doc in created] == list(range(len(created)))


def test_ingest_many_skips_blank_documents(db):
    created = RAGService(db).ingest_many(
        [
            {"text": "", "source": "a", "document_type": "article"},
            {"text": "   ", "source": "b", "document_type": "article"},
            {"text": "real content here", "source": "c", "document_type": "article"},
        ],
        embed=False,
    )
    assert len(created) == 1
    assert created[0].source == "c"


def test_search_finds_documents_by_lexical_overlap(db):
    rag = RAGService(db)
    rag.ingest(
        text="Blade Runner 2049 is renowned for its cinematography.",
        source="a",
        document_type="article",
    )
    rag.ingest(
        text="A completely unrelated document about baking bread.",
        source="b",
        document_type="article",
    )

    results = rag.search("cinematography Blade Runner")
    assert len(results) >= 1
    assert "Blade Runner" in results[0]["text"]


def test_search_returns_empty_for_blank_query(db):
    rag = RAGService(db)
    rag.ingest(text="Some content.", source="a", document_type="article")
    assert rag.search("") == []
    assert rag.search("   ") == []


def test_search_scopes_results_to_movie_id_when_given(db):
    rag = RAGService(db)
    rag.ingest(
        text="Movie one has a great soundtrack.",
        source="a",
        document_type="article",
        movie_id=1,
    )
    rag.ingest(
        text="Movie two also has a great soundtrack.",
        source="b",
        document_type="article",
        movie_id=2,
    )

    results = rag.search("great soundtrack", movie_id=1)
    assert all(r["movie_id"] == 1 for r in results)


def test_rerank_prefers_higher_source_quality_at_equal_relevance(db):
    rag = RAGService(db)
    items = [
        {
            "text": "the film explores memory and identity",
            "score": 1.0,
            "source_type": "web",
            "url": "https://blog.example.com/a",
            "published_at": None,
        },
        {
            "text": "the film explores memory and identity",
            "score": 1.0,
            "source_type": "primary",
            "url": "https://primary.example.com/a",
            "published_at": None,
        },
    ]
    ranked = rag._rerank(items, "memory identity", limit=2)
    assert ranked[0]["source_type"] == "primary"


def test_rerank_diversifies_across_hosts(db):
    rag = RAGService(db)
    # Equal base scores and equal query-term overlap for every item, so the
    # only thing that can break ties is the diversity/novelty bonus.
    items = [
        {
            "text": "a review about the film themes",
            "score": 1.0,
            "source_type": "web",
            "url": f"https://host-a.example.com/{i}",
            "published_at": None,
        }
        for i in range(5)
    ] + [
        {
            "text": "a review about the film themes",
            "score": 1.0,
            "source_type": "web",
            "url": "https://host-b.example.com/1",
            "published_at": None,
        }
    ]
    ranked = rag._rerank(items, "film themes", limit=3)
    hosts = {item["url"].split("/")[2] for item in ranked}
    # host-b should be pulled in for diversity rather than three host-a picks
    assert "host-b.example.com" in hosts
