import logging
import re
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


def count_products() -> int:
    """Total number of products currently in the Prom.ua account.

    Prom.ua's API has no dedicated count endpoint, so this pages through
    GET /products/list (sorted by id) and sums how many come back, using
    each page's lowest id as the next page's upper bound.
    """
    total = 0
    last_id = None
    page_size = 100
    while True:
        params = {"limit": page_size}
        if last_id is not None:
            params["last_id"] = last_id
        response = requests.get(
            f"{config.PROM_API_BASE_URL}/products/list",
            headers=_HEADERS,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        products = response.json().get("products", [])
        if not products:
            break
        total += len(products)
        if len(products) < page_size:
            break
        last_id = min(p["id"] for p in products) - 1
    return total


_ARTICLE_PATTERN = re.compile(r"^v(\d+)$", re.IGNORECASE)


def find_max_article_number() -> int:
    """Scans existing products for 'vNNNN'-style external_id/sku and returns
    the highest N found. Used to seed article_counter's local counter, since
    count_products() alone isn't safe here: Prom.ua's import is async, so a
    just-submitted product's article might already be higher than the
    current live count reflects.
    """
    max_number = 0
    last_id = None
    page_size = 100
    while True:
        params = {"limit": page_size}
        if last_id is not None:
            params["last_id"] = last_id
        response = requests.get(
            f"{config.PROM_API_BASE_URL}/products/list",
            headers=_HEADERS,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        products = response.json().get("products", [])
        if not products:
            break
        for product in products:
            for value in (product.get("external_id"), product.get("sku")):
                match = _ARTICLE_PATTERN.match(str(value or ""))
                if match:
                    max_number = max(max_number, int(match.group(1)))
        if len(products) < page_size:
            break
        last_id = min(p["id"] for p in products) - 1
    return max_number


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

        try:
            error_message = response.json().get("error", {}).get("message", "")
        except ValueError:
            error_message = response.text

        if _CONCURRENT_IMPORT_MARKER in error_message:
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

        logger.error("Prom.ua import_file error response: %s", error_message)
        response.raise_for_status()
