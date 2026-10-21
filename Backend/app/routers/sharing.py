"""Public, read-only links to a conversation (C4).

A link shows one branch of a conversation as it was when shared: the chosen
message and its ancestors, user and assistant turns only. Anyone with the link
can read it without an account; nothing else about the owner is exposed. The
owner can list and revoke links; a revoked link, a deleted conversation or a
deleted account makes the link stop working at once.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select

from app import main
from app.database import Conversation, Message, SharedLink, User, branch_lineage_ids

router = APIRouter(prefix="/api/v1", tags=["sharing"])

MAX_SHARED_MESSAGES = 400
MAX_ACTIVE_LINKS_PER_CONVERSATION = 20


class ShareRequest(BaseModel):
    # The last message to include (the end of the branch the user is looking at).
    # Omitted: the newest finished answer.
    message_id: uuid.UUID | None = None


def _owned_conversation(db, cid: int, user_id: int) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == cid,
            Conversation.user_id == user_id,
            Conversation.is_deleted.is_(False),
        )
    )
    if conversation is None:
        raise HTTPException(404, "Conversation not found.") from None
    return conversation


def _link_out(link: SharedLink) -> dict:
    return {
        "token": link.token,
        "path": f"/s/{link.token}",
        "created_at": link.created_at,
        "message_id": link.through_message_uuid,
    }


def create_share_link(cid: int, payload: ShareRequest | None = None, u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        _owned_conversation(db, cid, u.id)
        query = select(Message).where(
            Message.conversation_id == cid,
            Message.is_deleted.is_(False),
            Message.role.in_(("user", "assistant")),
        )
        if payload is not None and payload.message_id is not None:
            query = query.where(Message.uuid_id == str(payload.message_id))
        else:
            query = query.where(Message.role == "assistant", Message.status == "complete").order_by(
                Message.created_at.desc(), Message.id.desc()
            )
        leaf = db.scalars(query.limit(1)).first()
        if leaf is None:
            raise HTTPException(409, "There is nothing to share in this conversation yet.") from None
        existing = db.scalar(
            select(SharedLink).where(
                SharedLink.conversation_id == cid,
                SharedLink.through_message_uuid == leaf.uuid_id,
                SharedLink.revoked_at.is_(None),
            )
        )
        if existing is not None:
            return _link_out(existing)
        active = db.scalars(
            select(SharedLink.id).where(SharedLink.conversation_id == cid, SharedLink.revoked_at.is_(None))
        ).all()
        if len(active) >= MAX_ACTIVE_LINKS_PER_CONVERSATION:
            raise HTTPException(
                409, "This conversation has too many active links. Revoke one first."
            ) from None
        link = SharedLink(
            token=secrets.token_urlsafe(24),
            conversation_id=cid,
            user_id=u.id,
            through_message_uuid=leaf.uuid_id,
        )
        db.add(link)
        db.commit()
        return _link_out(link)


def list_share_links(cid: int, u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        _owned_conversation(db, cid, u.id)
        links = db.scalars(
            select(SharedLink)
            .where(SharedLink.conversation_id == cid, SharedLink.revoked_at.is_(None))
            .order_by(SharedLink.created_at.desc(), SharedLink.id.desc())
        ).all()
        return {"items": [_link_out(link) for link in links]}


def revoke_share_link(token: str, u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        link = db.scalar(select(SharedLink).where(SharedLink.token == token, SharedLink.user_id == u.id))
        if link is None:
            raise HTTPException(404, "Link not found.") from None
        if link.revoked_at is None:
            link.revoked_at = datetime.now(UTC)
            db.commit()
        return {"token": token, "revoked": True}


def read_shared_conversation(token: str, response: Response):
    """The public page's data. No authentication: the token is the permission."""
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "private, no-store"
    if not token or len(token) > 64:
        raise HTTPException(404, "This link doesn't work. It may have been removed.") from None
    with main.SessionLocal() as db:
        row = db.execute(
            select(SharedLink, Conversation)
            .join(Conversation, Conversation.id == SharedLink.conversation_id)
            .join(User, User.id == SharedLink.user_id)
            .where(
                SharedLink.token == token,
                SharedLink.revoked_at.is_(None),
                Conversation.is_deleted.is_(False),
                # A deleted or deactivated account's links stop working at once.
                User.deleted_at.is_(None),
                User.is_active.is_(True),
            )
        ).first()
        if row is None:
            raise HTTPException(404, "This link doesn't work. It may have been removed.") from None
        link, conversation = row
        lineage = branch_lineage_ids(
            db, conversation.id, link.through_message_uuid, max_depth=MAX_SHARED_MESSAGES
        )
        found = {
            m.id: m
            for m in db.scalars(
                select(Message).where(
                    Message.id.in_(lineage),
                    Message.is_deleted.is_(False),
                    Message.role.in_(("user", "assistant")),
                )
            ).all()
        }
        messages = [
            {
                "role": m.role,
                "content": m.content,
                "sources": [
                    {k: s.get(k) for k in ("url", "title", "domain", "kind") if k in s}
                    for s in (m.sources_json or [])
                    if isinstance(s, dict) and s.get("url")
                ][:8],
                "created_at": m.created_at,
            }
            for m in (found[i] for i in reversed(lineage) if i in found)
            if m.content and (m.role == "user" or m.status in {"complete", "stopped"})
        ]
        return {
            "title": conversation.title,
            "shared_at": link.created_at,
            "messages": messages,
        }


router.add_api_route("/conversations/{cid}/share", create_share_link, methods=["POST"])
router.add_api_route("/conversations/{cid}/shares", list_share_links, methods=["GET"])
router.add_api_route("/shares/{token}", revoke_share_link, methods=["DELETE"])
router.add_api_route("/shared/{token}", read_shared_conversation, methods=["GET"])
