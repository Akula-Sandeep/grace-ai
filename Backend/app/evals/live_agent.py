"""Live evaluation of Grace's agent with the real AI provider and movie database.

Run where the app's keys and database are configured, e.g. inside the backend
container of the Compose stack (``scripts/run-live-eval.ps1`` does all of this):

    docker compose exec backend python -m app.evals.live_agent --out /tmp/grace-eval/model
    docker compose cp backend:/tmp/grace-eval ./eval-reports

(or ``python -m app.evals.live_agent`` from Backend/ with the same environment).

It first calls every outside service once (the same live check as Settings →
Help), then drives the real FastAPI app in-process as a guest (no account
needed), turn by turn, using live Groq, TMDB and Gemini. It never runs in CI
and nothing here is mocked. For each turn it records latency, the answer
source, the model, token usage, tool calls, fallbacks and the changes the app
verified. Each finished scenario is then graded by a model judge
(``app/evals/judge.py``, Gemini, never the model that answered): every turn's
criteria met or not, and the rubric scored 0/1/2, each with a reason. Code
checks only what code can know: the turn ended without an error and was not a
canned stand-in. It writes:

* ``live-eval-<time>.json`` - diagnostics, every turn, check, grade and timing;
* ``live-eval-<time>.md`` - the transcript with the judge's grades and reasons.

Both files are created in ``--out`` before anything runs and rewritten after
every scenario, so a failing provider or a crash still leaves the evidence.
Each scenario ends ``passed``, ``failed``, ``blocked`` (it failed while a
required provider check had failed), ``unverified`` (it passed but a required
provider was down, so the answer cannot count as the agent's), ``unjudged``
(the judge could not grade it) or ``error`` (the harness could not run it). Exit status: 0 everything passed;
1 a scenario failed, errored or could not be graded; 2 a required provider check failed; 3 the
run itself could not complete (the partial report is still saved).

Numbers from this script are the only basis for claims about live quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCENARIOS = Path(__file__).with_name("live_scenarios.json")
_DEFAULT_OUT = os.environ.get("GRACE_EVAL_DIR") or str(Path(tempfile.gettempdir()) / "grace-eval-reports")
# Replies that are not an answer: an offline notice, or the movie database's raw facts
# instead of a written answer. A labelled backup-model answer ("…the backup model
# answered") is an answer and is counted under answer_sources, not here.
_CANNED = re.compile(
    r"can.t reach my ai service|couldn.t complete that response|"
    r"ai (?:isn.t available right now|is busy for a moment), so this comes straight from the movie database|"
    r"took longer than expected|getting a lot of requests",
    re.IGNORECASE,
)


def check_turn(turn: dict[str, Any], answer: str, errors: list[str]) -> list[dict[str, Any]]:
    """What code can know about a turn. Whether the reply is right is the judge's call."""
    checks = [{"check": "no error", "passed": not errors, "detail": ",".join(errors)}]
    checks.append({"check": "not a canned fallback", "passed": not _CANNED.search(answer or "")})
    return checks


def _events(block: str) -> tuple[str | None, dict[str, Any] | None]:
    event, data = None, []
    for line in block.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data.append(line[6:])
    if not event or not data:
        return None, None
    try:
        return event, json.loads("\n".join(data))
    except json.JSONDecodeError:
        return event, {"invalid_json": True}


