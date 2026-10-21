from fastapi import APIRouter

from app import main

router = APIRouter(prefix="/api/v1", tags=["chat"])

router.add_api_route("/guest/session", main.start_guest_session, methods=["POST"])
router.add_api_route(
    "/guest/conversations/{conversation_id}",
    main.get_guest_conversation,
    methods=["GET"],
)
router.add_api_route(
    "/chat",
    main.chat_v1,
    methods=["POST"],
    response_model=main.ChatOut,
)
router.add_api_route(
    "/chat/stream",
    main.chat_stream_v1,
    methods=["POST"],
    responses=main.CHAT_STREAM_RESPONSES,
)
