from fastapi import APIRouter

from app import main

router = APIRouter(prefix="/api/v1", tags=["conversations"])

router.add_api_route("/conversations", main.conversations_v1, methods=["GET"])
router.add_api_route(
    "/conversations/{cid}",
    main.conversation_v1,
    methods=["GET"],
)
router.add_api_route(
    "/conversations/{cid}/messages",
    main.conversation_messages_v1,
    methods=["GET"],
)
router.add_api_route(
    "/conversations/{cid}",
    main.rename_conversation_v1,
    methods=["PATCH"],
)
router.add_api_route(
    "/conversations/{cid}",
    main.delete_conversation_v1,
    methods=["DELETE"],
)
router.add_api_route(
    "/conversations/{cid}/restore",
    main.restore_conversation,
    methods=["POST"],
)
router.add_api_route(
    "/conversations/{cid}/pin",
    main.pin_conversation,
    methods=["PATCH"],
)
router.add_api_route(
    "/conversations/{cid}/archive",
    main.archive_conversation,
    methods=["PATCH"],
)
router.add_api_route(
    "/messages/{message_id}/feedback",
    main.set_message_feedback,
    methods=["PATCH"],
)