async def _turn(client, message: str, conversation_id) -> dict[str, Any]:
    body: dict[str, Any] = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    started = time.monotonic()
    first_token = None
    out: dict[str, Any] = {
        "answer": "",
        "errors": [],
        "statuses": [],
        "actions": [],
        "done": {},
        "sources": 0,
    }
    answer: list[str] = []
    async with client.stream("POST", "/api/v1/chat/stream", json=body) as response:
        if response.status_code != 200:
            detail = (await response.aread()).decode("utf-8", "replace")[:200]
            out["errors"] = [f"http_{response.status_code}"]
            out["http_detail"] = detail
            out["conversation_id"] = conversation_id
            out["timing"] = {"first_token_ms": None, "total_ms": round((time.monotonic() - started) * 1000)}
            return out
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                event, payload = _events(block)
                if event is None or payload is None:
                    continue
                if event == "started":
                    conversation_id = payload.get("conversation_id", conversation_id)
                elif event == "token":
                    if first_token is None:
                        first_token = time.monotonic() - started
                    answer.append(payload.get("text") or "")
                elif event == "status":
                    out["statuses"].append(payload.get("label"))
                elif event == "action":
                    # A change the app made and read back: the truth the judge grades claims against.
                    out["actions"].append(payload.get("text"))
                elif event == "sources":
                    out["sources"] = len(payload.get("sources") or [])
                elif event == "done":
                    out["done"] = {k: payload.get(k) for k in ("provider", "model", "usage")}
                elif event == "error":
                    out["errors"].append(str(payload.get("code") or "unknown"))
    out["answer"] = "".join(answer)
    out["conversation_id"] = conversation_id
    # The app runs in-process (httpx.ASGITransport), which hands over the response only
    # once it is complete, so a first token seen here arrives with the last one. Time to
    # first token is taken from the server's turn record instead (_run_scenario).
    out["timing"] = {
        "first_token_ms": None,
        "client_first_event_ms": round(first_token * 1000) if first_token is not None else None,
        "total_ms": round((time.monotonic() - started) * 1000),
    }
    return out


def _percentile(values: list[float], q: float):
    values = sorted(v for v in values if isinstance(v, int | float))
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


_STATUSES = ("passed", "failed", "blocked", "unverified", "unjudged", "error")


def scenario_status(result: dict[str, Any], diagnostics: dict[str, Any] | None) -> str:
    """passed / failed / blocked / unverified / unjudged / error (see the module docstring)."""
    from app.evals.judge import outcome

    if result.get("harness_error"):
        return "error"
    judged, problems = outcome(result.get("judgement") or {"error": "not graded"}, result)
    result["problems"] = problems
    checks_ok = bool(result["turns"]) and all(c["passed"] for t in result["turns"] for c in t["checks"])
    if checks_ok and judged is None:
        return "unjudged"
    passed = checks_ok and bool(judged)
    providers_ok = bool((diagnostics or {}).get("ok"))
    if passed:
        return "passed" if providers_ok else "unverified"
    return "failed" if providers_ok else "blocked"


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [t for r in results for t in r["turns"]]
    checks = [c for t in turns for c in t["checks"]]
    sources: dict[str, int] = {}
    tool_calls = 0
    prompt_tokens = completion_tokens = cached_tokens = 0
    with_usage = 0
    for t in turns:
        record = t.get("turn_record") or {}
        source = record.get("answer_source") or "unknown"
        sources[source] = sources.get(source, 0) + 1
        tool_calls += sum(1 for f in record.get("fallbacks") or [] if str(f).startswith("tool:"))
        usage = (t.get("done") or {}).get("usage") or {}
        if usage:
            with_usage += 1
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        cached_tokens += int(usage.get("cached_tokens") or 0)
    rate_limited = sum(
        1
        for t in turns
        if "rate_limited" in (t.get("errors") or [])
        or any(
            f in {"model_turn_models_resting", "waited_for_rate_limit"} or str(f).endswith(":rate_limited")
            for f in (t.get("turn_record") or {}).get("fallbacks") or []
        )
    )
    waited = sum(int(t["timing"].get("waited_before_ms") or 0) for t in turns)
    firsts = [t["timing"].get("first_token_ms") for t in turns]
    totals = [t["timing"].get("total_ms") for t in turns]
    scenario_pass = sum(
        1 for r in results if r["turns"] and all(c["passed"] for t in r["turns"] for c in t["checks"])
    )
    graded = [r["judgement"] for r in results if r.get("judgement") and not r["judgement"].get("error")]
    criteria = [c for j in graded for t in j.get("turns") or [] for c in t.get("criteria") or []]
    scores = [item.get("score", 0) for j in graded for item in j.get("rubric") or []]
    statuses = {status: sum(1 for r in results if r.get("status") == status) for status in _STATUSES}
    return {
        "scenarios": len(results),
        "scenario_status": statuses,
        "scenarios_all_checks_passed": scenario_pass,
        "turns": len(turns),
        "checks_passed": sum(1 for c in checks if c["passed"]),
        "checks_total": len(checks),
        "judged_scenarios": len(graded),
        "criteria_met": sum(1 for c in criteria if c.get("met")),
        "criteria_total": len(criteria),
        "rubric_points": sum(scores),
        "rubric_max": 2 * len(scores),
        "error_turns": sum(1 for t in turns if t["errors"]),
        "canned_fallback_turns": sum(1 for t in turns if _CANNED.search(t["answer"] or "")),
        "answer_sources": sources,
        "distinct_tool_calls_recorded": tool_calls,
        # Only what the provider reported; None when no turn reported usage.
        "tokens": (
            {
                "prompt": prompt_tokens,
                "cached": cached_tokens,
                "completion": completion_tokens,
                "turns_reporting": with_usage,
            }
            if with_usage
            else None
        ),
        "turns_without_usage": len(turns) - with_usage,
        "rate_limited_turns": rate_limited,
        "paced_wait_ms": waited,
        # Server-side: request received -> first answer text sent (None for turns with no text).
        "first_token_ms": {"p50": _percentile(firsts, 0.5), "p95": _percentile(firsts, 0.95)},
        "total_ms": {
            "p50": _percentile(totals, 0.5),
            "p95": _percentile(totals, 0.95),
            "mean": round(statistics.mean([v for v in totals if v]), 1) if any(totals) else None,
        },
    }


