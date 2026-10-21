from fastapi import APIRouter

from app import main

router = APIRouter(prefix="/api/v1", tags=["people"])

router.add_api_route("/people/search", main.people_search_v1, methods=["GET"])
router.add_api_route("/people/{person_id}", main.person_details_v1, methods=["GET"])
