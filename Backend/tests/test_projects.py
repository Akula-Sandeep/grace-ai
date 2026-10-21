"""Projects with standing instructions (C5)."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import main
from app.database import Conversation
from app.routers import projects
from tests.jarvis_harness import Conversation as Chat
from tests.jarvis_harness import install, new_user
from tests.model_harness import ScriptedModel, install_model, say, system_text

INSTRUCTIONS = "We are two adults. Keep films under two hours. Never suggest horror."


@pytest.fixture
def world(monkeypatch):
    factory, _tmdb = install(monkeypatch, offline=False)
    monkeypatch.setattr(projects.main, "SessionLocal", factory)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    user_id = new_user(factory)
    return SimpleNamespace(factory=factory, model=model, user=SimpleNamespace(id=user_id), user_id=user_id)


def test_create_list_rename_and_validate(world):
    created = projects.create_project(
        projects.ProjectIn(name="  Date   night ", instructions=f"  {INSTRUCTIONS} "), u=world.user
    )
    assert created["name"] == "Date night" and created["instructions"] == INSTRUCTIONS
    renamed = projects.update_project(created["id"], projects.ProjectPatch(name="Date nights"), u=world.user)
    assert renamed["name"] == "Date nights" and renamed["instructions"] == INSTRUCTIONS
    assert [p["name"] for p in projects.list_projects(u=world.user)["items"]] == ["Date nights"]
    with pytest.raises(ValidationError):
        projects.ProjectIn(name="   ")
    with pytest.raises(ValidationError):
        projects.ProjectIn(name="x", instructions="y" * 2001)
    with pytest.raises(HTTPException) as other:
        projects.update_project(created["id"], projects.ProjectPatch(name="Mine"), u=SimpleNamespace(id=999))
    assert other.value.status_code == 404


@pytest.mark.asyncio
async def test_a_chat_started_in_a_project_follows_its_instructions(world):
    project = projects.create_project(
        projects.ProjectIn(name="Date night", instructions=INSTRUCTIONS), u=world.user
    )
    chat = Chat(world.factory, world.user_id)
    world.model.script(say("How about **Paddington 2**?"))
    reply = await chat.say("something for tonight", project_id=project["id"])
    assert reply.error is None
    assert INSTRUCTIONS in system_text(world.model.requests[-1])
    assert 'project "Date night"' in system_text(world.model.requests[-1])
    with world.factory() as db:
        assert db.get(Conversation, int(chat.conversation_id)).project_id == project["id"]
    page = main.conversations(world.user, project=project["id"])
    assert [item["id"] for item in page["items"]] == [int(chat.conversation_id)]
    assert page["items"][0]["project_id"] == project["id"]
    assert projects.list_projects(u=world.user)["items"][0]["conversation_count"] == 1

    # A chat outside the project is not told the instructions.
    other = Chat(world.factory, world.user_id)
    world.model.script(say("Sure."))
    await other.say("hello")
    assert INSTRUCTIONS not in system_text(world.model.requests[-1])


@pytest.mark.asyncio
async def test_moving_a_chat_in_and_removing_the_project(world):
    project = projects.create_project(
        projects.ProjectIn(name="Horror marathon", instructions="Only horror."), u=world.user
    )
    chat = Chat(world.factory, world.user_id)
    world.model.script(say("Hi."))
    await chat.say("hello")
    cid = int(chat.conversation_id)
    assert projects.set_conversation_project(
        cid, projects.ConversationProjectIn(project_id=project["id"]), u=world.user
    ) == {
        "id": cid,
        "project_id": project["id"],
    }
    world.model.script(say("**The Thing**."))
    await chat.say("pick one")
    assert "Only horror." in system_text(world.model.requests[-1])

    projects.delete_project(project["id"], u=world.user)
    assert projects.list_projects(u=world.user)["items"] == []
    with world.factory() as db:
        assert db.get(Conversation, cid).project_id is None  # the chat stays, outside any project
    world.model.script(say("Sure."))
    await chat.say("another")
    assert "Only horror." not in system_text(world.model.requests[-1])
    with pytest.raises(HTTPException):
        projects.set_conversation_project(
            cid, projects.ConversationProjectIn(project_id=project["id"]), u=world.user
        )


@pytest.mark.asyncio
async def test_someone_elses_project_cannot_be_used(world):
    project = projects.create_project(
        projects.ProjectIn(name="Mine", instructions="Secret rules."), u=world.user
    )
    stranger = new_user(world.factory)
    chat = Chat(world.factory, stranger)
    world.model.script(say("Hi."))
    with pytest.raises(HTTPException) as refused:
        await chat.say("hello", project_id=project["id"])
    assert refused.value.status_code == 404
    assert all("Secret rules." not in system_text(r) for r in world.model.requests)
