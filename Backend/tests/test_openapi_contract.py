from app.main import app


def test_auth_and_chat_stream_contracts_are_published_in_openapi():
    schema = app.openapi()
    paths = schema["paths"]

    for path, method in (
        ("/api/v1/auth/register", "post"),
        ("/api/v1/auth/verify", "post"),
        ("/api/v1/auth/login", "post"),
        ("/api/v1/auth/refresh", "post"),
        ("/api/v1/auth/logout", "post"),
        ("/api/v1/auth/me", "get"),
        ("/api/v1/auth/me", "patch"),
        ("/api/v1/auth/me", "delete"),
        ("/api/v1/chat/stream", "post"),
        ("/api/v1/conversations/{cid}/messages", "get"),
    ):
        assert method in paths[path], f"{method.upper()} {path} is missing from OpenAPI"

    assert not any(path.startswith(("/auth/", "/api/auth/", "/chat", "/conversations")) for path in paths)
    assert "/api/v1/auth/request-otp" not in paths
    assert "/api/v1/auth/verify-otp" not in paths
    assert all(
        len(operations) == len(set(operations))
        for operations in (
            [operation.get("operationId") for operation in path_item.values() if isinstance(operation, dict)]
            for path_item in paths.values()
        )
    )

    response = paths["/api/v1/chat/stream"]["post"]["responses"]["200"]
    event_schema = response["content"]["text/event-stream"]["schema"]
    variants = event_schema["oneOf"]
    event_types = {variant["properties"]["event"]["const"] for variant in variants}
    assert event_types == {
        "started",
        "status",
        "providers",
        "state",
        "sources",
        "token",
        "done",
        "error",
    }

    by_event = {variant["properties"]["event"]["const"]: variant for variant in variants}
    assert {"conversation_id", "message_id"} <= set(by_event["started"]["required"])
    assert {"stage", "label"} <= set(by_event["status"]["required"])
    assert {"text"} <= set(by_event["token"]["required"])
    assert {"message_id", "usage"} <= set(by_event["done"]["required"])
    assert {"code", "message", "retryable"} <= set(by_event["error"]["required"])
