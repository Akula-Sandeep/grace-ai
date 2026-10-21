"""The architecture rule, enforced: the model decides, code executes.

Owner, 2026-10-16: "don't drift away from the goal again." Rounds 1-7 grew a regex
understanding layer, an intent router and answer rewriting around the model, one
special case per live failure. These checks fail when that pattern returns, so the
fix for a bad answer goes into the prompt or a tool, not into a new rule.

See Backend/AGENT_RUNTIME.md and the project's GRACE_TARGET_AGENT_PLAN.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1] / "app" / "agent"

# The turn path: it routes and executes; it never reads meaning out of text.
TURN_PATH = ("dispatcher.py", "model_turn.py", "actions.py", "agent.py")

# Rule layers that were removed in round 8b and must not come back under these names.
REMOVED = (
    "understanding.py",  # regex intent parser in front of the model
    "guard.py",  # regex rewriting of model answers
    "spoiler_policy.py",  # regex deciding answer depth
    "place_check.py",  # special-case checker for trip plans
    "personal.py",  # regex taste extraction
    "evidence_digest.py",  # code-written answers
    "runtime_guard.py",  # regex filtering of answer lines
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_turn_path_never_pattern_matches_text():
    for name in TURN_PATH:
        assert "re" not in _imports(AGENT / name), (
            f"app/agent/{name} imports re: interpreting text belongs to the model "
            "(fix the prompt or a tool instead of adding a rule)"
        )


def test_removed_rule_layers_stay_removed():
    back = [name for name in REMOVED if (AGENT / name).exists()]
    assert back == [], f"rule layers are back: {back}"


def test_there_is_one_agent_path():
    from app.config import settings

    assert not hasattr(settings, "agent_mode"), "a second (rule-based) agent mode is back"
    source = (AGENT / "dispatcher.py").read_text(encoding="utf-8")
    for marker in ("route_user_query", "_dispatch_and_stream_impl", "_regex_fallback", "classification="):
        assert marker not in source, f"dispatcher routes by intent again ({marker})"


def test_model_answers_are_never_rewritten_in_code():
    for name in ("model_turn.py", "dispatcher.py", "agent.py"):
        source = (AGENT / name).read_text(encoding="utf-8")
        for marker in ("OutputPolicy", "guard_text", "_clean_user_answer", "enforce_runtime_limit"):
            assert marker not in source, f"app/agent/{name} rewrites model text ({marker})"


def test_chat_turns_have_one_model_and_no_stand_in_writer():
    """Owner, 2026-10-16: wait for the strong model; Gemini is for web search only."""
    from app.config import settings
    from app.inference.router import InferenceRouter
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    assert InferenceRouter().deep().models == [settings.groq_deep_model]
    assert not hasattr(MovieDataFallbackEngine, "write_up"), "another model writes chat answers again"
    source = (AGENT / "dispatcher.py").read_text(encoding="utf-8")
    assert "_written_by_next_model" not in source


def test_taste_is_never_parsed_from_text_in_code():
    """Tastes are the model's reading (remember_taste, memory_extraction), not keyword rules."""
    services = AGENT.parent / "services"
    assert "re" not in _imports(services / "preference_learning.py")
    source = (AGENT.parent / "memory.py").read_text(encoding="utf-8")
    assert "apply_explicit_preferences" not in source and "apply_aspect_feedback" not in source
