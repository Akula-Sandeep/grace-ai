from pathlib import Path

import yaml


def test_feedback_worker_is_wired_without_public_port():
    compose = yaml.safe_load(Path(__file__).resolve().parents[2].joinpath("docker-compose.yml").read_text())
    worker = compose["services"]["feedback-worker"]
    assert worker["command"] == ["python", "-m", "app.jobs.process_background"]
    assert "ports" not in worker
    assert worker["depends_on"]["backend"]["condition"] == "service_healthy"