def code_version() -> str:
    """A fingerprint of the backend source, so a report says which code it evaluated."""
    import hashlib

    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def stored_knowledge() -> dict[str, Any]:
    """Row counts only (no content): is the research cache populated, and is memory in use?"""
    from sqlalchemy import func, select
    from sqlalchemy.exc import SQLAlchemyError

    from app.database import KnowledgeDocument, Memory, SessionLocal

    try:
        with SessionLocal() as db:
            return {
                "knowledge_documents": db.scalar(select(func.count(KnowledgeDocument.id))) or 0,
                "films_with_knowledge": db.scalar(
                    select(func.count(func.distinct(KnowledgeDocument.movie_id)))
                )
                or 0,
                "knowledge_with_embeddings": db.scalar(
                    select(func.count(KnowledgeDocument.id)).where(KnowledgeDocument.embedding.is_not(None))
                )
                or 0,
                "memories": db.scalar(select(func.count(Memory.id))) or 0,
                "users_with_memories": db.scalar(select(func.count(func.distinct(Memory.user_id)))) or 0,
            }
    except (SQLAlchemyError, OSError) as exc:  # a report field, never fatal
        return {"error": type(exc).__name__}


def configuration() -> dict[str, Any]:
    """What this run actually used. Whether a key is set, never its value."""
    from app.config import settings
    from app.services.diagnostics import _sdk_versions

    return {
        "environment": settings.environment,
        "agent_mode": "model",
        "region": settings.default_region,
        "models": {
            "fast": settings.groq_model,
            "deep": settings.groq_deep_model,
            "summary": settings.groq_summary_model,
            "fallbacks": settings.groq_fallback_models,
            "vision": settings.groq_vision_model,
            "gemini": settings.gemini_model,
            "gemini_fallbacks": settings.gemini_fallback_models,
        },
        "keys_set": {
            "groq": bool(settings.groq_api_key),
            "tmdb": bool(settings.tmdb_api_key),
            "gemini": bool(settings.gemini_api_key),
        },
        "sdk": _sdk_versions(),
    }


async def _diagnose() -> dict[str, Any]:
    """The live check; if the check itself cannot run, that is recorded as a failed diagnostic."""
    from app.services.diagnostics import run_checks

    try:
        return await run_checks()
    except (ImportError, RuntimeError, OSError, ValueError, TypeError, AttributeError) as exc:
        return {
            "ok": False,
            "error": f"The live check could not run: {type(exc).__name__}",
            "failed_required": ["live check"],
            "checks": [],
        }


# What a scenario can fail with that is about the run, not the agent (a request
# timing out, the connection dropping, an unparseable event): recorded on the
# scenario, and the run moves on. Anything else is a harness bug and stops the
# run, after the partial report is saved.
_SCENARIO_FAILURES = (TimeoutError, OSError, ValueError, KeyError)


# Groq's free tier allows a few thousand tokens per minute per model; a script that
# fires 45 turns in 40 seconds measures that limit, not the agent (live run
# 2026-10-13). Turns are paced like a person: a minimum gap, and a wait while every
# answering model rests after a rate limit. Every wait is recorded in the report.
_MAX_RATE_LIMIT_WAIT = 75.0


