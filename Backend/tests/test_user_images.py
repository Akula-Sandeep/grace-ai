import base64

import pytest

from app.agent.dispatcher import dispatch_and_stream
from app.services.user_images import MAX_IMAGE_BYTES, validate_image_data_url, vision_messages

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _url(mime, raw):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def test_valid_png_is_accepted():
    url = _url("image/png", PNG)
    assert validate_image_data_url(url) == url


@pytest.mark.parametrize(
    "value, message",
    [
        ("https://example.org/a.png", "Attach a PNG"),
        ("data:image/svg+xml;base64,PHN2Zz4=", "Attach a PNG"),
        (_url("image/png", b"GIF89a" + b"\x00" * 10), "does not look like"),
        (_url("image/jpeg", b"\xff\xd8\xff" + b"\x00" * (MAX_IMAGE_BYTES + 1)), "4 MB"),
    ],
)
def test_invalid_images_are_rejected(value, message):
    with pytest.raises(ValueError, match=message):
        validate_image_data_url(value)


def test_vision_prompt_forbids_face_identification_and_keeps_images():
    messages = vision_messages("What film is this?", [_url("image/png", PNG)] * 5)
    assert "Never identify a real person" in messages[0]["content"]
    assert sum(1 for part in messages[-1]["content"] if part["type"] == "image_url") == 3


class _Vision:
    def __init__(self):
        self.request = None

    async def generate(self, request):
        self.request = request

        class R:
            text = "Warm desert light; the visible title reads 'Example Film'."

        return R()


class _Router:
    def __init__(self):
        self.vision_provider = _Vision()

    def vision(self):
        return self.vision_provider


@pytest.mark.asyncio
async def test_dispatcher_answers_attached_images_with_the_vision_model():
    router = _Router()
    chunks = [
        chunk
        async for chunk in dispatch_and_stream(
            "What can you tell me about this?", [], router=router, images=[_url("image/png", PNG)]
        )
    ]
    assert chunks[0].startswith("event: status\n")
    assert "Warm desert light" in chunks[1]
    assert "not stored" in chunks[1]
    user_turn = router.vision_provider.request.messages[-1]["content"]
    assert user_turn[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_chat_endpoint_rejects_invalid_image_with_422():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"message": "hi", "images": ["data:image/png;base64,AAAA"]},
        )
    assert response.status_code == 422
