from app.observability import logger
from app.services.account_lifecycle import purge_deleted_accounts

if __name__ == "__main__":
    logger.info("purged_deleted_accounts count=%s", purge_deleted_accounts())