async def _pace(*, first: bool, min_gap: float) -> int:
    from app import main

    started = time.monotonic()
    if not first and min_gap > 0:
        await asyncio.sleep(min_gap)
    try:
        chain = main.agent.router.deep()  # the chat model
    except (AttributeError, RuntimeError, ValueError):
        chain = None
    seconds = getattr(chain, "seconds_until_available", lambda: None)
    resting = getattr(chain, "all_resting", lambda: False)
    while resting() and time.monotonic() - started < _MAX_RATE_LIMIT_WAIT:
        wait = seconds()
        if wait is None:
            break  # resting for a reason that won't clear by itself (auth, missing model)
        await asyncio.sleep(min(wait + 0.2, _MAX_RATE_LIMIT_WAIT))
    return round((time.monotonic() - started) * 1000)


async def _run_scenario(
    index: int, scenario: dict[str, Any], origin: str, *, min_gap: float = 0.0
) -> dict[str, Any]:
    import httpx

    from app import main
    from app.services.provider_health import recent_turns

    turns: list[dict[str, Any]] = []
    result: dict[str, Any] = {**scenario, "turns": turns}
    transport = httpx.ASGITransport(app=main.app, client=(f"10.250.{index // 200}.{index % 200 + 1}", 5555))
    try:
        # https: production cookies are Secure. A distinct User-Agent per scenario keeps
        # the guest fingerprint rate limit (30/minute) from counting the whole run as one visitor.
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://live-eval",
            timeout=150,
            headers={"Origin": origin, "User-Agent": f"grace-live-eval/{scenario['id']}"},
        ) as client:
            csrf = await client.get("/api/v1/auth/csrf")
            token = csrf.headers.get("X-CSRF-Token")
            if token:
                client.headers["X-CSRF-Token"] = token
            await client.post("/api/v1/guest/session")
            conversation_id = None
            for position, turn in enumerate(scenario["turns"]):
                waited_ms = await _pace(first=index == 0 and position == 0, min_gap=min_gap)
                outcome = await _turn(client, turn["say"], conversation_id)
                conversation_id = outcome.pop("conversation_id", conversation_id)
                latest = recent_turns().get("latest") or []
                record = latest[0] if latest else {}  # newest first
                outcome["timing"]["first_token_ms"] = record.get("first_token_ms")
                outcome["timing"]["server_total_ms"] = record.get("total_ms")
                outcome["timing"]["waited_before_ms"] = waited_ms
                turns.append(
                    {
                        "say": turn["say"],
                        "criteria": turn.get("criteria") or [],
                        **outcome,
                        "turn_record": record,
                        "checks": check_turn(turn, outcome["answer"], outcome["errors"]),
                    }
                )
                sys.stdout.write(
                    f"[{scenario['id']}] {turn['say'][:50]!r} -> {outcome['answer'][:80]!r}"
                    f" ({outcome['timing']['total_ms']} ms, {record.get('answer_source')})\n"
                )
                sys.stdout.flush()
    except (httpx.HTTPError, *_SCENARIO_FAILURES) as exc:
        result["harness_error"] = f"{type(exc).__name__} after {len(turns)} of {len(scenario['turns'])} turns"
        sys.stdout.write(f"[{scenario['id']}] could not complete: {result['harness_error']}\n")
    return result


async def _judge(scenario: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, Any]:
    from app.evals.judge import grade

    return await grade(scenario, turns)


