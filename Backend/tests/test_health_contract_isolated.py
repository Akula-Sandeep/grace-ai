import sys
import types

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _CryptContext:
    def __init__(self, *args, **kwargs):
        pass

    def hash(self, value):
        return value

    def verify(self, value, encoded):
        return value == encoded


if "passlib" not in sys.modules:
    passlib = types.ModuleType("passlib")
    context = types.ModuleType("passlib.context")
    context.CryptContext = _CryptContext
    passlib.context = context
    sys.modules["passlib"] = passlib
    sys.modules["passlib.context"] = context

import asyncio

from fastapi import HTTPException

from app import main


def test_liveness_does_not_require_dependencies():
    result = asyncio.run(main.live())
    assert result["status"] == "alive"
    assert "timestamp" in result


def test_readiness_requires_integrations_and_shared_state(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}")
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main.settings, "environment", "development")
    monkeypatch.setattr(main.settings, "tmdb_api_key", "")
    monkeypatch.setattr(main.settings, "gemini_api_key", "")
    monkeypatch.setattr(main.settings, "groq_api_key", "")
    monkeypatch.setattr(main.settings, "jwt_secret", "x" * 64)
    monkeypatch.setattr(main.shared_state, "_ready", True)

    try:
        try:
            main.ready()
        except HTTPException as exc:
            assert exc.status_code == 503
            assert "Required integrations" in str(exc.detail)
        else:
            raise AssertionError("readiness unexpectedly passed without required integrations")

        monkeypatch.setattr(main.settings, "tmdb_api_key", "tmdb")
        monkeypatch.setattr(main.settings, "gemini_api_key", "gemini")
        monkeypatch.setattr(main.settings, "groq_api_key", "groq")
        result = main.ready()
        assert result["status"] == "ready"
        assert result["version"] == main.settings.app_version

        monkeypatch.setattr(main.shared_state, "_ready", False)
        try:
            main.ready()
        except HTTPException as exc:
            assert exc.status_code == 503
            assert "Shared state" in str(exc.detail)
        else:
            raise AssertionError("readiness unexpectedly passed with unavailable shared state")
    finally:
        engine.dispose()
