from scripts.check_repo_hygiene import blocked_tracked_paths


def test_tracked_local_artifacts_fail_the_repository_guard():
    paths = [
        "Backend/movie_agent.db",
        "Backend/pytest_failures.txt",
        "Backend/debug_models.py",
        "Frontend/coverage/lcov.info",
        "Frontend/dist/index.html",
        "Frontend/test-results/results.json",
        "Backend/app/main.py",
        "Frontend/src/App.jsx",
    ]

    assert blocked_tracked_paths(paths) == paths[:6]


def test_repository_guard_ignores_generated_schema_artifacts():
    assert blocked_tracked_paths(["Backend/openapi.json", "Frontend/src/generated/api-schema.ts"]) == []


def test_local_work_output_never_reaches_the_repository():
    paths = [
        "eval-reports/20261016-141854/model/live-eval.json",
        "_to_delete/removed-8b/guard.py",
        ".archify/architecture/index.html",
        "Backend/app/evals/live_scenarios.json",
    ]
    assert blocked_tracked_paths(paths) == paths[:3]