async def run(only: set[str] | None, *, save=None, min_gap: float = 0.0) -> dict[str, Any]:
    """Run the live check and the scenarios; ``save(report)`` is called after each step."""
    from app import main
    from app.config import settings

    scenarios = json.loads(_SCENARIOS.read_text(encoding="utf-8"))["scenarios"]
    if only:
        scenarios = [s for s in scenarios if s["id"].split("-")[0] in only or s["id"] in only]
    report: dict[str, Any] = {
        "ran_at": datetime.now(UTC).isoformat(),
        "code_version": code_version(),
        "agent_mode": "model",
        "configuration": configuration(),
        "models": {
            "fast": settings.groq_model,
            "deep": settings.groq_deep_model,
            "fallbacks": settings.groq_fallback_models,
            "gemini": settings.gemini_model,
            "gemini_fallbacks": settings.gemini_fallback_models,
        },
        "run_status": "running",
        "pacing": {"min_gap_seconds": min_gap, "max_rate_limit_wait_seconds": _MAX_RATE_LIMIT_WAIT},
        "diagnostics": None,
        "results": [],
        "summary": summarize([]),
    }

    def checkpoint() -> None:
        report["summary"] = summarize(report["results"])
        if save is not None:
            save(report)

    checkpoint()
    origin = (settings.cors_origins.split(",")[0] or "http://localhost:5173").strip()
    try:
        async with main.app.router.lifespan_context(main.app):
            diagnostics = await _diagnose()
            report["diagnostics"] = diagnostics
            for check in diagnostics.get("checks", []):
                state = "ok" if check["ok"] else f"FAILED ({check.get('category')}: {check.get('detail')})"
                sys.stdout.write(f"[diagnostic] {check['service']}: {state} {check.get('latency_ms')} ms\n")
            if diagnostics.get("error"):
                sys.stdout.write(f"[diagnostic] {diagnostics['error']}\n")
            if not diagnostics.get("ok"):
                sys.stdout.write(
                    "[diagnostic] Required checks failed: "
                    + ", ".join(diagnostics.get("failed_required") or ["unknown"])
                    + ". Scenarios still run; their results will be marked blocked or unverified.\n"
                )
            checkpoint()
            for index, scenario in enumerate(scenarios):
                result = await _run_scenario(index, scenario, origin, min_gap=min_gap)
                if result["turns"] and not result.get("harness_error"):
                    result["judgement"] = await _judge(scenario, result["turns"])
                result["status"] = scenario_status(result, diagnostics)
                report["results"].append(result)
                checkpoint()
        report["stored_knowledge"] = stored_knowledge()
        report["run_status"] = "complete"
    except BaseException as exc:
        report["run_status"] = f"stopped: {type(exc).__name__}"
        raise
    finally:
        checkpoint()
    return report


def exit_status(report: dict[str, Any]) -> int:
    if report.get("run_status") != "complete":
        return 3
    if not (report.get("diagnostics") or {}).get("ok"):
        return 2
    statuses = report["summary"].get("scenario_status") or {}
    return 1 if statuses.get("failed") or statuses.get("error") else 0


def markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"# Grace live evaluation — {report['ran_at']}",
        "",
        f"Models: {report.get('models')} · code {report.get('code_version')}",
        "",
        f"Run: **{report.get('run_status')}** · configuration: {report.get('configuration')}",
        "",
        f"Stored data (counts only): {report.get('stored_knowledge')}",
        "",
        "## Provider diagnostics",
        "",
        (
            "Required checks passed."
            if (report.get("diagnostics") or {}).get("ok")
            else "**Required checks failed:** "
            + ", ".join((report.get("diagnostics") or {}).get("failed_required") or ["not run"])
            + (
                f" ({(report.get('diagnostics') or {}).get('error')})"
                if (report.get("diagnostics") or {}).get("error")
                else ""
            )
        ),
        "",
        "| Service | Result | Latency | Category | Detail |",
        "|---|---|---|---|---|",
        *[
            f"| {c['service']} | {'ok' if c['ok'] else 'failed'} | {c.get('latency_ms')} ms | {c.get('category') or ''} | "
            f"{'' if c['ok'] else (c.get('detail') or '')} |"
            for c in (report.get("diagnostics") or {}).get("checks", [])
        ],
        "",
        "## Summary",
        "",
        f"Scenario status: {summary.get('scenario_status')}.",
        "",
        f"{summary['scenarios']} scenarios, {summary['turns']} turns. Judge: criteria met "
        f"{summary.get('criteria_met')}/{summary.get('criteria_total')}, rubric "
        f"{summary.get('rubric_points')}/{summary.get('rubric_max')} points over "
        f"{summary.get('judged_scenarios')} graded scenarios. Code checks "
        f"{summary['checks_passed']}/{summary['checks_total']}; error turns {summary['error_turns']}, "
        f"canned-fallback turns {summary['canned_fallback_turns']}.",
        "",
        f"Answer sources: {summary['answer_sources']} · tool calls recorded: {summary['distinct_tool_calls_recorded']} · "
        f"tokens: {summary['tokens']}",
        "",
        f"First token p50 {summary['first_token_ms']['p50']} ms, p95 {summary['first_token_ms']['p95']} ms · "
        f"end to end p50 {summary['total_ms']['p50']} ms, p95 {summary['total_ms']['p95']} ms.",
        "",
        "Grades are the judge's (Gemini, not the model that answered); read the reasons.",
        "",
    ]
    for result in report["results"]:
        lines += [f"## {result['id']} — {result['category']} — **{result.get('status', 'not run')}**", ""]
        if result.get("harness_error"):
            lines += [f"The harness could not complete this scenario: {result['harness_error']}.", ""]
        judgement = result.get("judgement") or {}
        grades = {t.get("turn"): t.get("criteria") or [] for t in judgement.get("turns") or []}
        for number, turn in enumerate(result["turns"], start=1):
            failed = [c["check"] for c in turn["checks"] if not c["passed"]]
            record = turn.get("turn_record") or {}
            lines += [
                f"**User:** {turn['say']}",
                "",
                f"**Grace** ({turn['timing'].get('total_ms')} ms, first token {turn['timing'].get('first_token_ms')} ms, "
                f"{record.get('answer_source') or 'unknown source'}, {record.get('model') or 'no model'}, "
                f"fallbacks {record.get('fallbacks') or []}):",
                "",
                "> " + (turn["answer"] or "(no answer)").replace("\n", "\n> "),
                "",
                *[f"App verified: {action}" for action in turn.get("actions") or []],
                *[
                    f"- {'met' if c.get('met') else '**not met**'}: {c.get('criterion')} — {c.get('reason')}"
                    for c in grades.get(number, [])
                ],
                ("Code checks failed: " + "; ".join(failed)) if failed else "",
                "",
            ]
        if judgement.get("error"):
            lines += [f"Not graded: {judgement['error']}.", ""]
        lines += [
            "| Rubric | Score | Reason |",
            "|---|---|---|",
            *[
                f"| {r.get('item')} | {r.get('score')} | {r.get('reason')} |"
                for r in judgement.get("rubric") or []
            ],
            "",
        ]
    return "\n".join(lines)


