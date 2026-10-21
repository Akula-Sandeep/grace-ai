from fastapi import APIRouter

from app import main

router = APIRouter(prefix="/api/v1", tags=["health"])

router.add_api_route("/live", main.live, methods=["GET"])
router.add_api_route("/health", main.health, methods=["GET"])
router.add_api_route("/ready", main.ready, methods=["GET"])
router.add_api_route("/status/ai", main.ai_status_v1, methods=["GET"])
router.add_api_route("/status/ai/check", main.ai_check_v1, methods=["POST"])
