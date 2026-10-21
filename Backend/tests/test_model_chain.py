import pytest

from app.agent.failures import FailureKind, classify_failure, failure_response
from app.inference.providers import chain as chain_module


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class FakeError(Exception):
    def __init__(self, status, headers=None, message="error"):
        super().__init__(message)
        self.status_code = status
        self.response = FakeResponse(status, headers)


class FakeProvider:
    behaviours: dict = {}
    calls: list = []

    def __init__(self, model, max_retries=2):
        self.model_name = model
        self.max_retries = max_retries
        self.client = object()
        self.last_usage = {"model": model}

    async def generate(self, request):
        FakeProvider.calls.append(self.model_name)
        outcome = FakeProvider.behaviours.get(self.model_name, "ok")
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {"text": f"from {self.model_name}", "usage": {}})()

    async def generate_structured(self, request, schema):
        return await self.generate(request)

    async def stream(self, request):
        FakeProvider.calls.append(self.model_name)
        outcome = FakeProvider.behaviours.get(self.model_name, "ok")
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "break-mid-stream":
            yield "partial "
            raise FakeError(503)
        yield f"text from {self.model_name}"


@pytest.fixture(autouse=True)
def fake_groq(monkeypatch):
    monkeypatch.setattr(chain_module, "GroqProvider", FakeProvider)
    chain_module._COOLDOWN_UNTIL.clear()
    FakeProvider.behaviours = {}
    FakeProvider.calls = []
    yield
    chain_module._COOLDOWN_UNTIL.clear()


@pytest.mark.asyncio
async def test_rate_limited_model_hands_off_and_rests_for_its_retry_after():
    FakeProvider.behaviours = {"big": FakeError(429, {"retry-after": "7"})}
    models = chain_module.ModelChain(["big", "small"])
    result = await models.generate(object())
    assert result.text == "from small"
    assert models.last_model == "small"
    # The busy model is skipped on the next call instead of being hammered.
    FakeProvider.calls.clear()
    await models.generate(object())
    assert FakeProvider.calls == ["small"]


@pytest.mark.asyncio
async def test_the_sdk_never_sleeps_out_retry_after_unseen():
    # The chain rests, waits visibly or moves on itself; the SDK never retries silently.
    models = chain_module.ModelChain(["big", "small"])
    assert all(provider.max_retries == 0 for provider in models._providers)
    assert chain_module.ModelChain(["only"])._providers[0].max_retries == 0


@pytest.mark.asyncio
async def test_request_errors_are_not_retried_on_other_models():
    FakeProvider.behaviours = {"big": FakeError(400, message="bad request")}
    with pytest.raises(FakeError):
        await chain_module.ModelChain(["big", "small"]).generate(object())
    assert FakeProvider.calls == ["big"]


@pytest.mark.asyncio
async def test_stream_falls_back_before_the_first_token_but_never_mid_answer():
    FakeProvider.behaviours = {"big": FakeError(429)}
    chunks = [chunk async for chunk in chain_module.ModelChain(["big", "small"]).stream(object())]
    assert chunks == ["text from small"]

    chain_module._COOLDOWN_UNTIL.clear()
    FakeProvider.behaviours = {"big": "break-mid-stream"}
    received = []
    with pytest.raises(FakeError):
        async for chunk in chain_module.ModelChain(["big", "small"]).stream(object()):
            received.append(chunk)  # noqa: PERF401 - the list must keep what arrived before the error
    assert received == ["partial "]  # no second answer spliced onto the first


@pytest.mark.asyncio
async def test_when_every_model_is_busy_the_failure_carries_the_real_wait():
    FakeProvider.behaviours = {
        "big": FakeError(429, {"retry-after": "9"}),
        "small": FakeError(429, {"retry-after": "4"}),
    }
    with pytest.raises(chain_module.AllModelsBusy) as caught:
        await chain_module.ModelChain(["big", "small"]).generate(object())
    assert 3 <= caught.value.retry_after_seconds <= 4
    assert classify_failure(caught.value) == FailureKind.RATE_LIMITED
    response = failure_response(caught.value)
    assert response.retry_after_seconds == caught.value.retry_after_seconds
    assert "busy" in response.message


@pytest.mark.asyncio
async def test_unknown_model_is_parked_for_minutes_not_retried_every_turn():
    FakeProvider.behaviours = {"retired": FakeError(404, message="model_not_found")}
    models = chain_module.ModelChain(["retired", "current"])
    assert (await models.generate(object())).text == "from current"
    assert chain_module._COOLDOWN_UNTIL["retired"] - chain_module.time.monotonic() > 60
