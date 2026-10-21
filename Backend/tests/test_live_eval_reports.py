"""Report writing and exit status of app/evals/live_agent.py (round 7e).

These are plumbing tests: the model is scripted and the live check is stubbed,
so they say nothing about live quality. They pin what failed on 2026-10-13: a
provider error stopped the run before any report existed.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.evals import live_agent
from app.services import diagnostics
from tests.jarvis_harness import install
from tests.model_harness import ScriptedModel, install_model, say

CANBERRA = "The capital of Australia is **Canberra**."


class Answers(ScriptedModel):
    """Answers G01."""

    async def stream(self, request):
        self.requests.append(request)
        yield CANBERRA


@pytest.fixture
def harness(monkeypatch):
    install(monkeypatch, offline=False)
    model = Answers()
    install_model(monkeypatch, model)
    model.script(*[say(CANBERRA) for _ in range(6)])
    from tests.test_agent_core_units import all_met

    monkeypatch.setattr(live_agent, "_judge", all_met)
    return model, monkeypatch


def _diagnostics(ok: bool):
    failed = [] if ok else ["Tool calling on openai/gpt-oss-20b"]

    async def checks():
        return {
            "ok": ok,
            "failed_required": failed,
            "checks": [
                {
                    "service": "Tool calling on openai/gpt-oss-20b",
                    "ok": ok,
                    "status": "ok" if ok else "failed",
                    "latency_ms": 3,
                    "category": None if ok else "sdk_incompatible",
                    "detail": "ok" if ok else "The installed groq SDK 1.7.0 does not accept: stream_options.",
                    "hint": None,
                },
                {"service": "TMDB search", "ok": True, "status": "ok", "latency_ms": 5, "category": None},
            ],
        }

    return checks


def _saved(out: Path) -> dict:
    files = sorted(out.glob("live-eval-*.json"))
    assert len(files) == 1, list(out.iterdir())
    assert files[0].with_suffix(".md").exists()
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_the_report_is_written_in_the_requested_directory(harness, tmp_path, capsys):
    _, monkeypatch = harness
    monkeypatch.setattr(diagnostics, "run_checks", _diagnostics(ok=True))
    out = tmp_path / "grace-eval" / "model"  # does not exist yet
    status = live_agent.main(["--only", "G01", "--out", str(out)])
    report = _saved(out)
    assert status == 0
    assert report["run_status"] == "complete" and report["code_version"] == live_agent.code_version()
    assert [r["status"] for r in report["results"]] == ["passed"]
    assert report["configuration"]["sdk"]["groq"]
    printed = capsys.readouterr().out
    assert f"Report: {out.resolve()}" in printed and "Exit status 0" in printed


def test_a_failed_provider_check_still_saves_the_report_and_nothing_counts_as_passed(
    harness, tmp_path, capsys
):
    _, monkeypatch = harness
    monkeypatch.setattr(diagnostics, "run_checks", _diagnostics(ok=False))
    status = live_agent.main(["--only", "G01", "--out", str(tmp_path)])
    report = _saved(tmp_path)
    assert status == 2
    assert report["diagnostics"]["failed_required"] == ["Tool calling on openai/gpt-oss-20b"]
    assert [r["status"] for r in report["results"]] == ["unverified"]  # its checks passed, but not as a pass
    assert report["summary"]["scenario_status"]["passed"] == 0
    text = (sorted(tmp_path.glob("*.md"))[0]).read_text(encoding="utf-8")
    assert "**Required checks failed:** Tool calling on openai/gpt-oss-20b" in text
    assert "stream_options" in text
    printed = capsys.readouterr().out
    assert "FAILED (sdk_incompatible" in printed and "Exit status 2" in printed


def test_a_live_check_that_cannot_run_is_a_failed_diagnostic_not_a_crash(harness, tmp_path):
    _, monkeypatch = harness

    async def broken():
        raise TypeError("AsyncCompletions.create() got an unexpected keyword argument 'stream_options'")

    monkeypatch.setattr(diagnostics, "run_checks", broken)
    status = live_agent.main(["--only", "G01", "--out", str(tmp_path)])
    report = _saved(tmp_path)
    assert status == 2
    assert report["diagnostics"]["ok"] is False and "TypeError" in report["diagnostics"]["error"]
    assert report["results"] and report["run_status"] == "complete"


def test_a_scenario_that_cannot_complete_is_recorded_and_the_others_still_run(harness, tmp_path):
    _, monkeypatch = harness
    monkeypatch.setattr(diagnostics, "run_checks", _diagnostics(ok=True))
    real_turn = live_agent._turn
    calls = {"n": 0}

    async def flaky(client, message, conversation_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("no answer within 150 s")
        return await real_turn(client, message, conversation_id)

    monkeypatch.setattr(live_agent, "_turn", flaky)
    status = live_agent.main(["--only", "G01,P01", "--out", str(tmp_path)])
    report = _saved(tmp_path)
    assert status == 1
    first, second = report["results"]
    assert first["status"] == "error" and "ReadTimeout after 0 of 1 turns" in first["harness_error"]
    assert second["id"].startswith("P01") and second["turns"]


def test_a_harness_bug_stops_the_run_but_the_partial_report_is_saved(harness, tmp_path, capsys):
    _, monkeypatch = harness
    monkeypatch.setattr(diagnostics, "run_checks", _diagnostics(ok=True))

    async def bug(index, scenario, origin, **_kwargs):
        raise AssertionError("harness bug")

    monkeypatch.setattr(live_agent, "_run_scenario", bug)
    status = live_agent.main(["--only", "G01", "--out", str(tmp_path)])
    report = _saved(tmp_path)
    assert status == 3
    assert report["run_status"] == "stopped: AssertionError"
    assert report["diagnostics"]["ok"] is True  # what ran before the bug is kept
    printed = capsys.readouterr()
    assert "Exit status 3" in printed.out and "AssertionError: harness bug" in printed.err
