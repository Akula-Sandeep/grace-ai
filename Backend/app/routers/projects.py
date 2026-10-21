"""Projects (C5): named groups of chats with standing instructions.

"Date night" with "We're two adults; under two hours; no horror" applies those
instructions to every chat in the project, on both agent paths. A chat joins a
project when it is started there or moved there. Removing a project keeps its
chats; they simply no longer belong to it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update

from app import main
from app.database import Conversation, Project

router = APIRouter(prefix="/api/v1", tags=["projects"])

MAX_PROJECTS = 50
INSTRUCTIONS_LIMIT = 2000


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=INSTRUCTIONS_LIMIT)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("A project needs a name.")
        return value

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: str) -> str:
        return value.strip()


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    instructions: str | None = Field(default=None, max_length=INSTRUCTIONS_LIMIT)

    _name = field_validator("name")(lambda cls, v: " ".join(v.split()) if v is not None else v)


class ConversationProjectIn(BaseModel):
    project_id: int | None = None


def owned_project(db, user_id: int, project_id: int) -> Project:
    project = db.scalar(
        select(Project).where(
            Project.id == project_id, Project.user_id == user_id, Project.is_deleted.is_(False)
        )
    )
    if project is None:
        raise HTTPException(404, "Project not found.") from None
    return project


def _out(project: Project, chats: int = 0) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "instructions": project.instructions or "",
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "conversation_count": chats,
    }


def list_projects(u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        projects = db.scalars(
            select(Project)
            .where(Project.user_id == u.id, Project.is_deleted.is_(False))
            .order_by(Project.updated_at.desc(), Project.id.desc())
        ).all()
        counts = dict(
            db.execute(
                select(Conversation.project_id, func.count(Conversation.id))
                .where(
                    Conversation.user_id == u.id,
                    Conversation.is_deleted.is_(False),
                    Conversation.project_id.in_([p.id for p in projects] or [-1]),
                )
                .group_by(Conversation.project_id)
            ).all()
        )
        return {"items": [_out(p, counts.get(p.id, 0)) for p in projects]}


def create_project(payload: ProjectIn, u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        count = db.scalar(
            select(func.count(Project.id)).where(Project.user_id == u.id, Project.is_deleted.is_(False))
        )
        if (count or 0) >= MAX_PROJECTS:
            raise HTTPException(409, f"You can have up to {MAX_PROJECTS} projects.") from None
        project = Project(user_id=u.id, name=payload.name, instructions=payload.instructions)
        db.add(project)
        db.commit()
        return _out(project)


def update_project(project_id: int, payload: ProjectPatch, u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        project = owned_project(db, u.id, project_id)
        if payload.name is not None:
            if not payload.name:
                raise HTTPException(422, "A project needs a name.") from None
            project.name = payload.name
        if payload.instructions is not None:
            project.instructions = payload.instructions.strip()
        project.updated_at = datetime.now(UTC)
        db.commit()
        return _out(project)


def delete_project(project_id: int, u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        project = owned_project(db, u.id, project_id)
        project.is_deleted = True
        db.execute(
            update(Conversation)
            .where(Conversation.user_id == u.id, Conversation.project_id == project.id)
            .values(project_id=None)
        )
        db.commit()
        return {"id": project_id, "deleted": True}


def set_conversation_project(cid: int, payload: ConversationProjectIn, u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == cid, Conversation.user_id == u.id, Conversation.is_deleted.is_(False)
            )
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found.") from None
        if payload.project_id is not None:
            owned_project(db, u.id, payload.project_id)
        conversation.project_id = payload.project_id
        db.commit()
        return {"id": cid, "project_id": conversation.project_id}


def project_instructions(db, conversation: Conversation | None) -> str | None:
    """The block the agent reads for a chat in a project, or None."""
    if conversation is None or conversation.project_id is None:
        return None
    project = db.scalar(
        select(Project).where(Project.id == conversation.project_id, Project.is_deleted.is_(False))
    )
    if project is None or not (project.instructions or "").strip():
        return None
    return (
        f'This chat is in the user\'s project "{project.name}". The user wrote these standing '
        "instructions for every chat in it; follow them unless the current message asks otherwise:\n"
        + project.instructions.strip()[:INSTRUCTIONS_LIMIT]
    )


router.add_api_route("/projects", list_projects, methods=["GET"])
router.add_api_route("/projects", create_project, methods=["POST"])
router.add_api_route("/projects/{project_id}", update_project, methods=["PATCH"])
router.add_api_route("/projects/{project_id}", delete_project, methods=["DELETE"])
router.add_api_route("/conversations/{cid}/project", set_conversation_project, methods=["PATCH"])
