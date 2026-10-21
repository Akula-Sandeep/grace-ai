"""Public read-only share links with revoke (C4), through the real app."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from app.database import Conversation, Message, SessionLocal, User
from app.routers import sharing


@pytest.fixture
def owned():
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        owner = User(username=f"sharer-{tag}", password_hash="x", status="active")
        stranger = User(username=f"stranger-{tag}", password_hash="x", status="active")
        db.add_all([owner, stranger])
        db.flush()
        convo = Conversation(user_id=owner.id, title="Villeneuve night")
        db.add(convo)
        db.flush()
        q1 = Message(conversation_id=convo.id, user_id=owner.id, role="user", content="Villeneuve films?")
        db.add(q1)
        db.flush()
        a1 = Message(
            conversation_id=convo.id,
            user_id=owner.id,
            role="assistant",
            content="**Arrival** and **Sicario** [1].",
            parent_id=q1.uuid_id,
            sources_json=[
                {
                    "url": "https://www.themoviedb.org/person/137427",
                    "title": "TMDB",
                    "kind": "structured",
                    "secret": "x",
                }
            ],
        )
        db.add(a1)
        db.flush()
        q2 = Message(
            conversation_id=convo.id, user_id=owner.id, role="user", content="Shortest?", parent_id=a1.uuid_id
        )
        db.add(q2)
        db.flush()
        a2 = Message(
            conversation_id=convo.id,
            user_id=owner.id,
            role="assistant",
            content="Polytechnique, 77 min.",
            parent_id=q2.uuid_id,
        )
        # An edited branch the shared view must not include.
        q2b = Message(
            conversation_id=convo.id, user_id=owner.id, role="user", content="Longest?", parent_id=a1.uuid_id
        )
        failed = Message(
            conversation_id=convo.id,
            user_id=owner.id,
            role="assistant",
            content="",
            parent_id=q2.uuid_id,
            status="error",
        )
        db.add_all([a2, q2b, failed])
        db.commit()
        return SimpleNamespace(
            owner=SimpleNamespace(id=owner.id),
            owner_id=owner.id,
            stranger=SimpleNamespace(id=stranger.id),
            cid=convo.id,
            a1=a1.uuid_id,
            a2=a2.uuid_id,
        )


def public(token):
    with TestClient(main.app) as client:
        return client.get(f"/api/v1/shared/{token}")


def test_anyone_with_the_link_reads_that_branch_without_signing_in(owned):
    link = sharing.create_share_link(
        owned.cid, sharing.ShareRequest(message_id=uuid.UUID(owned.a2)), u=owned.owner
    )
    assert link["path"] == f"/s/{link['token']}" and len(link["token"]) >= 32
    response = public(link["token"])
    assert response.status_code == 200
    assert "noindex" in response.headers["x-robots-tag"]
    body = response.json()
    assert body["title"] == "Villeneuve night"
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "Villeneuve films?"),
        ("assistant", "**Arrival** and **Sicario** [1]."),
        ("user", "Shortest?"),
        ("assistant", "Polytechnique, 77 min."),
    ]
    assert body["messages"][1]["sources"] == [
        {"url": "https://www.themoviedb.org/person/137427", "title": "TMDB", "kind": "structured"}
    ]
    assert "user_id" not in str(body) and "Longest?" not in str(body)


def test_sharing_the_same_point_twice_reuses_the_link_and_defaults_to_the_newest_answer(owned):
    first = sharing.create_share_link(owned.cid, None, u=owned.owner)
    assert first["message_id"] == owned.a2
    again = sharing.create_share_link(
        owned.cid, sharing.ShareRequest(message_id=uuid.UUID(owned.a2)), u=owned.owner
    )
    assert again["token"] == first["token"]
    earlier = sharing.create_share_link(
        owned.cid, sharing.ShareRequest(message_id=uuid.UUID(owned.a1)), u=owned.owner
    )
    assert earlier["token"] != first["token"]
    assert [m["content"] for m in public(earlier["token"]).json()["messages"]][-1].startswith("**Arrival**")
    assert {item["token"] for item in sharing.list_share_links(owned.cid, u=owned.owner)["items"]} == {
        first["token"],
        earlier["token"],
    }


def test_revoking_deleting_or_a_stranger_stops_the_link(owned):
    link = sharing.create_share_link(owned.cid, None, u=owned.owner)
    with pytest.raises(HTTPException) as other:
        sharing.revoke_share_link(link["token"], u=owned.stranger)
    assert other.value.status_code == 404
    with pytest.raises(HTTPException):
        sharing.create_share_link(owned.cid, None, u=owned.stranger)
    sharing.revoke_share_link(link["token"], u=owned.owner)
    assert public(link["token"]).status_code == 404
    assert sharing.list_share_links(owned.cid, u=owned.owner)["items"] == []

    second = sharing.create_share_link(owned.cid, None, u=owned.owner)
    assert second["token"] != link["token"]
    with SessionLocal() as db:
        db.get(Conversation, owned.cid).is_deleted = True
        db.commit()
    assert public(second["token"]).status_code == 404


def test_a_deleted_account_stops_its_links(owned):
    from datetime import UTC, datetime

    link = sharing.create_share_link(owned.cid, None, u=owned.owner)
    with SessionLocal() as db:
        db.get(User, owned.owner_id).deleted_at = datetime.now(UTC)
        db.commit()
    assert public(link["token"]).status_code == 404
    assert public("x" * 80).status_code == 404
