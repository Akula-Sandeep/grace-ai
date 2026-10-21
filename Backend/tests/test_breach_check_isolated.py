import sys
import types

import httpx
import pytest


class _CryptContext:
    def __init__(self, *args, **kwargs):
        pass

    def hash(self, value):
        return value

    def verify(self, value, encoded):
        return value == encoded


# This focused test isolates the HIBP policy from the optional password-hasher
# package, which is unavailable in the reduced verification sandbox.
if "passlib" not in sys.modules:
    passlib = types.ModuleType("passlib")
    context = types.ModuleType("passlib.context")
    context.CryptContext = _CryptContext
    passlib.context = context
    sys.modules["passlib"] = passlib
    sys.modules["passlib.context"] = context

from app import security


@pytest.mark.asyncio
async def test_production_breach_check_fails_closed_on_provider_failure(monkeypatch):
    monkeypatch.setattr(security.settings, "environment", "production")

    class OfflineClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("HIBP unavailable")

    monkeypatch.setattr(security.httpx, "AsyncClient", lambda **_kwargs: OfflineClient())

    with pytest.raises(security.PasswordBreachCheckUnavailable):
        await security.is_password_breached("a-long-password-for-isolation")


@pytest.mark.asyncio
async def test_development_breach_check_degrades_to_unbreached_on_provider_failure(
    monkeypatch,
):
    monkeypatch.setattr(security.settings, "environment", "development")

    class OfflineClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("HIBP unavailable")

    monkeypatch.setattr(security.httpx, "AsyncClient", lambda **_kwargs: OfflineClient())

    assert await security.is_password_breached("a-long-password-for-isolation") is False
