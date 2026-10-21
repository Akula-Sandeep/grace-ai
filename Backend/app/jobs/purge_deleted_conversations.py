import logging

from app.database import SessionLocal
from app.services.conversation_purge import purge_expired_conversations

logger = logging.getLogger(__name__)


def main() -> None:
    with SessionLocal() as db:
        purged = purge_expired_conversations(db)
    logger.info("purged_expired_conversations count=%s", purged)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