_EXIT_REASONS = {
    0: "every required provider check and every scenario passed",
    1: "a scenario failed, could not run or could not be graded (see the report)",
    2: "a required provider check failed; scenario results are blocked or unverified, not passes",
    3: "the run stopped before finishing; the partial report was saved",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", help="Comma-separated scenario ids or prefixes, e.g. G01,R01")
    parser.add_argument("--out", default=_DEFAULT_OUT)
    parser.add_argument(
        "--min-gap",
        type=float,
        default=5.0,
        help="Seconds between turns, like a person reading and typing (default 5); 0 sends them back to back.",
    )
    args = parser.parse_args(argv)
    only = {item.strip() for item in args.only.split(",")} if args.only else None

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)  # before anything runs, so evidence always has a home
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"live-eval-{stamp}"
    json_path, md_path = out / f"{name}.json", out / f"{name}.md"

    def save(report: dict[str, Any]) -> None:
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        md_path.write_text(markdown(report), encoding="utf-8")

    sys.stdout.write(f"Writing the report to {json_path} and {md_path.name}\n")
    status = 3
    try:
        status = exit_status(asyncio.run(run(only, save=save, min_gap=max(0.0, args.min_gap))))
    except Exception:  # printed in full and reported as exit status 3, never hidden
        import traceback

        traceback.print_exc()
    finally:
        if json_path.exists():
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            summary = saved.get("summary") or {}
            diagnostics = saved.get("diagnostics") or {}
            sys.stdout.write(
                "\n"
                + f"Code {saved.get('code_version')} · run {saved.get('run_status')}\n"
                + "Required provider checks: "
                + (
                    "passed"
                    if diagnostics.get("ok")
                    else "FAILED: " + ", ".join(diagnostics.get("failed_required") or ["not run"])
                )
                + "\n"
                + f"Scenarios: {summary.get('scenario_status')} of {summary.get('scenarios')}\n"
                + f"Judge: criteria met {summary.get('criteria_met')}/{summary.get('criteria_total')}, "
                + f"rubric {summary.get('rubric_points')}/{summary.get('rubric_max')}\n"
                + f"Code checks (errors, canned replies): {summary.get('checks_passed')}/{summary.get('checks_total')}\n"
                + f"Report: {json_path}\n        {md_path}\n"
                + f"Exit status {status}: {_EXIT_REASONS[status]}\n"
            )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
