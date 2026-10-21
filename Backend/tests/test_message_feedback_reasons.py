"""Not-helpful answers carry a reason, and become candidate eval cases (C9, D3)."""

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.database import Base, Conversation, Message, User
from app.evals import feedback_cases


@pytest.fixture
def chat(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    with sessions() as db:
        user = User(username="rater", password_hash="x")
        db.add(user)
        db.flush()
        convo = Conversation(user_id=user.id, title="Villeneuve")
        db.add(convo)
        db.flush()
        q1 = Message(conversation_id=convo.id, user_id=user.id, role="user", content="Villeneuve films?")
        a1 = Message(
            conversation_id=convo.id,
            user_id=user.id,
            role="assistant",
            content="Arrival, Dune…",
            parent_id=None,
        )
        db.add_all([q1])
        db.flush()
        a1.parent_id = q1.uuid_id
        db.add(a1)
        db.flush()
        q2 = Message(
            conversation_id=convo.id,
            user_id=user.id,
            role="user",
            content="Which is shortest?",
            parent_id=a1.uuid_id,
        )
        db.add(q2)
        db.flush()
        a2 = Message(
            conversation_id=convo.id,
            user_id=user.id,
            role="assistant",
            content="Polytechnique, about 107 minutes.",
            parent_id=q2.uuid_id,
            model="openai/gpt-oss-20b",
            provider="groq",
            turn_state={"focus": {"kind": "person", "name": "Denis Villeneuve"}},
        )
        db.add(a2)
        db.commit()
        yield SimpleNamespace(user=SimpleNamespace(id=user.id), answer=a2.uuid_id, sessions=sessions)
    engine.dispose()


def stored(chat):
    with chat.sessions() as db:
        return db.query(Message).filter_by(uuid_id=chat.answer).one()


def test_a_reason_and_note_are_kept_with_a_thumbs_down(chat):
    answer_id = uuid.UUID(chat.answer)
    main.set_message_feedback(answer_id, "down", None, u=chat.user)
    result = main.set_message_feedback(
        answer_id,
        "down",
        main.MessageFeedbackDetail(reason="wrong_facts", note="  It is 77 minutes.  "),
        u=chat.user,
    )
    assert result == {"id": chat.answer, "feedback": "down", "feedback_reason": "wrong_facts"}
    message = stored(chat)
    assert (message.feedback_reason, message.feedback_note) == ("wrong_facts", "It is 77 minutes.")


def test_changing_or_clearing_the_mark_drops_the_old_reason(chat):
    answer_id = uuid.UUID(chat.answer)
    main.set_message_feedback(answer_id, "down", main.MessageFeedbackDetail(reason="too_long"), u=chat.user)
    main.set_message_feedback(answer_id, "up", None, u=chat.user)
    assert (stored(chat).feedback, stored(chat).feedback_reason) == ("up", None)
    main.set_message_feedback(
        answer_id, "down", main.MessageFeedbackDetail(reason="other", note="meh"), u=chat.user
    )
    main.set_message_feedback(answer_id, "none", None, u=chat.user)
    message = stored(chat)
    assert (message.feedback, message.feedback_reason, message.feedback_note) == (None, None, None)


def test_unknown_reasons_and_other_users_are_refused(chat):
    with pytest.raises(ValidationError):
        main.MessageFeedbackDetail(reason="rude")
    with pytest.raises(ValidationError):
        main.MessageFeedbackDetail(note="x" * 1001)
    with pytest.raises(HTTPException) as refused:
        main.set_message_feedback(uuid.UUID(chat.answer), "down", None, u=SimpleNamespace(id=999))
    assert refused.value.status_code == 404


def test_thumbs_down_answers_export_as_replayable_cases(chat, tmp_path, monkeypatch):
    main.set_message_feedback(
        uuid.UUID(chat.answer),
        "down",
        main.MessageFeedbackDetail(reason="wrong_facts", note="77 min"),
        u=chat.user,
    )
    with chat.sessions() as db:
        cases = feedback_cases.collect_cases(db)
    assert len(cases) == 1
    case = cases[0]
    assert case["turns"] == ["Villeneuve films?", "Which is shortest?"]
    assert case["rejected_answer"] == "Polytechnique, about 107 minutes."
    assert (case["reason"], case["note"]) == ("wrong_facts", "77 min")
    assert "movie data" in case["hint"]
    assert case["produced_by"]["provider"] == "groq"
    assert case["focus"] == {"kind": "person", "name": "Denis Villeneuve"}

    import app.database as database

    monkeypatch.setattr(database, "SessionLocal", chat.sessions)
    out = tmp_path / "cases.jsonl"
    assert feedback_cases.main(["--out", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == [chat.answer]
