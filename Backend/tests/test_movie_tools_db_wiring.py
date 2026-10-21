"""Regression coverage for a real bug: `/movies/*` routes were constructing
MovieTools() without a db session (via `_movie_tools()`), which silently
disabled local search boosting, movie persistence, RAG-grounded
intelligence/review answers, and review deduplication against persisted
criticism -- all gated behind `self.db is not None` checks in tools.py.
These tests just assert the wiring, not the full tool behavior (that's
covered elsewhere), so a future refactor can't reintroduce the same gap
silently.
"""

from app import main


def test_movie_tools_without_db_arg_has_no_db():
    # Sanity check on the default: callers that don't need persistence
    # (e.g. routes that only reuse the shared tmdb client) get a MovieTools
    # with self.db is None, exactly as before.
    t = main._movie_tools()
    assert t.db is None


def test_movie_tools_with_db_arg_wires_it_through():
    sentinel = object()
    t = main._movie_tools(sentinel)
    assert t.db is sentinel


def test_movie_tools_with_db_factory_wires_it_through():
    t = main._movie_tools(db_factory=main.SessionLocal)
    assert t.db_factory is main.SessionLocal


def test_all_data_routes_pass_a_live_db_session(monkeypatch):
    """Every route that calls a MovieTools method depending on self.db
    (search, details, intelligence, visuals, recommendations, similar,
    reviews, compare-visuals) must give MovieTools database access. Routes
    pass ``db_factory=SessionLocal`` so each synchronous ORM unit runs in its
    own short session on a worker thread instead of holding one session
    across awaits."""
    import ast
    import inspect

    routes_needing_db = {
        "movie_search",
        "movie_details",
        "movie_intelligence",
        "movie_visuals",
        "movie_recommendations",
        "movie_similar",
        "compare_visuals",
        "movie_reviews",
    }

    tree = ast.parse(inspect.getsource(main))
    checked = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in routes_needing_db:
            checked.add(node.name)
            source = ast.get_source_segment(inspect.getsource(main), node) or ""
            assert "_movie_tools()" not in source, (
                f"{node.name} calls _movie_tools() with no db session; "
                "self.db-gated functionality (persistence, RAG grounding, "
                "review dedup) will be silently skipped."
            )
            assert (
                "_movie_tools(db_factory=SessionLocal)" in source
            ), f"{node.name} should pass db_factory=SessionLocal into _movie_tools()."

    assert checked == routes_needing_db, f"missing routes in main.py: {routes_needing_db - checked}"
