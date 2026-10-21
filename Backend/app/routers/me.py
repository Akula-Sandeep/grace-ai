from fastapi import APIRouter

from app import main

router = APIRouter(prefix="/api/v1", tags=["me"])

router.add_api_route("/state", main.state_v1, methods=["GET"])
router.add_api_route("/state/watchlist", main.state_watchlist, methods=["GET"])
router.add_api_route("/state/watched", main.state_watched, methods=["GET"])
router.add_api_route("/rag/ingest", main.ingest_v1, methods=["POST"])
router.add_api_route("/notifications", main.notifications_v1, methods=["GET"])
router.add_api_route("/notifications/read-all", main.notifications_read_all_v1, methods=["POST"])
router.add_api_route("/notifications/{notification_id}/read", main.notification_read_v1, methods=["POST"])
router.add_api_route("/notifications/{notification_id}", main.notification_dismiss_v1, methods=["DELETE"])
router.add_api_route("/reminders", main.create_reminder_v1, methods=["POST"])
