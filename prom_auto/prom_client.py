import logging
import time

import requests

from . import config

logger = logging.getLogger(__name__)

_HEADERS = {"Authorization": f"Bearer {config.PROM_API_TOKEN}"}

_MAX_TRIES = 5
_RETRY_WAIT_SECONDS = 5
_CONCURRENT_IMPORT_MARKER = "одновременных импорт"


class PromImportBusyError(Exception):
    """Prom.ua is refusing new imports - either a normal transient lock, or
    their known nightly restriction, which can outlast a short retry."""


def import_file(xlsx_bytes: bytes) -> dict:
    """Equivalent of the n8n 'HTTP Request to prom' node
    (POST /products/import_file, multipart xlsx upload). Retries on
    Prom.ua's "another import is already running" error, matching the
    original n8n node's 5 tries / 5s wait."""
    for attempt in range(1, _MAX_TRIES + 1):
        response = requests.post(
            f"{config.PROM_API_BASE_URL}/products/import_file",
            headers=_HEADERS,
            files={"file": ("products.xlsx", xlsx_bytes)},
        )
        if response.ok:
            return response.json()

        if _CONCURRENT_IMPORT_MARKER in response.text:
            if attempt < _MAX_TRIES:
                logger.warning(
                    "Prom.ua busy with a previous import, retrying (%d/%d)", attempt, _MAX_TRIES
                )
                time.sleep(_RETRY_WAIT_SECONDS)
                continue
            raise PromImportBusyError(
                "Prom.ua is still refusing imports after retrying - likely their "
                "known nightly restriction rather than a quick transient lock."
            )

        logger.error("Prom.ua import_file error response: %s", response.text)
        response.raise_for_status()
