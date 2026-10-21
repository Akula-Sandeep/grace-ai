from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.agent.events import is_event
from app.agent.failures import AgentFailure, FailureKind, failure_response
from app.config import settings
from app.database import Conversation, Message, SessionLocal, run_db
from app.inference.models import InferenceRequest
from app.inference.router import InferenceRouter
from app.memory import MemoryService
from app.observability import end_trace, log_fallback, logger, start_trace
from app.services.chat_orchestrator import assemble_chat_payload


def _run_turn_db(fn):
    """Run one synchronous ORM unit off the event loop with this module's session factory."""
    return run_db(fn, session_factory=SessionLocal)


class MovieAgent:
    def __init__(self):
        self.router = InferenceRouter()
        self.inference_router = self.router
        self._shared_tmdb = None
        self._shared_web = None

    def _shared_services(self):
        if self._shared_tmdb is None:
            from app.services.tmdb_client import TMDBClient
            from app.services.web_search_client import (
                GeminiGroundedSearchProvider,
                WebSearchClient,
            )

            self._shared_tmdb = TMDBClient()
            self._shared_web = WebSearchClient(GeminiGroundedSearchProvider())
        return self._shared_tmdb, self._shared_web

    async def close(self):
        if self._shared_tmdb is not None:
            await self._shared_tmdb.close()
            self._shared_tmdb = None
        if self._shared_web is not None:
            await self._shared_web.close()
            self._shared_web = None

    @staticmethod
    def _guest_user_block() -> str:
        from app.agent.context import user_block

        return user_block(signed_in=False)

    @staticmethod
    def _model_user_block(db, uid, msg, summary=None) -> str:
        """Who the user is, for every model-led turn (general or about films)."""
        from app.agent.context import user_block
        from app.database import (
            Preference,
            User,
            UserInterfaceSetting,
            UserPersonalizationSetting,
        )

        memory = MemoryService(db)
        user = db.get(User, uid)
        enabled = memory.memory_enabled(uid)
        region = db.scalar(select(UserInterfaceSetting.region).where(UserInterfaceSetting.user_id == uid))
        answer_style = db.scalar(
            select(UserPersonalizationSetting.answer_style).where(UserPersonalizationSetting.user_id == uid)
        )
        preferences = []
        memories: list[str] = []
        memories_omitted = 0
        if enabled:
            rows = db.scalars(
                select(Preference)
                .where(Preference.user_id == uid, Preference.source == "explicit")
                .order_by(Preference.strength.desc(), Preference.updated_at.desc())
                .limit(24)
            ).all()
            preferences = [{"value": row.value, "polarity": row.polarity} for row in rows]
            memories = memory.prompt_memories(uid, msg)
            memories_omitted = max(0, memory.memory_count(uid) - len(memories)) if memories else 0
        return user_block(
            name=(user.display_name or None) if user is not None else None,
            signed_in=True,
            memory_enabled=enabled,
            preferences=preferences,
            memories=memories,
            memories_omitted=memories_omitted,
            summary=summary,
            region=(region or settings.default_region).upper(),
            answer_style=answer_style,
        )

    async def compare_visuals(self, tools, movie_a: int, movie_b: int, focus: str):
        """The vision model compares two films' stills (the visual comparison endpoint)."""
        a, b = await asyncio.gather(
            tools.get_movie_details(movie_a, fresh=True),
            tools.get_movie_details(movie_b, fresh=True),
        )
        ua = a.get("backdrop_url") or a.get("poster_url")
        ub = b.get("backdrop_url") or b.get("poster_url")
        if not ua or not ub:
            return {"error": "Both movies need available images for visual comparison."}
        try:
            r = await self.router.vision().generate(
                InferenceRequest(
                    messages=[
                        {
                            "role": "system",
                            "content": "Compare only observable visual properties of the two supplied movie images. Discuss lighting, palette, contrast, composition, texture and apparent atmosphere. Do not infer plot, narrative emotion, director intent or genre as facts from still images. State limitations.",
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Focus: {focus}. Movie A: {a.get('title')}; Movie B: {b.get('title')}",
                                },
                                {"type": "image_url", "image_url": {"url": ua}},
                                {"type": "image_url", "image_url": {"url": ub}},
                            ],
                        },
                    ],
                    temperature=0.1,
                    max_output_tokens=700,
                    reasoning_level="low",
                )
            )
        except Exception as e:
            log_fallback("agent_fallback", "visual_comparison", e)
            return {"error": f"Visual comparison unavailable: {e}"}
        return {
            "movie_a": {"id": movie_a, "title": a.get("title"), "image_url": ua},
            "movie_b": {"id": movie_b, "title": b.get("title"), "image_url": ub},
            "focus": focus,
            "analysis": r.text,
        }

    @staticmethod
    def _iter_answer_chunks(text, chunk_size=28):
        cleaned = (text or "").strip()
        if not cleaned:
            return
        for i in range(0, len(cleaned), chunk_size):
            yield cleaned[i : i + chunk_size]

    async def stream(
        self,
        message,
        user_id=None,
        conversation_id=None,
        guest_history=None,
        guest_session_id=None,
        persisted_user_message_id=None,
        persist_assistant=True,
        continue_from_message_id=None,
        user_timezone=None,
        images=None,
        request_key=None,
    ):
        """Route and stream a turn. Binds a turn record unless the caller already did."""
        from app.agent.turn import TurnContext, bind_turn, current_turn

        arguments = dict(
            user_id=user_id,
            conversation_id=conversation_id,
            guest_history=guest_history,
            guest_session_id=guest_session_id,
            persisted_user_message_id=persisted_user_message_id,
            persist_assistant=persist_assistant,
            continue_from_message_id=continue_from_message_id,
            user_timezone=user_timezone,
            images=images,
        )
        turn = current_turn()
        if turn is not None:
            if turn.request_key is None:
                turn.request_key = request_key
            async for chunk in self._stream_turn(message, turn, **arguments):
                yield chunk
            return
        own = TurnContext(user_id=user_id, guest_session_id=guest_session_id, request_key=request_key)
        with bind_turn(own):
            async for chunk in self._stream_turn(message, own, **arguments):
                yield chunk

    async def _stream_turn(
        self,
        message,
        turn,
        *,
        user_id=None,
        conversation_id=None,
        guest_history=None,
        guest_session_id=None,
        persisted_user_message_id=None,
        persist_assistant=True,
        continue_from_message_id=None,
        user_timezone=None,
        images=None,
    ):
        """Route and stream a turn, keeping synchronous ORM work off the event loop."""
        if user_id is None:
            from app.agent.dispatcher import dispatch_and_stream

            history = list(guest_history or [])
            # A guest's working state is saved with their last reply (see main._store_guest_turn).
            base_state = next(
                (
                    entry.get("turn_state")
                    for entry in reversed(history)
                    if isinstance(entry, dict) and entry.get("role") == "assistant"
                ),
                None,
            )
            async for chunk in dispatch_and_stream(
                message,
                [
                    {"role": e.get("role"), "content": e.get("content")}
                    for e in history
                    if isinstance(e, dict)
                ],
                router=self.router,
                guest_session_id=guest_session_id,
                # Guests get movie links too; there is nothing to persist.
                movie_reference_callback=lambda _references: None,
                user_timezone=user_timezone,
                images=images,
                conversation_key=(
                    f"guest:{guest_session_id}:{conversation_id}" if guest_session_id else None
                ),
                base_state=base_state if isinstance(base_state, dict) else None,
                model_context={"user_block": self._guest_user_block()},
            ):
                yield chunk
            return

        trace = start_trace(user_id)
        answer_parts = []
        reference_events: list[list[dict]] = []
        conversation_id_box = {"value": conversation_id}
        user_message_id_box = {"value": persisted_user_message_id}
        summary_box = {"value": None}

        def prepare_turn(db):
            local_conversation_id = conversation_id_box["value"]
            if local_conversation_id is not None:
                conversation = db.scalar(
                    select(Conversation).where(
                        Conversation.id == local_conversation_id,
                        Conversation.user_id == user_id,
                        Conversation.is_deleted.is_(False),
                    )
                )
                if not conversation:
                    raise ValueError("Conversation not found.")
            else:
                conversation = Conversation(user_id=user_id)
                db.add(conversation)
                db.flush()
                local_conversation_id = conversation.id

            local_user_message_id = user_message_id_box["value"]
            if local_user_message_id is None:
                user_message = Message(
                    conversation_id=local_conversation_id,
                    user_id=user_id,
                    role="user",
                    content=message.strip(),
                )
                db.add(user_message)
                conversation.updated_at = datetime.now(UTC)
                db.flush()
                local_user_message_id = user_message.id
            else:
                user_message = db.get(Message, local_user_message_id)
                if (
                    user_message is None
                    or user_message.conversation_id != local_conversation_id
                    or user_message.user_id != user_id
                    or user_message.role != "user"
                ):
                    raise ValueError("Persisted user message not found.")

            payload = assemble_chat_payload(
                local_conversation_id,
                message.strip(),
                db,
                current_message_id=user_message.id,
                window=settings.max_history_messages,
            )
            if continue_from_message_id is not None:
                stopped_message = db.get(Message, continue_from_message_id)
                if (
                    stopped_message is None
                    or stopped_message.status != "stopped"
                    or stopped_message.conversation_id != local_conversation_id
                    or stopped_message.user_id != user_id
                ):
                    raise ValueError("Stopped answer not found.")
                payload[-1:-1] = [
                    {"role": "user", "content": user_message.content},
                    {"role": "assistant", "content": stopped_message.content},
                ]
            history = [item for item in payload[1:-1] if item["role"] in {"user", "assistant"}]
            summary = getattr(conversation, "summary", None)
            from app.routers.projects import project_instructions

            instructions = project_instructions(db, conversation)
            # This turn starts from the working state saved with the reply it follows
            # on this branch (an edit or regeneration therefore starts from the right one).
            base_state = None
            if user_message.parent_id:
                base_state = db.scalar(
                    select(Message.turn_state).where(
                        Message.uuid_id == user_message.parent_id,
                        Message.conversation_id == local_conversation_id,
                    )
                )
            # The user's standing project instructions first, then who they are.
            user_block = "\n\n".join(
                part for part in (instructions, self._model_user_block(db, user_id, message, summary)) if part
            )
            # The turn's conversation/user message must survive this session.
            db.commit()
            return {
                "model_context": {"user_block": user_block},
                "conversation_id": local_conversation_id,
                "user_message_id": user_message.id,
                "user_message_uuid": user_message.uuid_id,
                "history": history,
                "context": user_block,
                "base_state": base_state if isinstance(base_state, dict) else None,
            }

        try:
            prepared = await _run_turn_db(prepare_turn)
            conversation_id_box["value"] = prepared["conversation_id"]
            user_message_id_box["value"] = prepared["user_message_id"]
            if turn.request_key is None:
                turn.request_key = prepared["user_message_uuid"]
            summary_box["value"] = prepared
            trace.event("request_persistence")
            trace.event("context_built", context_chars=len(prepared["context"]))

            from app.agent.dispatcher import dispatch_and_stream

            async for chunk in dispatch_and_stream(
                message,
                prepared["history"],
                router=self.router,
                user_context=prepared["context"],
                db_factory=SessionLocal,
                user_id=user_id,
                movie_reference_callback=lambda references: reference_events.append(references),
                user_timezone=user_timezone,
                images=images,
                # The conversation may have been created by this turn: key the
                # working state on the real id, or the first answer's films
                # are forgotten and "add it" has nothing to refer to.
                conversation_key=(
                    f"user:{user_id}:{conversation_id_box['value']}" if conversation_id_box["value"] else None
                ),
                base_state=prepared.get("base_state"),
                model_context=prepared.get("model_context"),
            ):
                if not is_event(chunk):
                    answer_parts.append(chunk)
                yield chunk

            # Saved exactly as the user saw it streamed.
            answer = "".join(answer_parts).strip()
            if not answer:
                raise AgentFailure(FailureKind.NO_EVIDENCE)

            if persist_assistant:

                def persist_success(db):
                    conversation = db.scalar(
                        select(Conversation).where(
                            Conversation.id == conversation_id_box["value"],
                            Conversation.user_id == user_id,
                            Conversation.is_deleted.is_(False),
                        )
                    )
                    if conversation is None:
                        return
                    assistant_message = Message(
                        conversation_id=conversation.id,
                        user_id=user_id,
                        role="assistant",
                        content=answer,
                        parent_id=prepared.get("user_message_uuid"),
                        turn_state=turn.state_out,
                    )
                    db.add(assistant_message)
                    conversation.updated_at = datetime.now(UTC)
                    if conversation.title == "New movie conversation":
                        conversation.title = (message[:80] + "…") if len(message) > 80 else message[:80]
                    db.flush()
                    memory = MemoryService(db)
                    for references in reference_events:
                        movie_refs = [ref for ref in references if ref.get("kind") != "person"]
                        if not movie_refs:
                            continue
                        memory.save_movie_references(
                            user_id,
                            conversation.id,
                            user_message_id_box["value"],
                            movie_refs,
                        )
                    # The cards this reply showed (the films it names), so a reloaded chat shows
                    # them too; before, they were stored only with the user's message and
                    # never came back (2026-10-15).
                    from app.agent.events import named_references

                    shown = named_references(
                        answer,
                        [
                            ref
                            for references in reference_events
                            for ref in references
                            if ref.get("kind") != "person"
                        ],
                    )
                    from app.memory import ANSWER_CARD

                    memory.save_movie_references(
                        user_id,
                        conversation.id,
                        assistant_message.id,
                        [{**r, "source": ANSWER_CARD} for r in shown],
                    )
                    db.commit()

                await _run_turn_db(persist_success)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not persist_assistant:
                raise
            logger.warning("agent_dispatch_failed error=%r", exc)
            if not answer_parts:
                failure = failure_response(exc)
                answer = f"{failure.message} {failure.next_step}"
                for chunk in self._iter_answer_chunks(answer):
                    answer_parts.append(chunk)
                    yield chunk
            if conversation_id_box["value"] is not None:
                try:
                    failure_content = "".join(answer_parts)

                    def persist_failure(db):
                        conversation = db.scalar(
                            select(Conversation).where(
                                Conversation.id == conversation_id_box["value"],
                                Conversation.user_id == user_id,
                                Conversation.is_deleted.is_(False),
                            )
                        )
                        if conversation is None:
                            return
                        db.add(
                            Message(
                                conversation_id=conversation.id,
                                user_id=user_id,
                                role="assistant",
                                content=failure_content,
                            )
                        )
                        conversation.updated_at = datetime.now(UTC)
                        db.commit()

                    await _run_turn_db(persist_failure)
                except Exception:
                    logger.exception("agent_failure_response_persistence_failed")
        finally:
            trace.log()
            end_trace(trace)

    async def run(
        self,
        message,
        user_id=None,
        conversation_id=None,
        guest_history=None,
        guest_session_id=None,
    ):
        chunks = [
            chunk
            async for chunk in self.stream(
                message,
                user_id,
                conversation_id,
                guest_history,
                guest_session_id,
            )
        ]
        return "".join(chunk for chunk in chunks if not is_event(chunk))
